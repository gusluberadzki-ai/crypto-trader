"""Simulated (paper) broker: virtual cash, long-only spot positions, fees and slippage.

Nothing here talks to an exchange. Fills are computed locally from market prices.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Position:
    symbol: str
    qty: float
    entry_price: float      # fill price incl. slippage
    entry_time: int
    entry_fee: float
    stop: float
    initial_stop: float
    take_profit: float | None
    highest: float
    atr: float
    trade_id: int | None = None
    entry_explanation: str = ""

    @property
    def cost(self) -> float:
        return self.qty * self.entry_price + self.entry_fee

    @property
    def trailing(self) -> bool:
        return self.stop > self.initial_stop + 1e-12

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClosedTrade:
    portfolio: str
    symbol: str
    qty: float
    entry_time: int
    entry_price: float
    entry_fee: float
    exit_time: int
    exit_price: float
    exit_fee: float
    pnl: float
    pnl_pct: float
    exit_reason: str
    trade_id: int | None = None


@dataclass
class Portfolio:
    name: str
    start_balance: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    peak_equity: float = 0.0
    day: str | None = None
    day_start_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    cooldowns: dict[str, int] = field(default_factory=dict)  # symbol -> bar time (ms) re-entry allowed
    closed: list[ClosedTrade] = field(default_factory=list)  # filled in backtests only

    @classmethod
    def new(cls, name: str, balance: float) -> "Portfolio":
        return cls(name=name, start_balance=balance, cash=balance, peak_equity=balance, day_start_equity=balance)

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(p.qty * prices.get(s, p.entry_price) for s, p in self.positions.items())

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def exposure(self, prices: dict[str, float]) -> float:
        eq = self.equity(prices)
        return self.market_value(prices) / eq if eq > 0 else 0.0


class PaperBroker:
    def __init__(self, cfg: dict):
        c = cfg["costs"]
        self.fee_rate = float(c["fee_rate"])
        self.slip_bps = float(c["slippage_bps"])
        self.slip_vol = float(c["slippage_vol_factor"])

    def slippage(self, atr_pct: float | None) -> float:
        extra = self.slip_vol * atr_pct if atr_pct and atr_pct == atr_pct else 0.0
        return self.slip_bps / 1e4 + extra

    def buy(self, pf: Portfolio, symbol: str, notional: float, price: float, t: int, atr: float,
            stop: float, take_profit: float | None, explanation: str = "") -> Position | None:
        if symbol in pf.positions or notional <= 0:
            return None
        notional = min(notional, pf.cash / (1 + self.fee_rate))
        if notional <= 0:
            return None
        fill = price * (1 + self.slippage(atr / price if price else None))
        qty = notional / fill
        fee = notional * self.fee_rate
        pf.cash -= notional + fee
        pf.fees_paid += fee
        pos = Position(symbol, qty, fill, t, fee, stop, stop, take_profit, fill, atr,
                       entry_explanation=explanation)
        pf.positions[symbol] = pos
        return pos

    def sell(self, pf: Portfolio, symbol: str, price: float, t: int, reason: str,
             apply_slippage: bool = True) -> ClosedTrade | None:
        pos = pf.positions.pop(symbol, None)
        if pos is None:
            return None
        slip = self.slippage(pos.atr / price if price else None) if apply_slippage else 0.0
        fill = price * (1 - slip)
        gross = pos.qty * fill
        fee = gross * self.fee_rate
        pf.cash += gross - fee
        pf.fees_paid += fee
        pnl = gross - fee - pos.cost
        pf.realized_pnl += pnl
        return ClosedTrade(pf.name, symbol, pos.qty, pos.entry_time, pos.entry_price, pos.entry_fee,
                           t, fill, fee, pnl, pnl / pos.cost if pos.cost else 0.0, reason, pos.trade_id)
