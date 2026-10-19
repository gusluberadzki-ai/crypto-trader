"""Risk management: position sizing, stops, trailing stops, take-profit and circuit breakers."""
from __future__ import annotations

from datetime import datetime, timezone

from .broker import Portfolio, Position
from .config import INTERVAL_MS


def utc_day(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, cfg: dict):
        self.r = cfg["risk"]
        self.overrides = cfg.get("strategy_risk") or {}
        self.step = INTERVAL_MS[cfg["interval"]]

    def p(self, name: str) -> dict:
        """Risk settings for one portfolio (global settings + that strategy's overrides)."""
        o = self.overrides.get(name)
        return {**self.r, **o} if o else self.r

    # --------------------------------------------------------- bookkeeping
    def roll(self, pf: Portfolio, t: int, equity: float) -> None:
        """Update peak equity, daily baseline and drawdown halt."""
        r = self.p(pf.name)
        day = utc_day(t)
        if pf.day != day:
            pf.day = day
            pf.day_start_equity = equity
        pf.peak_equity = max(pf.peak_equity, equity)
        if not pf.halted and equity < pf.peak_equity * (1 - r["max_drawdown_halt"]):
            pf.halted = True
            pf.halt_reason = (f"drawdown {(1 - equity / pf.peak_equity) * 100:.1f}% exceeded "
                              f"{r['max_drawdown_halt'] * 100:.0f}% limit")

    # --------------------------------------------------------- entry rules
    def can_open(self, pf: Portfolio, symbol: str, t: int, prices: dict, confidence: float) -> tuple[bool, str]:
        r = self.p(pf.name)
        if pf.halted:
            return False, f"trading halted ({pf.halt_reason})"
        eq = pf.equity(prices)
        if pf.day_start_equity and eq < pf.day_start_equity * (1 - r["daily_loss_limit"]):
            return False, f"daily loss limit hit ({(eq / pf.day_start_equity - 1) * 100:.1f}% today)"
        if symbol in pf.positions:
            return False, "already holding"
        if len(pf.positions) >= r["max_open_positions"]:
            return False, "max open positions reached"
        if pf.cooldowns.get(symbol, 0) > t:
            return False, "cooling down after a stop-out"
        if confidence < r["min_confidence"]:
            return False, f"confidence {confidence:.2f} below minimum {r['min_confidence']:.2f}"
        if pf.exposure(prices) >= r["max_total_exposure"] - 0.01:
            return False, "portfolio exposure limit reached"
        return True, ""

    def size(self, pf: Portfolio, price: float, atr: float, confidence: float, prices: dict) -> dict | None:
        """Risk-based size: lose ~risk_per_trade of equity if the ATR stop is hit."""
        r = self.p(pf.name)
        if not atr or atr != atr or atr <= 0:
            return None
        eq = pf.equity(prices)
        stop_dist = r["atr_stop_mult"] * atr
        risk_amt = eq * r["risk_per_trade"] * (0.6 + 0.4 * confidence)
        notional = risk_amt / (stop_dist / price)
        caps = {
            "risk": notional,
            "max position": eq * r["max_position_pct"],
            "exposure room": eq * r["max_total_exposure"] - pf.market_value(prices),
            "cash": pf.cash * 0.995,
        }
        binding = min(caps, key=caps.get)
        notional = max(0.0, caps[binding])
        if notional < r["min_notional"]:
            return None
        stop = price - stop_dist
        rr = self.r.get("take_profit_rr")
        tp = price + rr * stop_dist if rr else None
        return {"notional": notional, "stop": stop, "take_profit": tp, "risk_amount": notional * stop_dist / price,
                "equity": eq, "limited_by": binding}

    def size_target(self, pf: Portfolio, price: float, weight: float, stop: float, prices: dict) -> dict | None:
        """Fixed-allocation size (daily strategies): a target share of equity, with a wide safety stop."""
        r = self.p(pf.name)
        eq = pf.equity(prices)
        caps = {
            "target allocation": eq * weight,
            "max position": eq * r["max_position_pct"],
            "exposure room": eq * r["max_total_exposure"] - pf.market_value(prices),
            "cash": pf.cash * 0.995,
        }
        binding = min(caps, key=caps.get)
        notional = max(0.0, caps[binding])
        if notional < r["min_notional"]:
            return None
        return {"notional": notional, "stop": stop, "take_profit": None,
                "risk_amount": notional * max(0.0, price - stop) / price, "equity": eq, "limited_by": binding}

    # ---------------------------------------------------------- exit rules
    def update_trailing(self, pos: Position, price: float) -> bool:
        changed = False
        if pos.atr <= 0 or not pos.trail:  # benchmark / daily-strategy positions don't trail
            return False
        if price > pos.highest:
            pos.highest = price
            changed = True
        if pos.highest - pos.entry_price >= self.r["trail_activate_atr"] * pos.atr:
            new_stop = pos.highest - self.r["trail_atr_mult"] * pos.atr
            if new_stop > pos.stop:
                pos.stop = new_stop
                changed = True
        return changed

    def exit_on_price(self, pos: Position, price: float) -> str | None:
        """Live tick check."""
        if price <= pos.stop:
            return "trailing_stop" if pos.trailing else "stop_loss"
        if pos.take_profit and price >= pos.take_profit:
            return "take_profit"
        return None

    def exit_on_bar(self, pos: Position, o: float, h: float, l: float) -> tuple[str, float] | None:
        """Bar-based check (backtests / gaps while offline). Assumes the stop is hit first if both are touched."""
        if l <= pos.stop:
            return ("trailing_stop" if pos.trailing else "stop_loss"), (o if o <= pos.stop else pos.stop)
        if pos.take_profit and h >= pos.take_profit:
            return "take_profit", (o if o >= pos.take_profit else pos.take_profit)
        return None

    def after_stop(self, pf: Portfolio, symbol: str, t: int) -> None:
        pf.cooldowns[symbol] = t + self.p(pf.name)["cooldown_bars"] * self.step
