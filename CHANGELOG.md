# Changelog

## Unreleased

### Phase 0
- Project skeleton: `pyproject.toml` with `uv`, `Makefile`, configs, `.env.example`, documentation.
- Core: data models (`MarketSnapshot`, `TargetAction`, instrument rules), Decimal-based rounding to lot steps, typed configs that reject unknown keys, JSON logs.
- `ExchangeAdapter` interface, ccxt implementation (instrument reference, volumes, funding and candle history), Parquet store, downloader with forward paging that only fetches new data and isolates per-coin errors.
- Strategy: funding forecast (EWMA capped by predicted funding), round-trip cost model, position size as the minimum of every cap, entry/exit/rotation state machine with confirmation counters driven by new funding settlements.
- Backtester: history aligned on an hourly index with coverage checks, event-driven engine with fees, spread, slippage, funding payments, isolated short-leg margin with top-up or reduction when the liquidation distance shrinks, daily drawdown stop; metrics.
- Walk-forward: rolling train/test folds, parameter grid, parallel evaluation, chained out-of-sample equity.
- Report: markdown, PNG with equity curve and drawdown, JSON with metrics; data coverage and caveats sections.
- CLI: `fundarb data sync`, `fundarb backtest`, stubs `paper`, `live`, `status`, `pause`, `resume`, `flatten`; three-lock live-trading gate.
- Binance public-archive adapter (`binance_vision`) for funding and candle history with an on-disk cache; adapter factory; `config/settings.binance_vision.yaml`, targets `make data-archive` and `make backtest-archive`.
- Funding interval inferred from the data per coin; data-source note in the report.
- Cash reserve for margin top-ups, spread and slippage reported separately, funding-environment section in the report, default holding horizon 90 periods, stablecoins blacklisted.
- First real-data backtest (Binance archive, 60 coins, 2024-01 to 2026-08): report in `reports/latest.md`.
- Interface and repository switched to English.
