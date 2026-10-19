import asyncio
from pathlib import Path

from papertrader.agent import NAME, AIAgent
from papertrader.config import load_config
from papertrader.db import DB
from papertrader.engine import Hub, LiveEngine

ROOT = Path(__file__).resolve().parents[1]


def test_agent_briefing_decision_and_memory(tmp_path, monkeypatch):
    cfg = load_config(ROOT / "config.yaml", {
        "history_days": 260, "data": {"source": "sim"}, "agent": {"enabled": True},
        "storage": {"db_path": str(tmp_path / "a.db"), "model_dir": str(tmp_path / "m")}})
    calls = {}

    async def fake_ask(self, brief):
        calls["brief"] = brief
        return ({"allocations": {"BTCUSDT": 0.3, "ETHUSDT": 0.9, "SOLUSDT": -1},   # out-of-range values get clipped
                 "reasoning": "BTC trend is up; ETH ok; SOL weak.", "per_coin": {"BTCUSDT": "uptrend"},
                 "outlook_7d": {"BTCUSDT": 0.55, "ETHUSDT": 0.5, "SOLUSDT": 0.45},
                 "memory_add": ["Avoid SOL when BTC is weak."], "memory_remove": [], "confidence": 0.6}, "test/model", "")

    monkeypatch.setattr(AIAgent, "ask", fake_ask)

    async def run():
        db = DB(cfg["storage"]["db_path"])
        eng = LiveEngine(cfg, db, Hub())
        await eng.backfill()
        eng.load_frames()
        eng.load_portfolios()
        await eng.catch_up(bar_stops=True)
        await eng.daily_cycle()
        pf = eng.portfolios[NAME]
        assert set(pf.positions) == {"BTCUSDT", "ETHUSDT"}                 # SOL clipped to 0
        eq = pf.equity(eng.prices)
        assert all(p.qty * p.entry_price <= eq * 0.34 for p in pf.positions.values())  # ETH 0.9 -> capped at 0.33
        b = calls["brief"]
        assert {"market", "your_portfolio", "your_memory", "research_bots"} <= set(b)
        assert eng.agent.notes() and "Avoid SOL" in eng.agent.notes()[0]
        assert db.one("SELECT COUNT(*) AS n FROM forecasts WHERE source='AI agent'")["n"] == 3
        assert db.one("SELECT COUNT(*) AS n FROM journal")["n"] == 1
        # same day again -> no second decision
        assert await eng.agent.step(eng) is None
        await eng.source.close()

    asyncio.run(run())
