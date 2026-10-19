"""Historical backtesting with walk-forward ML retraining.

Uses the same TradingCore / RiskManager / PaperBroker as live trading. Rules:
  * decisions are made at each bar's close using features up to that bar only
  * stops / take-profits are checked against the NEXT bars' high/low (stop assumed hit first)
  * the ML model is retrained every `retrain_every_days` using only data available at that time,
    and goes through the same promotion gate as live retraining
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Callable

import numpy as np
import pandas as pd

from .broker import PaperBroker, Portfolio
from .config import INTERVAL_MS, bars_per_day, bars_per_year
from .core import TradingCore
from .features import FEATURE_COLS, features_for_all
from .metrics import clean, equity_stats, trade_stats
from .ml import ModelManager, promotion_decision, train_candidate
from .risk import RiskManager
from .strategies import build_strategies, evaluate_all


def run_backtest(cfg: dict, frames: dict[str, pd.DataFrame], days: int | None = None,
                 strategies: list[str] | None = None,
                 progress: Callable[[str], None] | None = None) -> dict:
    say = progress or (lambda m: None)
    t0 = time.time()
    step = INTERVAL_MS[cfg["interval"]]
    days = days or cfg["backtest"]["days"]
    names = strategies or list(cfg["strategies"]["enabled"])
    frames = {s: df.sort_index() for s, df in frames.items() if not df.empty}
    symbols = [s for s in cfg["symbols"] if s in frames]
    if not symbols:
        raise ValueError("no candle data for any symbol; run a backfill first")
    feats = features_for_all({s: frames[s] for s in symbols})
    all_times = sorted(set().union(*[set(f.index) for f in feats.values()]))
    end = all_times[-1]
    start = max(end - days * 86_400_000, all_times[min(len(all_times) - 1, 400)])
    test_times = [t for t in all_times if t >= start]
    say(f"backtest {len(test_times)} bars from {pd.Timestamp(start, unit='ms')} to {pd.Timestamp(end, unit='ms')}")

    # ---------------- walk-forward ML: train at each retrain point, predict the following segment
    mm = ModelManager(cfg)
    segments: list[tuple[int, object]] = []
    preds = {s: pd.Series(np.nan, index=feats[s].index) for s in symbols}
    model_log = []
    if {"ml", "ensemble"} & set(names):
        every = max(1, int(cfg["backtest"]["retrain_every_days"] * bars_per_day(cfg)))
        points = test_times[::every]
        for i, rt in enumerate(points):
            cand, val, rep = train_candidate(cfg, frames, end_time=rt, feats=feats, version=i + 1)
            if cand is not None:
                ok, reason, act_exp = promotion_decision(cfg, cand, val, rep, mm.active)
                if ok:
                    mm.active = cand
            else:
                ok, reason = False, rep.get("error", "training failed")
            model_log.append(clean({"time": rt, "version": i + 1, "promoted": ok, "reason": reason,
                                    **({k: rep[k] for k in ("auc", "expectancy", "trades")} if cand else {})}))
            say(f"model v{i + 1} @ {pd.Timestamp(rt, unit='ms').date()}: {reason}")
            seg_end = points[i + 1] if i + 1 < len(points) else end + 1
            segments.append((rt, mm.active))
            if mm.active is not None:
                for s in symbols:
                    f = feats[s]
                    mask = (f.index >= rt) & (f.index < seg_end)
                    if mask.any():
                        preds[s].loc[mask] = mm.active.proba(f.loc[mask, FEATURE_COLS])

    # ---------------- simulation
    broker, risk = PaperBroker(cfg), RiskManager(cfg)
    core = TradingCore(cfg, broker, risk)
    strat = build_strategies(cfg, mm)
    bal = float(cfg["starting_balance"])
    pfs = {n: Portfolio.new(n, bal) for n in names}
    bench = Portfolio.new("buy_hold", bal)
    curves = {n: [] for n in [*names, "buy_hold"]}
    actions = {n: Counter() for n in names}
    prices: dict[str, float] = {}
    seg_i = -1
    ctx_base = {"label_threshold": cfg["ml"]["label_threshold"], "explain": False}

    for t in test_times:
        while seg_i + 1 < len(segments) and segments[seg_i + 1][0] <= t:
            seg_i += 1
            mm.active = segments[seg_i][1]
        rows = {s: feats[s].loc[t] for s in symbols if t in feats[s].index}
        for s, row in rows.items():
            for pf in pfs.values():
                core.check_bar(pf, s, row, t)
            prices[s] = float(row["close"])
        if not bench.positions and len(prices) == len(symbols):
            core.buy_and_hold(bench, dict(prices), t)
        for s, row in rows.items():
            p = preds[s].get(t, np.nan)
            sigs = evaluate_all(strat, s, row, {**ctx_base, "ml_p": None if np.isnan(p) else float(p)})
            for n, pf in pfs.items():
                d = core.on_bar_close(pf, s, row, sigs[n], t, float(row["close"]), prices)
                actions[n][d.action] += 1
        for n, pf in pfs.items():
            curves[n].append(pf.equity(prices))
        curves["buy_hold"].append(bench.equity(prices) if bench.positions else bal)

    # ---------------- results
    bpy = bars_per_year(cfg)
    results = {}
    bh_ret = curves["buy_hold"][-1] / bal - 1
    for n in [*names, "buy_hold"]:
        pf = bench if n == "buy_hold" else pfs[n]
        stats = equity_stats(curves[n], bpy, bal)
        tr = trade_stats([c.pnl for c in pf.closed], [c.pnl_pct for c in pf.closed],
                         [(c.exit_time - c.entry_time) / 3_600_000 for c in pf.closed], pf.fees_paid)
        results[n] = {**stats, **tr, "open_positions": len(pf.positions),
                      "vs_buy_hold": stats["total_return"] - bh_ret,
                      "actions": dict(actions.get(n, {}))}
    stride = max(1, len(test_times) // 600)
    eq = {"time": test_times[::stride], **{n: [round(v, 2) for v in c[::stride]] for n, c in curves.items()}}
    say(f"done in {time.time() - t0:.1f}s")
    return clean({
        "start_time": int(test_times[0]), "end_time": int(test_times[-1]), "days": days,
        "symbols": symbols, "interval": cfg["interval"], "results": results, "equity": eq,
        "models": model_log, "runtime_sec": round(time.time() - t0, 1),
    })


def format_table(res: dict) -> str:
    def pct(v):
        return "   n/a" if v is None else f"{v * 100:+6.2f}%"
    lines = [f"Backtest {pd.Timestamp(res['start_time'], unit='ms').date()} -> "
             f"{pd.Timestamp(res['end_time'], unit='ms').date()} ({res['interval']}, {', '.join(res['symbols'])})",
             f"{'strategy':<16}{'return':>9}{'vs B&H':>9}{'max DD':>9}{'sharpe':>8}{'trades':>8}{'win%':>7}{'PF':>6}"]
    for n, r in res["results"].items():
        sh = f"{r['sharpe']:.2f}" if r.get("sharpe") is not None else "n/a"
        wr = f"{r['win_rate'] * 100:.0f}" if r.get("win_rate") is not None else "-"
        pf = f"{r['profit_factor']:.2f}" if r.get("profit_factor") is not None else "-"
        lines.append(f"{n:<16}{pct(r['total_return']):>9}{pct(r['vs_buy_hold']):>9}{pct(r['max_drawdown']):>9}"
                     f"{sh:>8}{r['trades']:>8}{wr:>7}{pf:>6}")
    return "\n".join(lines)
