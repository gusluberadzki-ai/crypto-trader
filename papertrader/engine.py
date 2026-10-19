"""Live paper-trading engine: market feed -> features -> strategies -> risk -> simulated fills -> SQLite.

Designed for 24/7 operation:
  * every background task runs under a supervisor that restarts it with backoff if it crashes
  * all state (candles, portfolios, open positions, trades, decisions, equity, models) is in SQLite
    and reloaded on start, so a restart resumes exactly where it stopped
  * after any disconnect the engine back-fills missed candles and applies stops that would have
    triggered while offline (filled at the historical stop/gap price)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import pandas as pd

from .broker import ClosedTrade, PaperBroker, Portfolio, Position
from .config import INTERVAL_MS
from .core import REASON_TEXT, Decision, TradingCore, fmt_price
from .db import DB, now_ms
from .features import features_for_all
from .market import make_source
from .metrics import clean
from .ml import ModelManager, promotion_decision, train_candidate
from .risk import RiskManager
from .strategies import LABELS, build_strategies, evaluate_all

log = logging.getLogger("papertrader.engine")
FRAME_BARS = 1500
BENCH = "buy_hold"


class Hub:
    """Fan-out of live events to dashboard WebSocket clients."""

    def __init__(self):
        self.clients: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def publish(self, msg: dict) -> None:
        msg = clean(msg)
        for q in list(self.clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)


async def supervise(name: str, factory: Callable[[], Awaitable[None]], db: DB, hub: Hub) -> None:
    backoff = 1.0
    while True:
        try:
            await factory()
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("task %s crashed", name)
            ev = db.event("error", name, f"crashed: {type(exc).__name__}: {exc}. Restarting in {backoff:.0f}s")
            hub.publish({"type": "event", "event": ev})
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


class LiveEngine:
    def __init__(self, cfg: dict, db: DB, hub: Hub):
        self.cfg, self.db, self.hub = cfg, db, hub
        self.symbols: list[str] = cfg["symbols"]
        self.interval = cfg["interval"]
        self.step = INTERVAL_MS[self.interval]
        self.source = make_source(cfg)
        self.broker = PaperBroker(cfg)
        self.risk = RiskManager(cfg)
        self.core = TradingCore(cfg, self.broker, self.risk)
        self.models = ModelManager(cfg, cfg["storage"]["model_dir"])
        self.strategies = build_strategies(cfg, self.models)
        self.names: list[str] = list(cfg["strategies"]["enabled"])
        self.portfolios: dict[str, Portfolio] = {}
        self.frames: dict[str, pd.DataFrame] = {}
        self.prices: dict[str, float] = {}
        self.live_bar: dict[str, dict] = {}
        self.pending: dict[int, set] = {}
        self.last_processed: int | None = db.kv_get("last_processed_bar")
        self.started_at = time.time()
        self.last_bar_at: float | None = None
        self.training = False
        self.dirty: set[tuple[str, str]] = set()
        self._last_tick_pub: dict[str, float] = {}
        self.tasks: list[asyncio.Task] = []
        self.ready = False

    # ================================================================ lifecycle
    async def start(self) -> None:
        self.event("info", "engine", f"starting in PAPER mode, {len(self.symbols)} symbols, {self.interval} bars, "
                                     f"source={self.source.name}")
        await self.backfill()
        self.load_frames()
        self.load_portfolios()
        self.load_active_model()
        if self.models.active is None and self.db.kv_get("last_retrain_at") is None:
            await self.retrain("initial training")
        sup = lambda name, f: asyncio.create_task(supervise(name, f, self.db, self.hub), name=name)
        self.tasks = [
            sup("market-feed", lambda: self.source.stream(self.on_kline, self.catch_up)),
            sup("retrain-scheduler", self.retrain_loop),
            sup("housekeeping", self.housekeeping_loop),
        ]
        self.ready = True

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.flush_dirty()
        for pf in self.portfolios.values():
            self.save_portfolio(pf)
        await self.source.close()
        self.event("info", "engine", "stopped cleanly")

    def event(self, level: str, source: str, message: str) -> None:
        getattr(log, "warning" if level == "warn" else level if level in ("info", "error") else "info")(message)
        ev = self.db.event(level, source, message)
        self.hub.publish({"type": "event", "event": ev})

    # ================================================================ market data
    async def backfill(self) -> None:
        now = now_ms()
        for s in self.symbols:
            last = self.db.last_candle_time(s, self.interval)
            since = last + self.step if last else now - self.cfg["history_days"] * 86_400_000
            rows = await self.source.fetch_klines(s, since)
            closed = [r for r in rows if r["closed"]]
            self.db.upsert_candles(s, self.interval, closed)
            if rows:
                self.prices[s] = rows[-1]["close"]
            self.event("info", "data", f"{s}: back-filled {len(closed)} candles")

    def load_frames(self) -> None:
        for s in self.symbols:
            self.frames[s] = self.db.load_candles(s, self.interval, limit=FRAME_BARS)
            if s not in self.prices and not self.frames[s].empty:
                self.prices[s] = float(self.frames[s]["close"].iloc[-1])

    def _append_candle(self, s: str, k: dict) -> None:
        self.db.upsert_candles(s, self.interval, [k])
        df = self.frames.get(s)
        row = pd.DataFrame([{c: k[c] for c in ("open", "high", "low", "close", "volume", "quote_volume")}],
                           index=pd.Index([k["open_time"]], name="open_time"))
        if df is None or df.empty:
            self.frames[s] = row
        elif k["open_time"] in df.index:
            df.loc[k["open_time"]] = row.iloc[0]
        else:
            self.frames[s] = pd.concat([df, row]).iloc[-FRAME_BARS:]

    async def catch_up(self) -> None:
        """Runs on every (re)connect: fetch missed candles, apply offline stops, process a fresh bar."""
        new_closed: dict[int, dict[str, dict]] = {}
        for s in self.symbols:
            last = self.db.last_candle_time(s, self.interval)
            since = last + self.step if last else now_ms() - 3 * self.step
            rows = await self.source.fetch_klines(s, since)
            for r in rows:
                if r["closed"]:
                    self._append_candle(s, r)
                    new_closed.setdefault(r["open_time"], {})[s] = r
                self.prices[s] = r["close"]
        latest = max((self.frames[s].index[-1] for s in self.symbols if not self.frames[s].empty), default=None)
        if latest is None:
            return
        if self.last_processed is None:
            self.last_processed = latest - self.step
        gap = sorted(t for t in new_closed if self.last_processed < t < latest)
        for t in gap:  # stops that would have triggered while we were offline
            for s, bar in new_closed[t].items():
                self.reconcile_bar(s, bar, t)
        if gap:
            self.event("warn", "data", f"recovered {len(gap)} missed bar(s); stops re-checked against their high/low")
        fresh = now_ms() - (latest + self.step) < self.step or self.source.name == "sim"
        if latest > self.last_processed:
            if fresh:
                self.process_bar(latest)
            else:
                self.mark_processed(latest)

    def reconcile_bar(self, s: str, bar: dict, t: int) -> None:
        for name, pf in self.portfolios.items():
            if name == BENCH:
                continue
            ct = self.core.check_bar(pf, s, bar, t)
            if ct:
                self.record_exit(pf, ct, offline=True)
            elif s in pf.positions:
                self.dirty.add((name, s))

    async def on_kline(self, s: str, k: dict) -> None:
        if s not in self.symbols:
            return
        price = k["close"]
        self.prices[s] = price
        self.live_bar[s] = k
        t = k["open_time"]
        for name, pf in self.portfolios.items():
            if name == BENCH or s not in pf.positions:
                continue
            ct, changed = self.core.check_tick(pf, s, price, t)
            if ct:
                self.record_exit(pf, ct)
            elif changed:
                self.dirty.add((name, s))
        now = time.time()
        if k["closed"] or now - self._last_tick_pub.get(s, 0) > 0.75:
            self._last_tick_pub[s] = now
            self.hub.publish({"type": "tick", "symbol": s, "price": price, "bar": k})
        if not k["closed"]:
            return
        self._append_candle(s, k)
        if self.last_processed is not None and t <= self.last_processed:
            return
        pend = self.pending.setdefault(t, set())
        first = not pend
        pend.add(s)
        if pend >= set(self.symbols):
            self.process_bar(t)
        elif first:
            asyncio.get_running_loop().call_later(20, self._flush_pending, t)

    def _flush_pending(self, t: int) -> None:
        if t in self.pending and (self.last_processed is None or t > self.last_processed):
            self.process_bar(t)

    # ================================================================ decisions
    def process_bar(self, t: int) -> None:
        try:
            self._process_bar(t)
        except Exception as exc:
            log.exception("bar processing failed")
            self.event("error", "engine", f"bar {pd.Timestamp(t, unit='ms')} failed: {exc}")
            self.mark_processed(t)

    def _process_bar(self, t: int) -> None:
        self.pending.pop(t, None)
        feats = features_for_all(self.frames)
        for s in self.symbols:
            if t in feats[s].index:
                self.prices[s] = float(feats[s].loc[t, "close"])
        bench = self.portfolios[BENCH]
        if not bench.positions and len(self.prices) == len(self.symbols):
            self.core.buy_and_hold(bench, dict(self.prices), t)
            for pos in bench.positions.values():
                self.persist_open(bench, pos)
            self.event("info", "benchmark", "buy & hold benchmark opened with equal weights")
        ctx = {"explain": True, "label_threshold": self.cfg["ml"]["label_threshold"]}
        for s in self.symbols:
            if t not in feats[s].index:
                continue
            row = feats[s].loc[t]
            sigs = evaluate_all(self.strategies, s, row, ctx)
            for name in self.names:
                pf = self.portfolios[name]
                d = self.core.on_bar_close(pf, s, row, sigs[name], t, self.prices[s], self.prices)
                if d.opened:
                    self.persist_open(pf, d.opened)
                    self.hub.publish({"type": "trade", "portfolio": name, "side": "BUY", "symbol": s,
                                      "price": d.opened.entry_price, "time": now_ms()})
                if d.closed:
                    self.persist_close(d.closed, d.explanation)
                    self.hub.publish({"type": "trade", "portfolio": name, "side": "SELL", "symbol": s,
                                      "price": d.closed.exit_price, "pnl": d.closed.pnl, "time": now_ms()})
                self.persist_decision(d)
        self.snapshot_equity(t)
        for pf in self.portfolios.values():
            self.save_portfolio(pf)
        self.flush_dirty()
        self.mark_processed(t)
        self.last_bar_at = time.time()
        self.hub.publish({"type": "bar", "time": t})

    def mark_processed(self, t: int) -> None:
        self.last_processed = t
        self.db.kv_set("last_processed_bar", t)
        for k in [k for k in self.pending if k <= t]:
            self.pending.pop(k, None)

    def record_exit(self, pf: Portfolio, ct: ClosedTrade, offline: bool = False) -> None:
        text = (f"SELL {LABELS.get(pf.name, pf.name)} on {ct.symbol} at {fmt_price(ct.exit_price)}: "
                f"{REASON_TEXT.get(ct.exit_reason, ct.exit_reason)}{' while the bot was offline' if offline else ''}. "
                f"Result {'+' if ct.pnl >= 0 else '-'}${abs(ct.pnl):,.2f} ({ct.pnl_pct * 100:+.2f}%) after fees.")
        self.persist_close(ct, text)
        self.persist_decision(Decision(pf.name, ct.symbol, ct.exit_time, "exit", "SELL", 1.0, ct.exit_price, text,
                                       {"exit_reason": ct.exit_reason}))
        self.save_portfolio(pf)
        self.hub.publish({"type": "trade", "portfolio": pf.name, "side": "SELL", "symbol": ct.symbol,
                          "price": ct.exit_price, "pnl": ct.pnl, "reason": ct.exit_reason, "time": now_ms()})

    # ================================================================ persistence
    def load_portfolios(self) -> None:
        bal = float(self.cfg["starting_balance"])
        rows = {r["name"]: r for r in self.db.query("SELECT * FROM portfolios")}
        for name in [*self.names, BENCH]:
            r = rows.get(name)
            if r is None:
                pf = Portfolio.new(name, bal)
                self.portfolios[name] = pf
                self.save_portfolio(pf, created=True)
                continue
            pf = Portfolio(name=name, start_balance=r["start_balance"], cash=r["cash"],
                           realized_pnl=r["realized_pnl"], fees_paid=r["fees_paid"], peak_equity=r["peak_equity"],
                           day=r["day"], day_start_equity=r["day_start_equity"], halted=bool(r["halted"]),
                           halt_reason=r["halt_reason"] or "", cooldowns=json.loads(r["cooldowns"] or "{}"))
            for tr in self.db.query("SELECT * FROM trades WHERE portfolio=? AND status='open'", (name,)):
                pf.positions[tr["symbol"]] = Position(
                    tr["symbol"], tr["qty"], tr["entry_price"], tr["entry_time"], tr["entry_fee"], tr["stop"],
                    tr["initial_stop"], tr["take_profit"], tr["highest"], tr["atr"], tr["id"],
                    tr["entry_explanation"] or "")
            self.portfolios[name] = pf
        n_open = sum(len(p.positions) for p in self.portfolios.values())
        self.event("info", "engine", f"loaded {len(self.portfolios)} portfolios with {n_open} open positions")

    def save_portfolio(self, pf: Portfolio, created: bool = False) -> None:
        self.db.execute(
            "INSERT INTO portfolios(name, start_balance, cash, realized_pnl, fees_paid, peak_equity, day, "
            "day_start_equity, halted, halt_reason, cooldowns, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET cash=excluded.cash, realized_pnl=excluded.realized_pnl, "
            "fees_paid=excluded.fees_paid, peak_equity=excluded.peak_equity, day=excluded.day, "
            "day_start_equity=excluded.day_start_equity, halted=excluded.halted, halt_reason=excluded.halt_reason, "
            "cooldowns=excluded.cooldowns, updated_at=excluded.updated_at",
            (pf.name, pf.start_balance, pf.cash, pf.realized_pnl, pf.fees_paid, pf.peak_equity, pf.day,
             pf.day_start_equity, int(pf.halted), pf.halt_reason, json.dumps(pf.cooldowns), now_ms(), now_ms()))

    def persist_open(self, pf: Portfolio, pos: Position) -> None:
        cur = self.db.execute(
            "INSERT INTO trades(portfolio, symbol, status, qty, entry_time, entry_price, entry_fee, stop, "
            "initial_stop, take_profit, highest, atr, entry_explanation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pf.name, pos.symbol, "open", pos.qty, now_ms(), pos.entry_price, pos.entry_fee, pos.stop,
             pos.initial_stop, pos.take_profit, pos.highest, pos.atr, pos.entry_explanation))
        pos.trade_id = cur.lastrowid
        self.save_portfolio(pf)

    def persist_close(self, ct: ClosedTrade, explanation: str) -> None:
        self.db.execute(
            "UPDATE trades SET status='closed', exit_time=?, exit_price=?, exit_fee=?, pnl=?, pnl_pct=?, "
            "exit_reason=?, exit_explanation=? WHERE id=?",
            (now_ms(), ct.exit_price, ct.exit_fee, ct.pnl, ct.pnl_pct, ct.exit_reason, explanation, ct.trade_id))

    def persist_decision(self, d: Decision) -> None:
        cur = self.db.execute(
            "INSERT INTO decisions(time, bar_time, portfolio, symbol, stance, action, confidence, price, explanation, details)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (now_ms(), d.bar_time, d.portfolio, d.symbol, d.stance, d.action, d.confidence, d.price,
             d.explanation, json.dumps(clean(d.details))))
        if d.action != "HOLD":
            self.hub.publish({"type": "decision", "decision": {
                "id": cur.lastrowid, "time": now_ms(), "portfolio": d.portfolio, "symbol": d.symbol,
                "action": d.action, "confidence": d.confidence, "price": d.price, "explanation": d.explanation}})

    def flush_dirty(self) -> None:
        for name, s in list(self.dirty):
            pos = self.portfolios[name].positions.get(s)
            if pos and pos.trade_id:
                self.db.execute("UPDATE trades SET stop=?, highest=? WHERE id=?", (pos.stop, pos.highest, pos.trade_id))
        self.dirty.clear()

    def snapshot_equity(self, t: int) -> None:
        rows = []
        for name, pf in self.portfolios.items():
            rows.append((t, name, pf.equity(self.prices), pf.cash, pf.exposure(self.prices)))
        self.db.executemany("INSERT OR REPLACE INTO equity(time, portfolio, equity, cash, exposure) VALUES(?,?,?,?,?)", rows)
        self.hub.publish({"type": "equity", "time": t, "equity": {r[1]: r[2] for r in rows}})

    # ================================================================ models
    def load_active_model(self) -> None:
        row = self.db.one("SELECT * FROM models WHERE active=1 ORDER BY version DESC LIMIT 1")
        if row:
            try:
                self.models.active = self.models.load(row["path"])
                self.event("info", "model", f"loaded active model v{row['version']}")
            except Exception as exc:
                self.event("error", "model", f"could not load model v{row['version']}: {exc}")

    async def retrain(self, reason: str = "scheduled") -> dict:
        if self.training:
            return {"status": "already training"}
        self.training = True
        self.hub.publish({"type": "model", "status": "training"})
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, self._retrain_sync, reason)
            self.event("info" if res.get("promoted") else "warn", "model", res["message"])
            return res
        except Exception as exc:
            log.exception("retrain failed")
            self.event("error", "model", f"retraining failed: {exc}")
            return {"status": "error", "message": str(exc)}
        finally:
            self.training = False
            self.db.kv_set("last_retrain_at", now_ms())
            self.hub.publish({"type": "model", "status": "idle"})

    def _retrain_sync(self, reason: str) -> dict:
        frames = {s: self.db.load_candles(s, self.interval) for s in self.symbols}
        frames = {s: f for s, f in frames.items() if not f.empty}
        row = self.db.one("SELECT MAX(version) AS v FROM models")
        version = (row["v"] or 0) + 1 if row else 1
        cand, val, rep = train_candidate(self.cfg, frames, version=version)
        if cand is None:
            return {"status": "skipped", "promoted": False,
                    "message": f"retrain ({reason}) skipped: {rep.get('error')}"}
        ok, why, act_exp = promotion_decision(self.cfg, cand, val, rep, self.models.active)
        path = self.models.save(cand)
        with self.db.tx() as c:
            if ok:
                c.execute("UPDATE models SET active=0")
            c.execute(
                "INSERT INTO models(version, created_at, train_end, train_rows, val_rows, val_auc, val_expectancy, "
                "val_trades, baseline_expectancy, active_expectancy, promoted, active, reason, path) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (version, now_ms(), cand.meta["train_end"], cand.meta["train_rows"], cand.meta["val_rows"],
                 rep["auc"], rep["expectancy"], rep["trades"], rep["baseline_expectancy"], act_exp,
                 int(ok), int(ok), why, path))
        if ok:
            self.models.active = cand  # atomic swap; live predictions switch on the next bar
        return {"status": "done", "promoted": ok, "version": version,
                "message": f"model v{version} ({reason}): {why}"}

    def activate_model(self, version: int) -> dict:
        row = self.db.one("SELECT * FROM models WHERE version=?", (version,))
        if not row:
            raise ValueError("unknown model version")
        tm = self.models.load(row["path"])
        with self.db.tx() as c:
            c.execute("UPDATE models SET active=0")
            c.execute("UPDATE models SET active=1 WHERE version=?", (version,))
        self.models.active = tm
        self.event("warn", "model", f"model v{version} manually activated")
        return {"active": version}

    async def retrain_loop(self) -> None:
        every = self.cfg["ml"]["retrain_every_hours"] * 3_600_000
        while True:
            last = self.db.kv_get("last_retrain_at", 0) or 0
            if now_ms() - last >= every:
                await self.retrain("scheduled")
            await asyncio.sleep(300)

    # ================================================================ housekeeping
    async def housekeeping_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            self.flush_dirty()
            self.hub.publish({"type": "status", "status": self.status()})
            stale = self.cfg["data"]["stale_after_sec"] * 2
            if self.source.last_message_at and time.time() - self.source.last_message_at > stale:
                self.event("warn", "data", f"no market data for {time.time() - self.source.last_message_at:.0f}s")

    def status(self) -> dict:
        src = self.source
        return {
            "mode": "paper",
            "ready": self.ready,
            "source": src.name,
            "feed": src.status,
            "feed_detail": src.status_detail,
            "messages": src.messages,
            "reconnects": src.reconnects,
            "last_message_age": round(time.time() - src.last_message_at, 1) if src.last_message_at else None,
            "last_bar": self.last_processed,
            "uptime_sec": round(time.time() - self.started_at),
            "training": self.training,
            "model_version": self.models.active.version if self.models.active else None,
            "interval": self.interval,
            "symbols": self.symbols,
            "primary": self.cfg["strategies"]["primary"],
            "strategies": self.names,
        }
