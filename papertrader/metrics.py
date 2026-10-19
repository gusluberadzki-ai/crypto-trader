"""Performance statistics."""
from __future__ import annotations

import math

import numpy as np


def equity_stats(equity: list[float] | np.ndarray, bars_per_year: float, start_balance: float | None = None) -> dict:
    eq = np.asarray(equity, dtype=float)
    if len(eq) == 0:
        return {}
    start = start_balance or eq[0]
    rets = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([])
    peak = np.maximum.accumulate(np.concatenate([[start], eq]))
    dd = (np.concatenate([[start], eq]) / peak - 1).min()
    total = eq[-1] / start - 1
    n = len(eq)
    years = n / bars_per_year
    out = {
        "equity": float(eq[-1]),
        "total_return": float(total),
        "max_drawdown": float(dd),
        "bars": int(n),
        "days": float(n / bars_per_year * 365),
    }
    if len(rets) > 2 and rets.std() > 0:
        out["volatility_ann"] = float(rets.std(ddof=1) * math.sqrt(bars_per_year))
        out["sharpe"] = float(rets.mean() / rets.std(ddof=1) * math.sqrt(bars_per_year))
        downside = rets[rets < 0]
        out["sortino"] = float(rets.mean() / downside.std(ddof=1) * math.sqrt(bars_per_year)) if len(downside) > 2 and downside.std() > 0 else None
    else:
        out.update(volatility_ann=None, sharpe=None, sortino=None)
    # annualised figures from a few weeks of data are very noisy; only report them after 30 days
    out["cagr"] = float((1 + total) ** (1 / years) - 1) if years >= 30 / 365 and total > -1 else None
    return out


def trade_stats(pnls: list[float], pnl_pcts: list[float], hold_hours: list[float], fees: float = 0.0) -> dict:
    n = len(pnls)
    if n == 0:
        return {"trades": 0, "win_rate": None, "profit_factor": None, "avg_trade_pct": None,
                "avg_win_pct": None, "avg_loss_pct": None, "avg_hold_hours": None, "fees_paid": fees,
                "significance_note": "no closed trades yet"}
    p = np.asarray(pnls)
    pp = np.asarray(pnl_pcts)
    wins, losses = p[p > 0], p[p <= 0]
    gross_loss = -losses.sum()
    note = None
    if n < 30:
        note = f"only {n} closed trades: too few to judge whether the edge is real"
    else:
        t = pp.mean() / (pp.std(ddof=1) / math.sqrt(n)) if pp.std() > 0 else 0.0
        note = f"t-stat of average trade = {t:.2f} ({'likely real' if t > 2 else 'not statistically significant'})"
    return {
        "trades": n,
        "win_rate": float(len(wins) / n),
        "profit_factor": float(wins.sum() / gross_loss) if gross_loss > 0 else None,
        "avg_trade_pct": float(pp.mean()),
        "avg_win_pct": float(pp[pp > 0].mean()) if (pp > 0).any() else None,
        "avg_loss_pct": float(pp[pp <= 0].mean()) if (pp <= 0).any() else None,
        "best_trade_pct": float(pp.max()),
        "worst_trade_pct": float(pp.min()),
        "avg_hold_hours": float(np.mean(hold_hours)) if hold_hours else None,
        "fees_paid": float(fees),
        "significance_note": note,
    }


def clean(obj):
    """Make numbers JSON-safe (NaN/inf -> None, numpy -> python)."""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj
