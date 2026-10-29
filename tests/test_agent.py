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


def test_agent_notes_do_not_repeat(tmp_path):
    db = DB(str(tmp_path / "n.db"))
    agent = AIAgent.__new__(AIAgent)
    agent.db = db
    db.kv_set("agent_notes", ["20 Oct: BTC trend is key.", "20 Oct: Watch SOL.", "21 Oct: BTC trend is key."])
    agent._update_memory({"memory_remove": [], "memory_add": ["BTC trend is key", "New lesson."]})
    n = agent.notes()
    assert len(n) == 3 and n[0] == "20 Oct: BTC trend is key." and n[-1].endswith("New lesson.")


def test_fact_checker_catches_wrong_claims():
    from papertrader.agent import coin_facts, fact_check
    market = [
        {"coin": "BTCUSDT", "price": 81343.5, "vs_sma50": -0.007, "vs_sma100": 0.071, "vs_sma200": 0.02,
         "ret_7d": -0.03, "ret_30d": -0.026, "implied_vol": 45.0, "trend_rule_says": "stay out"},
        {"coin": "ETHUSDT", "price": 2359.7, "vs_sma50": -0.07, "vs_sma100": 0.012, "vs_sma200": -0.1,
         "ret_7d": -0.04, "ret_30d": -0.122, "implied_vol": 65.0, "trend_rule_says": "stay out"},
        {"coin": "SOLUSDT", "price": 102.4, "vs_sma50": -0.065, "vs_sma100": 0.05, "vs_sma200": -0.2,
         "ret_7d": -0.04, "ret_30d": -0.138, "implied_vol": 80.0, "trend_rule_says": "stay out"},
    ]
    assert "DOWN" in coin_facts(market[0]) and "ABOVE its 100-day" in coin_facts(market[0])
    bad = {"reasoning": "BTC is above its 100-day average and up over the last 30 days. ETH and SOL are below their "
                        "100-day averages and have high implied volatility.",
           "per_coin": {"SOLUSDT": "Below 100-day average."}}
    errs = fact_check(bad, market)
    assert len(errs) == 3 and any("30 days" in e for e in errs) and any("SOL" in e and "100-day" in e for e in errs)
    good = {"reasoning": "BTC is above its 100-day average but down over 30 days, so the trend rule says stay out. "
                         "ETH and SOL are below their 50-day averages.", "per_coin": {"BTCUSDT": "Below 50-day average."}}
    assert fact_check(good, market) == []
