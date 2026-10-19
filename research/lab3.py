"""Strategy lab, round 3: forward-looking information, crowding, goals and unique timing.

Question: does information about what the market EXPECTS (implied volatility from options, how crowded the long side is
via perpetual funding rates, crowd sentiment) or an explicit drawdown goal improve the Daily-trend bot out of sample?

Usage: python research/lab3.py <research-data folder> <out.json>
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent))
import lab  # noqa: E402
import lab2 as L  # noqa: E402

OOS = L.OOS_START


def daily_index(s: pd.Series) -> pd.Series:
    s.index = pd.to_datetime(s.index, unit="ms", utc=True).floor("1D")
    return s.groupby(level=0).mean()


def load_signals(folder: str, index: pd.DatetimeIndex) -> pd.DataFrame:
    f = Path(folder)
    fg = pd.read_csv(f / "fear_greed.csv.gz").set_index("timestamp")["value"]
    out = pd.DataFrame(index=index)
    out["fear_greed"] = daily_index(fg).reindex(index)
    for c in ("BTC", "ETH"):
        fu = pd.read_csv(f / f"funding_deribit_{c}.csv.gz").set_index("timestamp")["interest_8h"]
        out[f"funding_{c}"] = daily_index(fu).reindex(index) * 3 * 365          # annualised funding
        dv = pd.read_csv(f / f"dvol_{c}.csv.gz").set_index("timestamp")["close"]
        out[f"dvol_{c}"] = daily_index(dv).reindex(index)
    out["funding_mkt"] = out[["funding_BTC", "funding_ETH"]].mean(axis=1).rolling(7, min_periods=3).mean()
    # all signals are known at the END of their day -> usable for the decision at that day's close
    return out


def past_pct_rank(s: pd.Series, window: int = 365) -> pd.Series:
    """Percentile of today's value within the previous `window` days (no look-ahead)."""
    return s.rolling(window, min_periods=120).apply(lambda a: (a[:-1] < a[-1]).mean() if len(a) > 1 else np.nan, raw=True)


def scale_pos(base: pd.DataFrame, mult: pd.Series | pd.DataFrame, step=0.25) -> pd.DataFrame:
    m = mult if isinstance(mult, pd.DataFrame) else pd.DataFrame({c: mult for c in base}, index=base.index)
    out = (base * m.reindex(base.index).fillna(1.0))
    return (out / step).round() * step


def drawdown_goal(base: pd.DataFrame, close: pd.DataFrame, W: pd.DataFrame, budget: float) -> pd.Series:
    """Explicit goal: keep drawdown within `budget`. Halve exposure past half the budget, quarter it past the budget."""
    ret = close.pct_change().fillna(0)
    pos = base.reindex(close.index).fillna(0)
    eq, peak, mult, out = 1.0, 1.0, 1.0, []
    prev = pd.Series(0.0, index=close.columns)
    for i, t in enumerate(close.index):
        target = pos.iloc[i] * mult
        r = float((prev * ret.iloc[i] * W.shift(1).fillna(0).iloc[i]).sum())
        cost = float(((target - prev).abs() * W.iloc[i]).sum()) * L.COST
        eq *= 1 + r - cost
        out.append(r - cost)
        peak = max(peak, eq)
        dd = eq / peak - 1
        mult = 0.25 if dd < -budget else 0.5 if dd < -budget / 2 else 1.0
        prev = target
    return pd.Series(out, index=close.index)


def cutoff_close(folder: str, hour: int) -> pd.DataFrame:
    out = {}
    for c in lab.COINS:
        d = pd.read_csv(Path(folder) / f"{c}_1h.csv.gz")
        d.index = pd.to_datetime(d["open_time"], unit="ms", utc=True)
        # close of the hourly bar that ends at `hour`:00 UTC each day
        sel = d["close"][d.index.hour == (hour - 1) % 24]
        sel.index = sel.index.floor("1D")
        out[c] = sel
    return pd.DataFrame(out)


def main():
    folder, outp = sys.argv[1], sys.argv[2]
    close, high, low, avail = L.panel(folder)
    W = L.eq_weights(avail)
    sig = load_signals(folder, close.index)
    base = L.trend_pos(close)
    R, notes = {}, {}
    R["baseline Daily trend"] = L.sim(base, close, W)

    # A. crowding: cut exposure when funding (what longs pay) is unusually high vs the past year
    crowd = past_pct_rank(sig["funding_mkt"])
    gridA = {}
    for q in (0.8, 0.9, 0.95):
        for f in (0.0, 0.5):
            gridA[(q, f)] = L.sim(scale_pos(base, pd.Series(np.where(crowd > q, f, 1.0), index=close.index)), close, W)
    R["A crowding filter (walk-forward)"], notes["A_picks"] = L.splice(gridA, lambda a, b: max(gridA, key=lambda k: L.train_score(gridA[k], a, b)), close.index)
    R["A crowding filter fixed (top 10% -> half size)"] = gridA[(0.9, 0.5)]

    # B. sentiment: extreme greed -> reduce
    gridB = {}
    for g in (75, 80, 85, 90):
        for f in (0.0, 0.5):
            gridB[(g, f)] = L.sim(scale_pos(base, pd.Series(np.where(sig["fear_greed"] > g, f, 1.0), index=close.index)), close, W)
    R["B extreme-greed filter (walk-forward)"], notes["B_picks"] = L.splice(gridB, lambda a, b: max(gridB, key=lambda k: L.train_score(gridB[k], a, b)), close.index)
    R["B extreme-greed fixed (>80 -> half size)"] = gridB[(80, 0.5)]

    # C. forward-looking volatility: size by the options market's volatility forecast
    dv = pd.DataFrame({"BTCUSDT": sig["dvol_BTC"], "ETHUSDT": sig["dvol_ETH"], "SOLUSDT": sig["dvol_ETH"] * 1.3})
    gridC = {}
    for tv in (40, 50, 60, 75):
        m = (tv / dv).clip(0.25, 1.0).fillna(1.0)
        gridC[tv] = L.sim(scale_pos(base, m), close, W)
    R["C implied-vol sizing (walk-forward)"], notes["C_picks"] = L.splice(gridC, lambda a, b: max(gridC, key=lambda k: L.train_score(gridC[k], a, b)), close.index)
    R["C implied-vol sizing fixed (target 60%)"] = gridC[60]

    # D. explicit drawdown goal
    for b in (0.2, 0.3):
        R[f"D drawdown goal {int(b * 100)}%"] = drawdown_goal(base, close, W, b)

    # E. unique timing: does the daily cut-off hour matter? (robustness + avoiding the crowd at 00:00 UTC)
    timing = {}
    for h in (0, 4, 8, 12, 16, 20):
        cc = cutoff_close(folder, h).reindex(columns=close.columns)
        av = pd.DataFrame({c: (cc.index >= cc[c].first_valid_index() + pd.Timedelta(days=210)) for c in cc}, index=cc.index).astype(float)
        r = L.sim(L.trend_pos(cc), cc, L.eq_weights(av)).loc[OOS:]
        timing[h] = L.stats(r)
    notes["E_timing"] = {h: {k: round(v, 3) for k, v in s.items() if v is not None} for h, s in timing.items()}

    # report
    oos = {k: v.loc[OOS:] for k, v in R.items()}
    by = L.yearly(oos["baseline Daily trend"])
    rep = {}
    print(f"Out-of-sample {OOS.date()} -> {close.index[-1].date()}")
    print(f"{'idea':<48}{'CAGR':>7}{'Sharpe':>8}{'MaxDD':>8}{'12m':>8}  yrs>base")
    for k, r in sorted(oos.items(), key=lambda kv: -L.stats(kv[1])["sharpe"]):
        s, y = L.stats(r), L.yearly(r)
        l12 = L.stats(r.loc[r.index[-1] - pd.Timedelta(days=365):])["total"]
        beat = sum(1 for yr in y if y[yr] > by.get(yr, -9) + 1e-9)
        rep[k] = {**s, "last_12m": l12, "yearly": y, "beat": beat}
        print(f"{k:<48}{s['cagr']*100:6.0f}%{s['sharpe']:8.2f}{s['max_dd']*100:7.0f}%{l12*100:7.1f}%  {beat}/{len(y)}")
    print("timing by daily cut-off hour (UTC):")
    for h, s in timing.items():
        print(f"  {h:02d}:00  CAGR {s['cagr']*100:5.0f}%  Sharpe {s['sharpe']:.2f}  DD {s['max_dd']*100:4.0f}%")
    print("walk-forward picks:", {k: v[-4:] for k, v in notes.items() if k.endswith("picks")})
    Path(outp).write_text(json.dumps({"report": rep, "notes": notes}, default=str))


if __name__ == "__main__":
    main()
