import asyncio
from pathlib import Path

from papertrader.config import load_config
from papertrader.db import DB
from papertrader.engine import Hub, LiveEngine

ROOT = Path(__file__).resolve().parents[1]


def test_engine_restart_and_tick_stop(tmp_path):
    cfg = load_config(ROOT / "config.yaml", {
        "history_days": 120, "data": {"source": "sim"},
        "storage": {"db_path": str(tmp_path / "t.db"), "model_dir": str(tmp_path / "m")}})

    async def run():
        db = DB(cfg["storage"]["db_path"])
        eng = LiveEngine(cfg, db, Hub())
        await eng.backfill()
        eng.load_frames()
        eng.load_portfolios()
        await eng.retrain("test")
        await eng.catch_up()  # processes the latest bar -> decisions + benchmark
        assert db.one("SELECT COUNT(*) AS n FROM decisions")["n"] >= 12
        assert len(eng.portfolios["buy_hold"].positions) == 3

        # open a position by hand and knock it out with a live tick below the stop
        pf = eng.portfolios["trend"]
        price = eng.prices["BTCUSDT"]
        pos = eng.broker.buy(pf, "BTCUSDT", 1000, price, 0, atr=price * 0.01, stop=price * 0.98, take_profit=None)
        if pos:
            eng.persist_open(pf, pos)
            await eng.on_kline("BTCUSDT", {"open_time": eng.last_processed + eng.step, "open": price, "high": price,
                                           "low": price * 0.97, "close": price * 0.97, "volume": 1, "quote_volume": 1,
                                           "closed": False})
            assert "BTCUSDT" not in pf.positions
            row = db.one("SELECT status, exit_reason FROM trades WHERE id=?", (pos.trade_id,))
            assert row == {"status": "closed", "exit_reason": "stop_loss"}

        # restart: state must come back from SQLite
        eq_before = {n: p.equity(eng.prices) for n, p in eng.portfolios.items()}
        eng2 = LiveEngine(cfg, db, Hub())
        eng2.prices = dict(eng.prices)
        eng2.load_portfolios()
        for n, v in eq_before.items():
            assert abs(eng2.portfolios[n].equity(eng.prices) - v) < 1e-6
        await eng.source.close()

    asyncio.run(run())
