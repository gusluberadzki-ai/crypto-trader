"""Scheduled mode: one run = fetch new candles, check stops, decide, retrain if due, export a static site.

Used by the GitHub Actions workflow (.github/workflows/papertrader.yml) to run the bot hourly for free.
State lives in the SQLite database, which the workflow saves to a separate `state` branch between runs.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from .api import create_app, save_backtest
from .backtest import run_backtest
from .db import DB, now_ms
from .engine import BENCH, Hub, LiveEngine
from .market import pick_source

WEB = Path(__file__).parent / "web"


async def run_step(cfg: dict, site_dir: str | None) -> None:
    db = DB(cfg["storage"]["db_path"])
    if cfg["data"]["source"] == "auto":
        chosen = db.kv_get("data_source")
        if not chosen:
            chosen = await pick_source(cfg)
            db.kv_set("data_source", chosen)
        cfg["data"]["source"] = chosen
    eng = LiveEngine(cfg, db, Hub())
    eng.source.status, eng.source.status_detail = "hourly", f"{eng.source.name} public data, hourly run"
    first = db.kv_get("last_processed_bar") is None
    eng.event("info", "runner", f"scheduled run started (data: {eng.source.name})")
    await eng.backfill()
    eng.load_frames()
    eng.load_portfolios()
    eng.load_active_model()
    last_train = db.kv_get("last_retrain_at") or 0
    if now_ms() - last_train >= cfg["ml"]["retrain_every_hours"] * 3_600_000:
        await eng.retrain("initial training" if first else "scheduled")
    before = (db.one("SELECT MAX(id) AS m FROM decisions") or {}).get("m") or 0
    await eng.catch_up(bar_stops=True)
    eng.ready = True
    from .notify import daily_summary, hello, trade_alerts
    hello(cfg, db)
    if not first:  # don't spam on the very first run
        trade_alerts(cfg, db, before)
    daily_summary(cfg, eng)

    last_bt = db.kv_get("last_backtest_at") or 0
    if now_ms() - last_bt >= 7 * 86_400_000:
        frames = {s: db.load_candles(s, cfg["interval"]) for s in cfg["symbols"]}
        res = await asyncio.get_running_loop().run_in_executor(None, lambda: run_backtest(cfg, frames))
        save_backtest(db, cfg, res)
        db.kv_set("last_backtest_at", now_ms())
        eng.event("info", "backtest", f"weekly {res['days']}-day backtest finished in {res['runtime_sec']}s")

    if site_dir:
        export_site(cfg, eng, Path(site_dir))
    eng.flush_dirty()
    for pf in eng.portfolios.values():
        eng.save_portfolio(pf)
    await eng.source.close()
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.conn.close()


def export_site(cfg: dict, eng: LiveEngine, out: Path) -> None:
    """Render every dashboard API response into data.json + a static copy of the dashboard."""
    from fastapi.testclient import TestClient

    client = TestClient(create_app(cfg, start_engine=False, engine=eng))  # no `with`: lifespan not started
    get = lambda path: client.get(path).json()
    primary = cfg["strategies"]["primary"]
    pfs = [*eng.names, BENCH]
    status = get("/api/status")
    status["generated_at"] = now_ms()
    data = {
        "static": True,
        "status": status,
        "markets": get("/api/markets"),
        "portfolios": get("/api/portfolios"),
        "positions": get("/api/positions"),
        "trades": get("/api/trades?limit=1000"),
        "decisions": get("/api/decisions?limit=500"),
        "equity": get("/api/equity"),
        "models": get("/api/models"),
        "events": get("/api/events?limit=150"),
        "backtests": get("/api/backtests"),
        "candles": {}, "markers": {}, "chart_positions": {}, "backtest_details": {},
    }
    for s in cfg["symbols"]:
        c = get(f"/api/candles?symbol={s}&limit=600&portfolio={primary}")
        data["candles"][s] = {k: c[k] for k in ("candles", "ema20", "ema50")}
        for pf in pfs:
            m = c if pf == primary else get(f"/api/candles?symbol={s}&limit=600&portfolio={pf}")
            data["markers"].setdefault(pf, {})[s] = m["markers"]
            data["chart_positions"].setdefault(pf, {})[s] = m["positions"]
    for b in data["backtests"][:5]:
        data["backtest_details"][str(b["id"])] = get(f"/api/backtests/{b['id']}")

    out.mkdir(parents=True, exist_ok=True)
    static = out / "static"
    if static.exists():
        shutil.rmtree(static)
    shutil.copytree(WEB, static)
    html = (WEB / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/', 'href="static/').replace('src="/static/', 'src="static/')
    html = html.replace("<script src=", "<script>window.PT_STATIC = true;</script>\n<script src=", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "data.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    (out / ".nojekyll").write_text("")
    (out / "widget.json").write_text(json.dumps(widget_data(cfg, eng, data)), encoding="utf-8")
    shutil.copy(Path(__file__).parent / "widget" / "widget.js", out / "widget.js")


def widget_data(cfg: dict, eng: LiveEngine, data: dict) -> dict:
    """Small summary for the phone widget."""
    bh = next(p for p in data["portfolios"] if p["name"] == BENCH)
    return {
        "updated": data["status"]["generated_at"],
        "primary": cfg["strategies"]["primary"],
        "buy_hold": bh["total_return"],
        "portfolios": [{"name": p["name"], "label": p["label"].split(" (")[0], "equity": round(p["equity"], 2),
                        "return": p["total_return"], "open": p["open_positions"], "trades": p["trades"]}
                       for p in data["portfolios"] if p["name"] != BENCH],
        "prices": [{"symbol": m["symbol"].replace("USDT", ""), "price": m["price"], "change": m["change_24h"]}
                   for m in data["markets"]],
        "url": cfg["notify"].get("dashboard_url") or "",
    }
