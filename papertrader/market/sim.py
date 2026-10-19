"""Synthetic market data for demos and tests (data.source: sim).

Generates correlated, regime-switching random walks with volume, then streams new candles at an
accelerated pace. Useful for checking the whole system offline; NOT useful for judging strategies.
"""
from __future__ import annotations

import asyncio
import time

import numpy as np

from ..config import INTERVAL_MS

START_PRICES = {"BTCUSDT": 65000.0, "ETHUSDT": 2500.0, "SOLUSDT": 150.0}


class SimMarket:
    """Deterministic generator shared by history and live stream."""

    def __init__(self, symbols: list[str], interval: str, seed: int = 7):
        self.symbols = symbols
        self.step = INTERVAL_MS[interval]
        self.rng = np.random.default_rng(seed)
        self.prices = {s: START_PRICES.get(s, 100.0) for s in symbols}
        self.regime_drift = 0.0
        self.regime_vol = 1.0
        betas = {"BTCUSDT": 1.0, "ETHUSDT": 1.2, "SOLUSDT": 1.6}
        self.beta = {s: betas.get(s, 1.3) for s in symbols}

    def _maybe_switch_regime(self):
        if self.rng.random() < 0.01:
            self.regime_drift = self.rng.normal(0, 0.0006)
            self.regime_vol = float(self.rng.choice([0.7, 1.0, 1.4, 2.0], p=[0.3, 0.4, 0.2, 0.1]))

    def next_bar(self, open_time: int) -> dict[str, dict]:
        self._maybe_switch_regime()
        market = self.rng.normal(self.regime_drift, 0.006 * self.regime_vol)
        out = {}
        for s in self.symbols:
            o = self.prices[s]
            ret = self.beta[s] * market + self.rng.normal(0, 0.004 * self.regime_vol)
            c = o * float(np.exp(ret))
            wick = abs(self.rng.normal(0, 0.003 * self.regime_vol))
            h = max(o, c) * (1 + wick)
            l = min(o, c) * (1 - abs(self.rng.normal(0, 0.003 * self.regime_vol)))
            vol = float(self.rng.lognormal(3, 0.5) * (1 + 20 * abs(ret)))
            self.prices[s] = c
            out[s] = {"open_time": open_time, "open": o, "high": h, "low": l, "close": c,
                      "volume": vol, "quote_volume": vol * c, "closed": True}
        return out


class SimSource:
    name = "sim"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.symbols = cfg["symbols"]
        self.interval = cfg["interval"]
        self.step = INTERVAL_MS[self.interval]
        self.sec_per_bar = float(cfg["data"].get("sim_seconds_per_bar", 6))
        self.market = SimMarket(self.symbols, self.interval)
        self.history: dict[str, list[dict]] = {s: [] for s in self.symbols}
        self.status, self.status_detail = "starting", "synthetic data"
        self.messages = 0
        self.reconnects = 0
        self.last_message_at = 0.0
        self.next_open: int | None = None

    def _ensure_history(self, start_ms: int):
        if self.next_open is not None:
            return
        now = int(time.time() * 1000)
        end = now - now % self.step - self.step  # last fully closed bar
        t = start_ms - start_ms % self.step
        while t <= end:
            for s, bar in self.market.next_bar(t).items():
                self.history[s].append(bar)
            t += self.step
        self.next_open = t

    async def fetch_klines(self, symbol: str, start_ms: int, end_ms: int | None = None,
                           interval: str | None = None) -> list[dict]:
        if interval and interval != self.interval:
            return []  # sim mode only generates the trading interval
        self._ensure_history(start_ms)
        return [dict(b) for b in self.history[symbol]
                if b["open_time"] >= start_ms and (end_ms is None or b["open_time"] <= end_ms)]

    async def close(self):
        pass

    async def stream(self, on_kline, on_reconnect):
        self._ensure_history(int(time.time() * 1000) - 30 * self.step)
        self.status, self.status_detail = "live", "synthetic (sim mode)"
        await on_reconnect()
        ticks = 4
        while True:
            t = self.next_open
            bars = self.market.next_bar(t)
            for i in range(1, ticks + 1):
                await asyncio.sleep(self.sec_per_bar / ticks)
                frac = i / ticks
                for s, b in bars.items():
                    partial = dict(b)
                    partial["close"] = b["open"] + (b["close"] - b["open"]) * frac
                    partial["high"] = max(b["open"], partial["close"], b["high"] if i == ticks else partial["close"])
                    partial["low"] = min(b["open"], partial["close"], b["low"] if i == ticks else partial["close"])
                    partial["closed"] = i == ticks
                    if partial["closed"]:
                        self.history[s].append(b)
                        partial = dict(b)
                    self.messages += 1
                    self.last_message_at = time.time()
                    res = on_kline(s, partial)
                    if asyncio.iscoroutine(res):
                        await res
            self.next_open = t + self.step


def make_sim_frames(symbols: list[str], interval: str, bars: int, seed: int = 7):
    """Convenience for tests/backtests: dict symbol -> DataFrame indexed by open_time."""
    import pandas as pd
    m = SimMarket(symbols, interval, seed)
    step = INTERVAL_MS[interval]
    start = 1_700_000_000_000 - 1_700_000_000_000 % step
    rows = {s: [] for s in symbols}
    for i in range(bars):
        for s, b in m.next_bar(start + i * step).items():
            rows[s].append(b)
    return {s: pd.DataFrame(r).drop(columns=["closed"]).set_index("open_time") for s, r in rows.items()}
