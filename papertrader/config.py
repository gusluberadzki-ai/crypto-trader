"""Configuration loading and validation."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}

DEFAULTS: dict = {
    "mode": "paper",
    "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "interval": "1h",
    "history_days": 365,
    "starting_balance": 10000.0,
    "data": {
        "source": "binance",
        "rest_base": "https://data-api.binance.vision",
        "ws_base": "wss://data-stream.binance.vision",
        "stale_after_sec": 90,
        "ws_failures_before_polling": 5,
        "poll_seconds": 15,
        "poll_mode_minutes": 10,
        "sim_seconds_per_bar": 6,
    },
    "costs": {"fee_rate": 0.001, "slippage_bps": 5, "slippage_vol_factor": 0.05},
    "risk": {
        "risk_per_trade": 0.01, "atr_stop_mult": 2.0, "take_profit_rr": 4.0,
        "trail_activate_atr": 1.5, "trail_atr_mult": 2.5, "max_position_pct": 0.30,
        "max_total_exposure": 0.90, "max_open_positions": 3, "min_notional": 25,
        "daily_loss_limit": 0.03, "max_drawdown_halt": 0.20, "cooldown_bars": 3,
        "min_confidence": 0.55,
    },
    "strategies": {"enabled": ["ensemble", "trend", "mean_reversion", "ml"], "primary": "ensemble"},
    "trend": {"adx_min": 20},
    "mean_reversion": {"rsi_entry": 30, "rsi_exit": 55},
    "ml": {
        "horizon": 6, "label_threshold": 0.004, "buy_quantile": 0.85, "sell_quantile": 0.50,
        "validation_bars": 720, "min_train_rows": 2000, "min_auc": 0.51, "min_val_trades": 30,
        "promotion_margin": 0.0, "retrain_every_hours": 24,
    },
    "ensemble": {
        "weights": {"trend": 0.4, "mean_reversion": 0.2, "ml": 0.4},
        "buy_score": 0.30, "sell_score": -0.20, "max_vol_percentile": 0.95,
    },
    "backtest": {"days": 180, "retrain_every_days": 14},
    "notify": {"ntfy_topic": "", "portfolios": ["ensemble"], "daily_summary_hour_utc": 1, "dashboard_url": ""},
    "server": {"host": "127.0.0.1", "port": 8000},
    "storage": {"db_path": "data/papertrader.db", "model_dir": "data/models", "log_dir": "data/logs"},
}

KNOWN_STRATEGIES = {"ensemble", "trend", "mean_reversion", "ml", "daily_trend", "breakout", "adaptive_trend"}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = "config.yaml", overrides: dict | None = None) -> dict:
    data = {}
    if path and Path(path).exists():
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    cfg = _merge(DEFAULTS, data)
    if overrides:
        cfg = _merge(cfg, overrides)
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    if cfg.get("mode") != "paper":
        raise ValueError("Only mode: paper is supported. This project never places real orders.")
    for forbidden in ("api_key", "api_secret", "secret_key"):
        if _contains_key(cfg, forbidden):
            raise ValueError(f"'{forbidden}' found in config. Exchange credentials are not used or accepted.")
    if cfg["interval"] not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval {cfg['interval']}. Use one of {list(INTERVAL_MS)}")
    unknown = set(cfg["strategies"]["enabled"]) - KNOWN_STRATEGIES
    if unknown:
        raise ValueError(f"Unknown strategies: {unknown}")
    if cfg["strategies"]["primary"] not in cfg["strategies"]["enabled"]:
        cfg["strategies"]["primary"] = cfg["strategies"]["enabled"][0]
    cfg["symbols"] = [s.upper() for s in cfg["symbols"]]
    if os.environ.get("PT_DATA_SOURCE"):
        cfg["data"]["source"] = os.environ["PT_DATA_SOURCE"]
    if cfg["data"]["source"] not in ("binance", "coinbase", "auto", "sim"):
        raise ValueError("data.source must be 'binance', 'coinbase', 'auto' or 'sim'")


def _contains_key(obj, key: str) -> bool:
    if isinstance(obj, dict):
        return any(k == key or _contains_key(v, key) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_contains_key(v, key) for v in obj)
    return False


def interval_ms(cfg: dict) -> int:
    return INTERVAL_MS[cfg["interval"]]


def bars_per_day(cfg: dict) -> float:
    return 86_400_000 / interval_ms(cfg)


def bars_per_year(cfg: dict) -> float:
    return bars_per_day(cfg) * 365
