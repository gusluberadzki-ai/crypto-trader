"""Trade post-mortems: label every closed trade with WHY it won or lost, and summarise the patterns per bot."""
from __future__ import annotations

DAILY = {"adaptive_trend", "daily_trend", "breakout"}

CATEGORIES = {
    "big_win": "Rode the trend",
    "small_win": "Small win",
    "fee_drag": "Lost to fees",
    "whipsaw": "Whipsaw",
    "stopped": "Stopped out",
    "reversal": "Trend reversed",
}
EXPLAIN = {
    "big_win": "the price kept moving its way and the bot stayed in",
    "small_win": "it made a little after fees",
    "fee_drag": "the price barely moved, so trading fees caused the loss",
    "whipsaw": "the signal flipped back soon after buying (a false start)",
    "stopped": "the price fell through the safety stop-loss",
    "reversal": "the trend turned and the bot exited on its signal, below the entry price",
}


def classify(t: dict) -> str:
    pct = t.get("pnl_pct") or 0.0
    held_h = ((t.get("exit_time") or 0) - (t.get("entry_time") or 0)) / 3.6e6
    if pct > 0.03:
        return "big_win"
    if pct > 0:
        return "small_win"
    if pct > -0.005:
        return "fee_drag"
    if t.get("exit_reason") in ("stop_loss", "trailing_stop"):
        return "stopped"
    if held_h < (72 if t.get("portfolio") in DAILY else 6):
        return "whipsaw"
    return "reversal"


def summarise(trades: list[dict]) -> dict:
    """Per bot: counts and P/L per category, plus the main lesson in plain language."""
    out: dict[str, dict] = {}
    for t in trades:
        pf = t["portfolio"]
        if pf == "buy_hold":
            continue
        cat = classify(t)
        b = out.setdefault(pf, {"trades": 0, "pnl": 0.0, "fees": 0.0, "categories": {}})
        c = b["categories"].setdefault(cat, {"count": 0, "pnl": 0.0, "label": CATEGORIES[cat]})
        c["count"] += 1
        c["pnl"] += t.get("pnl") or 0.0
        b["trades"] += 1
        b["pnl"] += t.get("pnl") or 0.0
        b["fees"] += (t.get("entry_fee") or 0) + (t.get("exit_fee") or 0)
    for pf, b in out.items():
        losses = {k: v for k, v in b["categories"].items() if v["pnl"] < 0}
        if losses:
            worst = min(losses, key=lambda k: losses[k]["pnl"])
            n = losses[worst]["count"]
            b["main_lesson"] = (f"Biggest source of losses: {CATEGORIES[worst].lower()} ({n} trade{'s' if n != 1 else ''}, "
                                f"${losses[worst]['pnl']:,.2f}) — {EXPLAIN[worst]}.")
        if b["trades"] and b["fees"] > 0 and b["pnl"] < 0 and b["fees"] > abs(b["pnl"]) * 0.5:
            b["fee_note"] = f"Fees (${b['fees']:,.2f}) were more than half of the total loss: this bot trades too often for its edge."
    return out
