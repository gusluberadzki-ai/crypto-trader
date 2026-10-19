import pandas as pd

from papertrader.market.sim import make_sim_frames
from papertrader.meta import MIN_SIZE, MetaLearner, daily_closes, day_ms, trade_table
from papertrader.postmortem import classify, summarise


def test_meta_learner_sizes_and_review():
    frames = make_sim_frames(["BTCUSDT", "ETHUSDT", "SOLUSDT"], "1h", 24 * 1100, seed=3)
    close = daily_closes(frames)
    tt = trade_table(close)
    assert len(tt) > 0 and set(tt.columns) >= {"entry", "exit", "win", "recent_entries", "prev_loss"}
    m = MetaLearner()
    info = m.fit(close)
    if info["status"] == "active":
        m.prepare_live(close)
        sizes = [m.size_for(c, day_ms(d))[0] for c in close for d in close.index[-200:]]
        assert all(MIN_SIZE <= s <= 1.0 for s in sizes)
    # walk-forward: every scored trade must start after the window it was trained for began
    wf = MetaLearner()
    start = close.index[-400]
    wf.build_walk_forward(close, start)
    for (coin, d), p in wf.day_prob.items():
        assert pd.Timestamp(d, unit="ms", tz="UTC") >= start
        assert 0 <= p <= 1
    rev = m.review(close, 365)
    assert {"learned_sharpe", "full_size_sharpe", "helping"} <= set(rev)


def test_postmortem_labels():
    base = {"portfolio": "trend", "entry_time": 0, "entry_fee": 1, "exit_fee": 1}
    assert classify({**base, "pnl_pct": 0.05, "exit_time": 3.6e6 * 10, "exit_reason": "signal"}) == "big_win"
    assert classify({**base, "pnl_pct": -0.002, "exit_time": 3.6e6, "exit_reason": "signal"}) == "fee_drag"
    assert classify({**base, "pnl_pct": -0.02, "exit_time": 3.6e6, "exit_reason": "stop_loss"}) == "stopped"
    assert classify({**base, "pnl_pct": -0.02, "exit_time": 3.6e6 * 2, "exit_reason": "signal"}) == "whipsaw"
    s = summarise([{**base, "pnl": -20, "pnl_pct": -0.02, "exit_time": 3.6e6 * 2, "exit_reason": "signal"}])
    assert "whipsaw" in s["trend"]["main_lesson"].lower()
