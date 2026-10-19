"""Decision core shared by live trading and backtests, so both run exactly the same rules."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from .broker import ClosedTrade, PaperBroker, Portfolio, Position
from .risk import RiskManager
from .strategies import LABELS, Signal

REASON_TEXT = {
    "stop_loss": "stop-loss hit", "trailing_stop": "trailing stop hit", "take_profit": "take-profit reached",
    "signal": "strategy exit signal",
}


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:,.3f}"
    return f"${p:.6f}"


@dataclass
class Decision:
    portfolio: str
    symbol: str
    bar_time: int
    stance: str
    action: str           # BUY / SELL / HOLD / BLOCKED
    confidence: float
    price: float
    explanation: str
    details: dict = field(default_factory=dict)
    opened: Position | None = None
    closed: ClosedTrade | None = None


class TradingCore:
    def __init__(self, cfg: dict, broker: PaperBroker, risk: RiskManager):
        self.cfg = cfg
        self.broker = broker
        self.risk = risk

    # ------------------------------------------------------------ exits on price
    def check_tick(self, pf: Portfolio, symbol: str, price: float, t: int) -> tuple[ClosedTrade | None, bool]:
        """Returns (closed trade or None, whether stop/highest changed)."""
        pos = pf.positions.get(symbol)
        if pos is None:
            return None, False
        reason = self.risk.exit_on_price(pos, price)
        if reason:
            return self._close(pf, symbol, price, t, reason), True
        return None, self.risk.update_trailing(pos, price)

    def check_bar(self, pf: Portfolio, symbol: str, bar: pd.Series | dict, t: int) -> ClosedTrade | None:
        pos = pf.positions.get(symbol)
        if pos is None:
            return None
        hit = self.risk.exit_on_bar(pos, bar["open"], bar["high"], bar["low"])
        if hit:
            reason, px = hit
            return self._close(pf, symbol, px, t, reason)
        self.risk.update_trailing(pos, bar["high"])
        return None

    def _close(self, pf: Portfolio, symbol: str, price: float, t: int, reason: str) -> ClosedTrade:
        ct = self.broker.sell(pf, symbol, price, t, reason)
        if reason in ("stop_loss", "trailing_stop") and ct.pnl < 0:
            self.risk.after_stop(pf, symbol, t)
        pf.closed.append(ct)
        return ct

    # ------------------------------------------------------------ bar decisions
    def on_bar_close(self, pf: Portfolio, symbol: str, row: pd.Series, sig: Signal, t: int,
                     price: float, prices: dict[str, float]) -> Decision:
        equity = pf.equity(prices)
        self.risk.roll(pf, t, equity)
        pos = pf.positions.get(symbol)
        head = f"{LABELS.get(sig.strategy, sig.strategy)} on {symbol} at {fmt_price(price)}"
        why = "; ".join(sig.reasons)

        if pos is not None:
            if sig.stance == "bear":
                ct = self._close(pf, symbol, price, t, "signal")
                text = (f"SELL {head}. The strategy turned bearish: {why}. "
                        f"Closed {ct.qty:.6g} {symbol[:-4]} for {'+' if ct.pnl >= 0 else '-'}${abs(ct.pnl):,.2f} "
                        f"({ct.pnl_pct * 100:+.2f}%) after fees.")
                return Decision(pf.name, symbol, t, sig.stance, "SELL", sig.confidence, price, text,
                                self._details(sig, row), closed=ct)
            upl = pos.qty * price - pos.cost
            text = (f"HOLD {head}. Staying in the position ({'+' if upl >= 0 else '-'}${abs(upl):,.2f} unrealised); "
                    f"stop at {fmt_price(pos.stop)}{' (trailing)' if pos.trailing else ''}. {why}.")
            return Decision(pf.name, symbol, t, sig.stance, "HOLD", sig.confidence, price, text, self._details(sig, row))

        if sig.stance != "bull":
            text = f"HOLD {head}. No entry: {why}." if why else f"HOLD {head}."
            return Decision(pf.name, symbol, t, sig.stance, "HOLD", sig.confidence, price, text, self._details(sig, row))

        ok, block = self.risk.can_open(pf, symbol, t, prices, sig.confidence)
        atr = float(row.get("atr", float("nan")))
        plan = self.risk.size(pf, price, atr, sig.confidence, prices) if ok else None
        if not ok or plan is None:
            reason = block or "position would be below the minimum size"
            text = f"BLOCKED {head}. The signal was bullish ({why}) but risk rules prevented a trade: {reason}."
            return Decision(pf.name, symbol, t, sig.stance, "BLOCKED", sig.confidence, price, text,
                            self._details(sig, row))

        risk_text = (f"Risk plan: ${plan['notional']:,.0f} position ({plan['notional'] / plan['equity'] * 100:.0f}% of "
                     f"equity, limited by {plan['limited_by']}), stop {fmt_price(plan['stop'])} "
                     f"({self.risk.r['atr_stop_mult']:.1f}x ATR), risking ${plan['risk_amount']:,.0f}")
        if plan["take_profit"]:
            risk_text += f", take-profit {fmt_price(plan['take_profit'])}"
        text = f"BUY {head} (confidence {sig.confidence * 100:.0f}%). Why: {why}. {risk_text}."
        pos = self.broker.buy(pf, symbol, plan["notional"], price, t, atr, plan["stop"], plan["take_profit"], text)
        if pos is None:
            return Decision(pf.name, symbol, t, sig.stance, "BLOCKED", sig.confidence, price,
                            f"BLOCKED {head}: not enough cash.", self._details(sig, row))
        return Decision(pf.name, symbol, t, sig.stance, "BUY", sig.confidence, price, text,
                        {**self._details(sig, row), "plan": plan}, opened=pos)

    @staticmethod
    def _details(sig: Signal, row: pd.Series) -> dict:
        snap = {}
        for k in ("rsi", "adx", "atr_pct", "ema_20_50", "macd_hist", "bb_pctb", "vol_pctile", "volume_z"):
            v = row.get(k)
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                snap[k] = round(float(v), 5)
        return {"score": round(sig.score, 4), "indicators": snap, **sig.details}

    # ------------------------------------------------------------ benchmark
    def buy_and_hold(self, pf: Portfolio, prices: dict[str, float], t: int) -> None:
        """Split the balance equally across all coins once, never sell."""
        if pf.positions:
            return
        n = len(prices)
        budget = pf.cash / n
        for s, p in prices.items():
            notional = budget / (1 + self.broker.fee_rate) * 0.999
            pos = self.broker.buy(pf, s, notional, p, t, 0.0, 0.0, None, "Benchmark: buy and hold")
