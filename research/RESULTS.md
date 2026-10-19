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
