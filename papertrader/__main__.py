"""Command line entry point.

  python -m papertrader run                 start bot + dashboard (http://127.0.0.1:8000)
  python -m papertrader run --sim           same, with synthetic data (offline demo)
  python -m papertrader backtest --days 180 historical backtest vs buy & hold
  python -m papertrader train               train / evaluate a new ML model now
  python -m papertrader reset --yes         wipe paper portfolios, trades and decisions (keeps candles)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from .config import load_config


def setup_logging(cfg: dict) -> None:
    Path(cfg["storage"]["log_dir"]).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.handlers.RotatingFileHandler(Path(cfg["storage"]["log_dir"]) / "papertrader.log",
                                              maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [sh, fh]
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="papertrader", description="Crypto paper-trading bot (simulation only)")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the bot and dashboard")
    r.add_argument("--sim", action="store_true", help="use synthetic data instead of Binance")
    r.add_argument("--port", type=int)
    r.add_argument("--host")
    b = sub.add_parser("backtest", help="run a historical backtest")
    b.add_argument("--days", type=int)
    b.add_argument("--sim", action="store_true")
    t = sub.add_parser("train", help="train and evaluate a model now")
    t.add_argument("--sim", action="store_true")
    z = sub.add_parser("reset", help="reset paper portfolios")
    z.add_argument("--yes", action="store_true")
    z.add_argument("--sim", action="store_true")
    args = ap.parse_args(argv)

    overrides: dict = {}
    if getattr(args, "sim", False):
        overrides = {"data": {"source": "sim"}, "storage": {"db_path": "data/sim.db", "model_dir": "data/sim_models"}}
    cfg = load_config(args.config, overrides)
    setup_logging(cfg)

    if args.cmd == "run":
        import uvicorn
        from .api import create_app
        if "PORT" in os.environ and not (os.environ.get("DASHBOARD_PASSWORD") or cfg["server"].get("password")):
            sys.exit("Refusing to start on a cloud host without a dashboard password. Set the DASHBOARD_PASSWORD variable.")
        app = create_app(cfg)
        host = args.host or cfg["server"]["host"]
        port = args.port or int(os.environ.get("PORT", 0)) or cfg["server"]["port"]
        print(f"\n  PaperTrader (PAPER MODE - no real orders)  ->  http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}\n")
        uvicorn.run(app, host=host, port=port, log_level="warning", ws_ping_interval=20)

    elif args.cmd == "backtest":
        from .api import save_backtest
        from .backtest import format_table, run_backtest
        from .db import DB
        from .engine import Hub, LiveEngine
        db = DB(cfg["storage"]["db_path"])
        eng = LiveEngine(cfg, db, Hub())
        asyncio.run(eng.backfill())  # make sure history is present
        frames = {s: db.load_candles(s, cfg["interval"]) for s in cfg["symbols"]}
        res = run_backtest(cfg, frames, days=args.days, progress=print)
        save_backtest(db, cfg, res)
        print()
        print(format_table(res))

    elif args.cmd == "train":
        from .db import DB
        from .engine import Hub, LiveEngine
        db = DB(cfg["storage"]["db_path"])
        eng = LiveEngine(cfg, db, Hub())

        async def go():
            await eng.backfill()
            eng.load_active_model()
            print((await eng.retrain("manual (CLI)"))["message"])
        asyncio.run(go())

    elif args.cmd == "reset":
        if not args.yes:
            sys.exit("This deletes all paper portfolios, trades, decisions and equity history. Re-run with --yes.")
        from .db import DB
        db = DB(cfg["storage"]["db_path"])
        with db.tx() as c:
            for tbl in ("portfolios", "trades", "decisions", "equity"):
                c.execute(f"DELETE FROM {tbl}")
            c.execute("DELETE FROM kv WHERE key='last_processed_bar'")
        db.event("warn", "cli", "paper portfolios reset")
        print("Paper portfolios reset. Candles and models were kept.")


if __name__ == "__main__":
    main()
