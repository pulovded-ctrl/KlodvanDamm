from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from fundarb.backtest.data import build_dataset
from fundarb.backtest.walkforward import (
    MONTH_MS,
    grid_candidates,
    make_folds,
    run_walk_forward,
    score,
)
from fundarb.core.config import FeeSchedule, StrategyParams, load_strategy
from fundarb.marketdata.store import ParquetStore
from helpers import synthetic_store

ROOT = Path(__file__).resolve().parents[1]
FEES = FeeSchedule(spot_maker_bps=10, spot_taker_bps=10, perp_maker_bps=2, perp_taker_bps=5.5)
HOUR = 3_600_000


@pytest.fixture
def params() -> StrategyParams:
    return load_strategy(ROOT / "config" / "strategy.yaml")


def test_make_folds_rolls_by_test_window() -> None:
    n_hours = int(15 * MONTH_MS // HOUR) + 1  # a full 15 months, rounded up
    index = np.arange(0, n_hours * HOUR, HOUR, dtype=np.int64)
    folds = make_folds(index, train_months=6, test_months=3)
    # 6+3=9 fits, then 12, then 15 -> three folds
    assert [f.number for f in folds] == [0, 1, 2]
    for f in folds:
        assert f.train_start < f.train_end == f.test_start < f.test_end
    # rolls forward by one test window: next training window ends where this test ended
    assert folds[1].train_end == folds[0].test_end
    assert folds[1].test_start == folds[0].test_end
    assert folds[1].train_start == folds[0].train_start + (folds[0].test_end - folds[0].test_start)
    assert make_folds(index[: int(5 * MONTH_MS // HOUR)], 6, 3) == []
    assert make_folds(index[:1], 6, 3) == []


def test_grid_candidates_and_score() -> None:
    cands = grid_candidates({"a": [1, 2], "b": [0.1, 0.2, 0.3]})
    assert len(cands) == 6
    assert cands[0] == {"a": 1, "b": 0.1}
    from fundarb.backtest.metrics import Metrics

    m0 = Metrics(None, None, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert score(m0) == (float("-inf"), float("-inf"))


def test_walk_forward_end_to_end(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    start_ms = int(start_dt.timestamp() * 1000)

    def funding(_b: str, ts: int) -> float:  # strong first, fades to nothing after ~7 months
        months = (ts - start_ms) / MONTH_MS
        return 0.001 if months < 7 else 0.00001

    synthetic_store(tmp_path, start_dt, days=290, funding_rate=funding)
    dataset = build_dataset(ParquetStore(tmp_path, "fake"))
    small_grid = params.backtest.walk_forward.model_copy(
        update={
            "grid": {
                "entry_threshold_apr": [0.08, 0.2],
                "exit_threshold_apr": [0.04],
                "hold_horizon_periods": [9, 30, 90],
            }
        }
    )
    settings = params.backtest.model_copy(update={"walk_forward": small_grid})
    wf = run_walk_forward(dataset, params, FEES, settings, jobs=1)
    assert wf.skipped_reason is None
    assert wf.grid_size == 2 * 1 * 3
    assert len(wf.folds) == 1
    fold = wf.folds[0]
    assert fold.candidates == 6
    assert fold.train_metrics.trades >= 1
    assert set(fold.overrides) == {
        "entry_threshold_apr",
        "exit_threshold_apr",
        "hold_horizon_periods",
    }
    assert wf.oos_metrics is not None
    assert not wf.oos_equity.empty
    assert float(wf.oos_equity.iloc[0]) == pytest.approx(settings.initial_capital_usd, rel=1e-9)


def test_walk_forward_skipped_on_short_data(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    synthetic_store(tmp_path, start_dt, days=60)
    dataset = build_dataset(ParquetStore(tmp_path, "fake"))
    wf = run_walk_forward(dataset, params, FEES, params.backtest, jobs=1)
    assert wf.skipped_reason is not None
    assert wf.folds == [] and wf.oos_metrics is None
