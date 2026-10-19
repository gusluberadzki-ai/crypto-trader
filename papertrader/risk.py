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
        self.step = INTERVAL_MS[cfg["interval"]]

    # --------------------------------------------------------- bookkeeping
    def roll(self, pf: Portfolio, t: int, equity: float) -> None:
        """Update peak equity, daily baseline and drawdown halt."""
        day = utc_day(t)
        if pf.day != day:
            pf.day = day
            pf.day_start_equity = equity
        pf.peak_equity = max(pf.peak_equity, equity)
        if not pf.halted and equity < pf.peak_equity * (1 - self.r["max_drawdown_halt"]):
            pf.halted = True
            pf.halt_reason = (f"drawdown {(1 - equity / pf.peak_equity) * 100:.1f}% exceeded "
                              f"{self.r['max_drawdown_halt'] * 100:.0f}% limit")

    # --------------------------------------------------------- entry rules
    def can_open(self, pf: Portfolio, symbol: str, t: int, prices: dict, confidence: float) -> tuple[bool, str]:
        if pf.halted:
            return False, f"trading halted ({pf.halt_reason})"
        eq = pf.equity(prices)
        if pf.day_start_equity and eq < pf.day_start_equity * (1 - self.r["daily_loss_limit"]):
            return False, f"daily loss limit hit ({(eq / pf.day_start_equity - 1) * 100:.1f}% today)"
        if symbol in pf.positions:
            return False, "already holding"
        if len(pf.positions) >= self.r["max_open_positions"]:
            return False, "max open positions reached"
        if pf.cooldowns.get(symbol, 0) > t:
            return False, "cooling down after a stop-out"
        if confidence < self.r["min_confidence"]:
            return False, f"confidence {confidence:.2f} below minimum {self.r['min_confidence']:.2f}"
        if pf.exposure(prices) >= self.r["max_total_exposure"] - 0.01:
            return False, "portfolio exposure limit reached"
        return True, ""

    def size(self, pf: Portfolio, price: float, atr: float, confidence: float, prices: dict) -> dict | None:
        """Risk-based size: lose ~risk_per_trade of equity if the ATR stop is hit."""
        if not atr or atr != atr or atr <= 0:
            return None
        eq = pf.equity(prices)
        stop_dist = self.r["atr_stop_mult"] * atr
        risk_amt = eq * self.r["risk_per_trade"] * (0.6 + 0.4 * confidence)
        notional = risk_amt / (stop_dist / price)
        caps = {
            "risk": notional,
            "max position": eq * self.r["max_position_pct"],
            "exposure room": eq * self.r["max_total_exposure"] - pf.market_value(prices),
            "cash": pf.cash * 0.995,
        }
        binding = min(caps, key=caps.get)
        notional = max(0.0, caps[binding])
        if notional < self.r["min_notional"]:
            return None
        stop = price - stop_dist
        rr = self.r.get("take_profit_rr")
        tp = price + rr * stop_dist if rr else None
        return {"notional": notional, "stop": stop, "take_profit": tp, "risk_amount": notional * stop_dist / price,
                "equity": eq, "limited_by": binding}

    # ---------------------------------------------------------- exit rules
    def update_trailing(self, pos: Position, price: float) -> bool:
        changed = False
        if pos.atr <= 0:  # benchmark positions have no stops
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
        pf.cooldowns[symbol] = t + self.r["cooldown_bars"] * self.step
