import logging

import httpx

from .binance import BinanceSource
from .coinbase import CoinbaseSource
from .sim import SimSource

log = logging.getLogger("papertrader.market")


def make_source(cfg: dict):
    src = cfg["data"]["source"]
    if src == "sim":
        return SimSource(cfg)
    if src == "coinbase":
        return CoinbaseSource(cfg)
    return BinanceSource(cfg)


async def pick_source(cfg: dict) -> str:
    """'auto': Binance if reachable from this machine, otherwise Coinbase."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{cfg['data']['rest_base'].rstrip('/')}/api/v3/klines",
                            params={"symbol": cfg["symbols"][0], "interval": cfg["interval"], "limit": 1})
            if r.status_code == 200 and isinstance(r.json(), list):
                return "binance"
            log.warning("Binance answered %s; falling back to Coinbase", r.status_code)
    except Exception as exc:
        log.warning("Binance unreachable (%s); falling back to Coinbase", exc)
    return "coinbase"
