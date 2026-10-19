"""Push notifications through ntfy.sh (free, no account). Install the ntfy app and subscribe to the topic."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from .core import fmt_price
from .db import DB
from .engine import BENCH, LiveEngine
from .strategies import LABELS

log = logging.getLogger("papertrader.notify")


def send(cfg: dict, title: str, body: str, tags: str = "", priority: str = "default") -> None:
    topic = (cfg.get("notify") or {}).get("ntfy_topic")
    if not topic:
        return
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    url = cfg["notify"].get("dashboard_url")
    if url:
        headers["Click"] = url
    try:
        httpx.post(f"https://ntfy.sh/{topic}", content=body.encode("utf-8"), headers=headers, timeout=15)
    except Exception as exc:  # notifications must never break trading
        log.warning("notification failed: %s", exc)


def trade_alerts(cfg: dict, db: DB, since_id: int) -> None:
    wanted = set(cfg["notify"].get("portfolios") or [])
    rows = db.query("SELECT d.portfolio, d.symbol, d.action, d.price, d.confidence, d.details FROM decisions d "
                    "WHERE d.id > ? AND d.action IN ('BUY','SELL') ORDER BY d.id", (since_id,))
    for r in rows:
        if r["portfolio"] not in wanted:
            continue
        coin = r["symbol"].replace("USDT", "")
        label = LABELS.get(r["portfolio"], r["portfolio"]).split(" (")[0]
        if r["action"] == "BUY":
            send(cfg, f"Bought {coin} ({label})", f"Paper buy at {fmt_price(r['price'])}, "
                 f"confidence {r['confidence'] * 100:.0f}%. Tap for the reasoning.", "green_circle")
        else:
            t = db.one("SELECT pnl, pnl_pct, exit_reason FROM trades WHERE portfolio=? AND symbol=? AND status='closed' "
                       "ORDER BY exit_time DESC LIMIT 1", (r["portfolio"], r["symbol"]))
            res = f" Result {'+' if t['pnl'] >= 0 else '-'}${abs(t['pnl']):,.2f} ({t['pnl_pct'] * 100:+.2f}%)." if t else ""
            why = (t or {}).get("exit_reason", "signal").replace("_", " ")
            send(cfg, f"Sold {coin} ({label})", f"Paper sell at {fmt_price(r['price'])} ({why}).{res}",
                 "white_check_mark" if t and t["pnl"] >= 0 else "red_circle")


def hello(cfg: dict, db: DB) -> None:
    """One-time message so you can confirm notifications reach your phone."""
    if cfg["notify"].get("ntfy_topic") and not db.kv_get("notify_hello_sent"):
        send(cfg, "PaperTrader notifications are on",
             "You'll get a message when the bot buys or sells, plus a daily summary. Tap to open the dashboard.", "tada")
        db.kv_set("notify_hello_sent", True)


def daily_summary(cfg: dict, eng: LiveEngine) -> None:
    hour = cfg["notify"].get("daily_summary_hour_utc")
    if hour is None:
        return
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    if now.hour < hour or eng.db.kv_get("last_summary_day") == day:
        return
    bh = eng.portfolios[BENCH]
    bh_ret = bh.equity(eng.prices) / bh.start_balance - 1
    lines = []
    for name in eng.order:
        pf = eng.portfolios[name]
        ret = pf.equity(eng.prices) / pf.start_balance - 1
        n = eng.db.one("SELECT COUNT(*) AS n FROM trades WHERE portfolio=? AND status='closed'", (name,))["n"]
        lines.append(f"{LABELS.get(name, name).split(' (')[0]}: {ret * 100:+.2f}%" + ("" if name == BENCH else f" ({n} trades)"))
    bots = [n for n in eng.order if n != BENCH and n in eng.portfolios]
    best = max(bots, key=lambda n: eng.portfolios[n].equity(eng.prices))
    beat = eng.portfolios[best].equity(eng.prices) / eng.portfolios[best].start_balance - 1 > bh_ret
    lines.append(f"{LABELS[best].split(' (')[0]} is beating buy & hold." if beat else "No strategy is beating buy & hold yet.")
    try:
        from .memory import goal_status
        sums = {x["name"]: x for x in eng.portfolio_summaries()}
        prim = cfg["strategies"]["primary"]
        gs = goal_status(cfg, sums[prim], sums.get(BENCH)) if prim in sums else []
        if gs:
            lines.append("Goals: " + "; ".join(f"{g['goal'].lower()} - {g['status']}" for g in gs))
        sc = eng.memory.scorecard()
        if sc.get("resolved"):
            lines.append(f"Forecasts checked: {sc['resolved']}, {sc['range_hit_rate'] * 100:.0f}% landed in range, "
                         f"direction right {sc['direction_hit_rate'] * 100:.0f}%")
    except Exception:
        pass
    send(cfg, "PaperTrader daily summary", "\n".join(lines), "bar_chart", "low")
    eng.db.kv_set("last_summary_day", day)
