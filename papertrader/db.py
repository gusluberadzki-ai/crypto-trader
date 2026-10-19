"""SQLite persistence (WAL mode). Everything the bot needs to resume after a restart lives here."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles(
    symbol TEXT, interval TEXT, open_time INTEGER,
    open REAL, high REAL, low REAL, close REAL, volume REAL, quote_volume REAL,
    PRIMARY KEY(symbol, interval, open_time));
CREATE TABLE IF NOT EXISTS portfolios(
    name TEXT PRIMARY KEY, start_balance REAL, cash REAL, realized_pnl REAL, fees_paid REAL,
    peak_equity REAL, day TEXT, day_start_equity REAL, halted INTEGER, halt_reason TEXT,
    cooldowns TEXT, created_at INTEGER, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio TEXT, symbol TEXT, status TEXT,
    qty REAL, entry_time INTEGER, entry_price REAL, entry_fee REAL,
    stop REAL, initial_stop REAL, take_profit REAL, highest REAL, atr REAL,
    exit_time INTEGER, exit_price REAL, exit_fee REAL, pnl REAL, pnl_pct REAL, exit_reason TEXT,
    entry_explanation TEXT, exit_explanation TEXT, trail INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_trades_pf ON trades(portfolio, status);
CREATE TABLE IF NOT EXISTS decisions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER, bar_time INTEGER, portfolio TEXT,
    symbol TEXT, stance TEXT, action TEXT, confidence REAL, price REAL, explanation TEXT, details TEXT);
CREATE INDEX IF NOT EXISTS ix_decisions_pf ON decisions(portfolio, id);
CREATE TABLE IF NOT EXISTS equity(
    time INTEGER, portfolio TEXT, equity REAL, cash REAL, exposure REAL,
    PRIMARY KEY(time, portfolio));
CREATE TABLE IF NOT EXISTS models(
    version INTEGER PRIMARY KEY, created_at INTEGER, train_end INTEGER, train_rows INTEGER,
    val_rows INTEGER, val_auc REAL, val_expectancy REAL, val_trades INTEGER,
    baseline_expectancy REAL, active_expectancy REAL, promoted INTEGER, active INTEGER,
    reason TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS backtests(
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER, start_time INTEGER, end_time INTEGER,
    params TEXT, results TEXT, equity TEXT);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER, level TEXT, source TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class DB:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)")}
        if "trail" not in cols:  # migration for databases created before daily strategies existed
            self.conn.execute("ALTER TABLE trades ADD COLUMN trail INTEGER DEFAULT 1")

    # ---- generic helpers -------------------------------------------------
    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows) -> None:
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- key/value -------------------------------------------------------
    def kv_get(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value) -> None:
        self.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, json.dumps(value)))

    # ---- events ----------------------------------------------------------
    def event(self, level: str, source: str, message: str) -> dict:
        t = now_ms()
        cur = self.execute("INSERT INTO events(time, level, source, message) VALUES(?,?,?,?)",
                           (t, level, source, message))
        return {"id": cur.lastrowid, "time": t, "level": level, "source": source, "message": message}

    # ---- candles ---------------------------------------------------------
    def upsert_candles(self, symbol: str, interval: str, rows: list[dict]) -> None:
        if not rows:
            return
        self.executemany(
            "INSERT OR REPLACE INTO candles(symbol, interval, open_time, open, high, low, close, volume, quote_volume)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [(symbol, interval, r["open_time"], r["open"], r["high"], r["low"], r["close"],
              r["volume"], r.get("quote_volume", 0.0)) for r in rows])

    def last_candle_time(self, symbol: str, interval: str) -> int | None:
        row = self.one("SELECT MAX(open_time) AS t FROM candles WHERE symbol=? AND interval=?", (symbol, interval))
        return row["t"] if row and row["t"] is not None else None

    def load_candles(self, symbol: str, interval: str, since: int | None = None,
                     until: int | None = None, limit: int | None = None) -> pd.DataFrame:
        sql = "SELECT open_time, open, high, low, close, volume, quote_volume FROM candles WHERE symbol=? AND interval=?"
        params: list = [symbol, interval]
        if since is not None:
            sql += " AND open_time >= ?"
            params.append(since)
        if until is not None:
            sql += " AND open_time <= ?"
            params.append(until)
        if limit:
            sql = f"SELECT * FROM ({sql} ORDER BY open_time DESC LIMIT ?) ORDER BY open_time"
            params.append(limit)
        else:
            sql += " ORDER BY open_time"
        with self.lock:
            df = pd.read_sql_query(sql, self.conn, params=params)
        return df.set_index("open_time") if not df.empty else df
