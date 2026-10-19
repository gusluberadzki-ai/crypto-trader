"""Strategies turn one bar of features into a Signal.

stance: 'bull' (want to be long), 'bear' (want to be flat), or 'neutral' (no opinion).
The engine is long-only spot: bull + flat -> BUY, bear + long -> SELL, otherwise HOLD.
score: -1..+1, used by the ensemble.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Signal:
    strategy: str
    stance: str = "neutral"
    confidence: float = 0.0
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    available: bool = True


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def missing(row: pd.Series, cols) -> bool:
    return any(c not in row or row[c] is None or (isinstance(row[c], float) and math.isnan(row[c])) for c in cols)


class TrendStrategy:
    name = "trend"
    label = "Trend following"

    def __init__(self, cfg: dict):
        self.adx_min = cfg["trend"]["adx_min"]

    def evaluate(self, symbol: str, row: pd.Series, ctx: dict) -> Signal:
        need = ["ema20", "ema50", "ema200", "adx", "macd_hist"]
        if missing(row, need):
            return Signal(self.name, reasons=["indicators still warming up"], available=False)
        up = row.ema20 > row.ema50
        above200 = row.close > row.ema200
        macd_up = row.macd_hist > 0
        strong = row.adx >= self.adx_min
        raw = (0.45 if up else -0.45) + (0.25 if above200 else -0.25) + (0.3 if macd_up else -0.3)
        score = clip(raw * clip(row.adx / 25, 0.5, 1.2), -1, 1)
        reasons = [
            f"EMA20 is {'above' if up else 'below'} EMA50 ({row.ema_20_50*100:+.2f}%)",
            f"price is {'above' if above200 else 'below'} the 200-bar EMA",
            f"MACD momentum is {'positive' if macd_up else 'negative'}",
            f"ADX {row.adx:.0f} ({'trending' if strong else 'weak/no trend'})",
        ]
        if up and above200 and macd_up and strong:
            conf = clip(0.55 + (row.adx - self.adx_min) / 60 + min(abs(row.ema_20_50) * 10, 0.1), 0.55, 0.95)
            return Signal(self.name, "bull", conf, score, reasons)
        if not up:
            return Signal(self.name, "bear", clip(0.55 + abs(row.ema_20_50) * 10, 0.55, 0.9), score, reasons)
        return Signal(self.name, "neutral", 0.5, score, reasons)


class MeanReversionStrategy:
    name = "mean_reversion"
    label = "Mean reversion"

    def __init__(self, cfg: dict):
        self.rsi_entry = cfg["mean_reversion"]["rsi_entry"]
        self.rsi_exit = cfg["mean_reversion"]["rsi_exit"]

    def evaluate(self, symbol: str, row: pd.Series, ctx: dict) -> Signal:
        need = ["rsi", "bb_lo", "bb_mid", "bb_pctb", "ema_50_200"]
        if missing(row, need):
            return Signal(self.name, reasons=["indicators still warming up"], available=False)
        score = clip(0.5 * (50 - row.rsi) / 25 + 0.5 * (0.5 - row.bb_pctb), -1, 1)
        reasons = [f"RSI {row.rsi:.0f}", f"price at {row.bb_pctb*100:.0f}% of the Bollinger range"]
        not_crashing = row.ema_50_200 > -0.05
        if row.rsi < self.rsi_entry and row.close < row.bb_lo and not_crashing:
            reasons.append("oversold below the lower band while the longer trend is intact")
            conf = clip(0.55 + (self.rsi_entry - row.rsi) / 40, 0.55, 0.9)
            return Signal(self.name, "bull", conf, score, reasons)
        if row.rsi > self.rsi_exit or row.close >= row.bb_mid:
            reasons.append("price has reverted to the mean")
            return Signal(self.name, "bear", 0.6, score, reasons)
        if not not_crashing:
            reasons.append("EMA50 well below EMA200: avoiding falling knives")
        return Signal(self.name, "neutral", 0.5, score, reasons)


class MLStrategy:
    name = "ml"
    label = "Machine learning"

    def __init__(self, cfg: dict, models):
        self.models = models

    def evaluate(self, symbol: str, row: pd.Series, ctx: dict) -> Signal:
        tm = self.models.active
        if tm is None:
            return Signal(self.name, reasons=["no validated model is active yet"], available=False)
        p = ctx.get("ml_p")
        if p is None:
            p = self.models.predict_row(row)
        if p is None or math.isnan(p):
            return Signal(self.name, reasons=["model could not score this bar"], available=False)
        bt, st = tm.meta["buy_threshold"], tm.meta["sell_threshold"]
        span = max(bt - st, 1e-6)
        score = clip((p - st) / span * 2 - 1, -1, 1) if p >= st else clip((p - st) / span, -1, 0)
        h = tm.meta.get("horizon", 6)
        thr = ctx.get("label_threshold", 0.004)
        reasons = [f"model v{tm.version} gives {p*100:.0f}% probability of a >{thr*100:.1f}% gain over the next {h} bars "
                   f"(buy above {bt*100:.0f}%, exit below {st*100:.0f}%)"]
        details = {"p": p, "buy_threshold": bt, "sell_threshold": st, "model_version": tm.version}
        if ctx.get("explain"):
            drivers = self.models.explain_row(row)
            details["drivers"] = drivers
            if drivers:
                reasons.append("main drivers: " + ", ".join(
                    f"{d['feature']} ({'+' if d['effect'] > 0 else '-'}{abs(d['effect'])*100:.1f} pts)" for d in drivers))
        if p >= bt:
            return Signal(self.name, "bull", clip(0.6 + (p - bt) * 2, 0.6, 0.95), score, reasons, details)
        if p < st:
            return Signal(self.name, "bear", clip(0.55 + (st - p) * 2, 0.55, 0.9), score, reasons, details)
        return Signal(self.name, "neutral", 0.5, score, reasons, details)


class EnsembleStrategy:
    name = "ensemble"
    label = "Ensemble (ML + rules)"

    def __init__(self, cfg: dict):
        e = cfg["ensemble"]
        self.weights = e["weights"]
        self.buy = e["buy_score"]
        self.sell = e["sell_score"]
        self.max_vol = e["max_vol_percentile"]

    def evaluate(self, symbol: str, row: pd.Series, ctx: dict) -> Signal:
        parts: dict[str, Signal] = ctx.get("signals", {})
        num = den = 0.0
        breakdown = {}
        for name, w in self.weights.items():
            s = parts.get(name)
            if s is None or not s.available:
                continue  # unavailable component -> renormalise weights over the rest
            num += w * s.score
            den += w
            breakdown[name] = round(s.score, 3)
        if den == 0:
            return Signal(self.name, reasons=["components not ready"], available=False)
        score = num / den
        reasons = ["weighted score {:+.2f} from ".format(score) +
                   ", ".join(f"{k.replace('_', ' ')} {v:+.2f}" for k, v in breakdown.items())]
        details = {"components": breakdown, "score": score}
        conf = clip(0.5 + abs(score) * 0.6, 0.5, 0.95)
        if score >= self.buy:
            vp = row.get("vol_pctile")
            if vp is not None and not math.isnan(vp) and vp > self.max_vol:
                reasons.append(f"but volatility is in its top {100 - self.max_vol*100:.0f}% regime: standing aside")
                return Signal(self.name, "neutral", conf, score, reasons, details)
            return Signal(self.name, "bull", conf, score, reasons, details)
        if score <= self.sell:
            return Signal(self.name, "bear", conf, score, reasons, details)
        return Signal(self.name, "neutral", conf, score, reasons, details)


class DailyTrendStrategy:
    """Researched winner: hold a coin while it is above its 100-day average AND up over the last 30 days.
    Altcoins only while BTC itself is above its 200-day average. Decides on completed daily candles."""
    name = "daily_trend"
    label = "Daily trend"

    def __init__(self, cfg: dict):
        self.weight = 1.0 / max(1, len(cfg["symbols"]))
        self.stop_atr = cfg.get("daily", {}).get("stop_atr", 3.0)

    def evaluate(self, symbol: str, row: pd.Series, ctx: dict) -> Signal:
        if missing(row, ["d_close", "d_sma100", "d_mom30", "d_atr"]):
            return Signal(self.name, reasons=["needs 100+ days of history"], available=False)
        above = row.d_close > row.d_sma100
        mom = row.d_mom30 > 0
        btc_ok = symbol == "BTCUSDT" or bool(row.get("btc_trend_ok") == 1.0)
        reasons = [f"daily close {'above' if above else 'below'} its 100-day average ({(row.d_close / row.d_sma100 - 1) * 100:+.1f}%)",
                   f"30-day change {row.d_mom30 * 100:+.1f}%"]
        if symbol != "BTCUSDT":
            reasons.append(f"BTC {'is' if btc_ok else 'is not'} above its 200-day average")
        details = {"target_weight": self.weight, "stop": row.close - self.stop_atr * row.d_atr, "daily_atr": row.d_atr}
        score = (0.5 if above else -0.5) + (0.5 if mom else -0.5)
        if above and mom and btc_ok:
            return Signal(self.name, "bull", 0.75, score, reasons, details)
        return Signal(self.name, "bear", 0.75, score, reasons, details)


class BreakoutStrategy:
    """Researched runner-up: buy when price breaks above its 30-day high, sell below its 20-day low.
    Same BTC filter for altcoins. Decides on completed daily candles."""
    name = "breakout"
    label = "Breakout"

    def __init__(self, cfg: dict):
        self.weight = 1.0 / max(1, len(cfg["symbols"]))
        self.stop_atr = cfg.get("daily", {}).get("stop_atr", 3.0)

    def evaluate(self, symbol: str, row: pd.Series, ctx: dict) -> Signal:
        if missing(row, ["d_close", "d_high30", "d_low20", "d_atr"]):
            return Signal(self.name, reasons=["needs 30+ days of history"], available=False)
        btc_ok = symbol == "BTCUSDT" or bool(row.get("btc_trend_ok") == 1.0)
        details = {"target_weight": self.weight, "stop": row.close - self.stop_atr * row.d_atr, "daily_atr": row.d_atr}
        span = f"30-day high {row.d_high30:,.2f}, 20-day low {row.d_low20:,.2f}"
        if not btc_ok:
            return Signal(self.name, "bear", 0.75, -1, [f"BTC is below its 200-day average, so altcoins stay out ({span})"], details)
        if row.d_close > row.d_high30:
            return Signal(self.name, "bull", 0.75, 1, [f"daily close broke above the {span.split(',')[0]}"], details)
        if row.d_close < row.d_low20:
            return Signal(self.name, "bear", 0.75, -1, [f"daily close fell below the {span.split(', ')[1]}"], details)
        return Signal(self.name, "neutral", 0.5, 0, [f"no breakout: price between the {span}"], details)


class AdaptiveTrendStrategy(DailyTrendStrategy):
    """Daily trend + learning from failure: a model trained on the rule's own past trades (which ones lost and in
    what conditions) sizes each new position between 25% and 100% of a full one."""
    name = "adaptive_trend"
    label = "Adaptive trend"

    def __init__(self, cfg: dict, meta=None):
        super().__init__(cfg)
        self.meta = meta

    def evaluate(self, symbol: str, row: pd.Series, ctx: dict) -> Signal:
        sig = super().evaluate(symbol, row, ctx)
        sig.strategy = self.name
        if sig.stance != "bull" or self.meta is None:
            return sig
        size, p = self.meta.size_for(symbol, row.get("d_day", 0))
        sig.details["target_weight"] = self.weight * size
        sig.details["learned_size"] = size
        sig.details["win_prob"] = p
        info = getattr(self.meta, "info", {}) or {}
        if p is None:
            sig.reasons.append("learning not active yet, so full size")
        else:
            base = info.get("win_rate")
            sig.reasons.append(f"learned sizing: {size * 100:.0f}% of a full position — past trades that started like this "
                               f"one won about {p * 100:.0f}% of the time (full size needs 60%+"
                               + (f"; the average past trade won {base * 100:.0f}%)" if base else ")"))
        return sig


BASE_ORDER = ["trend", "mean_reversion", "ml", "daily_trend", "breakout", "adaptive_trend"]
LABELS = {"trend": TrendStrategy.label, "mean_reversion": MeanReversionStrategy.label, "ml": MLStrategy.label,
          "ensemble": EnsembleStrategy.label, "buy_hold": "Buy & hold (benchmark)",
          "daily_trend": DailyTrendStrategy.label, "breakout": BreakoutStrategy.label,
          "adaptive_trend": AdaptiveTrendStrategy.label}


def build_strategies(cfg: dict, models, meta=None) -> dict:
    return {
        "adaptive_trend": AdaptiveTrendStrategy(cfg, meta),
        "trend": TrendStrategy(cfg),
        "mean_reversion": MeanReversionStrategy(cfg),
        "ml": MLStrategy(cfg, models),
        "ensemble": EnsembleStrategy(cfg),
        "daily_trend": DailyTrendStrategy(cfg),
        "breakout": BreakoutStrategy(cfg),
    }


def evaluate_all(strategies: dict, symbol: str, row: pd.Series, ctx: dict) -> dict[str, Signal]:
    signals: dict[str, Signal] = {}
    for name in BASE_ORDER:
        signals[name] = strategies[name].evaluate(symbol, row, ctx)
    signals["ensemble"] = strategies["ensemble"].evaluate(symbol, row, {**ctx, "signals": signals})
    return signals
