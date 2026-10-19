"""Strategy lab: fast vectorised tests of many long-only strategy families on daily/4h candles,
with honest walk-forward evaluation (parameters chosen on past data only, judged on the next unseen window).

Usage: python research/lab.py <folder with *_1h.csv.gz> <out.json>
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
COST = 0.002          # per unit of turnover per side: 0.10% fee + ~0.10% slippage (same as the live bot)
BARS_YEAR = {"1d": 365, "4h": 365 * 6}


def load(folder: str, rule: str) -> dict[str, pd.DataFrame]:
    out = {}
    for c in COINS:
        d = pd.read_csv(Path(folder) / f"{c}_1h.csv.gz")
        d.index = pd.to_datetime(d["open_time"], unit="ms", utc=True)
        agg = d.resample(rule, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        out[c] = agg
    return out


# ------------------------------------------------------------------ strategy families
# Each returns a target exposure series in [0, 1] decided at the bar close (applied to the NEXT bar).

def f_sma(d, n, **_):
    return (d.close > d.close.rolling(n).mean()).astype(float)


def f_cross(d, fast, slow, **_):
    return (d.close.ewm(span=fast, adjust=False).mean() > d.close.ewm(span=slow, adjust=False).mean()).astype(float)


def f_donchian(d, n_in, n_out, **_):
    hi = d.high.rolling(n_in).max().shift(1)
    lo = d.low.rolling(n_out).min().shift(1)
    pos = np.zeros(len(d))
    cur = 0.0
    c, h, l = d.close.to_numpy(), hi.to_numpy(), lo.to_numpy()
    for i in range(len(d)):
        if cur == 0 and not math.isnan(h[i]) and c[i] > h[i]:
            cur = 1.0
        elif cur == 1 and not math.isnan(l[i]) and c[i] < l[i]:
            cur = 0.0
        pos[i] = cur
    return pd.Series(pos, index=d.index)


def f_tsmom(d, look, **_):
    return (d.close.pct_change(look) > 0).astype(float)


def f_dual(d, n, look, **_):
    """trend AND momentum must agree"""
    return f_sma(d, n) * f_tsmom(d, look)


def f_rsi_dip(d, n_trend, rsi_in, rsi_out, **_):
    """mean reversion on daily bars: buy oversold dips while above the long trend"""
    delta = d.close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    trend = d.close > d.close.rolling(n_trend).mean()
    pos, cur = np.zeros(len(d)), 0.0
    r, t = rsi.to_numpy(), trend.to_numpy()
    for i in range(len(d)):
        if cur == 0 and t[i] and r[i] < rsi_in:
            cur = 1.0
        elif cur == 1 and (r[i] > rsi_out or not t[i]):
            cur = 0.0
        pos[i] = cur
    return pd.Series(pos, index=d.index)


FAMILIES = {
    "sma": (f_sma, {"n": [20, 30, 50, 75, 100, 150, 200]}),
    "ema_cross": (f_cross, {"fast": [10, 20, 30, 50], "slow": [50, 100, 150, 200]}),
    "donchian": (f_donchian, {"n_in": [20, 30, 55, 90], "n_out": [10, 20, 30]}),
    "momentum": (f_tsmom, {"look": [14, 30, 60, 90, 120]}),
    "trend+momentum": (f_dual, {"n": [50, 100, 150], "look": [30, 60, 90]}),
    "daily_dip": (f_rsi_dip, {"n_trend": [100, 200], "rsi_in": [30, 35, 40], "rsi_out": [55, 65]}),
}
OVERLAYS = {"vol_target": [None, 0.6, 0.9], "btc_filter": [False, True]}


def param_grid(family):
    fn, grid = FAMILIES[family]
    keys = list(grid)
    for vals in itertools.product(*grid.values()):
        p = dict(zip(keys, vals))
        if family == "ema_cross" and p["fast"] >= p["slow"]:
            continue
        for vt, bf in itertools.product(OVERLAYS["vol_target"], OVERLAYS["btc_filter"]):
            yield {**p, "vol_target": vt, "btc_filter": bf}


# ------------------------------------------------------------------ simulation
def sleeve_returns(frames, family, params, bars_year):
    """Per-coin net returns of the strategy (each coin is an equal-weight sleeve)."""
    fn = FAMILIES[family][0]
    btc = frames["BTCUSDT"]
    btc_ok = (btc.close > btc.close.rolling(200 if bars_year == 365 else 1200).mean()).astype(float)
    out = {}
    for c, d in frames.items():
        pos = fn(d, **params).fillna(0.0)
        if params.get("btc_filter") and c != "BTCUSDT":
            pos = pos * btc_ok.reindex(d.index).fillna(0.0)
        ret = d.close.pct_change().fillna(0.0)
        if params.get("vol_target"):
            vol = ret.rolling(20).std() * math.sqrt(bars_year)
            pos = pos * (params["vol_target"] / vol).clip(upper=1.0).fillna(0.0)
            pos = (pos * 4).round() / 4  # trade in 25% steps to avoid constant tiny rebalances
        held = pos.shift(1).fillna(0.0)          # decided at close t, earns return of bar t+1
        turnover = pos.diff().abs().fillna(pos.abs()).shift(1).fillna(0.0)
        out[c] = held * ret - turnover * COST
    return pd.DataFrame(out)


def portfolio(sleeves: pd.DataFrame, frames) -> pd.Series:
    """Equal weight across coins that exist at that time (SOL only after listing + 200 bars)."""
    avail = pd.DataFrame({c: pd.Series(1.0, index=f.index[f.index >= f.index[0] + pd.Timedelta(days=210)]) for c, f in frames.items()}).reindex(sleeves.index).fillna(0)
    w = avail.div(avail.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    return (sleeves.fillna(0) * w).sum(axis=1)


def buy_hold(frames, index):
    rets = pd.DataFrame({c: f.close.pct_change() for c, f in frames.items()}).reindex(index).fillna(0)
    avail = pd.DataFrame({c: pd.Series(1.0, index=f.index[f.index >= f.index[0] + pd.Timedelta(days=210)]) for c, f in frames.items()}).reindex(index).fillna(0)
    w = avail.div(avail.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    return (rets * w).sum(axis=1)


def stats(r: pd.Series, bars_year) -> dict:
    r = r.dropna()
    if len(r) < 5:
        return {}
    eq = (1 + r).cumprod()
    years = len(r) / bars_year
    sd = r.std()
    return {
        "total": float(eq.iloc[-1] - 1),
        "cagr": float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 and eq.iloc[-1] > 0 else -1.0,
        "sharpe": float(r.mean() / sd * math.sqrt(bars_year)) if sd > 0 else 0.0,
        "max_dd": float((eq / eq.cummax() - 1).min()),
        "exposure": None,
    }


def score(r, bars_year):
    """Selection score on the training window: Sharpe, lightly penalised for deep drawdowns."""
    s = stats(r, bars_year)
    return s["sharpe"] + 0.5 * s["max_dd"] if s else -9


# ------------------------------------------------------------------ walk-forward
def walk_forward(frames, bars_year, train_days=730, test_days=182, start="2021-01-01"):
    idx = frames["BTCUSDT"].index
    per_day = bars_year / 365
    cache: dict[tuple, pd.Series] = {}

    def run(fam, p):
        key = (fam, json.dumps(p, sort_keys=True))
        if key not in cache:
            cache[key] = portfolio(sleeve_returns(frames, fam, p, bars_year), frames)
        return cache[key]

    all_params = {fam: list(param_grid(fam)) for fam in FAMILIES}
    print("candidate configurations:", sum(len(v) for v in all_params.values()), flush=True)
    for fam, ps in all_params.items():
        for p in ps:
            run(fam, p)
    t0 = pd.Timestamp(start, tz="UTC")
    windows = []
    while t0 < idx[-1]:
        t1 = min(t0 + pd.Timedelta(days=test_days), idx[-1] + pd.Timedelta(hours=1))
        windows.append((t0 - pd.Timedelta(days=train_days), t0, t1))
        t0 = t1
    oos = {fam: [] for fam in FAMILIES}
    oos["auto (best family)"] = []
    picks = []
    for tr0, te0, te1 in windows:
        best_overall = None
        for fam, ps in all_params.items():
            scored = [(score(run(fam, p).loc[tr0:te0 - pd.Timedelta(seconds=1)], bars_year), i) for i, p in enumerate(ps)]
            sc, i = max(scored)
            r = run(fam, ps[i]).loc[te0:te1 - pd.Timedelta(seconds=1)]
            oos[fam].append(r)
            if best_overall is None or sc > best_overall[0]:
                best_overall = (sc, fam, ps[i], r)
        oos["auto (best family)"].append(best_overall[3])
        picks.append({"test_start": str(te0.date()), "family": best_overall[1], "params": best_overall[2]})
    res = {fam: pd.concat(parts) for fam, parts in oos.items()}
    res["buy & hold"] = buy_hold(frames, res["sma"].index)
    return res, picks, cache, all_params


def main():
    folder, out = sys.argv[1], sys.argv[2]
    report = {}
    for tf, rule in (("1d", "1D"), ("4h", "4h")):
        frames = load(folder, rule)
        by = BARS_YEAR[tf]
        res, picks, cache, all_params = walk_forward(frames, by)
        summary = {k: stats(v, by) for k, v in res.items()}
        # robustness: how many configurations of each family beat buy & hold on Sharpe over the full test span
        span = res["sma"].index
        bh = stats(res["buy & hold"], by)
        robust = {}
        for fam, ps in all_params.items():
            sh = [stats(cache[(fam, json.dumps(p, sort_keys=True))].loc[span[0]:], by)["sharpe"] for p in ps]
            robust[fam] = {"configs": len(sh), "median_sharpe": float(np.median(sh)), "share_beating_bh_sharpe": float(np.mean([s > bh["sharpe"] for s in sh]))}
        # yearly breakdown
        yearly = {k: {str(y): float((1 + g).prod() - 1) for y, g in v.groupby(v.index.year)} for k, v in res.items()}
        last180 = {k: stats(v.loc[v.index[-1] - pd.Timedelta(days=180):], by) for k, v in res.items()}
        report[tf] = {"walk_forward": summary, "robustness": robust, "yearly": yearly, "last_180d": last180, "picks": picks,
                      "equity": {k: [round(float(x), 4) for x in (1 + v).cumprod().resample("7D").last().to_numpy()] for k, v in res.items()},
                      "equity_dates": [str(d.date()) for d in (1 + res["sma"]).cumprod().resample("7D").last().index]}
        print(f"\n===== {tf} walk-forward (out-of-sample {span[0].date()} -> {span[-1].date()}) =====")
        for k, s in sorted(summary.items(), key=lambda kv: -kv[1]["sharpe"]):
            print(f"{k:<20} total {s['total']*100:8.0f}%  CAGR {s['cagr']*100:6.1f}%  Sharpe {s['sharpe']:5.2f}  maxDD {s['max_dd']*100:6.1f}%  last180 {last180[k]['total']*100:6.1f}%")
        print("robustness:", json.dumps(robust, indent=None))
        print("picks:", [(p["test_start"], p["family"]) for p in picks])
    Path(out).write_text(json.dumps(report))


if __name__ == "__main__":
    main()
