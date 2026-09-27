# Specification (the original Claude Code prompt, translated)

This is the brief the project was started from. Starting parameter values in section 5 are
the original ones; current defaults live in `config/strategy.yaml` and every change is logged
in `DECISIONS.md`.

---

You are a senior quant developer and, at the same time, a careful risk manager. Your task:
build a crypto trading bot in this folder according to the specification below. Work in
phases, verify with tests, invent nothing.

## 1. Who I am and how to work with me

- I am not a programmer. Explain everything to me in plain Russian, no jargon. Write code,
  file names and code comments in English.
- Take all technical decisions yourself. Do not ask me technical questions. Record every
  important decision as one line in `DECISIONS.md`.
- You may ask me about three things only: exchange API keys, the Telegram bot token,
  confirmation before trading real money.
- Never enable real-money trading on your own. Only after I write exactly this phrase in
  chat: `ВКЛЮЧАЮ РЕАЛЬНУЮ ТОРГОВЛЮ`.
- Do not promise me returns. Do not invent backtest results. If there is no data or
  something does not work, say so.
- If an instruction below contradicts reality, for example an exchange API changed or a
  library is deprecated, pick the closest working alternative and record it in `DECISIONS.md`.
- Before using any API or library you are unsure about, check the current documentation
  online. Do not invent endpoints or parameters.
- Remind me once, at the very beginning, that I must check myself whether the chosen
  exchange is available in my country. Give no legal advice.

## 2. What we build and why

The bot earns the funding of perpetual contracts. The scheme: buy a coin on spot and at the
same time open a short of the same size on that coin's perpetual. The coin's price does not
matter to the bot: a rise in spot is offset by the short's loss and vice versa. The income is
the funding payments the short receives when funding is positive.

Why this: no price prediction is needed, trades are few, so fees do not eat the result, and
the scheme works in any market regime as long as funding is positive.

What we do NOT do in any phase: scalping, grid, DCA with add-ons, martingale, price
prediction with models, an LLM inside the trading decision loop, shorting spot with borrowed
coins.

## 3. Stack and principles

- Python 3.12, `asyncio`. Dependencies via `uv`, versions pinned in a lock file.
- `pydantic` v2 for data models and configs. `ruff` for linting and formatting, `mypy` for
  types, `pytest` for tests, `pytest-asyncio` for async tests.
- One process, but code split into modules along future service boundaries: `marketdata`,
  `strategy`, `risk`, `execution`, `ledger`, `control`, `backtest`. In phases 0, 1 and 2 no
  Redis, Kafka, ClickHouse, Postgres. Ledger and orders in SQLite, history in Parquet.
- Exchange access only through our own `ExchangeAdapter` interface with abstract methods:
  order book, trades, funding, order placement and cancellation, positions, balances,
  transfers between wallets. First implementation through `ccxt` in async mode with
  WebSocket. If `ccxt` lacks something for our exchange, a native adapter on the official
  SDK is allowed. Second implementation: `PaperExchangeAdapter`, which simulates fills
  against the live order book with fees and slippage.
- First exchange: Bybit, unified account, it has a testnet. The architecture must allow
  adding Hyperliquid and OKX later without rewriting the strategy.
- The strategy code is the same for backtest, paper mode and live. No duplicated logic.
- Configs in YAML validated with pydantic: `config/settings.yaml` for the environment and
  `config/strategy.yaml` for strategy parameters. Secrets only in `.env`, which is in
  `.gitignore`. Provide `.env.example` next to it.
- Structured JSON logs with an `event` field. Telegram alerts through a plain HTTP request
  to the Bot API, no heavy frameworks.
- `Dockerfile` and `docker-compose.yml` for running on a VPS. `Makefile` with targets:
  `install`, `lint`, `test`, `backtest`, `paper`, `live`, `status`, `flatten`.
- CLI on `typer`: `fundarb backtest`, `fundarb paper`, `fundarb live`, `fundarb status`,
  `fundarb pause`, `fundarb resume`, `fundarb flatten`.

## 4. Safety, never violated

- API keys with trading rights only, no withdrawal rights, IP-bound. At startup in live mode
  the bot checks the key's permissions through the API where the exchange allows it and
  refuses to start if withdrawals are enabled.
- Default mode: paper. Live requires, at the same time, the environment variable
  `LIVE_TRADING=true`, the `--live` flag and a confirmation phrase typed in the console at
  startup.
- Never commit `.env`, keys, tokens. Check `.gitignore` before the first commit.
- Short-leg leverage never above `max_leverage_perp` from the config. No position unhedged
  for longer than `unhedged_max_sec`.
- Two kill-switch levels. Soft: no new entries. Hard: close every position at market. Both
  available from the CLI and Telegram, the hard one needs confirmation.
- Every exchange call with retries and exponential backoff, but with idempotent
  `clientOrderId` so a retry never creates a duplicate order.

## 5. Strategy logic, implement exactly this

All numbers come from `config/strategy.yaml`; starting values are below.

Universe:
- USDT perpetuals on the exchange that have a USDT spot pair on the same exchange.
- 24h volume of both the perp and the spot above `min_24h_volume_usd`.
- Spread on both legs not above `max_spread_bps`.
- The coin is not in the `blacklist` and not marked as delisting in `exchange_events`.

Funding forecast:
- For every coin read the funding interval from the API; it varies: 8 hours, 4 hours, 1 hour.
- Forecast of the next funding: exponential average of the last `funding_ewma_span`
  payments; if the exchange publishes a predicted funding, take the minimum of the forecast
  and the predicted value.
- Annualised rate: the forecast times the number of periods per year for that interval.

Costs of one round trip, entry and exit:
- Spot and perp fees for entry and exit at the configured rates.
- Half the spread of each leg on entry and on exit.
- Slippage estimated from the order book for our size.
- Costs are spread over the holding horizon `hold_horizon_periods` and subtracted from the
  annualised rate. Result: `expected_net_apr`.

Entry:
- `expected_net_apr` above `entry_threshold_apr` for `confirm_periods` evaluations in a row.
- Both legs liquid, order size not above `max_order_pct_of_1min_volume` of each leg's
  one-minute volume.
- Free capital and limits allow it.

Position size:
- Minimum of: `max_asset_pct` of capital, available order-book depth, margin buffer,
  `max_positions` simultaneous positions.
- Not below `min_notional_usd`, otherwise no entry.

Executing the two legs:
- First the less liquid leg, usually spot, with a post-only limit order at the best price.
- As soon as the first leg is filled, immediately the second leg with an IOC order capped by
  `max_hedge_slippage_bps` of slippage.
- If the second leg is not filled within `leg_timeout_sec`, roll the first leg back at market
  and record an incident.
- Hedge partial fills as they happen, do not wait for a full fill.
- Round sizes to exchange rules: lot step, minimum amount, price tick.

Holding:
- If the unified account accepts the spot coin as collateral, use that. Otherwise keep a
  USDT buffer on the derivatives balance and transfer between wallets automatically.
- Distance from the current price to the short's liquidation price never below
  `min_liq_distance_pct`. If closer, add margin, and if there is nothing to add, reduce the
  position.

Exit:
- `expected_net_apr` below `exit_threshold_apr` for `confirm_periods` evaluations in a row.
- Or the funding forecast is negative.
- Or a risk trigger from section 6 fired.
- Close with the same two-leg rules in reverse order: close the short first, then sell spot,
  or both at once if the order book allows.

Rotation:
- Once per funding period re-evaluate the whole portfolio. Replace a position with another
  one only if the difference in `expected_net_apr` covers the replacement costs with a
  margin of `rotation_margin_apr`.

Starting parameters:

| Parameter | Value |
|---|---|
| `entry_threshold_apr` | 0.12 |
| `exit_threshold_apr` | 0.04 |
| `rotation_margin_apr` | 0.05 |
| `confirm_periods` | 3 |
| `funding_ewma_span` | 6 |
| `hold_horizon_periods` | 9 |
| `min_24h_volume_usd` | 20000000 |
| `max_spread_bps` | 5 |
| `max_order_pct_of_1min_volume` | 5 |
| `max_hedge_slippage_bps` | 10 |
| `leg_timeout_sec` | 2 |
| `unhedged_max_sec` | 10 |
| `max_asset_pct` | 10 |
| `max_positions` | 8 |
| `min_notional_usd` | 100 |
| `max_leverage_perp` | 2.0 |
| `min_liq_distance_pct` | 40 |
| `stale_data_sec` | 5 |
| `daily_dd_hard_stop_pct` | 3 |
| `reconcile_interval_sec` | 60 |
| `api_error_rate_breaker` | 5 errors in 60 seconds |
| `stablecoin_depeg_pct` | 1 |

## 6. Risk controls, without them the bot does not enter paper mode

- Stale data: if the last order-book or price update is older than `stale_data_sec`, no new
  orders, only reconnection. REST is used for reconciliation, not for trading decisions.
- Reconciliation with the exchange every `reconcile_interval_sec`: positions, balances, open
  orders. Any mismatch with the local ledger is a soft kill switch plus an alert.
- API error breaker: above `api_error_rate_breaker`, soft kill switch.
- Daily drawdown above `daily_dd_hard_stop_pct` of the capital at the start of the day: hard
  kill switch.
- USDT or USDC deviating from the dollar by more than `stablecoin_depeg_pct`: soft kill
  switch and position reduction.
- The `exchange_events` list in the config: delistings and maintenance with dates. A position
  is closed `event_close_before_hours` before the event. In early phases the list is filled by
  hand.
- After any restart the state is restored from the exchange, not from the local database.
  The local database is reconciled with the exchange and corrected, mismatches are logged.

## 7. Backtester

- Event-driven, runs on the same strategy code.
- Inputs: funding history per coin with intervals, 1-hour candles for spot and perp, 1-minute
  where possible, fee rates from the config, a volume-based slippage model.
- Accounts for every cost from section 5, funding payments on the exchange schedule, volume
  limits.
- Output: equity curve, annualised return, Sharpe, max drawdown, turnover, share of fees in
  the result, number of trades, average position duration. Report in markdown plus a PNG
  chart in `reports/`.
- Walk-forward is mandatory: parameters are chosen on one window and checked on the next;
  the report shows both.
- A history downloader from the exchange into Parquet that only fetches new data.
- If history is unavailable or incomplete, the report says so plainly. No tables filled with
  invented numbers.

## 8. Observability and control

- Telegram alerts: position entry and exit, any kill switch, reconciliation mismatch, stale
  data, leg rollback, a daily report with capital, income for the day, open positions and fees.
- Telegram commands: `/status`, `/pause`, `/resume`, `/flatten` with confirmation.
- `fundarb status` shows capital, positions, expected annualised rate per position, kill
  switch state, data age.
- `RUNBOOK.md`: what to do if the bot stops, how to close everything by hand, how to restart,
  how to check that everything matches the exchange.

## 9. Phases and readiness criteria

Do not move to the next phase until the current one's criteria are met and I wrote `дальше`.

Phase 0. Skeleton, data, backtest.
- Project structure, configs, `ExchangeAdapter`, the `ccxt` implementation for REST history.
- Downloader of Bybit funding and candle history into Parquet.
- Backtester and report. `make backtest` works on real data covering at least a year.
- Tests for expected return, costs, position size, rounding to exchange rules.
- Done when: `make lint`, `make test`, `make backtest` pass, the report is in `reports/`, and
  you explained the result to me in plain words, including the share of fees and the worst
  drawdown.

Phase 1. Paper mode on live data.
- WebSocket data, `PaperExchangeAdapter`, strategy loop, risk module, two-leg execution,
  ledger, reconciliation, Telegram alerts, Docker.
- Done when: the bot runs in Docker in paper mode for seven days in a row without manual
  intervention, not a single unhedged episode longer than `unhedged_max_sec`, reconciliation
  clean, and you showed me the report.

Phase 2. Live with small capital.
- Bybit testnet first. Then a real account with an amount I name.
- Key permission check, confirmation phrase at startup, `RUNBOOK.md`.
- Done when: a month of operation without incidents or mismatches.

Phase 3. Expansion, only on my order.
- Second adapter: Hyperliquid, a perp-perp scheme across exchanges for negative funding.
- Funding forecast with a LightGBM model, enabled only if it beats the exponential average on
  walk-forward.
- An LLM parser of exchange announcements about delistings and maintenance that fills
  `exchange_events` automatically. Use the Anthropic SDK and the cheapest current model per
  the documentation. The parser's output is always validated against a schema and never
  creates orders directly.

## 10. How to work

- First create `CLAUDE.md` with a short version of the rules from sections 1, 3, 4 and 9 so
  they apply in every session. Then `PLAN.md` with the phase 0 plan, `DECISIONS.md`,
  `README.md` for a beginner.
- Commit after every finished step with a clear message. Keep `CHANGELOG.md`.
- Before every commit: `make lint` and `make test` green.
- At the end of every phase give me a plain-language report: what is done, which commands to
  run, what to look at, which risks remain.
- Start with phase 0 now.
