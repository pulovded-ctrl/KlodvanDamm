# Plan

## Phase 0: skeleton, data, backtest

- [x] Project structure, `pyproject.toml`, `Makefile`, configs, `.env.example`, `.gitignore`
- [x] Core: data models, rounding to exchange rules, config loading, JSON logs
- [x] `ExchangeAdapter` and the ccxt implementation for REST history
- [x] History downloader for Bybit funding and hourly candles into Parquet, incremental
- [x] Strategy: funding forecast, costs, `expected_net_apr`, position size, entry/exit/rotation
- [x] Backtester: event-driven, fees, spread, slippage, funding, margin, daily drawdown stop
- [x] Metrics, walk-forward, markdown + PNG report
- [x] CLI `fundarb data sync`, `fundarb backtest`; stubs for the phase 1 and 2 commands
- [x] Tests for calculations, strategy, downloader, backtester, CLI
- [x] `make lint`, `make test` green
- [ ] `make backtest` on Bybit data: Bybit blocks the cloud region, needs a run from the
      user's own computer
- [x] `make backtest-archive` on Binance's public archive as a stand-in: report in
      `reports/latest.md`
- [x] Report to the user in plain language

## Study before phase 1: cross-venue perp-perp (user's choice)

- [x] Hyperliquid history adapter (funding hourly since 2023, recent candles only)
- [x] Pair dataset: both directions per coin, net funding spread per 8h period
- [x] Pair engine: per-venue cash, margin on both legs, daily cash equalisation
- [x] Pair mode in the CLI, settings and strategy files, Makefile targets
- [x] Download Hyperliquid funding for the coins shared with the Binance archive (24 coins with history)
- [x] Run the pair backtest and explain the result to the user: `reports/latest_pair.md`
- [ ] User decides which scheme goes to phase 1

## Phase 1: paper mode on live data

Goal: the bot runs around the clock on live prices without real money, and the whole
mechanism (data, risk, two legs, ledger, reconciliation, alerts) is proven in the field.

- [ ] Live data: WebSocket subscriptions to order book, trades, mark price and funding via
      ccxt; staleness control, automatic reconnect
- [ ] `PaperExchangeAdapter`: fills against the live order book with fees and slippage, same
      interface as the real exchange
- [ ] Trading loop: strategy once per funding period, risk module with every limit, soft and
      hard kill switch, API error breaker, stablecoin depeg check
- [ ] Two-leg execution: first leg post-only, second IOC, timeout and rollback, partial fills,
      idempotent `clientOrderId`, retries with exponential backoff
- [ ] SQLite ledger: positions, orders, fills, funding, P&L; reconciliation with the exchange
      every minute; state restored from the exchange after a restart
- [ ] Telegram: alerts (entry, exit, kill switch, mismatch, stale data, leg rollback, daily
      report) and commands `/status`, `/pause`, `/resume`, `/flatten` with confirmation
- [ ] Docker and docker-compose, VPS deployment, `fundarb status`
- [ ] Report to the user in plain language

Needed from the user: Telegram bot token and chat id. No exchange keys: the data is public.

Done when: seven days in a row in Docker without manual intervention, no unhedged episode
longer than `unhedged_max_sec`, clean reconciliation, report shown, user wrote `дальше`.

## Phase 2: live with small capital

Goal: real money, a small amount, testnet first.

- [ ] Real trading adapter: orders, balances, positions, transfers between wallets
- [ ] API key permission check at startup: no withdrawal rights, otherwise refuse to start
- [ ] Live-trading gate wired to the real adapter: `LIVE_TRADING=true`, `--live`, console phrase
- [ ] Bybit testnet mode, run there before the real account
- [ ] `RUNBOOK.md`: what to do if the bot stops, how to flatten by hand, how to restart, how
      to verify reconciliation
- [ ] Daily Telegram report with capital, income, positions and fees

Needed from the user: a Bybit API key with trading rights only, no withdrawals, IP-bound to
the server (testnet first, then real), the starting amount, the phrase
`ВКЛЮЧАЮ РЕАЛЬНУЮ ТОРГОВЛЮ` in chat.

Done when: a month of operation without incidents or mismatches.

## Phase 3: expansion, only on the user's order

- [ ] Hyperliquid adapter and a perp-perp scheme across exchanges for negative funding
- [ ] Funding forecast with LightGBM; enabled only if it beats the exponential average on
      walk-forward
- [ ] LLM parser of exchange announcements (delistings, maintenance) that fills
      `exchange_events`; output validated against a schema, never creates orders directly
- [ ] Optional: Bybit premium-index history so the backtest gets a predicted funding rate
