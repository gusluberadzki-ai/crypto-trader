"""Binance PUBLIC market data (no API key, read-only).

Uses the market-data-only hosts (data-api.binance.vision / data-stream.binance.vision). These hosts
serve prices and candles only; they cannot accept orders. This module never sends orders.

Reliability features:
  * WebSocket kline stream with exponential backoff + jitter on disconnect
  * staleness watchdog (no message for N seconds -> reconnect)
  * automatic fallback to REST polling after repeated WebSocket failures, then retries WebSocket
  * on every (re)connect the engine's catch-up hook back-fills any candles missed while offline
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Awaitable, Callable

import httpx
import websockets

from ..config import INTERVAL_MS

log = logging.getLogger("papertrader.binance")

KlineHandler = Callable[[str, dict], Awaitable[None] | None]


def parse_rest_kline(k: list, now: int | None = None) -> dict:
    now = now or int(time.time() * 1000)
    return {
        "open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
        "close": float(k[4]), "volume": float(k[5]), "quote_volume": float(k[7]),
        "close_time": int(k[6]), "closed": int(k[6]) < now,
    }


def parse_ws_kline(k: dict) -> dict:
    return {
        "open_time": int(k["t"]), "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
        "close": float(k["c"]), "volume": float(k["v"]), "quote_volume": float(k["q"]),
        "close_time": int(k["T"]), "closed": bool(k["x"]),
    }


class BinanceSource:
    name = "binance"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.symbols = cfg["symbols"]
        self.interval = cfg["interval"]
        self.rest = cfg["data"]["rest_base"].rstrip("/")
        self.ws_base = cfg["data"]["ws_base"].rstrip("/")
        self.status = "starting"
        self.status_detail = ""
        self.messages = 0
        self.reconnects = 0
        self.last_message_at = 0.0
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), headers={"User-Agent": "papertrader/1.0"})
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------ REST
    async def _get(self, path: str, params: dict) -> list:
        delay = 1.0
        for attempt in range(6):
            try:
                c = await self.client()
                r = await c.get(f"{self.rest}{path}", params=params)
                if r.status_code in (418, 429):
                    wait = float(r.headers.get("Retry-After", 30))
                    log.warning("rate limited by Binance, waiting %.0fs", wait)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == 5:
                    raise
                log.warning("REST %s failed (%s), retry in %.0fs", path, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        return []

    async def fetch_klines(self, symbol: str, start_ms: int, end_ms: int | None = None,
                           interval: str | None = None) -> list[dict]:
        """All klines from start_ms (inclusive) up to end_ms/now, paginated 1000 at a time."""
        out: list[dict] = []
        interval = interval or self.interval
        step = INTERVAL_MS[interval]
        cursor = start_ms
        while True:
            params = {"symbol": symbol, "interval": interval, "startTime": cursor, "limit": 1000}
            if end_ms:
                params["endTime"] = end_ms
            batch = await self._get("/api/v3/klines", params)
            if not batch:
                break
            now = int(time.time() * 1000)
            out.extend(parse_rest_kline(k, now) for k in batch)
            if len(batch) < 1000:
                break
            cursor = int(batch[-1][0]) + step
            await asyncio.sleep(0.15)  # be polite with public rate limits
        return out

    # ------------------------------------------------------------- streaming
    async def stream(self, on_kline: KlineHandler, on_reconnect: Callable[[], Awaitable[None]]):
        failures = 0
        stale_after = self.cfg["data"]["stale_after_sec"]
        max_failures = self.cfg["data"]["ws_failures_before_polling"]
        streams = "/".join(f"{s.lower()}@kline_{self.interval}" for s in self.symbols)
        url = f"{self.ws_base}/stream?streams={streams}"
        while True:
            if failures >= max_failures:
                await self._poll_mode(on_kline, on_reconnect)
                failures = 0
                continue
            try:
                self.status, self.status_detail = "connecting", url
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              close_timeout=5, max_size=2**20) as ws:
                    self.status, self.status_detail = "live", "WebSocket"
                    log.info("WebSocket connected")
                    await on_reconnect()
                    failures = 0
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=stale_after)
                        self.messages += 1
                        self.last_message_at = time.time()
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        if data.get("e") != "kline":
                            continue
                        res = on_kline(data["s"], parse_ws_kline(data["k"]))
                        if asyncio.iscoroutine(res):
                            await res
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                failures += 1
                self.reconnects += 1
                self.status, self.status_detail = "reconnecting", f"stale for {stale_after}s"
                log.warning("WebSocket stale, reconnecting")
            except Exception as exc:  # network errors, server closes, DNS, ...
                failures += 1
                self.reconnects += 1
                self.status, self.status_detail = "reconnecting", f"{type(exc).__name__}: {exc}"[:200]
                log.warning("WebSocket error (%s); failure %d", exc, failures)
            delay = min(60, 2 ** min(failures, 6)) * (0.5 + random.random())
            await asyncio.sleep(delay)

    async def _poll_mode(self, on_kline: KlineHandler, on_reconnect):
        """REST polling fallback while WebSocket is unavailable."""
        until = time.time() + self.cfg["data"]["poll_mode_minutes"] * 60
        period = self.cfg["data"]["poll_seconds"]
        self.status, self.status_detail = "polling", "REST fallback (WebSocket unavailable)"
        log.warning("switching to REST polling for %d min", self.cfg["data"]["poll_mode_minutes"])
        caught_up = False
        while time.time() < until:
            try:
                if not caught_up:
                    await on_reconnect()
                    caught_up = True
                for s in self.symbols:
                    batch = await self._get("/api/v3/klines", {"symbol": s, "interval": self.interval, "limit": 2})
                    now = int(time.time() * 1000)
                    for k in batch:
                        res = on_kline(s, parse_rest_kline(k, now))
                        if asyncio.iscoroutine(res):
                            await res
                self.messages += 1
                self.last_message_at = time.time()
                self.status = "polling"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status, self.status_detail = "offline", f"{type(exc).__name__}: {exc}"[:200]
                log.warning("polling failed: %s", exc)
            await asyncio.sleep(period)
