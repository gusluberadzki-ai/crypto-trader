# PaperTrader

A 24/7 crypto **paper-trading** bot: live Binance public market data, four competing strategies (trend, mean reversion, machine learning, and an ensemble), realistic fees and slippage, risk management, walk-forward backtesting, and a dark web dashboard.

**Simulation only.** It reads public prices and simulates fills locally. There is no order code, it refuses API keys in the config, and `mode` must be `paper`. A test checks that no exchange order endpoints exist in the source.

## Quick start

Needs Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m papertrader run          # live Binance data
# open http://localhost:8000
```

First start back-fills 365 days of 1h candles for BTC, ETH and SOL (about a minute), trains the first model, then starts trading on the next closed candle.

No internet or Binance blocked? `python -m papertrader run --sim` runs the whole system on synthetic data (separate database) so you can see it working.

## Run it for weeks

Option A, Docker (restarts after crashes and reboots):

```bash
docker compose up -d --build
docker compose logs -f
```

Option B, plain Python: run it inside `tmux`/`screen`, or as a systemd service on Linux. The bot keeps all state in `data/papertrader.db`, so stopping and restarting is safe: positions, cash, trades and equity history come back, missed candles are back-filled, and any stop-loss that would have triggered while it was off is applied at the historical price.

Option C, free on GitHub (no server): the included workflow `.github/workflows/papertrader.yml` runs one trading step every hour on GitHub Actions and publishes a read-only dashboard to GitHub Pages. Make the repo public, set Settings → Pages → Source to "GitHub Actions", then run the workflow once from the Actions tab. State is saved to the `state` branch. It uses Binance data when reachable, otherwise Coinbase (GitHub's runners are in the US, where Binance is blocked).

Option D, Railway (cloud, set up from a phone): create a project from this GitHub repo, add a variable `DASHBOARD_PASSWORD`, attach a volume mounted at `/app/data`, and generate a public domain. The bot refuses to start on a cloud host without a password.

Your computer must stay awake. For a multi-week run, a cheap always-on machine (Raspberry Pi, old laptop, small cloud VM) is ideal.

## Commands

| Command | What it does |
|---|---|
| `python -m papertrader run` | bot + dashboard |
| `python -m papertrader backtest --days 180` | replay history, compare with buy & hold |
| `python -m papertrader train` | train and evaluate a new model now |
| `python -m papertrader reset --yes` | wipe paper portfolios (keeps candles and models) |
| `python -m pytest` | run the tests |

Add `--sim` to any command to use synthetic data.

## How it works

```
Binance WebSocket ──► candles (SQLite) ──► indicators ──► strategies ──► risk manager ──► paper broker
   (REST fallback)                                          │                              │
                                                ML model ◄──┘ retrained daily      trades, decisions, equity ──► dashboard
```

**Strategies** — each gets its own $10,000 portfolio so you can compare them fairly on identical data:

- **Trend following**: EMA20/50 crossover, price above EMA200, MACD momentum, ADX trend strength.
- **Mean reversion**: buys oversold dips (RSI < 30 below the lower Bollinger Band) in an intact uptrend, exits at the mean.
- **Machine learning**: gradient-boosted trees on 22 features (returns, RSI, MACD, Bollinger, ATR, ADX, volume, volatility regime, BTC moves, time of day). Predicts whether the next 6 bars gain more than 0.4%.
- **Ensemble**: weighted vote of the three above, sits out extreme-volatility regimes.
- **Buy & hold**: equal-weight benchmark, never sells.

**Risk management** (all in `config.yaml`): ~1% of equity risked per trade (sized from a 2×ATR stop), max 30% per coin and 90% total, trailing stop after +1.5 ATR, 4R take-profit, cooldown after stop-outs, no new entries after a 3% losing day, full halt at 20% drawdown (resume from the dashboard).

**Costs**: 0.10% fee per side plus slippage of 5 bps + 5% of ATR%, so fills are worse in volatile markets.

**Controlled retraining**: every 24h a candidate model is trained on all history except the last ~30 days, which are held out (with a gap so labels don't leak). It is promoted only if, on that unseen window, it beats AUC 0.51, has positive expected return per signal after costs, beats random entry, and beats the current model on the same window. Every version is saved; you can roll back from the dashboard. If no model passes, the ML strategy simply stays in cash — that is the system working correctly.

**Explanations**: every decision gets a plain-language explanation built from the indicator values, the ML model's probability, and a sensitivity analysis showing which features pushed that probability up or down.

## Reading the results honestly

- A few weeks of hourly trading gives tens of trades per strategy. The dashboard prints a significance note: under ~30 trades, results are mostly luck.
- Compare against buy & hold, not against zero. In a rising market almost anything "makes money".
- Backtests use the same code as live trading and retrain the model walk-forward (only past data), but they still can't capture everything (real order books, outages, exchange fee tiers).
- Most simple strategies do not beat buy & hold after costs. Finding that out with fake money is the point of this project.

## Project layout

```
papertrader/
  config.py        config loading + paper-only validation
  db.py            SQLite storage (WAL)
  market/          Binance public data client (WS + REST fallback), synthetic data
  features.py      indicators and ML features
  strategies/      trend, mean reversion, ML, ensemble
  ml.py            training, validation, promotion, explanations
  broker.py        paper broker: cash, positions, fees, slippage
  risk.py          sizing, stops, trailing stops, circuit breakers
  core.py          decision logic shared by live trading and backtests
  engine.py        live loop, persistence, reconnection, supervisor
  backtest.py      walk-forward backtester
  metrics.py       Sharpe, Sortino, drawdown, win rate, profit factor
  api.py           FastAPI REST + WebSocket
  web/             dashboard (vanilla JS + TradingView Lightweight Charts)
tests/
config.yaml
```

## Tweaks

- Change coins with `symbols` (any Binance USDT pair) and timeframe with `interval` (e.g. `15m` for more trades, at a higher fee cost). Changing `interval` starts a fresh candle set; run `reset --yes` too.
- If `data-api.binance.vision` is unreachable where you live, set `rest_base: https://api.binance.us` and `ws_base: wss://stream.binance.us:9443` (US users).
- To view the dashboard from your phone on the same Wi-Fi, set `server.host: 0.0.0.0`. Set `DASHBOARD_PASSWORD` (environment variable) or `server.password` before exposing it to the internet.
