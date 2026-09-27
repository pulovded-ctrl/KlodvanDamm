from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from fundarb.backtest.data import build_dataset
from fundarb.backtest.engine import BacktestEngine
from fundarb.backtest.metrics import compute_metrics
from fundarb.core.config import FeeSchedule, StrategyParams, load_strategy
from helpers import synthetic_store

ROOT = Path(__file__).resolve().parents[1]
FEES = FeeSchedule(spot_maker_bps=10, spot_taker_bps=10, perp_maker_bps=2, perp_taker_bps=5.5)


@pytest.fixture
def params() -> StrategyParams:
    return load_strategy(ROOT / "config" / "strategy.yaml")


def run(store_root: Path, params: StrategyParams):  # type: ignore[no-untyped-def]
    from fundarb.marketdata.store import ParquetStore

    store = ParquetStore(store_root, "fake")
    dataset = build_dataset(store, min_bars=24)
    engine = BacktestEngine(dataset, params, FEES, params.backtest)
    return dataset, engine.run()


def test_constant_positive_funding_is_harvested(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    synthetic_store(tmp_path, start_dt, days=30, funding_rate=lambda _b, _t: 0.001)
    dataset, result = run(tmp_path, params)
    assert dataset.coverage.warnings == []
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.reason == "end_of_backtest"
    assert trade.funding_usd > 0
    assert trade.fees_usd > 0
    assert trade.spread_slippage_usd > 0
    assert trade.basis_pnl_usd == pytest.approx(0.0, abs=1e-9)  # price flat: basis at mids is 0
    assert trade.net_pnl_usd > 0
    # entry after 6 fundings of history plus 3 confirmations: bar 56
    assert trade.opened_at == dataset.ts(56)
    # accounting identity: equity change equals the sum of trade P&L
    final = float(result.equity.iloc[-1])
    assert final - params.backtest.initial_capital_usd == pytest.approx(
        sum(t.net_pnl_usd for t in result.trades), abs=1e-6
    )
    # funding: qty * price * rate per settlement, three settlements a day
    fundings_during_hold = int(np.sum(~np.isnan(dataset.coins["BTC"].funding_rate_at[57:])))
    assert trade.funding_usd == pytest.approx(trade.qty * 100.0 * 0.001 * fundings_during_hold)
    m = compute_metrics(result)
    assert m.trades == 1
    assert m.total_return > 0
    assert m.time_in_market > 0.8
    assert 0 < m.fees_share_of_gross < 1


def test_negative_funding_never_trades(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    synthetic_store(tmp_path, start_dt, days=20, funding_rate=lambda _b, _t: -0.001)
    _, result = run(tmp_path, params)
    assert result.trades == []
    assert float(result.equity.iloc[-1]) == params.backtest.initial_capital_usd
    m = compute_metrics(result)
    assert m.total_return == 0.0 and m.sharpe == 0.0 and m.max_drawdown == 0.0


def test_daily_drawdown_hard_stop(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    synthetic_store(tmp_path, start_dt, days=10, funding_rate=lambda _b, _t: 0.001)
    tight = params.with_overrides(daily_dd_hard_stop_pct=0.001)  # entry costs alone trip it
    _, result = run(tmp_path, tight)
    assert result.hard_stops >= 1
    assert any(t.reason == "kill_switch_hard" for t in result.trades)


def test_margin_topup_on_rising_price(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    start_ms = int(start_dt.timestamp() * 1000)

    def price(_b: str, ts: int) -> float:  # +100% over 60 days, linear
        return 100.0 * (1.0 + (ts - start_ms) / (60 * 86_400_000))

    synthetic_store(tmp_path, start_dt, days=60, funding_rate=lambda _b, _t: 0.001, price=price)
    _, result = run(tmp_path, params)
    assert result.margin_topups > 0
    assert result.reductions == 0
    assert all(t.reason != "margin_reduce" for t in result.trades)
    final = float(result.equity.iloc[-1])
    assert final - params.backtest.initial_capital_usd == pytest.approx(
        sum(t.net_pnl_usd for t in result.trades), abs=1e-6
    )
    # hedged: the 100% rally must not blow up or enrich the book beyond funding
    assert (
        0 < final - params.backtest.initial_capital_usd < 0.25 * params.backtest.initial_capital_usd
    )


def test_margin_reduce_when_cash_is_short(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    start_ms = int(start_dt.timestamp() * 1000)

    def price(_b: str, ts: int) -> float:  # +150% over 40 days
        return 100.0 * (1.0 + 1.5 * (ts - start_ms) / (40 * 86_400_000))

    greedy = params.with_overrides(max_asset_pct=60.0)  # 60% of equity in one coin: cash runs out
    synthetic_store(
        tmp_path, start_dt, days=40, funding_rate=lambda _b, _t: 0.001, price=price, volume=1e7
    )
    _, result = run(tmp_path, greedy)
    assert result.reductions > 0
    assert any(t.reason == "margin_reduce" and t.partial for t in result.trades)
    final = float(result.equity.iloc[-1])
    assert final - params.backtest.initial_capital_usd == pytest.approx(
        sum(t.net_pnl_usd for t in result.trades), abs=1e-6
    )


def test_low_volume_coin_is_never_traded(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    synthetic_store(tmp_path, start_dt, days=20, funding_rate=lambda _b, _t: 0.001, volume=100.0)
    _, result = run(tmp_path, params)
    assert result.trades == []


def test_run_window_and_end_close(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    synthetic_store(tmp_path, start_dt, days=30, funding_rate=lambda _b, _t: 0.001)
    from fundarb.marketdata.store import ParquetStore

    dataset = build_dataset(ParquetStore(tmp_path, "fake"), min_bars=24)
    engine = BacktestEngine(dataset, params, FEES, params.backtest)
    result = engine.run(start_idx=24 * 10, end_idx=24 * 20)
    assert len(result.equity) == 240
    assert result.equity.index[0] == dataset.ts(240)
    assert all(t.closed_at <= dataset.ts(24 * 20 - 1) for t in result.trades)
    empty = engine.run(start_idx=100, end_idx=50)
    assert empty.equity.empty and empty.trades == []


def test_funding_interval_inferred_from_data(tmp_path: Path, start_dt: datetime) -> None:
    from fundarb.backtest.data import build_dataset
    from fundarb.marketdata.store import ParquetStore
    from helpers import make_rules

    # reference says 8h but the data settles every 4h
    synthetic_store(tmp_path, start_dt, days=20, funding_rate=lambda _b, _t: 0.001)
    store = ParquetStore(tmp_path, "fake")
    rules = make_rules("BTC", funding_interval_hours=8.0)
    from fundarb.core.models import FundingRecord

    four_hourly = [
        FundingRecord(ts_ms=int(start_dt.timestamp() * 1000) + k * 4 * 3_600_000, rate=0.001)
        for k in range(20 * 6)
    ]
    store.append_funding("BTC", four_hourly)
    store.save_instruments({"BTC": rules})
    dataset = build_dataset(store, min_bars=24)
    assert any("funding interval" in w for w in dataset.coverage.warnings)
    coin = dataset.coins["BTC"]
    assert coin.interval_upto(len(dataset) - 1, 6) == 4.0
    assert coin.rules_at(len(dataset) - 1, 6).funding_interval_hours == 4.0
    assert coin.rules_at(0, 6).funding_interval_hours in (4.0, 8.0)
