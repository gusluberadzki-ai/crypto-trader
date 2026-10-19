"""Learning from failure: a model that studies the Daily-trend rule's own past trades and learns which kinds of
entries tend to fail, then sizes new positions accordingly (25%-100% of a full position).

Research (research/lab2.py, research/RESULTS.md): hard "skip the trade" filters were fragile, but this soft version
(bagged committee of small gradient-boosted models, retrained as trades finish) lifted out-of-sample Sharpe from
~1.37 to ~1.55 over 2021-2026 with similar returns, consistently across all settings tried.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

COST = 0.002
FEATS = ["d100", "d50", "d200", "mom30", "mom90", "vol30", "btc_d200", "btc_mom30", "recent_entries", "prev_loss"]
FEAT_TEXT = {"d100": "distance above the 100-day average", "d50": "distance above the 50-day average",
             "d200": "distance above the 200-day average", "mom30": "30-day momentum", "mom90": "90-day momentum",
             "vol30": "volatility", "btc_d200": "BTC's distance above its 200-day average", "btc_mom30": "BTC's 30-day momentum",
             "recent_entries": "number of recent false starts", "prev_loss": "whether the last trade lost"}
COMMITTEE = [(2, 100, 10), (2, 200, 10), (3, 100, 10), (2, 100, 5), (3, 200, 20), (2, 150, 8)]
MIN_SIZE, CENTER, WIDTH = 0.25, 0.45, 0.30
MIN_TRADES = 40


def daily_closes(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Completed UTC daily closes from hourly candles (index: open_time ms)."""
    out = {}
    for s, df in frames.items():
        h = df["close"].copy()
        h.index = pd.to_datetime(h.index, unit="ms", utc=True)
        counts = h.resample("1D").count()
        d = h.resample("1D").last()
        out[s] = d[counts >= 20]  # skip partial days
    return pd.DataFrame(out).sort_index()


def rule_positions(close: pd.DataFrame) -> pd.DataFrame:
    """The Daily-trend rule on daily closes: above 100-day SMA and 30-day momentum > 0, alts only if BTC > 200-day SMA."""
    raw = ((close > close.rolling(100).mean()) & (close.pct_change(30, fill_method=None) > 0)).astype(float)
    if "BTCUSDT" in close:
        b = close["BTCUSDT"]
        ok = (b > b.rolling(200).mean()).astype(float)
        for c in raw:
            if c != "BTCUSDT":
                raw[c] = raw[c] * ok
    return raw


def trade_table(close: pd.DataFrame, pos: pd.DataFrame | None = None) -> pd.DataFrame:
    pos = rule_positions(close) if pos is None else pos
    s100, s50, s200 = (close.rolling(n).mean() for n in (100, 50, 200))
    ret = close.pct_change(fill_method=None)
    b = close["BTCUSDT"] if "BTCUSDT" in close else close.iloc[:, 0]
    btc_d200 = b / b.rolling(200).mean() - 1
    btc_m30 = b.pct_change(30, fill_method=None)
    rows = []
    idx = close.index
    for c in close:
        p = pos[c].to_numpy()
        entries = [i for i in range(1, len(p)) if p[i] == 1 and p[i - 1] == 0]
        prev_ret = np.nan
        for n_e, i in enumerate(entries):
            j = i + 1
            while j < len(p) and p[j] == 1:
                j += 1
            done = j < len(p)
            exit_i = j if done else len(p) - 1
            r = close[c].iloc[exit_i] / close[c].iloc[i] - 1 - 2 * COST
            px = close[c].iloc[i]
            rows.append({
                "coin": c, "entry": idx[i], "exit": idx[exit_i], "done": done, "ret": r, "win": int(r > 0),
                "d100": px / s100[c].iloc[i] - 1, "d50": px / s50[c].iloc[i] - 1,
                "d200": (px / s200[c].iloc[i] - 1) if not math.isnan(s200[c].iloc[i]) else 0.0,
                "mom30": close[c].pct_change(30, fill_method=None).iloc[i],
                "mom90": close[c].pct_change(90, fill_method=None).iloc[i],
                "vol30": ret[c].rolling(30).std().iloc[i] * math.sqrt(365),
                "btc_d200": 0.0 if math.isnan(btc_d200.iloc[i]) else btc_d200.iloc[i],
                "btc_mom30": btc_m30.iloc[i],
                "recent_entries": sum(1 for k in entries[:n_e] if i - 60 <= k),
                "prev_loss": 0.0 if math.isnan(prev_ret) else float(prev_ret < 0),
            })
            prev_ret = r if done else prev_ret
    return pd.DataFrame(rows)


def size_from_prob(p: float) -> float:
    return float(np.clip(MIN_SIZE + (1 - MIN_SIZE) * (p - (CENTER - WIDTH / 2)) / WIDTH, MIN_SIZE, 1.0))


def fit_committee(train: pd.DataFrame):
    X, y = train[FEATS].fillna(0).to_numpy(), train["win"].to_numpy()
    return [HistGradientBoostingClassifier(max_iter=it, max_depth=d, learning_rate=0.05, min_samples_leaf=lf).fit(X, y)
            for d, it, lf in COMMITTEE]


def predict(models, rows: pd.DataFrame) -> np.ndarray:
    X = rows[FEATS].fillna(0).to_numpy()
    return np.mean([m.predict_proba(X)[:, 1] for m in models], axis=0)


def day_ms(ts: pd.Timestamp) -> int:
    return int((ts - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1))


def sim_daily(close: pd.DataFrame, pos: pd.DataFrame) -> pd.Series:
    """Equal-weight daily portfolio returns of fractional positions, with costs (used by the weekly self-review)."""
    ret = close.pct_change(fill_method=None).fillna(0)
    avail = close.notna().astype(float)
    w = avail.div(avail.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    held = pos.shift(1).fillna(0)
    turn = pos.diff().abs().fillna(pos.abs()).shift(1).fillna(0)
    return ((held * ret - turn * COST) * w.shift(1).fillna(0)).sum(axis=1)


def sharpe(r: pd.Series) -> float:
    return float(r.mean() / r.std() * math.sqrt(365)) if r.std() > 0 else 0.0


@dataclass
class MetaLearner:
    models: list | None = None
    info: dict = field(default_factory=dict)
    day_prob: dict = field(default_factory=dict)    # (coin, day_ms) -> win probability of the trade running that day
    enabled: bool = True

    def _map_days(self, tt: pd.DataFrame, probs, last_day: pd.Timestamp) -> None:
        for (_, r), p in zip(tt.iterrows(), probs):
            if p is None or (isinstance(p, float) and math.isnan(p)):
                continue
            end = r["exit"] if r["done"] else last_day
            for d in pd.date_range(r["entry"], end, freq="1D"):
                self.day_prob[(r["coin"], day_ms(d))] = float(p)

    # ---------------- live: learn from every finished trade so far
    def fit(self, close: pd.DataFrame) -> dict:
        tt = trade_table(close)
        done = tt[tt["done"]]
        if len(done) < MIN_TRADES or done["win"].nunique() < 2:
            self.models = None
            self.info = {"status": f"waiting for {MIN_TRADES} finished trades (have {len(done)})", "trades": int(len(done))}
            return self.info
        self.models = fit_committee(done)
        recent = done.sort_values("exit").tail(20)
        self.info = {
            "status": "active", "trained_at": int(time.time() * 1000), "trades": int(len(done)),
            "win_rate": float(done["win"].mean()), "avg_win": float(done[done.win == 1]["ret"].mean()),
            "avg_loss": float(done[done.win == 0]["ret"].mean()),
            "recent_win_rate": float(recent["win"].mean()),
            "first_trade": str(done["entry"].min().date()), "last_trade": str(done["exit"].max().date()),
            "lessons": self._lessons(done),
        }
        return self.info

    def prepare_live(self, close: pd.DataFrame) -> None:
        """Score every rule trade (incl. the one running now) so strategies can look sizes up by day."""
        self.day_prob = {}
        if self.models is None or close.empty:
            return
        tt = trade_table(close)
        if tt.empty:
            return
        self._map_days(tt, predict(self.models, tt), close.index[-1])

    @staticmethod
    def _lessons(done: pd.DataFrame) -> list[str]:
        """Plain-language patterns: which conditions at entry separated winning from losing trades."""
        out = []
        for f in ("recent_entries", "btc_mom30", "mom90", "vol30", "d50", "prev_loss"):
            med = done[f].median()
            hi, lo = (done[done[f] > 0], done[done[f] <= 0]) if f == "prev_loss" else (done[done[f] > med], done[done[f] <= med])
            if len(hi) < 10 or len(lo) < 10:
                continue
            wh, wl = hi["win"].mean(), lo["win"].mean()
            if abs(wh - wl) >= 0.08:
                better = ("fewer" if wh < wl else "more") if f == "recent_entries" else ("higher" if wh > wl else "lower")
                label = "recent false starts" if f == "recent_entries" else FEAT_TEXT[f]
                text = (f"After a losing trade, the next one won {wh * 100:.0f}% of the time vs {wl * 100:.0f}% otherwise."
                        if f == "prev_loss" else
                        f"Trades that started with {better} {label} won {max(wh, wl) * 100:.0f}% of the time "
                        f"vs {min(wh, wl) * 100:.0f}% otherwise.")
                out.append(text)
        return out[:4]

    # ---------------- backtest: walk-forward (only trades finished before each window are used)
    def build_walk_forward(self, close: pd.DataFrame, start: pd.Timestamp, step_days: int = 182) -> None:
        tt = trade_table(close)
        self.day_prob, self.models = {}, None
        probs = pd.Series(np.nan, index=tt.index, dtype=float)
        t0 = start
        end = close.index[-1] + pd.Timedelta(days=1)
        while t0 < end:
            t1 = t0 + pd.Timedelta(days=step_days)
            train = tt[(tt["exit"] < t0) & tt["done"]]
            test = tt[(tt["entry"] >= t0) & (tt["entry"] < t1)]
            if len(test) and len(train) >= MIN_TRADES and train["win"].nunique() > 1:
                probs.loc[test.index] = predict(fit_committee(train), test)
            t0 = t1
        self._map_days(tt, probs.tolist(), close.index[-1])
        self.info = {"status": "walk-forward", "trades": int(tt["done"].sum())}

    def size_for(self, coin: str, day: int) -> tuple[float, float | None]:
        """(position size multiplier, win probability) for the rule trade running on `day` (ms, UTC midnight)."""
        if not self.enabled:
            return 1.0, None
        p = self.day_prob.get((coin, int(day)))
        return (1.0, None) if p is None else (size_from_prob(p), p)

    # ---------------- weekly self-review: is the learning actually helping lately?
    def review(self, close: pd.DataFrame, days: int = 365) -> dict:
        start = close.index[-1] - pd.Timedelta(days=days)
        wf = MetaLearner()
        wf.build_walk_forward(close, start, step_days=91)
        base = rule_positions(close)
        arr = base.to_numpy(copy=True)
        dms = [day_ms(d) for d in base.index]
        for j, c in enumerate(base.columns):
            for i in range(len(dms)):
                if arr[i, j] > 0:
                    arr[i, j] = wf.size_for(c, dms[i])[0]
        sized = (pd.DataFrame(arr, index=base.index, columns=base.columns) * 4).round() / 4
        rb, rs = sim_daily(close, base).loc[start:], sim_daily(close, sized).loc[start:]
        res = {"days": days, "full_size_sharpe": sharpe(rb), "learned_sharpe": sharpe(rs),
               "full_size_return": float((1 + rb).prod() - 1), "learned_return": float((1 + rs).prod() - 1),
               "reviewed_at": int(time.time() * 1000)}
        res["helping"] = res["learned_sharpe"] >= res["full_size_sharpe"] - 0.3
        return res
