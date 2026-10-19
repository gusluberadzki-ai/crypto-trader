# Strategy research (October 2026)

**Question:** which long-only strategy for BTC/ETH/SOL works best after realistic costs (0.1% fee + ~0.1% slippage per side)?

**Data:** Binance hourly candles, Jan 2019 – Oct 2026 (SOL from Aug 2020). Downloaded by `.github/workflows/research-data.yml`.

## Method

1. `research/lab.py` tests 360 configurations across 6 families on daily and 4-hour candles:
   moving-average filter, EMA crossover, Donchian breakout, time-series momentum, trend + momentum combined,
   and daily dip-buying, each with/without volatility targeting and with/without a "BTC above its 200-day average" filter.
2. **Walk-forward** (the honest test): parameters are picked on the previous 2 years only, then judged on the next
   6 months they never saw. Repeated every 6 months from Jan 2021 to Oct 2026.
3. **Robustness:** how many parameter settings of each family beat buy & hold, so a lucky setting can't win by accident.
4. Finalists re-tested in the **real bot engine** (hour by hour, same broker, stops, fees, slippage) via `backtest.py`.

## Findings

| Walk-forward, out-of-sample 2021–2026 (daily) | CAGR | Sharpe | Max drawdown |
|---|---|---|---|
| Trend + momentum | 48% | 1.33 | −36% |
| Momentum | 41% | 1.08 | −42% |
| Moving-average filter | 41% | 1.09 | −41% |
| Buy & hold | 44% | 0.87 | −86% |
| Daily dip-buying | 1% | 0.13 | −18% |
| Auto-switching between families | 15% | 0.57 | −52% |

- **Daily beats 4-hour; slow beats fast.** Fewer trades means less lost to fees and noise.
- **Trend-following families are robust:** 90–98% of their daily settings beat buy & hold on risk-adjusted return.
- **The BTC filter helps** (alts only while BTC is above its 200-day average): smaller drawdowns, better recent year.
- **Volatility targeting did not help** here. **Dip-buying did not work.** **Switching families based on recent results made things worse** (classic overfitting), so the bot uses fixed, middle-of-the-range settings instead.

## Chosen strategies (real-engine results)

- **Daily trend** (new primary): hold a coin while its daily close is above the 100-day average and it is up over 30 days; altcoins only while BTC > its 200-day average. 1/3 of equity per coin, safety stop 3× daily ATR.
- **Breakout:** buy a close above the 30-day high, sell a close below the 20-day low; same BTC filter.

| Real bot engine | Last 180 days | Last 12 months | Jan 2021 – Oct 2026 |
|---|---|---|---|
| Daily trend | **+12.4%** (DD −14%) | **+12.7%** (DD −14%) | +2,545% (DD −40%) |
| Breakout | +10.3% (DD −14%) | +4.0% (DD −14%) | +2,650% (DD −37%) |
| Old hourly bots | −20% to −2% | −21% to −16% | −16% to +2% |
| Buy & hold | +8.4% (DD −33%) | −35.5% (DD −61%) | +2,241% (DD −96%) |

## Caveats

- The final settings were chosen with the whole history visible; the walk-forward test (which wasn't) supports them, but expect live results to be worse than backtests.
- 2021 (SOL went ~100×) dominates the long-run totals. The main, repeatable benefit is **much smaller drawdowns in crashes**, not beating every bull run: trend following lagged buy & hold in 2021 and 2023.
- The hourly strategies are kept running for comparison.

---

# Round 2: can the bot learn from its failures? (October 2026)

`research/lab2.py` tested six "learning" ideas on top of Daily trend, all judged out-of-sample 2021–2026
(anything fitted only saw data from before the 6-month window it was scored on).

| Idea | CAGR | Sharpe | Max DD | Verdict |
|---|---|---|---|---|
| Baseline Daily trend | 59% | 1.37 | −30% | — |
| **Learn from its own failed trades** (model of past trades → skip/size new entries) | 57–59% | **1.55–1.62** | −30% | **Adopted (soft sizing version)** |
| Whipsaw memory (wait / demand more after a losing trade) | 54–57% | 1.31–1.38 | −30% | No real gain |
| Committee of 60 parameter settings | 53–55% | 1.29–1.33 | −35% | Worse |
| Daily machine learning on prices | 36–42% | 1.29–1.36 | −29% | Worse |
| Learn which bot works now (performance-weighted mix) | 59% | 1.35 | −31% | No gain |
| Inverse-volatility coin weights | 50% | 1.30 | −26% | Lower risk, lower return |

**Robustness of the winner.** A hard "skip trades the model dislikes" filter was fragile: depending on model settings,
CAGR ranged from 30% to 59%. A **soft** version — a committee of 6 small models, position size 25–100% depending on the
predicted chance of a win, retrained as trades finish — lifted Sharpe to 1.49–1.56 in **all 9** setting combinations
tried, with CAGR 52–59%. It helps most in bad years (2022, 2025, 2026) and costs a little in strong rallies (2023–24).
It did not transfer to the Breakout rule (Sharpe 1.28 → 1.33), so only Adaptive trend uses it.

What it learned (2019–2026, 176 trades): trades started with stronger BTC momentum, further above the 50-day average,
or after a losing trade won more often; entries after several recent false starts won less often.

| Real bot engine | Last 180 days | Last 12 months | Jan 2021 – Oct 2026 |
|---|---|---|---|
| **Adaptive trend** | +12.4% | **+13.5%** (PF 3.48) | +2,465%, **Sharpe 1.50**, PF 3.42 |
| Daily trend | +12.4% | +12.7% (PF 2.83) | +2,545%, Sharpe 1.42, PF 2.36 |

Live safeguards: the model retrains daily; a **weekly self-review** re-checks the last 12 months walk-forward and switches
learned sizing off if it is clearly hurting (Sharpe more than 0.3 below full size). Fast hourly bots that have clearly
lost money per trade after 30+ trades are **benched** (no new entries). Every closed trade gets a post-mortem label
(rode the trend / whipsaw / stopped out / trend reversed / lost to fees) shown on the dashboard.


---

# Round 3: memory, goals, forward-looking data, crowding (October 2026)

`research/lab3.py` added data that reflects what the market *expects* rather than past prices: Deribit options-implied
volatility (DVOL, since 2021), perpetual funding rates (how crowded the long side is, since 2019) and the Crypto Fear &
Greed index (since 2018). All judged out-of-sample 2021–2026 on top of Daily trend.

| Idea | CAGR | Sharpe | Max DD | Verdict |
|---|---|---|---|---|
| Baseline Daily trend | 59% | 1.37 | −30% | — |
| Size by implied volatility (options market's forecast) | 46–50% | 1.34–1.41 | **−24%** | **Adopted for Steady trend** (lower risk, lower return) |
| Avoid crowded trades (high funding) | 33–47% | 1.02–1.30 | −30% | Hurt |
| Avoid extreme greed (sentiment) | 51–52% | 1.31–1.34 | −30% | Hurt |
| Explicit drawdown goal by cutting exposure | 31–43% | 1.03–1.18 | −24 to −26% | Hurt (misses recoveries) |
| Different daily decision time (00/04/…/20 UTC) | 56–60% | 1.29–1.37 | −29 to −33% | Edge doesn't depend on the crowded midnight close |

Forecasting check (2021–2026, 6,330 forecasts): 7-day **up/down** probabilities scored a Brier of 0.253 vs 0.250 for a
coin flip — **no skill**. Options-implied ±1σ 7-day **ranges** contained the outcome 74–78% of the time (68% expected):
useful and slightly cautious.

Real engine 2021–2026: **Steady trend** (Adaptive trend + implied-vol sizing, 30% drawdown goal) — +1,643%, Sharpe 1.48,
max drawdown **−29%** vs Adaptive trend +2,461%, Sharpe 1.50, max drawdown −40%.
