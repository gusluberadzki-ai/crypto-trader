"""Forward-looking market data (all public, no keys):

  * Implied volatility (Deribit DVOL index): the options market's forecast of how much BTC/ETH will move over the
    next 30 days. Used by the Steady-trend bot to size positions (research round 3: it kept drawdowns near 24%).
  * Perpetual funding rates (Deribit): what leveraged longs pay shorts. High = a crowded long trade.
  * Crypto Fear & Greed index (alternative.me): crowd sentiment.

Funding and sentiment are shown as context only: in the 2021-2026 tests, trading on them made results worse.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
import pandas as pd

log = logging.getLogger("papertrader.signals")
DAY = 86_400_000
DERIBIT = "https://www.deribit.com/api/v2/public"


def dvol_for(symbol: str, values: dict) -> float | None:
    """Implied vol for a coin. SOL and others have no DVOL index, so use ETH's x1.3 (they move ~30% more)."""
    if symbol.startswith("BTC"):
        return values.get("dvol_BTC")
    if symbol.startswith("ETH"):
        return values.get("dvol_ETH")
    eth = values.get("dvol_ETH")
    return eth * 1.3 if eth else None


class MarketSignals:
    """Daily values of forward-looking signals, looked up by day (ms of the UTC midnight the day starts)."""

    NAMES = ("dvol_BTC", "dvol_ETH", "funding_BTC", "funding_ETH", "fear_greed")

    def __init__(self):
        self.daily: pd.DataFrame = pd.DataFrame(columns=self.NAMES)

    def values_on(self, day: int) -> dict:
        if self.daily.empty:
            return {}
        sub = self.daily.loc[:day]
        if sub.empty:
            return {}
        last = sub.ffill().iloc[-1]
        return {k: float(v) for k, v in last.items() if pd.notna(v)}

    def latest(self) -> dict:
        return self.values_on(int(time.time() * 1000))

    # ------------------------------------------------------------ live
    def load_db(self, db) -> None:
        rows = db.query("SELECT day, name, value FROM signals")
        if rows:
            df = pd.DataFrame(rows).pivot_table(index="day", columns="name", values="value", aggfunc="last")
            self.daily = df.sort_index()

    async def fetch(self, db) -> dict:
        """Fetch the latest values (last few days) and store them. Failures never block trading."""
        now = int(time.time() * 1000)
        got: dict[str, dict[int, float]] = {}
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "papertrader/1.0"}) as c:
            for cur in ("BTC", "ETH"):
                try:
                    r = await c.get(f"{DERIBIT}/get_volatility_index_data",
                                    params={"currency": cur, "start_timestamp": now - 4 * DAY, "end_timestamp": now, "resolution": "1D"})
                    for ts, _o, _h, _l, cl in r.json()["result"]["data"]:
                        got.setdefault(f"dvol_{cur}", {})[int(ts) // DAY * DAY] = float(cl)
                except Exception as exc:
                    log.warning("DVOL %s fetch failed: %s", cur, exc)
                try:
                    r = await c.get(f"{DERIBIT}/get_funding_rate_history",
                                    params={"instrument_name": f"{cur}-PERPETUAL", "start_timestamp": now - 3 * DAY, "end_timestamp": now})
                    s = pd.DataFrame(r.json()["result"])
                    if not s.empty:
                        s["day"] = s["timestamp"] // DAY * DAY
                        for d, v in s.groupby("day")["interest_8h"].mean().items():
                            got.setdefault(f"funding_{cur}", {})[int(d)] = float(v) * 3 * 365
                except Exception as exc:
                    log.warning("funding %s fetch failed: %s", cur, exc)
            try:
                r = await c.get("https://api.alternative.me/fng/", params={"limit": 5, "format": "json"})
                for x in r.json()["data"]:
                    got.setdefault("fear_greed", {})[int(x["timestamp"]) * 1000 // DAY * DAY] = float(x["value"])
            except Exception as exc:
                log.warning("fear & greed fetch failed: %s", exc)
        rows = [(d, n, v) for n, vals in got.items() for d, v in vals.items()]
        if rows:
            db.executemany("INSERT OR REPLACE INTO signals(day, name, value) VALUES(?,?,?)", rows)
        self.load_db(db)
        return {n: max(v.items())[1] for n, v in got.items()}

    # ------------------------------------------------------------ backtests
    @classmethod
    def from_research(cls, folder: str | Path) -> "MarketSignals":
        f = Path(folder)
        out = {}

        def daily(s):
            s.index = (s.index.astype("int64") // DAY) * DAY
            return s.groupby(level=0).mean()

        for cur in ("BTC", "ETH"):
            p = f / f"dvol_{cur}.csv.gz"
            if p.exists():
                out[f"dvol_{cur}"] = daily(pd.read_csv(p).set_index("timestamp")["close"].astype(float))
        m = cls()
        if out:
            m.daily = pd.DataFrame(out).sort_index()
        return m


def describe(values: dict) -> list[dict]:
    """Plain-language reading of the current mood, for the dashboard."""
    out = []
    fg = values.get("fear_greed")
    if fg is not None:
        mood = ("extreme fear" if fg < 25 else "fear" if fg < 45 else "neutral" if fg <= 55 else "greed" if fg <= 75 else "extreme greed")
        out.append({"name": "Crowd sentiment", "value": f"{fg:.0f}/100", "label": mood,
                    "note": "Fear & Greed index. Context only: buying fear / selling greed did not help in our tests."})
    fb, fe = values.get("funding_BTC"), values.get("funding_ETH")
    if fb is not None:
        avg = (fb + (fe if fe is not None else fb)) / 2
        crowd = "very crowded longs" if avg > 0.30 else "crowded longs" if avg > 0.15 else "balanced" if avg > -0.02 else "crowded shorts"
        out.append({"name": "Leverage crowding", "value": f"{avg * 100:+.1f}%/yr", "label": crowd,
                    "note": "What leveraged longs pay shorts (perpetual funding). High = everyone is long the same trade."})
    for cur in ("BTC", "ETH"):
        v = values.get(f"dvol_{cur}")
        if v is not None:
            wk = v / 100 * (7 / 365) ** 0.5
            out.append({"name": f"{cur} expected move", "value": f"±{wk * 100:.1f}% / week", "label": f"implied vol {v:.0f}%",
                        "note": "The options market's forecast (DVOL). Steady trend uses it to size positions."})
    return out
