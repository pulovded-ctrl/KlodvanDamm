from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from fundarb.backtest.data import funding_environment
from fundarb.backtest.metrics import compute_metrics
from fundarb.backtest.pair_data import build_pair_dataset, pair_key
from fundarb.backtest.pair_engine import PairEngine, fee_schedule_for_pair, pair_engine_factory
from fundarb.backtest.walkforward import run_walk_forward
from fundarb.core.config import PairSettings, StrategyParams, VenueFees, load_strategy
from fundarb.marketdata.store import ParquetStore
from helpers import synthetic_venue_store

ROOT = Path(__file__).resolve().parents[1]
PAIR = PairSettings(
    venues=["va", "vb"],
    fees={
        "va": VenueFees(maker_bps=2.0, taker_bps=5.0),
        "vb": VenueFees(maker_bps=1.5, taker_bps=4.5),
    },
    price_venue="va",
)


@pytest.fixture
def params() -> StrategyParams:
    return load_strategy(ROOT / "config" / "strategy.yaml").with_overrides(
        long_leg="perp", min_24h_volume_usd=1_000_000.0
    )


def make_stores(
    tmp_path: Path,
    start: datetime,
    days: int,
    rate_a: float,
    rate_b_hourly: float,
    price=None,  # type: ignore[no-untyped-def]
) -> dict[str, ParquetStore]:
    """Venue A settles every 8h, venue B hourly (like Binance vs Hyperliquid)."""
    a = synthetic_venue_store(
        tmp_path, "va", start, days, funding_rate=lambda _b, _t: rate_a, price=price
    )
    b = synthetic_venue_store(
        tmp_path,
        "vb",
        start,
        days,
        funding_interval_hours=1.0,
        funding_rate=lambda _b, _t: rate_b_hourly,
        price=price,
        perp_only=True,
    )
    return {"va": a, "vb": b}


def test_pair_dataset_spreads(tmp_path: Path, start_dt: datetime) -> None:
    stores = make_stores(tmp_path, start_dt, days=10, rate_a=0.0002, rate_b_hourly=0.0001)
    ds = build_pair_dataset(stores, PAIR, min_bars=24)
    assert ds.coverage.warnings == []
    ab, ba = pair_key("BTC", "va", "vb"), pair_key("BTC", "vb", "va")
    assert set(ds.pairs) == {ab, ba}
    # per 8h period: B pays 8 * 0.0001 = 0.0008, A pays 0.0002 -> spread 0.0006 for long A / short B
    assert ds.pairs[ab].funding_rates[5] == pytest.approx(0.0006)
    assert ds.pairs[ba].funding_rates[5] == pytest.approx(-0.0006)
    # the settlement at the very first bar closes a period ending there: counted as period 1
    assert ds.pairs[ab].funding_count_at[0] == 1
    assert ds.pairs[ab].funding_count_at[8] == 2  # next period completes at hour 8
    assert (
        ds.pairs[ab].funding_count_at[-1] == 10 * 3
    )  # the period ending at the last bar's close is not complete yet
    assert ds.pairs[ab].rules.funding_interval_hours == 8.0
    env = funding_environment(ds)
    assert {e.base for e in env} == {ab, ba}
    # the median ignores the partial first period; the mean would be pulled down by it
    assert next(e for e in env if e.base == ab).median_apr == pytest.approx(0.0006 * 1095, rel=1e-6)


def test_pair_engine_harvests_the_spread(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    stores = make_stores(tmp_path, start_dt, days=40, rate_a=0.0002, rate_b_hourly=0.0001)
    ds = build_pair_dataset(stores, PAIR, min_bars=24)
    engine = PairEngine(ds, params, fee_schedule_for_pair(PAIR), params.backtest, PAIR)
    result = engine.run()
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.base == pair_key("BTC", "va", "vb")  # long the cheap-funding venue
    assert trade.reason == "end_of_backtest"
    assert trade.funding_usd > 0 and trade.fees_usd > 0 and trade.spread_slippage_usd > 0
    assert trade.basis_pnl_usd == 0.0
    final = float(result.equity.iloc[-1])
    assert final - params.backtest.initial_capital_usd == pytest.approx(
        sum(t.net_pnl_usd for t in result.trades), abs=1e-6
    )
    # funding received: qty * price * net spread per settlement
    periods_held = int(np.sum(~np.isnan(ds.pairs[trade.base].short_rate_at[57:]))) / 8
    expected = trade.qty * 100.0 * 0.0006 * periods_held
    assert trade.funding_usd == pytest.approx(expected, rel=0.05)
    m = compute_metrics(result)
    assert m.trades == 1 and m.total_return > 0


def test_pair_engine_captures_negative_funding(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    # venue A pays longs (negative funding), venue B flat: long A, short B collects it
    stores = make_stores(tmp_path, start_dt, days=30, rate_a=-0.001, rate_b_hourly=0.0)
    ds = build_pair_dataset(stores, PAIR, min_bars=24)
    result = PairEngine(ds, params, fee_schedule_for_pair(PAIR), params.backtest, PAIR).run()
    assert len(result.trades) == 1
    assert result.trades[0].base == pair_key("BTC", "va", "vb")
    assert result.trades[0].funding_usd > 0


def test_pair_engine_margin_both_ways(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    start_ms = int(start_dt.timestamp() * 1000)

    def rally(_b: str, ts: int) -> float:  # +80% over 40 days: the short leg needs margin
        return 100.0 * (1.0 + 0.8 * (ts - start_ms) / (40 * 86_400_000))

    def crash(_b: str, ts: int) -> float:  # -60% over 40 days: the long leg needs margin
        return 100.0 * (1.0 - 0.6 * (ts - start_ms) / (40 * 86_400_000))

    for path_fn in (rally, crash):
        root = tmp_path / path_fn.__name__
        stores = make_stores(
            root, start_dt, days=40, rate_a=0.0002, rate_b_hourly=0.0001, price=path_fn
        )
        ds = build_pair_dataset(stores, PAIR, min_bars=24)
        result = PairEngine(ds, params, fee_schedule_for_pair(PAIR), params.backtest, PAIR).run()
        assert result.margin_topups > 0, path_fn.__name__
        assert result.reductions == 0, path_fn.__name__
        final = float(result.equity.iloc[-1])
        assert final - params.backtest.initial_capital_usd == pytest.approx(
            sum(t.net_pnl_usd for t in result.trades), abs=1e-6
        )
        assert final > params.backtest.initial_capital_usd  # hedged: the move itself is neutral


def test_pair_walk_forward_uses_pair_engine(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    stores = make_stores(tmp_path, start_dt, days=290, rate_a=0.0002, rate_b_hourly=0.0001)
    ds = build_pair_dataset(stores, PAIR, min_bars=24)
    small = params.backtest.model_copy(
        update={
            "walk_forward": params.backtest.walk_forward.model_copy(
                update={
                    "grid": params.backtest.walk_forward.grid.model_copy(
                        update={"entry_threshold_apr": [0.08], "exit_threshold_apr": [0.04]}
                    )
                }
            )
        }
    )
    wf = run_walk_forward(
        ds, params, fee_schedule_for_pair(PAIR), small, jobs=1, factory=pair_engine_factory(PAIR)
    )
    assert wf.skipped_reason is None and len(wf.folds) == 1
    assert wf.oos_metrics is not None and wf.oos_metrics.trades >= 1
