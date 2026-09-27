# Backtest report: funding-rate arbitrage

Generated 2026-09-27 15:41 UTC. Exchange: `binance_vision`. Initial capital: 10 000 $.

## Summary

Result on the out-of-sample walk-forward folds: positive, 0.6% annualised with a max drawdown of 0.2% over 27 trades. Costs took 46.0% of gross income.

## Out-of-sample (the numbers that matter)

Grid of 18 combinations, 8 folds. Parameters were tuned on the training window only and evaluated on the window that follows it.

| Metric | Value |
|---|---|
| Period | 2024-07-01 to 2026-07-02 (730 days) |
| Capital, start to end | 10 000 $ to 10 119 $ |
| Return over the period | 1.2% |
| Annualised return | 0.6% |
| Sharpe (daily returns) | 2.37 |
| Max drawdown | 0.2% |
| Trades (full round trips) | 27, partial reductions 6 |
| Winning trades | 59.3% |
| Average holding time | 15.9 days |
| Time in market | 21.8% |
| Annual turnover (x capital) | 2.6x |
| Funding received | 193 $ |
| Basis P&L (spot minus perp, at mid prices) | 28 $ |
| Exchange fees | 68 $ |
| Spread and slippage | 34 $ |
| All costs as a share of gross income | 46.0% |
| Hard stops, margin top-ups, reductions | 0, 49, 6 |
| Rejected entries (not enough cash or size) | 0 |

| Fold | Training | Test | Chosen parameters | Train Sharpe | Train return | Test return | Test drawdown | Test trades |
|---|---|---|---|---|---|---|---|---|
| 1 | 2024-01-01 to 2024-07-01 | 2024-07-01 to 2024-09-30 | entry_threshold_apr=0.12, exit_threshold_apr=0.02, hold_horizon_periods=180 | 11.46 | 5.3% | 0.0% | -0.0% | 0 |
| 2 | 2024-04-01 to 2024-09-30 | 2024-09-30 to 2024-12-31 | entry_threshold_apr=0.12, exit_threshold_apr=0.04, hold_horizon_periods=180 | 3.06 | 0.5% | 1.3% | 0.1% | 7 |
| 3 | 2024-07-01 to 2024-12-31 | 2024-12-31 to 2025-04-01 | entry_threshold_apr=0.12, exit_threshold_apr=0.02, hold_horizon_periods=180 | 6.14 | 1.3% | 0.1% | 0.0% | 2 |
| 4 | 2024-09-30 to 2025-04-01 | 2025-04-01 to 2025-07-01 | entry_threshold_apr=0.12, exit_threshold_apr=0.02, hold_horizon_periods=180 | 7.33 | 1.6% | -0.1% | 0.1% | 1 |
| 5 | 2024-12-31 to 2025-07-01 | 2025-07-01 to 2025-10-01 | entry_threshold_apr=0.12, exit_threshold_apr=0.02, hold_horizon_periods=180 | 0.45 | 0.0% | -0.0% | 0.1% | 9 |
| 6 | 2025-04-01 to 2025-10-01 | 2025-10-01 to 2025-12-31 | entry_threshold_apr=0.12, exit_threshold_apr=0.02, hold_horizon_periods=180 | -0.90 | -0.1% | -0.0% | 0.2% | 7 |
| 7 | 2025-07-01 to 2025-12-31 | 2025-12-31 to 2026-04-01 | entry_threshold_apr=0.12, exit_threshold_apr=0.02, hold_horizon_periods=180 | -0.07 | -0.0% | 0.0% | -0.0% | 0 |
| 8 | 2025-10-01 to 2026-04-01 | 2026-04-01 to 2026-07-02 | entry_threshold_apr=0.12, exit_threshold_apr=0.02, hold_horizon_periods=180 | -0.24 | -0.0% | -0.1% | 0.1% | 1 |

## Full period with default parameters (reference, in-sample)

| Metric | Value |
|---|---|
| Period | 2024-01-01 to 2026-08-31 (974 days) |
| Capital, start to end | 10 000 $ to 10 515 $ |
| Return over the period | 5.1% |
| Annualised return | 1.9% |
| Sharpe (daily returns) | 4.11 |
| Max drawdown | 0.4% |
| Trades (full round trips) | 46, partial reductions 14 |
| Winning trades | 58.7% |
| Average holding time | 19.7 days |
| Time in market | 22.0% |
| Annual turnover (x capital) | 3.1x |
| Funding received | 636 $ |
| Basis P&L (spot minus perp, at mid prices) | 54 $ |
| Exchange fees | 119 $ |
| Spread and slippage | 57 $ |
| All costs as a share of gross income | 25.4% |
| Hard stops, margin top-ups, reductions | 0, 88, 14 |
| Rejected entries (not enough cash or size) | 0 |

### By coin (full period, default parameters)

| Coin | Trades | Funding | Costs | Net |
|---|---|---|---|---|
| WLD | 3 | 132 $ | 8 $ | 123 $ |
| FIL | 1 | 91 $ | 3 $ | 89 $ |
| INJ | 3 | 93 $ | 8 $ | 86 $ |
| ACE | 2 | 70 $ | 5 $ | 63 $ |
| AVAX | 2 | 65 $ | 4 $ | 62 $ |
| LINK | 1 | 31 $ | 4 $ | 27 $ |
| FET | 3 | 26 $ | 5 $ | 19 $ |
| PYTH | 2 | 19 $ | 5 $ | 18 $ |
| SUI | 1 | 22 $ | 3 $ | 18 $ |
| ETH | 2 | 23 $ | 6 $ | 17 $ |
| ADA | 1 | 19 $ | 3 $ | 16 $ |
| TAO | 1 | 8 $ | 3 $ | 5 $ |
| other 13 | | | | -29 $ |

## Funding environment: what the market actually paid

Across all coins and settlements: funding above 15% annualised in 4.4% of settlements, above 50% in 1.6%, negative in 26.5%. The strategy only earns on settlements above the entry threshold after costs; the rest of the time it waits in cash.

| Coin | Settlements | Mean funding, annualised | Median | Share above 15% | Share negative |
|---|---|---|---|---|---|
| GIGGLE | 1959 | 16.1% | 10.9% | 4.6% | 8.5% |
| ASTER | 2078 | 10.5% | 10.9% | 4.3% | 18.1% |
| PUMP | 2912 | 10.0% | 10.9% | 6.8% | 16.7% |
| UNI | 2922 | 8.4% | 10.5% | 6.8% | 14.5% |
| LTC | 2922 | 8.1% | 8.4% | 7.2% | 17.6% |
| LINK | 2922 | 8.1% | 10.2% | 6.7% | 16.9% |
| DOGE | 2922 | 7.5% | 7.6% | 7.6% | 21.9% |
| ETH | 2922 | 7.1% | 6.3% | 6.9% | 15.8% |
| BTC | 2922 | 7.1% | 6.2% | 5.9% | 15.1% |
| XPL | 2246 | 7.1% | 10.9% | 0.9% | 14.4% |
| ADA | 2922 | 7.0% | 10.3% | 7.2% | 22.7% |
| NEAR | 2922 | 6.9% | 10.9% | 6.5% | 22.8% |
| FIL | 2922 | 6.7% | 10.9% | 8.2% | 22.1% |
| AAVE | 2922 | 6.6% | 7.5% | 5.3% | 20.7% |
| XRP | 2922 | 6.5% | 7.2% | 7.1% | 26.4% |
| ... 45 more coins | | | | | |

## Data coverage

- Source: exchange `binance_vision`, timeframe `1h`, last sync `2026-09-27T10:36:39.968172+00:00`.
- Coin selection rule: top by min(spot, perp) 24h volume at selection time.
- Coins in data: 60, usable: 60.
- Range: 2024-01-01 to 2026-08-31.
- **Warnings (2):**
  - DEXE: funding interval from data is 4h, reference says 1h; using the data
  - ONG: funding interval from data is 4h, reference says 1h; using the data

## Assumptions and caveats

- **Data source: Binance public archive used as a stand-in for Bybit. Bybit funding rates differ.**
- Fees: spot maker 10.0 bps, taker 10.0 bps; perp maker 2.0 bps, taker 5.5 bps. Check your own tier on the exchange; defaults are public non-VIP rates.
- Spread: no order-book history, 5.0 bps assumed on each leg.
- Slippage: 100.0 bps per 100% of one minute's volume.
- Short-leg margin is isolated, maintenance rate 1.00%. Stricter than a unified account.
- The exchange's predicted funding is not available in the backtest; the forecast uses history only. Live trading caps the forecast with it.
- The coin universe was picked by volume at download time: survivorship bias, results may be flattered.
- Positions are force-closed at the end of the period so every cost is counted.

## Strategy parameters

```yaml
entry_threshold_apr: 0.12
exit_threshold_apr: 0.04
rotation_margin_apr: 0.05
confirm_periods: 3
funding_ewma_span: 6
hold_horizon_periods: 90
min_24h_volume_usd: 20000000.0
max_spread_bps: 5.0
max_order_pct_of_1min_volume: 5.0
max_hedge_slippage_bps: 10.0
leg_timeout_sec: 2.0
unhedged_max_sec: 10.0
max_asset_pct: 10.0
max_positions: 8
min_notional_usd: 100.0
max_leverage_perp: 2.0
min_liq_distance_pct: 40.0
cash_reserve_pct: 15.0
stale_data_sec: 5.0
daily_dd_hard_stop_pct: 3.0
reconcile_interval_sec: 60
api_error_rate_breaker: {'max_errors': 5, 'window_sec': 60}
stablecoin_depeg_pct: 1.0
event_close_before_hours: 24.0
rebalance_interval_hours: 8
blacklist: ['USDC', 'FDUSD', 'USD1', 'TUSD', 'DAI', 'USDE', 'BUSD']
```

![Equity curve](latest.png)
