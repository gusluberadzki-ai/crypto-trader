"""FastAPI app: REST + WebSocket for the dashboard. Runs the engine inside the same event loop."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from urllib.parse import parse_qs
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .backtest import run_backtest
from .config import bars_per_day, bars_per_year
from .db import DB, now_ms
from .engine import BENCH, Hub, LiveEngine
from .features import ema
from .metrics import clean, equity_stats, trade_stats
from .memory import goal_status
from .postmortem import CATEGORIES, EXPLAIN, classify, summarise
from .signals import describe
from .strategies import LABELS

log = logging.getLogger("papertrader.api")
WEB = Path(__file__).parent / "web"
COOKIE = "pt_session"

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PaperTrader login</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0d12;color:#e7ebf2;font:15px system-ui,sans-serif}
form{background:#10141b;border:1px solid #212937;border-radius:14px;padding:28px;width:min(320px,88vw);display:flex;flex-direction:column;gap:14px}
h1{font-size:18px;margin:0}p{margin:0;color:#8a94a6;font-size:13px}.err{color:#f0616d}
input,button{font:inherit;padding:11px 12px;border-radius:9px;border:1px solid #2b3445;background:#161c26;color:inherit}
button{background:#8b9dff;color:#0a0d12;font-weight:600;border:none;cursor:pointer}</style></head><body>
<form method="post" action="/login"><h1>PaperTrader</h1><p>Paper trading dashboard. Enter the password.</p>{err}
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password"><button>Sign in</button></form></body></html>"""


def session_token(password: str) -> str:
    return hmac.new(password.encode(), b"papertrader-session-v1", hashlib.sha256).hexdigest()


def create_app(cfg: dict, start_engine: bool = True, engine: LiveEngine | None = None) -> FastAPI:
    db = engine.db if engine else DB(cfg["storage"]["db_path"])
    hub = engine.hub if engine else Hub()
    engine = engine or LiveEngine(cfg, db, hub)
    state = {"backtest_running": False, "start_error": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def boot():
            try:
                await engine.start()
            except Exception as exc:  # keep the dashboard up so the problem is visible
                log.exception("engine failed to start")
                state["start_error"] = f"{type(exc).__name__}: {exc}"
                db.event("error", "engine", f"failed to start: {exc}. Retrying in 60s")
                await asyncio.sleep(60)
                asyncio.create_task(boot())
        task = asyncio.create_task(boot()) if start_engine else None
        yield
        if task:
            task.cancel()
        await engine.stop()

    app = FastAPI(title="PaperTrader", lifespan=lifespan)
    password = os.environ.get("DASHBOARD_PASSWORD") or cfg["server"].get("password")
    token = session_token(password) if password else None

    def authed(cookies) -> bool:
        return token is None or hmac.compare_digest(cookies.get(COOKIE, ""), token)

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if token is None or path == "/login" or path.startswith("/static/") or authed(request.cookies):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "login required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login")
    def login_page():
        if token is None:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(LOGIN_HTML.replace("{err}", ""))

    @app.post("/login")
    async def login(request: Request):
        form = parse_qs((await request.body()).decode())
        given = (form.get("password") or [""])[0]
        if token is None or hmac.compare_digest(session_token(given), token):
            resp = RedirectResponse("/", status_code=303)
            https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
            resp.set_cookie(COOKIE, token or "", max_age=60 * 60 * 24 * 60, httponly=True, samesite="lax", secure=https)
            return resp
        await asyncio.sleep(1.5)  # slow down guessing
        return HTMLResponse(LOGIN_HTML.replace("{err}", '<p class="err">Wrong password.</p>'), status_code=401)
    app.state.engine, app.state.db, app.state.hub = engine, db, hub
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    # ------------------------------------------------------------------ overview
    @app.get("/api/status")
    def status():
        st = engine.status()
        st["start_error"] = state["start_error"]
        st["backtest_running"] = state["backtest_running"]
        return st

    @app.get("/api/markets")
    def markets():
        out = []
        per_day = int(round(bars_per_day(cfg)))
        for s in engine.symbols:
            df = engine.frames.get(s)
            price = engine.prices.get(s)
            ch = None
            if df is not None and len(df) > per_day and price:
                ch = price / float(df["close"].iloc[-per_day]) - 1
            out.append({"symbol": s, "price": price, "change_24h": ch})
        return clean(out)

    @app.get("/api/candles")
    def candles(symbol: str, limit: int = Query(500, le=1500), portfolio: str | None = None):
        symbol = symbol.upper()
        if symbol not in engine.symbols:
            raise HTTPException(404, "unknown symbol")
        df = db.load_candles(symbol, cfg["interval"], limit=limit + 200)
        if df.empty:
            return {"candles": [], "ema20": [], "ema50": [], "markers": [], "positions": []}
        e20, e50 = ema(df["close"], 20), ema(df["close"], 50)
        df = df.iloc[-limit:]
        out = {
            "candles": [{"time": int(t // 1000), "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                         "volume": r.quote_volume or r.volume} for t, r in df.iterrows()],
            "ema20": [{"time": int(t // 1000), "value": float(v)} for t, v in e20.iloc[-limit:].dropna().items()],
            "ema50": [{"time": int(t // 1000), "value": float(v)} for t, v in e50.iloc[-limit:].dropna().items()],
            "live": engine.live_bar.get(symbol),
            "markers": [], "positions": [],
        }
        pf = portfolio or cfg["strategies"]["primary"]
        start = int(df.index[0])
        for d in db.query("SELECT bar_time, action, price, explanation FROM decisions WHERE portfolio=? AND symbol=? "
                          "AND action IN ('BUY','SELL') AND bar_time>=? ORDER BY bar_time", (pf, symbol, start)):
            out["markers"].append({"time": int(d["bar_time"] // 1000), "action": d["action"], "price": d["price"]})
        p = engine.portfolios.get(pf)
        if p and symbol in p.positions:
            pos = p.positions[symbol]
            out["positions"].append({"entry": pos.entry_price, "stop": pos.stop, "take_profit": pos.take_profit})
        return clean(out)

    # ------------------------------------------------------------------ portfolios
    def summary(name: str) -> dict:
        pf = engine.portfolios[name]
        eq = pf.equity(engine.prices)
        curve = [r["equity"] for r in db.query("SELECT equity FROM equity WHERE portfolio=? ORDER BY time", (name,))]
        stats = equity_stats(curve + [eq], bars_per_year(cfg), pf.start_balance) if curve else {
            "total_return": eq / pf.start_balance - 1, "max_drawdown": 0.0}
        closed = db.query("SELECT pnl, pnl_pct, entry_time, exit_time FROM trades WHERE portfolio=? AND status='closed'",
                          (name,))
        ts = trade_stats([c["pnl"] for c in closed], [c["pnl_pct"] for c in closed],
                         [(c["exit_time"] - c["entry_time"]) / 3.6e6 for c in closed], pf.fees_paid)
        unreal = sum(p.qty * engine.prices.get(s, p.entry_price) - p.cost for s, p in pf.positions.items())
        return {
            "name": name, "label": LABELS.get(name, name), "equity": eq, "cash": pf.cash,
            "start_balance": pf.start_balance, "pnl": eq - pf.start_balance, "realized_pnl": pf.realized_pnl,
            "unrealized_pnl": unreal, "exposure": pf.exposure(engine.prices), "open_positions": len(pf.positions),
            "halted": pf.halted, "halt_reason": pf.halt_reason, **stats, **ts,
        }

    @app.get("/api/portfolios")
    def portfolios():
        if not engine.portfolios:
            return []
        rows = [summary(n) for n in engine.order if n in engine.portfolios]
        bh = next((r for r in rows if r["name"] == BENCH), None)
        for r in rows:
            r["vs_buy_hold"] = r["total_return"] - bh["total_return"] if bh else None
        return clean(rows)

    @app.post("/api/portfolios/{name}/resume")
    def resume(name: str):
        pf = engine.portfolios.get(name)
        if not pf:
            raise HTTPException(404)
        pf.halted, pf.halt_reason = False, ""
        pf.peak_equity = pf.equity(engine.prices)
        engine.save_portfolio(pf)
        engine.event("warn", "risk", f"{name}: trading manually resumed after halt")
        return {"ok": True}

    @app.get("/api/positions")
    def positions(portfolio: str | None = None):
        out = []
        for name, pf in engine.portfolios.items():
            if portfolio and name != portfolio:
                continue
            for s, p in pf.positions.items():
                px = engine.prices.get(s, p.entry_price)
                out.append({"portfolio": name, "symbol": s, "qty": p.qty, "entry_price": p.entry_price, "price": px,
                            "value": p.qty * px, "pnl": p.qty * px - p.cost, "pnl_pct": (p.qty * px - p.cost) / p.cost,
                            "stop": p.stop if name != BENCH else None, "take_profit": p.take_profit,
                            "trailing": p.trailing and name != BENCH, "trade_id": p.trade_id,
                            "explanation": p.entry_explanation})
        return clean(out)

    @app.get("/api/trades")
    def trades(portfolio: str | None = None, limit: int = Query(100, le=1000)):
        sql = "SELECT * FROM trades WHERE status='closed'"
        params: list = []
        if portfolio:
            sql += " AND portfolio=?"
            params.append(portfolio)
        sql += " ORDER BY exit_time DESC LIMIT ?"
        params.append(limit)
        rows = db.query(sql, params)
        for r in rows:
            cat = classify(r)
            r["lesson"], r["lesson_label"] = cat, CATEGORIES[cat]
        return clean(rows)

    @app.get("/api/memory")
    def memory():
        """Journal, forecast scorecard, goals per bot and the forward-looking market mood."""
        rows = portfolios()
        bh = next((r for r in rows if r["name"] == BENCH), None)
        vals = engine.signals.latest()
        return clean({
            "journal": db.query("SELECT day, time, text FROM journal ORDER BY day DESC LIMIT 30"),
            "scorecard": engine.memory.scorecard(),
            "goals": {r["name"]: goal_status(cfg, r, bh) for r in rows},
            "mood": describe(vals),
            "agent": engine.agent.summary(),
            "signals": vals,
        })

    @app.get("/api/learning")
    def learning():
        """What the bots learned: the learn-from-failure model, its weekly self-review, and trade post-mortems."""
        trades = db.query("SELECT portfolio, pnl, pnl_pct, entry_time, exit_time, exit_reason, entry_fee, exit_fee "
                          "FROM trades WHERE status='closed'")
        return clean({
            "meta": db.kv_get("meta_info") or engine.meta.info or {},
            "review": db.kv_get("meta_review"),
            "enabled": bool(db.kv_get("meta_enabled", True)),
            "postmortems": summarise(trades),
            "explain": EXPLAIN,
        })

    @app.get("/api/decisions")
    def decisions(portfolio: str | None = None, limit: int = Query(50, le=500), actions_only: bool = True):
        sql = "SELECT id, time, bar_time, portfolio, symbol, stance, action, confidence, price, explanation, details FROM decisions WHERE 1=1"
        params: list = []
        if portfolio:
            sql += " AND portfolio=?"
            params.append(portfolio)
        if actions_only:
            sql += " AND action != 'HOLD'"
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = db.query(sql, params)
        for r in rows:
            r["details"] = json.loads(r["details"] or "{}")
        return clean(rows)

    @app.get("/api/latest")
    def latest():
        """Most recent decision (including HOLD) for every strategy and coin: what each bot is thinking now."""
        rows = db.query(
            "SELECT d.id, d.time, d.bar_time, d.portfolio, d.symbol, d.stance, d.action, d.confidence, d.price, "
            "d.explanation FROM decisions d JOIN (SELECT portfolio, symbol, MAX(id) AS m FROM decisions "
            "GROUP BY portfolio, symbol) x ON d.id = x.m ORDER BY d.portfolio, d.symbol")
        return clean(rows)

    @app.get("/api/sims")
    def sims():
        path = Path(cfg.get("sims_path", "results/sims.json"))
        if not path.exists():
            return {"runs": []}
        data = json.loads(path.read_text())
        for run in data.get("runs", []):  # keep the payload small for phones
            for r in run.get("results", {}).values():
                r["trade_list"] = r.get("trade_list", [])[-150:]
        return data

    @app.get("/api/equity")
    def equity(max_points: int = 800):
        rows = db.query("SELECT time, portfolio, equity FROM equity ORDER BY time")
        series: dict[str, list] = {}
        for r in rows:
            series.setdefault(r["portfolio"], []).append({"time": int(r["time"] // 1000), "value": round(r["equity"], 2)})
        for k, v in series.items():
            if len(v) > max_points:
                stride = len(v) // max_points + 1
                series[k] = v[::stride] + ([v[-1]] if (len(v) - 1) % stride else [])
        return {"series": series, "labels": LABELS}

    # ------------------------------------------------------------------ models
    @app.get("/api/models")
    def models():
        return clean(db.query("SELECT * FROM models ORDER BY version DESC LIMIT 30"))

    @app.post("/api/models/retrain")
    async def retrain():
        if engine.training:
            return {"status": "already training"}
        asyncio.create_task(engine.retrain("manual"))
        return {"status": "started"}

    @app.post("/api/models/{version}/activate")
    def activate(version: int):
        try:
            return engine.activate_model(version)
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    # ------------------------------------------------------------------ backtests
    @app.get("/api/backtests")
    def backtests():
        rows = db.query("SELECT id, created_at, start_time, end_time, params, results FROM backtests ORDER BY id DESC LIMIT 20")
        for r in rows:
            r["params"] = json.loads(r["params"])
            r["results"] = json.loads(r["results"])
        return rows

    @app.get("/api/backtests/{bt_id}")
    def backtest(bt_id: int):
        r = db.one("SELECT * FROM backtests WHERE id=?", (bt_id,))
        if not r:
            raise HTTPException(404)
        for k in ("params", "results", "equity"):
            r[k] = json.loads(r[k])
        return r

    @app.post("/api/backtests")
    async def start_backtest(days: int = Query(None, ge=7, le=1000)):
        if state["backtest_running"]:
            return {"status": "already running"}
        days = days or cfg["backtest"]["days"]
        state["backtest_running"] = True

        def job():
            frames = {s: db.load_candles(s, cfg["interval"]) for s in cfg["symbols"]}
            return run_backtest(cfg, frames, days=days)

        async def runner():
            try:
                res = await asyncio.get_running_loop().run_in_executor(None, job)
                save_backtest(db, cfg, res)
                engine.event("info", "backtest", f"{days}-day backtest finished in {res['runtime_sec']}s")
            except Exception as exc:
                log.exception("backtest failed")
                engine.event("error", "backtest", f"backtest failed: {exc}")
            finally:
                state["backtest_running"] = False
                hub.publish({"type": "backtest"})
        asyncio.create_task(runner())
        return {"status": "started", "days": days}

    # ------------------------------------------------------------------ events + ws
    @app.get("/api/events")
    def events(limit: int = Query(50, le=500)):
        return db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        if not authed(sock.cookies):
            await sock.close(code=1008)
            return
        await sock.accept()
        q = hub.subscribe()
        try:
            await sock.send_json({"type": "status", "status": engine.status()})
            while True:
                msg = await q.get()
                await sock.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unsubscribe(q)

    return app


def save_backtest(db: DB, cfg: dict, res: dict) -> int:
    params = {"days": res["days"], "interval": res["interval"], "symbols": res["symbols"],
              "strategies": list(cfg["strategies"]["enabled"]), "models": res["models"]}
    cur = db.execute("INSERT INTO backtests(created_at, start_time, end_time, params, results, equity) VALUES(?,?,?,?,?,?)",
                     (now_ms(), res["start_time"], res["end_time"], json.dumps(params), json.dumps(res["results"]),
                      json.dumps(res["equity"])))
    return cur.lastrowid
