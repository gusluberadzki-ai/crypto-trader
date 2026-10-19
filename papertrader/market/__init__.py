from .binance import BinanceSource
from .sim import SimSource


def make_source(cfg: dict):
    return SimSource(cfg) if cfg["data"]["source"] == "sim" else BinanceSource(cfg)
