# Decisions

One line per decision. Format: date, decision, reason.

- 2026-09-27: Python 3.12 installed via `uv python install`; the system 3.11 is not used because the spec requires 3.12.
- 2026-09-27: Logging through `structlog` with the JSON renderer; its `event` key is built in, no extra code needed.
- 2026-09-27: The backtest has no order-book history, so the spread is `backtest.assumed_spread_bps` (default = `max_spread_bps`) and slippage is linear in the share of one minute's volume; conservative.
- 2026-09-27: Short-leg margin in the backtest is modelled as isolated per position; stricter than a unified account, so backtest results are not flattered.
- 2026-09-27: The portfolio is re-evaluated every `rebalance_interval_hours` (8h); entry and exit confirmation counters only advance when a coin has a new funding settlement.
- 2026-09-27: A coin is eligible only with at least `funding_ewma_span` settlements in history; otherwise the forecast is unreliable.
- 2026-09-27: Round-trip fees: first leg maker, second leg taker, on entry and on exit, as the execution rules say; in total spot maker + perp taker + perp maker + spot taker.
- 2026-09-27: The universe for history download is picked by today's 24h volume (top N); this carries survivorship bias, the report warns about it.
- 2026-09-27: Default fee rates in `settings.yaml` are Bybit's public non-VIP rates; the user must check them against their own account.
- 2026-09-27: Walk-forward picks parameters by Sharpe on the training window, ties broken by return; the grid is small to limit overfitting.
- 2026-09-27: The hard daily-drawdown kill switch in the backtest flattens everything and halts trading until the next UTC day.
- 2026-09-27: The development sandbox cannot reach `api.bybit.com` (network policy); the downloader is covered by tests with a fake adapter, real-data backtests must run where access exists.
- 2026-09-27: Spot and perp are matched only by identical base ticker (`BTC/USDT` with `BTC/USDT:USDT`); perps like `1000PEPE` without such a spot pair stay out of the universe.
- 2026-09-27: The exchange's predicted funding is not used in the backtest (it cannot be rebuilt honestly without premium-index history); the forecast uses settled payments only, the report says so.
- 2026-09-27: Walk-forward uses full training and test windows only; a data tail shorter than the test window is left out of the OOS result but is part of the full-period run.
- 2026-09-27: Grid candidates run in a process pool with one dataset load per process; `--jobs 1` runs sequentially for debugging.
- 2026-09-27: Open positions are force-closed at the end of the period so exit costs are counted; otherwise an open position would look free.
- 2026-09-27: Phase 1 and 2 commands exist as stubs with exit code 3 so the `Makefile` and docs do not change when they are implemented.
- 2026-09-27: Bybit blocks requests from the cloud region ("access from your country" from CloudFront) and OKX, Gate.io and Bitget serve only 1-6 months of funding history; a Binance public-archive adapter (`binance_vision`) was added as the nearest working stand-in, fees stay Bybit's, the report labels the data as a stand-in.
- 2026-09-27: `aiohttp_trust_env` is enabled in ccxt; otherwise the library ignores the system proxy and certificate variables. On a normal computer without a proxy this changes nothing.
- 2026-09-27: The downloader no longer stops on a "short" page, because exchanges return fewer rows than requested; the end of history is an empty page.
- 2026-09-27: The funding interval is taken from the data itself (median gap between recent settlements), not only from the exchange reference, because exchanges change it per coin over time; on a mismatch the report warns.
- 2026-09-27: The Binance archive has no lot rules, so for its data the rounding step is effectively disabled and the 5 $ minimum order is kept; the effect on results is pennies.
- 2026-09-27: The Telegram bot token lives only in the local `.env` (mode 600), never in git; the chat id is read from `getUpdates` after the user sends `/start` to the bot.
- 2026-09-27: The volume threshold for selecting coins to download is a quarter of the strategy's threshold; the strategy checks volume on every bar anyway, and a hard "today's volume" filter dropped coins that were liquid in the past.
- 2026-09-27: Default holding horizon is 90 periods instead of 9: at 9 the round-trip costs became 46% annualised and the bot almost never entered; walk-forward chose 90 in all eight folds. The grid moved to 30/90/180 and thresholds 5-12%.
- 2026-09-27: `cash_reserve_pct` (15% of equity) is never spent on entries and is kept for margin top-ups when prices rise; without it the bot reduced positions and paid fees once six or more positions were open.
- 2026-09-27: Spread and slippage are a separate line and the basis result is computed at mid prices; before, they were hidden inside "basis" and distorted the cost picture.
- 2026-09-27: Stablecoins are blacklisted in the strategy: there is no funding worth harvesting on them, yet they pass the volume screen.
- 2026-09-27: `reports/latest.md` and `reports/latest.png` are tracked in git, other reports are not; the latest result is visible in the repository without bloating it with history.
- 2026-09-27: The program's interface (CLI, reports, chart labels, future Telegram messages) and the whole repository are in English at the user's request; chat with the user stays in Russian. The console confirmation phrase is `ENABLE LIVE TRADING`, the chat phrase stays as in the rules.
