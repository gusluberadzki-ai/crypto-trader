"""Coinbase Exchange PUBLIC candles (REST only, no key). Fallback when Binance is geo-blocked,
e.g. on GitHub's US-based runners. Pairs map BTCUSDT -> BTC-USD."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from ..config import INTERVAL_MS

log = logging.getLogger("papertrader.coinbase")
GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
BASE = "https://api.exchange.coinbase.com"


def product(symbol: str) -> str:
    for quote in ("USDT", "USDC", "USD"):
        if symbol.endswith(quote):
            return f"{symbol[:-len(quote)]}-USD"
    return symbol


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CoinbaseSource:
    name = "coinbase"

    def __init__(self, cfg: dict):
        if cfg["interval"] not in GRANULARITY:
            raise ValueError(f"Coinbase does not offer {cfg['interval']} candles; use one of {list(GRANULARITY)}")
        self.cfg = cfg
        self.interval = cfg["interval"]
        self.gran = GRANULARITY[self.interval]
        self.step = INTERVAL_MS[self.interval]
        self.status, self.status_detail = "hourly", "Coinbase public candles"
        self.messages = self.reconnects = 0
        self.last_message_at = 0.0
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20, headers={"User-Agent": "papertrader/1.0"})
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()

    async def _get(self, url: str, params: dict) -> list:
        delay = 1.0
        for attempt in range(6):
            try:
                r = await (await self.client()).get(url, params=params)
                if r.status_code == 429:
                    await asyncio.sleep(2 + attempt * 2)
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.HTTPError as exc:
                if attempt == 5:
                    raise
                log.warning("coinbase request failed (%s), retrying", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 20)
        return []

    async def fetch_klines(self, symbol: str, start_ms: int, end_ms: int | None = None,
                           interval: str | None = None) -> list[dict]:
        gran = GRANULARITY[interval] if interval else self.gran
        step = gran * 1000
        now = int(time.time() * 1000)
        end_ms = min(end_ms or now, now)
        start_ms -= start_ms % step
        out: dict[int, dict] = {}
        cursor = start_ms
        while cursor <= end_ms:
            chunk_end = min(cursor + 299 * step, end_ms)
            rows = await self._get(f"{BASE}/products/{product(symbol)}/candles",
                                   {"granularity": gran, "start": iso(cursor), "end": iso(chunk_end)})
            for t, low, high, op, close, vol in rows:  # newest first
                ot = int(t) * 1000
                out[ot] = {"open_time": ot, "open": float(op), "high": float(high), "low": float(low),
                           "close": float(close), "volume": float(vol), "quote_volume": float(vol) * float(close),
                           "closed": ot + step <= now}
            cursor = chunk_end + step
            await asyncio.sleep(0.2)
        self.messages += 1
        self.last_message_at = time.time()
        return [out[k] for k in sorted(out)]

    async def stream(self, on_kline, on_reconnect):
        raise RuntimeError("Coinbase source is REST-only; use it with the scheduled 'step' command")
