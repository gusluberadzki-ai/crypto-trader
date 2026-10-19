"""The bot's memory and goals.

  * Forecasts: every day the bot writes down explicit 7-day forecasts per coin (a price range from the options
    market's implied volatility, and a probability of being higher, from its own memory of what happened after
    similar trend states). A week later it checks itself and keeps a scorecard - so it remembers how good (or bad)
    its forecasts really are.
  * Journal: a short diary entry per day (what it did, why, goal progress, forecast results).
  * Goals: each bot has explicit goals (e.g. beat buy & hold, keep drawdown under 25%) tracked continuously.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .meta import rule_positions
from .signals import dvol_for
from .strategies import LABELS

DAY = 86_400_000
HORIZON = 7

DEFAULT_GOALS = {"beat_buy_hold": True}


def day_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%a %d %b")


# ------------------------------------------------------------------ goals
def goals_for(cfg: dict, name: str) -> dict:
    g = (cfg.get("goals") or {})
    return {**DEFAULT_GOALS, **(g.get("default") or {}), **(g.get(name) or {})}


def goal_status(cfg: dict, p: dict, bh: dict | None) -> list[dict]:
    """p, bh: portfolio summaries (as returned by /api/portfolios)."""
    if p["name"] == "buy_hold":
        return []
    g = goals_for(cfg, p["name"])
    out = []
    if g.get("beat_buy_hold") and bh:
        diff = p["total_return"] - bh["total_return"]
        out.append({"goal": "Beat buy & hold", "now": f"{diff * 100:+.2f} pts",
                    "status": "on track" if diff >= 0 else "behind",
                    "text": "ahead of simply holding" if diff >= 0 else "behind simply holding (normal in rallies: trend bots buy late)"})
    if g.get("max_drawdown"):
        budget, dd = g["max_drawdown"], -(p.get("max_drawdown") or 0.0)
        status = "on track" if dd < budget * 0.6 else "at risk" if dd < budget else "missed"
        out.append({"goal": f"Keep drawdown under {budget * 100:.0f}%", "now": f"worst so far {dd * 100:.1f}%",
                    "status": status, "text": f"{budget * 100 - dd * 100:.1f} pts of room left" if dd < budget else "budget exceeded"})
    if g.get("annual_return"):
        cagr = p.get("cagr")
        out.append({"goal": f"Earn {g['annual_return'] * 100:.0f}%+ a year", "now": "too early" if cagr is None else f"{cagr * 100:.0f}%/yr pace",
                    "status": "too early" if cagr is None else "on track" if cagr >= g["annual_return"] else "behind", "text": ""})
    return out


# ------------------------------------------------------------------ forecasts
class Memory:
    def __init__(self, db, cfg: dict):
        self.db, self.cfg = db, cfg

    def _p_up(self, close: pd.Series, state: pd.Series, day: pd.Timestamp) -> tuple[float, int]:
        """From memory: how often was the coin higher 7 days later, in past days with the same trend state?"""
        fwd_up = (close.shift(-HORIZON) > close).astype(float).where(close.shift(-HORIZON).notna())
        known = fwd_up.index <= day - pd.Timedelta(days=HORIZON)          # outcomes already known on `day`
        st = state.loc[day] if day in state.index else 0.0
        sample = fwd_up[known & (state == st)].dropna()
        n = len(sample)
        return float((sample.sum() + 1) / (n + 2)), n                     # Laplace-smoothed frequency

    def make_forecasts(self, close: pd.DataFrame, signal_values: dict) -> list[dict]:
        if close.empty:
            return []
        day = close.index[-1]
        day_ms = int(day.value // 1_000_000)
        if self.db.one("SELECT 1 AS x FROM forecasts WHERE made_day=?", (day_ms,)):
            return []
        state = rule_positions(close)
        rows = []
        for c in close.columns:
            px = close[c].iloc[-1]
            if pd.isna(px):
                continue
            iv = dvol_for(c, signal_values)
            if iv:
                sd, src = iv / 100 * math.sqrt(HORIZON / 365), "options-implied volatility"
            else:
                sd, src = float(np.log(close[c]).diff().tail(30).std() * math.sqrt(HORIZON)), "recent volatility"
            p_up, n = self._p_up(close[c], state[c], day)
            rows.append((day_ms, c, HORIZON, float(px), float(px * math.exp(-sd)), float(px * math.exp(sd)), p_up,
                         int(state[c].iloc[-1]), n, src))
        self.db.executemany("INSERT INTO forecasts(made_day, symbol, horizon, price, low, high, p_up, trend_state, "
                            "sample, source) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        return [{"symbol": r[1], "price": r[3], "low": r[4], "high": r[5], "p_up": r[6]} for r in rows]

    def resolve(self, close: pd.DataFrame) -> list[dict]:
        done = []
        if close.empty:
            return done
        last_ms = int(close.index[-1].value // 1_000_000)
        for f in self.db.query("SELECT * FROM forecasts WHERE resolved=0 AND made_day + horizon*86400000 <= ?", (last_ms,)):
            t = pd.Timestamp(f["made_day"] + f["horizon"] * DAY, unit="ms", tz="UTC")
            if t not in close.index or pd.isna(close.loc[t, f["symbol"]]):
                continue
            out = float(close.loc[t, f["symbol"]])
            inside, up = int(f["low"] <= out <= f["high"]), int(out > f["price"])
            self.db.execute("UPDATE forecasts SET resolved=1, outcome=?, inside=?, went_up=? WHERE id=?", (out, inside, up, f["id"]))
            done.append({**f, "outcome": out, "inside": inside, "went_up": up})
        return done

    def scorecard(self) -> dict:
        res = self.db.query("SELECT * FROM forecasts WHERE resolved=1")
        pending = self.db.query("SELECT * FROM forecasts WHERE resolved=0 ORDER BY made_day DESC, symbol LIMIT 6")
        out = {"resolved": len(res), "pending": pending}
        if res:
            br = [(r["p_up"] - r["went_up"]) ** 2 for r in res]
            out.update({
                "range_hit_rate": float(np.mean([r["inside"] for r in res])), "range_expected": 0.68,
                "brier": float(np.mean(br)), "coin_flip_brier": 0.25,
                "direction_hit_rate": float(np.mean([(r["p_up"] >= 0.5) == bool(r["went_up"]) for r in res])),
                "recent": sorted(res, key=lambda r: -r["made_day"])[:9],
            })
        return out

    # ------------------------------------------------------------------ journal
    def write_journal(self, eng, day_ms: int, forecasts: list[dict], resolved: list[dict], summaries: list[dict]) -> str:
        if self.db.one("SELECT 1 AS x FROM journal WHERE day=?", (day_ms,)):
            return ""
        last = self.db.one("SELECT MAX(time) AS t FROM journal")
        since = (last or {}).get("t") or (time.time() * 1000 - DAY)
        acts = self.db.query("SELECT portfolio, symbol, action FROM decisions WHERE time > ? AND action IN ('BUY','SELL') "
                             "ORDER BY id", (since,))
        lines = []
        if acts:
            by = {}
            for a in acts:
                by.setdefault(LABELS.get(a["portfolio"], a["portfolio"]).split(" (")[0], []).append(f"{'bought' if a['action'] == 'BUY' else 'sold'} {a['symbol'][:-4]}")
            lines.append("Did: " + "; ".join(f"{k} {', '.join(v)}" for k, v in by.items()) + ".")
        else:
            lines.append("Did: no trades — every bot kept its positions or stayed in cash.")
        prim = eng.cfg["strategies"]["primary"]
        p = next((s for s in summaries if s["name"] == prim), None)
        bh = next((s for s in summaries if s["name"] == "buy_hold"), None)
        if p:
            gs = goal_status(eng.cfg, p, bh)
            lines.append(f"{LABELS.get(prim, prim)}: {p['total_return'] * 100:+.2f}% so far; "
                         + "; ".join(f"{g['goal'].lower()}: {g['status']}" for g in gs) + ".")
        if resolved:
            hits = sum(r["inside"] for r in resolved)
            right = sum((r["p_up"] >= 0.5) == bool(r["went_up"]) for r in resolved)
            lines.append(f"Checked {len(resolved)} forecast(s) from a week ago: {hits} landed inside the predicted range, "
                         f"{right} got the direction right.")
        if forecasts:
            lines.append("Expects (7 days): " + "; ".join(
                f"{f['symbol'][:-4]} {f['low']:,.0f}–{f['high']:,.0f} ({f['p_up'] * 100:.0f}% chance higher)" for f in forecasts) + ".")
        info = getattr(eng.meta, "info", {}) or {}
        if info.get("status") == "active":
            lines.append(f"Memory: has studied {info['trades']} past trend trades; recent ones won {info['recent_win_rate'] * 100:.0f}%.")
        text = " ".join(lines)
        self.db.execute("INSERT INTO journal(day, time, text) VALUES(?,?,?)", (day_ms, int(time.time() * 1000), text))
        return text
