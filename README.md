# fundarb: funding-rate arbitrage bot

The bot buys a coin on the spot market and at the same time opens a short of the same size
on that coin's perpetual contract. The coin's price does not matter to it: a rise in spot is
offset by the loss on the short and vice versa. The income is the funding payment the short
receives when funding is positive.

## Read this before you start

- The bot **does not guarantee income**. Funding can sit near zero for months; then the bot
  simply waits in cash.
- Check for yourself whether **Bybit is available in your country**. That is your
  responsibility.
- Create API keys **with trading rights only, no withdrawals, IP whitelist on**.
- The default mode is paper (no real money). Live trading needs three things at once:
  `LIVE_TRADING=true` in `.env`, the `--live` flag and a confirmation phrase typed in the
  console.

## What works now (phase 0)

- History downloader: funding rates and hourly candles from Bybit into Parquet files.
- A backtester that replays the strategy on the past with fees, spread, slippage and funding
  payments, and a walk-forward that tunes parameters on one window and checks them on the next.
- A report in `reports/`: markdown with the numbers and a PNG with the equity curve.

Not built yet: live trading, paper mode, Telegram. Those are phases 1 and 2.

## Installation

You need `git`, `make` and `uv` (a Python environment manager, one-line install from
https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone <repository url>
cd <folder>
make install
```

Copy `.env.example` to `.env`. Phase 0 needs no keys, the history is public.

## Commands

| Command | What it does |
|---|---|
| `make backtest` | Downloads Bybit history (only new data on repeat runs) and writes a report to `reports/` |
| `make data` | Only download or update the history |
| `make backtest-archive` | Same backtest on Binance's public archive, for when Bybit is unreachable from your network |
| `make data-hyperliquid` | Download Hyperliquid funding history for the coins already downloaded from Binance |
| `make backtest-pair` | Cross-venue study: long a perp on Binance, short it on Hyperliquid or the reverse, earn the funding difference |
| `make test` | Run the tests |
| `make lint` | Lint and type-check the code |
| `make paper`, `make live`, `make status`, `make flatten` | Phases 1 and 2, not implemented yet |

The first `make backtest` downloads history from `history_start` in `config/settings.yaml`
and can take tens of minutes. Later runs are fast.

Useful variants of the backtest command:

```bash
uv run fundarb backtest --start 2024-06-01 --end 2025-06-01   # only this period
uv run fundarb backtest --base BTC --base ETH                  # only these coins
uv run fundarb backtest --no-walk-forward                      # faster, no parameter tuning
uv run fundarb backtest --jobs 2                               # limit the number of processes
uv run fundarb data sync --max-symbols 20                      # download fewer coins
```

If a command says "Could not connect to exchange", this computer cannot reach
`api.bybit.com`: check your internet connection, VPN or network restrictions. Bybit also
refuses servers from some countries. In that case use `make backtest-archive`: it takes the
history from Binance's public archive. Binance and Bybit funding rates are similar but not
identical, so that report is an estimate, not an exact Bybit result.

## How to read the report

Open `reports/latest.md` (spot long + perp short) or `reports/latest_pair.md` (cross-venue
perp-perp). The lines that matter:

- **Out-of-sample** is the result on data the strategy "did not see" while parameters were
  tuned. Look at it, not at in-sample.
- **Max drawdown** is the worst fall of capital from a peak. If that number scares you, the
  strategy is not for you.
- **All costs as a share of gross income** shows how much of the gross income fees, spread
  and slippage ate.
- **Data coverage** says what data was missing. If there are warnings, do not trust the
  numbers above it.

## Project layout

```
config/                environment settings and strategy parameters
src/fundarb/core       data models, rounding, logging
src/fundarb/exchanges  exchange interface, ccxt implementation, Binance archive adapter
src/fundarb/marketdata history download and storage
src/fundarb/strategy   funding forecast, costs, position sizing, entry and exit logic
src/fundarb/backtest   backtester, metrics, walk-forward, report
src/fundarb/cli.py     commands
tests/                 tests
```

Project history lives in `PLAN.md` (the plan), `DECISIONS.md` (decisions taken) and
`CHANGELOG.md` (what changed). The original specification is in `PROMPT.md`.
