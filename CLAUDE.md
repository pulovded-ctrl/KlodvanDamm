# fundarb project rules (apply in every session)

## The user
- The user is not a programmer. Reports and explanations to them in chat: plain Russian,
  no jargon.
- Everything in the repository is English: code, file names, comments, docs, the program's
  interface (CLI, reports, Telegram messages).
- Take technical decisions yourself, do not ask technical questions. Every important
  decision: one line in `DECISIONS.md`.
- The user may only be asked about: exchange API keys, the Telegram bot token,
  confirmation of live trading.
- Enable live trading only after the user writes exactly this phrase in chat:
  `ВКЛЮЧАЮ РЕАЛЬНУЮ ТОРГОВЛЮ`.
- Never promise returns. Never invent backtest results. No data: say so.

## Stack
- Python 3.12, asyncio, `uv`, versions pinned in `uv.lock`.
- pydantic v2, ruff, mypy (strict), pytest, pytest-asyncio.
- One process, modules: `marketdata`, `strategy`, `risk`, `execution`, `ledger`, `control`,
  `backtest`. Before phase 3: no Redis, Kafka, ClickHouse, Postgres. SQLite + Parquet.
- Exchanges only through `ExchangeAdapter`. Implementations: ccxt (async, WS), paper,
  Binance public archive (history only).
- First exchange Bybit. One strategy code path for backtest, paper and live.
- Configs: `config/settings.yaml`, `config/strategy.yaml`. Secrets only in `.env`.
- JSON logs with an `event` field. Telegram through plain HTTP to the Bot API.

## Safety, never violated
- Keys with trading rights only, no withdrawals, IP whitelist. In live mode verify the key's
  permissions.
- Paper by default. Live = `LIVE_TRADING=true` + `--live` + the phrase typed in the console.
- Never commit `.env`, keys, tokens.
- Short-leg leverage never above `max_leverage_perp`. No position unhedged for longer than
  `unhedged_max_sec`.
- Kill switch: soft (no new entries) and hard (flatten everything), hard needs confirmation.
- Idempotent `clientOrderId`, retries with exponential backoff.

## Phases
- Phase 0: skeleton, history downloader, backtester, report, tests.
- Phase 1: paper mode on live data, risk, two-leg execution, ledger, reconciliation,
  Telegram, Docker.
- Phase 2: live with small capital, testnet first.
- Phase 3: Hyperliquid, LightGBM forecast, LLM announcement parser. Only on the user's order.
- Move to the next phase only after its readiness criteria are met and the user writes `дальше`.

## How to work
- Before every commit: `make lint` and `make test` green.
- Commit after every finished step. Keep `CHANGELOG.md`.
- Do not invent APIs: when in doubt, read the documentation.
