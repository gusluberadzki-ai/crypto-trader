"""AI agent bot: a large language model that manages its own $10k paper portfolio.

Once per day (after the daily close) it receives a compact briefing - prices and indicators, the options market's
volatility forecast, leverage crowding and sentiment, what the researched bots are doing, the research findings,
its own portfolio and goals, and its MEMORY (its notes to itself, its recent decisions and reasoning, its closed
trades with post-mortems, and how accurate its past forecasts were). It replies with target allocations, plain-English
reasoning, 7-day outlooks and updates to its own notes. Those notes are fed back next day, so it learns over time.

Model access: any OpenAI-compatible chat endpoint given by LLM_BASE_URL / LLM_MODEL / LLM_API_KEY. On GitHub Actions the
workflow runs a free open-source model (Qwen 2.5, 14B parameters) locally on the runner with Ollama once a day - no API
key, no account, no cost. (GitHub's own hosted models were retired on 30 July 2026.)
Everything stays paper: the model only outputs numbers; the engine validates them and simulates the fills.
It cannot be fairly backtested (a language model may remember historical prices), so its live paper record is
the real test - which is why the research bots keep running next to it as benchmarks.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone

import httpx
import pandas as pd

from .core import fmt_price
from .postmortem import CATEGORIES, classify
from .signals import dvol_for

log = logging.getLogger("papertrader.agent")
NAME = "ai_agent"
MAX_W = 0.33
DAY = 86_400_000

SYSTEM = """You are the portfolio manager of an AI crypto trading bot that PAPER-trades BTC, ETH and SOL against USDT
with a virtual $10,000. You decide once a day. You are long-only (no shorting, no leverage).

Your goals: (1) beat simply buying and holding the three coins equally, after fees; (2) never lose more than 30% from
your peak. Fees + slippage cost about 0.2-0.3% per round trip, so do not change allocations without a clear reason.

What decades of evidence and this bot's own research (2019-2026 data) show - weigh it seriously:
- Nobody, including you, reliably predicts 1-7 day price direction. Be humble: probabilities near 50% are honest.
- Slow trend-following worked: holding coins that are above their 100-day average and up over 30 days, and holding
  altcoins only while BTC is above its 200-day average, beat buy & hold mainly by avoiding crashes.
- Fast trading lost money to fees and whipsaws. Dip-buying in downtrends lost money.
- High implied volatility means bigger expected swings: hold less when it is extreme.
- Trading on sentiment (fear & greed) or funding-rate crowding alone did NOT help in tests; use them only as context.

The "facts" section states today's verified numbers in plain words. Base every claim on it - never on what you said
on earlier days, because the market changes. Write today's reasoning fresh. Your claims are checked automatically
against the facts, and wrong claims make you redo the decision.

Use your memory: your past decisions, what happened after them, and your notes are included. Learn from mistakes,
keep notes short and specific, remove notes that turned out wrong (notes that are no longer true today, too). Think
independently - the rule-based bots' positions are information, not instructions.

Reply with ONLY a JSON object, no markdown:
{"allocations": {"BTCUSDT": 0.0-0.33, "ETHUSDT": 0.0-0.33, "SOLUSDT": 0.0-0.33},
 "reasoning": "max 90 words, plain English a teenager understands",
 "per_coin": {"BTCUSDT": "one short sentence", "ETHUSDT": "...", "SOLUSDT": "..."},
 "outlook_7d": {"BTCUSDT": 0.0-1.0, "ETHUSDT": 0.0-1.0, "SOLUSDT": 0.0-1.0},
 "memory_add": ["at most 2 new short lessons"], "memory_remove": [indices of notes to delete],
 "confidence": 0.0-1.0}
allocations are fractions of total equity per coin (0 = none, 0.33 = full share); outlook_7d is your probability that
each coin is higher in 7 days."""


def _r(x, n=4):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(float(x), n)


COIN_WORDS = {"BTCUSDT": r"\b(btc|bitcoin)\b", "ETHUSDT": r"\b(eth|ether|ethereum)\b", "SOLUSDT": r"\b(sol|solana)\b"}
UP_WORDS = r"(positive|up|rising|gain|gaining|gained|strong|higher|uptrend|increase|increased)"
DOWN_WORDS = r"(negative|down|falling|fell|loss|lost|weak|weaker|lower|downtrend|decrease|decreased|dropped|drop)"


def coin_facts(row: dict) -> str:
    """One plain-English line of verified facts for a coin (from a briefing market row)."""
    name = row["coin"][:-4]
    parts = [f"{name}: price {row['price']:,.2f}"]
    for n in (50, 100, 200):
        v = row.get(f"vs_sma{n}")
        if v is not None:
            parts.append(f"{abs(v) * 100:.1f}% {'ABOVE' if v >= 0 else 'BELOW'} its {n}-day average")
    for n in (7, 30):
        v = row.get(f"ret_{n}d")
        if v is not None:
            parts.append(f"{n}-day change {v * 100:+.1f}% ({'UP' if v > 0 else 'DOWN'})")
    if row.get("implied_vol") is not None:
        parts.append(f"implied volatility {row['implied_vol']:.0f}%")
    parts.append(f"slow trend rule says: {row['trend_rule_says'].upper()} "
                 "(it holds only when above the 100-day average AND the 30-day change is positive)")
    return "; ".join(parts) + "."


def fact_check(d: dict, market: list[dict]) -> list[str]:
    """Find claims in the decision text that contradict the verified numbers. Returns a list of plain-English errors."""
    rows = {r["coin"]: r for r in market}
    texts = [(None, d.get("reasoning") or "")] + [(s, t or "") for s, t in (d.get("per_coin") or {}).items()]
    errors = []
    for coin_key, text in texts:
        for clause in re.split(r"[.;!?]|\bbut\b|\bwhile\b|\bwhereas\b|\bhowever\b", text.lower()):
            coins = [c for c, pat in COIN_WORDS.items() if re.search(pat, clause)]
            if not coins and coin_key in rows:
                coins = [coin_key]
            for m in re.finditer(r"\b(above|below)\b[^,]{0,25}?\b(50|100|200)[- ]?day", clause):
                for c in coins:
                    v = (rows.get(c) or {}).get(f"vs_sma{m.group(2)}")
                    if v is not None and (v >= 0) != (m.group(1) == "above"):
                        errors.append(f"You said {c[:-4]} is {m.group(1)} its {m.group(2)}-day average, but it is "
                                      f"{abs(v) * 100:.1f}% {'above' if v >= 0 else 'below'}.")
            if re.search(r"30[- ]day|30 days|month", clause):
                up, down = re.search(UP_WORDS, clause), re.search(DOWN_WORDS, clause)
                if bool(up) != bool(down):
                    for c in coins:
                        v = (rows.get(c) or {}).get("ret_30d")
                        if v is not None and abs(v) > 0.005 and (v > 0) != bool(up):
                            errors.append(f"You said {c[:-4]} is {'up' if up else 'down'} over 30 days, but its 30-day "
                                          f"change is {v * 100:+.1f}%.")
    return list(dict.fromkeys(errors))


class AIAgent:
    def __init__(self, cfg: dict, db):
        self.cfg, self.db = cfg, db
        a = cfg.get("agent") or {}
        self.models = a.get("models") or ["qwen2.5:14b"]
        self.enabled = a.get("enabled", True)
        self.last_check: dict = {}

    # ------------------------------------------------------------------ memory
    def notes(self) -> list[str]:
        return self.db.kv_get("agent_notes", []) or []

    def history(self, n: int = 7) -> list[dict]:
        return (self.db.kv_get("agent_history", []) or [])[-n:]

    # ------------------------------------------------------------------ briefing
    def briefing(self, eng, close: pd.DataFrame) -> dict:
        day = close.index[-1]
        pf = eng.portfolios[NAME]
        prices = eng.prices
        eq = pf.equity(prices)
        sums = {s["name"]: s for s in eng.portfolio_summaries()}
        me, bh = sums.get(NAME, {}), sums.get("buy_hold", {})
        sig = eng.signals.values_on(int(day.value // 1_000_000))
        from .meta import rule_positions
        rule = rule_positions(close).iloc[-1]
        market = []
        for c in close.columns:
            s = close[c].dropna()
            if len(s) < 30:
                continue
            px = float(s.iloc[-1])
            lr = s.pct_change().tail(30)
            row = {"coin": c, "price": round(px, 2)}
            for n in (1, 7, 30, 90):
                row[f"ret_{n}d"] = _r(px / s.iloc[-1 - n] - 1) if len(s) > n else None
            for n in (50, 100, 200):
                row[f"vs_sma{n}"] = _r(px / s.tail(n).mean() - 1) if len(s) >= n else None
            row["realized_vol_30d"] = _r(lr.std() * math.sqrt(365), 3)
            row["implied_vol"] = _r(dvol_for(c, sig), 1)
            size, p = eng.meta.size_for(c, int(day.value // 1_000_000))
            row["learned_win_prob_if_trend_trade"] = _r(p, 2)
            row["trend_rule_says"] = "hold" if rule.get(c, 0) > 0 else "stay out"
            market.append(row)
        positions = []
        for s, pos in pf.positions.items():
            px = prices.get(s, pos.entry_price)
            positions.append({"coin": s, "weight": _r(pos.qty * px / eq, 3), "entry": round(pos.entry_price, 2),
                              "pnl_pct": _r(pos.qty * px / pos.cost - 1), "safety_stop": round(pos.stop, 2)})
        others = {}
        for n in ("adaptive_trend", "steady_trend", "breakout"):
            p2 = eng.portfolios.get(n)
            if p2:
                others[n] = {"holding": sorted(p2.positions), "return": _r(sums.get(n, {}).get("total_return"))}
        trades = self.db.query("SELECT symbol, pnl_pct, entry_time, exit_time, exit_reason, portfolio FROM trades "
                               "WHERE portfolio=? AND status='closed' ORDER BY exit_time DESC LIMIT 8", (NAME,))
        sc = eng.memory.scorecard()
        mine = [r for r in self.db.query("SELECT p_up, went_up FROM forecasts WHERE resolved=1 AND source='AI agent'")]
        recent = []
        for h in self.history(5):                       # what it decided, and what the market did afterwards
            t0 = pd.Timestamp(h["day"], unit="ms", tz="UTC") if h.get("day") else None
            since = {}
            if t0 is not None and t0 in close.index:
                for c in close.columns:
                    a, b = close.loc[t0, c], close[c].iloc[-1]
                    if pd.notna(a) and pd.notna(b):
                        since[c] = _r(b / a - 1)
            recent.append({"date": h["date"], "allocations": h["allocations"], "coin_moves_since": since})
        return {
            "date": day.strftime("%Y-%m-%d"),
            "facts": [coin_facts(r) for r in market],
            "your_portfolio": {"equity": round(eq, 2), "cash": round(pf.cash, 2), "return_since_start": _r(me.get("total_return")),
                               "max_drawdown": _r(me.get("max_drawdown")), "buy_hold_return_same_period": _r(bh.get("total_return")),
                               "positions": positions, "paused": pf.halted},
            "market": market,
            "market_mood": {"fear_greed_0_100": sig.get("fear_greed"), "funding_btc_annual": _r(sig.get("funding_BTC")),
                            "funding_eth_annual": _r(sig.get("funding_ETH"))},
            "research_bots": others,
            "your_memory": {
                "notes": [f"{i}: {n}" for i, n in enumerate(self.notes())],
                "recent_decisions": recent,
                "closed_trades": [{"coin": t["symbol"], "pnl_pct": _r(t["pnl_pct"]), "days": round((t["exit_time"] - t["entry_time"]) / DAY, 1),
                                   "why": CATEGORIES[classify(t)]} for t in trades],
                "your_forecast_accuracy": ({"checked": len(mine), "brier": _r(sum((m["p_up"] - m["went_up"]) ** 2 for m in mine) / len(mine), 3),
                                            "coin_flip_brier": 0.25} if mine else "no forecasts checked yet"),
                "range_forecasts_hit_rate": _r(sc.get("range_hit_rate"), 2),
            },
        }

    # ------------------------------------------------------------------ model call
    async def ask(self, brief: dict) -> tuple[dict | None, str, str]:
        base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        if not base:
            return None, "", "no language model available this run (LLM_BASE_URL not set)"
        key = os.environ.get("LLM_API_KEY", "")
        models = [m for m in os.environ.get("LLM_MODEL", "").split(",") if m] or self.models
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "Today's briefing (JSON):\n" + json.dumps(brief, separators=(",", ":"))}]
        errors = []
        async with httpx.AsyncClient(timeout=900, follow_redirects=True) as c:
            for model in models:
                for attempt in range(2):
                    try:
                        body = {"model": model, "messages": msgs, "temperature": 0.2, "max_tokens": 900}
                        if attempt == 0:
                            body["response_format"] = {"type": "json_object"}
                        r = await c.post(f"{base}/chat/completions", json=body, headers=headers)
                        if r.status_code == 429:
                            errors.append(f"{model}: rate limited")
                            break
                        try:
                            payload = r.json()
                        except ValueError:
                            errors.append(f"{model}: HTTP {r.status_code}, non-JSON body {r.text[:120]!r}")
                            continue
                        if r.status_code >= 400 or "choices" not in payload:
                            errors.append(f"{model}: HTTP {r.status_code} {json.dumps(payload)[:200]}")
                            continue
                        text = payload["choices"][0]["message"]["content"] or ""
                        m = re.search(r"\{.*\}", text, re.S)
                        if not m:
                            errors.append(f"{model}: no JSON in reply {text[:80]!r}")
                            continue
                        return json.loads(m.group(0)), model, ""
                    except Exception as exc:
                        errors.append(f"{model}: {type(exc).__name__}: {exc}"[:200])
        return None, "", " | ".join(errors)[:900]

    REVIEW = ("Review your draft decision against the briefing before it is executed. Check: (1) every allocation matches "
              "what your own reasoning says (if you say 'hold less' or 'caution', the number must be clearly below 0.33; "
              "if you say 'avoid', it must be 0); (2) you did not ignore negative 30-day momentum or a 'stay out' trend rule "
              "without a specific reason; (3) you are not trading just for the sake of it (fees). Fix any contradiction. "
              "Reply with ONLY the corrected JSON in exactly the same format.")

    async def think(self, brief: dict) -> tuple[dict | None, str, str, bool]:
        """Draft a decision, then have the model critique and correct its own draft (catches self-contradictions)."""
        draft, model, err = await self.ask(brief)
        if draft is None:
            return None, model, err, False
        review_brief = {**brief, "your_draft_decision": draft, "instruction": self.REVIEW}
        errs = fact_check(draft, brief.get("market", []))
        if errs:
            review_brief["fact_checker_found_errors"] = errs
        final, model2, err2 = await self.ask(review_brief)
        if final is None or not isinstance(final.get("allocations"), dict):
            final, model2 = draft, model
        found = list(errs)
        left = fact_check(final, brief.get("market", []))
        if left:                                      # still wrong: one more, stricter correction round
            found += [e for e in left if e not in found]
            fix_brief = {**brief, "your_draft_decision": final, "fact_checker_found_errors": left,
                         "instruction": self.FIX}
            fixed, model3, _ = await self.ask(fix_brief)
            if fixed is not None and isinstance(fixed.get("allocations"), dict):
                final, model2 = fixed, model3 or model2
                left = fact_check(final, brief.get("market", []))
        self.last_check = {"errors_found": found, "errors_left": left}
        changed = json.dumps(final.get("allocations"), sort_keys=True) != json.dumps(draft.get("allocations"), sort_keys=True)
        return final, model2 or model, "", changed

    FIX = ("An automatic fact-checker compared your decision with the verified numbers in 'facts' and found the errors "
           "listed in 'fact_checker_found_errors'. Rewrite your reasoning and per_coin text using only the facts, and "
           "re-decide any allocation that relied on a wrong fact (e.g. holding a coin because you wrongly thought its "
           "trend was up). Reply with ONLY the corrected JSON in exactly the same format.")

    @staticmethod
    def validate(d: dict, symbols: list[str]) -> dict:
        alloc = {}
        for s in symbols:
            try:
                alloc[s] = min(MAX_W, max(0.0, float((d.get("allocations") or {}).get(s, 0.0))))
            except (TypeError, ValueError):
                alloc[s] = 0.0
        out = {}
        for s in symbols:
            try:
                out[s] = min(0.99, max(0.01, float((d.get("outlook_7d") or {}).get(s))))
            except (TypeError, ValueError):
                pass
        return {
            "allocations": alloc, "outlook_7d": out,
            "reasoning": str(d.get("reasoning", ""))[:700],
            "per_coin": {s: str((d.get("per_coin") or {}).get(s, ""))[:240] for s in symbols},
            "memory_add": [str(x)[:200] for x in (d.get("memory_add") or [])][:2],
            "memory_remove": [int(x) for x in (d.get("memory_remove") or []) if str(x).lstrip("-").isdigit()],
            "confidence": max(0.0, min(1.0, float(d.get("confidence", 0.5) or 0.5))),
        }

    # ------------------------------------------------------------------ daily cycle
    async def step(self, eng) -> str | None:
        if not self.enabled or NAME not in eng.portfolios:
            return None
        close = eng.daily_close()
        if close.empty:
            return None
        day_ms = int(close.index[-1].value // 1_000_000)
        if self.db.kv_get("agent_last_day") == day_ms:
            return None
        brief = self.briefing(eng, close)
        raw, model, err, revised = await self.think(brief)
        if os.environ.get("LLM_BASE_URL"):
            tries = self.db.kv_get("agent_tries", {}) or {}
            tries = {k: v for k, v in tries.items() if int(k) >= day_ms - 7 * DAY}
            tries[str(day_ms)] = tries.get(str(day_ms), 0) + 1
            self.db.kv_set("agent_tries", tries)
        if raw is None:
            self.db.kv_set("agent_status", {"ok": False, "error": err, "time": int(time.time() * 1000)})
            eng.event("warn", "ai-agent", f"AI agent could not think today ({err}); it keeps its positions")
            return None
        d = self.validate(raw, list(close.columns))
        self._update_memory(d)
        self._execute(eng, d, model, day_ms)
        self._save_forecasts(close, d, day_ms)
        hist = (self.db.kv_get("agent_history", []) or []) + [{
            "date": brief["date"], "day": day_ms, "model": model, "allocations": d["allocations"],
            "reasoning": d["reasoning"], "per_coin": d["per_coin"], "confidence": d["confidence"], "outlook_7d": d["outlook_7d"],
            "revised_after_self_check": revised,
            "fact_errors_found": self.last_check.get("errors_found", []),
            "fact_errors_left": self.last_check.get("errors_left", [])}]
        self.db.kv_set("agent_history", hist[-60:])
        self.db.kv_set("agent_last_day", day_ms)
        self.db.kv_set("agent_status", {"ok": True, "model": model, "time": int(time.time() * 1000)})
        eng.event("info", "ai-agent", f"AI agent ({model}) decided: " + ", ".join(f"{s[:-4]} {w * 100:.0f}%" for s, w in d["allocations"].items())
                  + f". {d['reasoning'][:200]}")
        return d["reasoning"]

    def _update_memory(self, d: dict) -> None:
        def bare(n: str) -> str:                      # note text without its "21 Oct: " date, for comparing
            return re.sub(r"^\d{1,2} \w{3}: ", "", n).strip().rstrip(".").lower()

        drop = set(d["memory_remove"])
        notes, seen = [], set()
        for i, n in enumerate(self.notes()):
            if i in drop or bare(n) in seen:          # deleted by the agent, or a repeat of an older note
                continue
            notes.append(n)
            seen.add(bare(n))
        for n in d["memory_add"]:
            n = re.sub(r"^\d{1,2} \w{3}: ", "", (n or "").strip())   # the model sometimes adds its own date
            if n and bare(n) not in seen:
                notes.append(f"{datetime.now(timezone.utc):%d %b}: {n}")
                seen.add(bare(n))
        self.db.kv_set("agent_notes", notes[-15:])

    def _save_forecasts(self, close: pd.DataFrame, d: dict, day_ms: int) -> None:
        rows = []
        for s, p in d["outlook_7d"].items():
            px = float(close[s].iloc[-1])
            rows.append((day_ms, s, 7, px, px, px, p, None, None, "AI agent"))
        if rows:
            self.db.executemany("INSERT INTO forecasts(made_day, symbol, horizon, price, low, high, p_up, trend_state, "
                                "sample, source) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)

    def _execute(self, eng, d: dict, model: str, day_ms: int) -> None:
        from .core import Decision
        pf = eng.portfolios[NAME]
        prices = eng.prices
        t = day_ms
        for s, w in d["allocations"].items():
            px = prices.get(s)
            if not px:
                continue
            eq = pf.equity(prices)
            pos = pf.positions.get(s)
            cur_w = pos.qty * px / eq if pos else 0.0
            why = d["per_coin"].get(s) or d["reasoning"]
            head = f"AI agent ({model.split('/')[-1]}) on {s} at {fmt_price(px)}"
            if pos and (w < 0.02 or abs(w - cur_w) > 0.10):
                ct = eng.core._close(pf, s, px, t, "signal")
                text = (f"SELL {head}: target {w * 100:.0f}% (was {cur_w * 100:.0f}%). {why} "
                        f"Result {'+' if ct.pnl >= 0 else '-'}${abs(ct.pnl):,.2f} ({ct.pnl_pct * 100:+.2f}%) after fees.")
                eng.persist_close(ct, text)
                eng.persist_decision(Decision(NAME, s, t, "bear" if w < 0.02 else "bull", "SELL", d["confidence"], px, text, {"target": w}))
                pos = None
            if not pos and w >= 0.02 and not pf.halted:
                atr = eng.frames[s]["close"].tail(24 * 14).pct_change().std() * math.sqrt(24) * px if s in eng.frames else px * 0.03
                stop = px - 3 * atr
                text = (f"BUY {head}: target {w * 100:.0f}% of equity (confidence {d['confidence'] * 100:.0f}%). {why} "
                        f"Overall view: {d['reasoning']} Safety stop {fmt_price(stop)}.")
                newp = eng.broker.buy(pf, s, eq * w, px, t, atr, stop, None, text, trail=False, slip_atr=atr / 4.9)
                if newp:
                    eng.persist_open(pf, newp)
                    eng.persist_decision(Decision(NAME, s, t, "bull", "BUY", d["confidence"], px, text, {"target": w}))
            elif pos:
                text = f"HOLD {head}: keeping {cur_w * 100:.0f}% (target {w * 100:.0f}%). {why}"
                eng.persist_decision(Decision(NAME, s, t, "bull", "HOLD", d["confidence"], px, text, {"target": w}))
            else:
                text = f"HOLD {head}: staying out. {why}"
                eng.persist_decision(Decision(NAME, s, t, "bear", "HOLD", d["confidence"], px, text, {"target": w}))
        eng.save_portfolio(pf)

    def summary(self) -> dict:
        h = self.history(30)
        return {"enabled": self.enabled, "status": self.db.kv_get("agent_status"), "notes": self.notes(),
                "last": h[-1] if h else None, "history": [{"date": x["date"], "allocations": x["allocations"],
                                                         "confidence": x["confidence"]} for x in h[-14:]]}
