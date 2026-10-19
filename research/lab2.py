"""Strategy lab, round 2: can the daily trend bot 'learn from its failures' in a way that survives honest testing?

Every idea is judged out-of-sample: anything that is fitted (thresholds, models, weights) only sees data from
BEFORE the 6-month window it is scored on. Baseline = the live Daily trend strategy
(close > 100-day SMA AND 30-day momentum > 0; altcoins only while BTC > its 200-day SMA).

Usage: python research/lab2.py <folder with *_1h.csv.gz> <out.json>
"""
from __future__ import annotations

import json
import math
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent))
import lab  # noqa: E402

COST = lab.COST
BY = 365
OOS_START = pd.Timestamp("2021-01-01", tz="UTC")


# ------------------------------------------------------------------ data
def panel(folder):
    fr = lab.load(folder, "1D")
    close = pd.DataFrame({c: f.close for c, f in fr.items()})
    high = pd.DataFrame({c: f.high for c, f in fr.items()})
    low = pd.DataFrame({c: f.low for c, f in fr.items()})
    first = {c: f.index[0] for c, f in fr.items()}
    avail = pd.DataFrame({c: (close.index >= first[c] + pd.Timedelta(days=210)) for c in close}, index=close.index).astype(float)
    return close, high, low, avail


def eq_weights(avail):
    return avail.div(avail.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)


def sim(pos: pd.DataFrame, close: pd.DataFrame, weights: pd.DataFrame) -> pd.Series:
    """Daily portfolio return: position decided at close t earns bar t+1; costs on every change."""
    ret = close.pct_change().fillna(0.0)
    pos = pos.reindex(close.index).fillna(0.0)
    held = pos.shift(1).fillna(0.0)
    turn = pos.diff().abs().fillna(pos.abs()).shift(1).fillna(0.0)
    sleeve = held * ret - turn * COST
    return (sleeve * weights.shift(1).fillna(0)).sum(axis=1)


def stats(r):
    return lab.stats(r, BY)


def yearly(r):
    return {int(y): float((1 + g).prod() - 1) for y, g in r.groupby(r.index.year)}


def windows(index, train_days=730, test_days=182):
    t0, out = OOS_START, []
    while t0 < index[-1]:
        t1 = min(t0 + pd.Timedelta(days=test_days), index[-1] + pd.Timedelta(days=1))
        out.append((t0 - pd.Timedelta(days=train_days), t0, t1))
        t0 = t1
    return out


def splice(series_by_choice: dict, choose_fn, index) -> pd.Series:
    """Walk-forward: for each window pick a key using training data only, take its out-of-sample returns."""
    parts, picks = [], []
    for tr0, te0, te1 in windows(index):
        key = choose_fn(tr0, te0)
        picks.append((str(te0.date()), key))
        parts.append(series_by_choice[key].loc[te0:te1 - pd.Timedelta(seconds=1)])
    return pd.concat(parts), picks


def train_score(r, tr0, te0):
    s = stats(r.loc[tr0:te0 - pd.Timedelta(seconds=1)])
    return s["sharpe"] + 0.5 * s["max_dd"] if s else -9


# ------------------------------------------------------------------ signals
def sma(close, n):
    return close.rolling(n).mean()


def btc_ok(close, n=200):
    b = close["BTCUSDT"]
    return (b > sma(close[["BTCUSDT"]], n)["BTCUSDT"]).astype(float)


def apply_btc_filter(pos, close, n=200):
    ok = btc_ok(close, n)
    out = pos.copy()
    for c in out:
        if c != "BTCUSDT":
            out[c] = out[c] * ok
    return out


def trend_pos(close, n=100, look=30, btc_n=200):
    raw = ((close > sma(close, n)) & (close.pct_change(look) > 0)).astype(float)
    return apply_btc_filter(raw, close, btc_n)


# ------------------------------------------------------------------ idea 1: committee of settings
def committee_pos(close, ns=(50, 75, 100, 125, 150), looks=(20, 30, 45, 60), btc_ns=(150, 200, 250), step=0.25):
    acc = 0
    k = 0
    for n in ns:
        for lk in looks:
            for bn in btc_ns:
                acc = acc + trend_pos(close, n, lk, bn)
                k += 1
    frac = acc / k
    return (frac / step).round() * step if step else frac


# ------------------------------------------------------------------ idea 2: whipsaw memory
def whipsaw_pos(close, base_n=100, look=30, cool_days=0, buffer=0.0):
    """After a LOSING trade on a coin: wait `cool_days` and require price `buffer` above the average to re-enter."""
    s = sma(close, base_n)
    mom = close.pct_change(look)
    ok = btc_ok(close)
    out = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for c in close:
        cl, sm, mo = close[c].to_numpy(), s[c].to_numpy(), mom[c].to_numpy()
        okv = np.ones(len(cl)) if c == "BTCUSDT" else ok.to_numpy()
        pos, entry, block_until, need_buf = 0, 0.0, -1, False
        res = np.zeros(len(cl))
        for i in range(len(cl)):
            if np.isnan(sm[i]) or np.isnan(mo[i]):
                continue
            sig = cl[i] > sm[i] and mo[i] > 0 and okv[i] > 0
            if pos == 0:
                strong = cl[i] > sm[i] * (1 + buffer) if need_buf else True
                if sig and i >= block_until and strong:
                    pos, entry, need_buf = 1, cl[i], False
            elif not sig:
                pos = 0
                if cl[i] / entry - 1 - 2 * COST < 0:      # the trade failed -> remember it
                    block_until, need_buf = i + cool_days, buffer > 0
            res[i] = pos
        out[c] = res
    return out


# ------------------------------------------------------------------ idea 3: meta-labelling (learn which entries fail)
def trade_table(close, pos):
    """Every entry of a 0/1 position series with features at entry and the outcome."""
    s100, s50, s200 = sma(close, 100), sma(close, 50), sma(close, 200)
    ret = close.pct_change()
    b = close["BTCUSDT"]
    btc_d200 = b / b.rolling(200).mean() - 1
    rows = []
    for c in close:
        p = pos[c].to_numpy()
        idx = close.index
        entries = [i for i in range(1, len(p)) if p[i] == 1 and p[i - 1] == 0]
        all_entries = set(entries)
        prev_ret = np.nan
        for i in entries:
            j = i + 1
            while j < len(p) and p[j] == 1:
                j += 1
            if j >= len(p):
                exit_i, done = len(p) - 1, False
            else:
                exit_i, done = j, True
            r = close[c].iloc[exit_i] / close[c].iloc[i] - 1 - 2 * COST
            t = idx[i]
            rows.append({
                "coin": c, "entry": t, "exit": idx[exit_i], "done": done, "ret": r, "win": int(r > 0),
                "d100": close[c].iloc[i] / s100[c].iloc[i] - 1,
                "d50": close[c].iloc[i] / s50[c].iloc[i] - 1,
                "d200": close[c].iloc[i] / s200[c].iloc[i] - 1 if not np.isnan(s200[c].iloc[i]) else 0.0,
                "mom30": close[c].pct_change(30).iloc[i], "mom90": close[c].pct_change(90).iloc[i],
                "vol30": ret[c].rolling(30).std().iloc[i] * math.sqrt(365),
                "btc_d200": btc_d200.iloc[i] if not np.isnan(btc_d200.iloc[i]) else 0.0,
                "btc_mom30": b.pct_change(30).iloc[i],
                "recent_entries": sum(1 for k in all_entries if i - 60 <= k < i),
                "prev_loss": 0.0 if np.isnan(prev_ret) else float(prev_ret < 0),
            })
            prev_ret = r
    return pd.DataFrame(rows)


META_FEATS = ["d100", "d50", "d200", "mom30", "mom90", "vol30", "btc_d200", "btc_mom30", "recent_entries", "prev_loss"]


def meta_filtered_pos(close, base_pos, threshold=0.5, model="logit", min_trades=40):
    """Skip entries a model (trained only on trades that already FINISHED before the window) expects to fail."""
    tt = trade_table(close, base_pos)
    keep = pd.Series(True, index=tt.index)
    probs = pd.Series(np.nan, index=tt.index)
    for _, te0, te1 in windows(close.index):
        train = tt[(tt["exit"] < te0) & tt["done"]]
        test = tt[(tt["entry"] >= te0) & (tt["entry"] < te1)]
        if len(test) == 0 or len(train) < min_trades or train["win"].nunique() < 2:
            continue
        X = train[META_FEATS].fillna(0).to_numpy()
        if model == "logit":
            sc = StandardScaler().fit(X)
            m = LogisticRegression(C=0.5, max_iter=500).fit(sc.transform(X), train["win"])
            p = m.predict_proba(sc.transform(test[META_FEATS].fillna(0).to_numpy()))[:, 1]
        else:
            m = HistGradientBoostingClassifier(max_iter=100, max_depth=2, learning_rate=0.05, min_samples_leaf=10).fit(X, train["win"])
            p = m.predict_proba(test[META_FEATS].fillna(0).to_numpy())[:, 1]
        probs.loc[test.index] = p
        keep.loc[test.index] = p >= threshold
    pos = base_pos.copy()
    for k, row in tt.iterrows():
        if not keep[k]:
            pos.loc[row["entry"]:row["exit"] - pd.Timedelta(seconds=1), row["coin"]] = 0.0
    return pos, tt.assign(prob=probs, kept=keep)


# ------------------------------------------------------------------ idea 4: daily machine learning
def daily_ml_pos(close, horizon=10, thr=0.55, use_filter=True):
    feats = {}
    ret = close.pct_change()
    b = close["BTCUSDT"]
    for c in close:
        f = pd.DataFrame(index=close.index)
        for n in (7, 14, 30, 60, 90):
            f[f"mom{n}"] = close[c].pct_change(n)
        for n in (20, 50, 100, 200):
            f[f"d{n}"] = close[c] / close[c].rolling(n).mean() - 1
        f["vol30"] = ret[c].rolling(30).std()
        f["volr"] = f["vol30"] / ret[c].rolling(120).std()
        f["btc_mom30"] = b.pct_change(30)
        f["btc_d200"] = b / b.rolling(200).mean() - 1
        f["fwd"] = close[c].shift(-horizon) / close[c] - 1
        f["coin"] = c
        feats[c] = f
    data = pd.concat(feats.values())
    cols = [x for x in data.columns if x not in ("fwd", "coin")]
    pos = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for _, te0, te1 in windows(close.index):
        tr = data[(data.index < te0 - pd.Timedelta(days=horizon)) & data["fwd"].notna()].dropna(subset=cols)
        if len(tr) < 500:
            continue
        m = HistGradientBoostingClassifier(max_iter=150, max_depth=3, learning_rate=0.04, min_samples_leaf=60,
                                           l2_regularization=1.0).fit(tr[cols], (tr["fwd"] > 0).astype(int))
        for c in close:
            f = feats[c].loc[te0:te1 - pd.Timedelta(seconds=1)]
            ok = f[cols].notna().all(axis=1)
            p = pd.Series(0.0, index=f.index)
            if ok.any():
                p[ok] = m.predict_proba(f.loc[ok, cols])[:, 1]
            pos.loc[f.index, c] = (p > thr).astype(float).to_numpy()
    return apply_btc_filter(pos, close) if use_filter else pos


# ------------------------------------------------------------------ idea 5: learn which strategy works now
def donchian_pos(close, high, low, n_in=30, n_out=20):
    out = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for c in close:
        hi = high[c].rolling(n_in).max().shift(1).to_numpy()
        lo = low[c].rolling(n_out).min().shift(1).to_numpy()
        cl, cur, res = close[c].to_numpy(), 0.0, np.zeros(len(close))
        for i in range(len(cl)):
            if cur == 0 and not np.isnan(hi[i]) and cl[i] > hi[i]:
                cur = 1.0
            elif cur == 1 and not np.isnan(lo[i]) and cl[i] < lo[i]:
                cur = 0.0
            res[i] = cur
        out[c] = res
    return apply_btc_filter(out, close)


def perf_weighted(sleeves: dict[str, pd.Series], lookback=180, rebalance="30D", floor=0.0):
    df = pd.DataFrame(sleeves).fillna(0)
    roll_sh = df.rolling(lookback).mean() / df.rolling(lookback).std()
    w = roll_sh.clip(lower=floor)
    w = w.div(w.sum(axis=1).replace(0, np.nan), axis=0)
    w = w.fillna(1 / df.shape[1]).resample(rebalance).first().reindex(df.index).ffill()
    return (df * w.shift(1).fillna(1 / df.shape[1])).sum(axis=1)


# ------------------------------------------------------------------ idea 6: risk-parity coin weights
def inv_vol_weights(close, avail, n=60):
    vol = close.pct_change().rolling(n).std()
    iv = (1 / vol).where(avail > 0)
    return iv.div(iv.sum(axis=1), axis=0).fillna(0)


# ------------------------------------------------------------------ main
def main():
    folder, out = sys.argv[1], sys.argv[2]
    close, high, low, avail = panel(folder)
    W = eq_weights(avail)
    R: dict[str, pd.Series] = {}
    notes: dict[str, object] = {}

    base = trend_pos(close)
    R["baseline (live Daily trend)"] = sim(base, close, W)
    R["buy & hold"] = lab.buy_hold({c: pd.DataFrame({"close": close[c].dropna()}) for c in close}, close.index)

    # 1. committee
    R["1 committee of 60 settings"] = sim(committee_pos(close), close, W)
    R["1b committee (no rounding)"] = sim(committee_pos(close, step=0), close, W)

    # 2. whipsaw memory, tuned walk-forward
    grid = {(k, bf): sim(whipsaw_pos(close, cool_days=k, buffer=bf), close, W) for k in (0, 5, 10, 20) for bf in (0.0, 0.02, 0.05)}
    R["2 whipsaw memory (walk-forward)"], notes["whipsaw_picks"] = splice(
        grid, lambda tr0, te0: max(grid, key=lambda key: train_score(grid[key], tr0, te0)), close.index)
    R["2b whipsaw fixed (10d, 2%)"] = grid[(10, 0.02)]

    # 3. meta-labelling filter
    for model in ("logit", "gbm"):
        p, tt = meta_filtered_pos(close, base, 0.5, model)
        R[f"3 learn-from-failed-trades filter ({model})"] = sim(p, close, W)
        oos = tt[tt["entry"] >= OOS_START]
        notes[f"meta_{model}"] = {"trades": int(len(oos)), "skipped": int((~oos["kept"]).sum()),
                                  "win_rate_kept": float(oos[oos["kept"]]["win"].mean()) if oos["kept"].any() else None,
                                  "win_rate_skipped": float(oos[~oos["kept"]]["win"].mean()) if (~oos["kept"]).any() else None,
                                  "avg_ret_kept": float(oos[oos["kept"]]["ret"].mean()) if oos["kept"].any() else None,
                                  "avg_ret_skipped": float(oos[~oos["kept"]]["ret"].mean()) if (~oos["kept"]).any() else None}
    tt_all = trade_table(close, base)
    notes["baseline_trades"] = {"n": int(len(tt_all)), "win_rate": float(tt_all["win"].mean()),
                                "avg_win": float(tt_all[tt_all.win == 1]["ret"].mean()), "avg_loss": float(tt_all[tt_all.win == 0]["ret"].mean())}

    # 4. daily ML
    R["4 daily ML (GBM, 10-day horizon)"] = sim(daily_ml_pos(close), close, W)
    R["4b daily ML AND trend rule"] = sim(daily_ml_pos(close) * base, close, W)

    # 5. learn which strategy works now
    sleeves = {
        "trend": R["baseline (live Daily trend)"],
        "breakout": sim(donchian_pos(close, high, low), close, W),
        "momentum": sim(apply_btc_filter((close.pct_change(30) > 0).astype(float), close), close, W),
        "sma": sim(apply_btc_filter((close > sma(close, 100)).astype(float), close), close, W),
    }
    R["5a equal mix of 4 trend bots"] = pd.DataFrame(sleeves).mean(axis=1)
    R["5 performance-weighted mix (learns which bot works)"] = perf_weighted(sleeves)

    # 6. risk parity across coins
    R["6 baseline + inverse-volatility coin weights"] = sim(base, close, inv_vol_weights(close, avail))
    R["6b committee + inverse-volatility"] = sim(committee_pos(close), close, inv_vol_weights(close, avail))

    # ---- report (out-of-sample span only)
    oos = {k: v.loc[OOS_START:] for k, v in R.items()}
    base_y = yearly(oos["baseline (live Daily trend)"])
    report = {}
    print(f"\nOut-of-sample {OOS_START.date()} -> {close.index[-1].date()}  (sorted by Sharpe)")
    print(f"{'idea':<52}{'CAGR':>7}{'Sharpe':>8}{'MaxDD':>8}{'12m':>8}  years beating baseline")
    for k, r in sorted(oos.items(), key=lambda kv: -stats(kv[1])["sharpe"]):
        s, y = stats(r), yearly(r)
        last12 = stats(r.loc[r.index[-1] - pd.Timedelta(days=365):])["total"]
        beat = sum(1 for yr in y if y[yr] > base_y.get(yr, -9) + 1e-9)
        report[k] = {**s, "last_12m": last12, "yearly": y, "years_beating_baseline": beat, "years": len(y)}
        print(f"{k:<52}{s['cagr']*100:6.0f}%{s['sharpe']:8.2f}{s['max_dd']*100:7.0f}%{last12*100:7.1f}%  {beat}/{len(y)}")
    print(json.dumps(notes, indent=1, default=str))
    Path(out).write_text(json.dumps({"report": report, "notes": notes}, default=str))


if __name__ == "__main__":
    main()
