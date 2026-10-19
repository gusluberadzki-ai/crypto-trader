import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from papertrader.backtest import run_backtest
from papertrader.broker import PaperBroker, Portfolio
from papertrader.config import load_config
from papertrader.core import TradingCore
from papertrader.features import compute_features
from papertrader.market.sim import make_sim_frames
from papertrader.ml import build_dataset, split
from papertrader.risk import RiskManager
from papertrader.strategies import Signal

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture(scope="module")
def frames():
    return make_sim_frames(["BTCUSDT", "ETHUSDT", "SOLUSDT"], "1h", 24 * 200)


def test_paper_only_enforced(cfg):
    with pytest.raises(ValueError):
        load_config(ROOT / "config.yaml", {"mode": "live"})
    with pytest.raises(ValueError):
        load_config(ROOT / "config.yaml", {"data": {"api_key": "x"}})
    src = "\n".join(p.read_text() for p in (ROOT / "papertrader").rglob("*.py"))
    for forbidden in ("/api/v3/order", "X-MBX-APIKEY", "signature=", "/sapi/"):
        assert forbidden not in src


def test_fees_and_slippage(cfg):
    b, pf = PaperBroker(cfg), Portfolio.new("t", 10_000)
    pos = b.buy(pf, "BTCUSDT", 1000, 100.0, 0, atr=1.0, stop=98, take_profit=None)
    slip = cfg["costs"]["slippage_bps"] / 1e4 + cfg["costs"]["slippage_vol_factor"] * 0.01
    assert pos.entry_price == pytest.approx(100 * (1 + slip))
    assert pf.cash == pytest.approx(10_000 - 1000 - 1000 * cfg["costs"]["fee_rate"])
    ct = b.sell(pf, "BTCUSDT", 100.0, 1, "signal")
    assert ct.pnl < 0  # round trip at the same price loses fees + slippage
    assert pf.cash == pytest.approx(10_000 + ct.pnl)


def test_stop_loss_bar_and_gap(cfg):
    core = TradingCore(cfg, PaperBroker(cfg), RiskManager(cfg))
    pf = Portfolio.new("t", 10_000)
    core.broker.buy(pf, "X", 1000, 100.0, 0, atr=1.0, stop=98.0, take_profit=110.0)
    ct = core.check_bar(pf, "X", {"open": 97.0, "high": 99.0, "low": 96.0}, 3_600_000)
    assert ct.exit_reason == "stop_loss" and ct.exit_price < 97.0  # gap: filled at open, minus slippage
    assert pf.cooldowns["X"] > 3_600_000


def test_trailing_stop_ratchets(cfg):
    risk = RiskManager(cfg)
    pf = Portfolio.new("t", 10_000)
    pos = PaperBroker(cfg).buy(pf, "X", 1000, 100.0, 0, atr=1.0, stop=98.0, take_profit=None)
    risk.update_trailing(pos, pos.entry_price + 3)
    assert pos.trailing and pos.stop == pytest.approx(pos.highest - cfg["risk"]["trail_atr_mult"])
    old = pos.stop
    risk.update_trailing(pos, pos.entry_price + 1)
    assert pos.stop == old  # never moves down


def test_position_sizing_caps(cfg):
    risk = RiskManager(cfg)
    pf = Portfolio.new("t", 10_000)
    plan = risk.size(pf, 100.0, 0.1, 0.9, {})  # tiny ATR -> risk size huge -> capped
    assert plan["notional"] <= 10_000 * cfg["risk"]["max_position_pct"] + 1e-6
    plan = risk.size(pf, 100.0, 5.0, 0.6, {})  # wide stop -> risk-limited
    assert plan["limited_by"] == "risk"
    assert plan["risk_amount"] <= 10_000 * cfg["risk"]["risk_per_trade"] + 1e-6


def test_drawdown_halt_blocks_entries(cfg):
    core = TradingCore(cfg, PaperBroker(cfg), RiskManager(cfg))
    pf = Portfolio.new("t", 10_000)
    pf.cash = 7_000  # 30% below peak
    row = pd.Series({"atr": 1.0, "close": 100.0})
    d = core.on_bar_close(pf, "X", row, Signal("trend", "bull", 0.9, 0.8, ["test"]), 0, 100.0, {})
    assert d.action == "BLOCKED" and pf.halted


def test_features_have_no_lookahead(frames):
    df = frames["BTCUSDT"]
    full = compute_features(df)
    part = compute_features(df.iloc[:3000])
    cols = ["rsi", "macd_hist", "atr", "adx", "ret_24", "volume_z", "vol_pctile"]
    pd.testing.assert_frame_equal(full[cols].iloc[:3000], part[cols], check_exact=False, rtol=1e-9)


def test_ml_split_purges_overlap(cfg, frames):
    data = build_dataset(cfg, frames)
    train, val = split(cfg, data)
    gap = val["time"].min() - train["time"].max()
    assert gap > cfg["ml"]["horizon"] * 3_600_000


def test_backtest_runs_and_benchmark_is_sane(cfg, frames):
    res = run_backtest(cfg, frames, days=60)
    r = res["results"]
    assert set(r) == {"ensemble", "trend", "mean_reversion", "ml", "buy_hold"}
    rets = [frames[s]["close"].iloc[-1] / frames[s]["close"].loc[res["start_time"]] - 1 for s in frames]
    assert r["buy_hold"]["total_return"] == pytest.approx(np.mean(rets), abs=0.01)
    for name, v in r.items():
        assert v["equity"] > 0 and v["max_drawdown"] <= 0
