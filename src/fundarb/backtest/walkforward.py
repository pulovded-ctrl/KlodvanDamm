"""Walk-forward: tune a small parameter grid on a training window, evaluate on the next
window, roll forward. Out-of-sample segments are chained into one equity curve."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import pandas as pd

from fundarb.backtest.engine import BacktestEngine, BacktestResult, TradeRecord
from fundarb.backtest.metrics import Metrics, compute_metrics
from fundarb.core.config import BacktestSettings, FeeSchedule, StrategyParams

DAY_MS = 86_400_000
MONTH_MS = int(365.25 / 12 * DAY_MS)
Overrides = dict[str, object]


class Runner(Protocol):
    def run(self, start_idx: int = 0, end_idx: int | None = None) -> BacktestResult: ...


class Dataset(Protocol):
    index_ms: np.ndarray

    def __len__(self) -> int: ...


EngineFactory = Callable[[Any, StrategyParams, FeeSchedule, BacktestSettings], Runner]


@dataclass(frozen=True, slots=True)
class Fold:
    number: int
    train_start: int
    train_end: int  # exclusive
    test_start: int
    test_end: int  # exclusive


@dataclass(slots=True)
class FoldReport:
    fold: Fold
    overrides: Overrides
    train_metrics: Metrics
    test_metrics: Metrics
    test_equity: pd.Series
    test_trades: list[TradeRecord]
    candidates: int
    note: str = ""


@dataclass(slots=True)
class WalkForwardResult:
    folds: list[FoldReport] = field(default_factory=list)
    oos_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    oos_metrics: Metrics | None = None
    grid_size: int = 0
    skipped_reason: str | None = None


def make_folds(index_ms: np.ndarray, train_months: int, test_months: int) -> list[Fold]:
    n = len(index_ms)
    if n < 2:
        return []
    tf = int(index_ms[1] - index_ms[0])
    train_ms, test_ms = train_months * MONTH_MS, test_months * MONTH_MS
    folds: list[Fold] = []
    start_ts = int(index_ms[0])
    last_ts = int(index_ms[-1]) + tf
    while True:
        train_end_ts = start_ts + train_ms
        test_end_ts = train_end_ts + test_ms
        if test_end_ts > last_ts:
            break
        a = int(np.searchsorted(index_ms, start_ts))
        b = int(np.searchsorted(index_ms, train_end_ts))
        c = int(np.searchsorted(index_ms, test_end_ts))
        folds.append(Fold(len(folds), a, b, b, c))
        start_ts += test_ms
    return folds


def grid_candidates(grid: Mapping[str, Iterable[object]]) -> list[Overrides]:
    keys = list(grid)
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*grid.values())]


def score(metrics: Metrics) -> tuple[float, float]:
    """Sharpe first, annual return as tie-break. No trades means no evidence: lowest score."""
    if metrics.trades == 0:
        return (float("-inf"), float("-inf"))
    return (metrics.sharpe, metrics.annual_return)


# --- worker plumbing --------------------------------------------------------------------------
_WORKER: dict[str, Any] = {}


def _init_worker(
    dataset: Any,
    params: StrategyParams,
    fees: FeeSchedule,
    settings: BacktestSettings,
    factory: EngineFactory,
) -> None:
    _WORKER["dataset"] = dataset
    _WORKER["params"] = params
    _WORKER["fees"] = fees
    _WORKER["settings"] = settings
    _WORKER["factory"] = factory


def _run_candidate(task: tuple[Overrides, int, int]) -> tuple[Overrides, Metrics]:
    overrides, start, end = task
    params = _WORKER["params"]
    assert isinstance(params, StrategyParams)
    tuned = params.with_overrides(**overrides)
    factory: EngineFactory = _WORKER["factory"]
    engine = factory(_WORKER["dataset"], tuned, _WORKER["fees"], _WORKER["settings"])
    return overrides, compute_metrics(engine.run(start, end))


def _evaluate_grid(
    dataset: Any,
    params: StrategyParams,
    fees: FeeSchedule,
    settings: BacktestSettings,
    factory: EngineFactory,
    candidates: list[Overrides],
    start: int,
    end: int,
    jobs: int,
) -> list[tuple[Overrides, Metrics]]:
    tasks = [(c, start, end) for c in candidates]
    if jobs <= 1 or len(tasks) <= 1:
        _init_worker(dataset, params, fees, settings, factory)
        return [_run_candidate(t) for t in tasks]
    with ProcessPoolExecutor(
        max_workers=jobs,
        initializer=_init_worker,
        initargs=(dataset, params, fees, settings, factory),
    ) as pool:
        return list(pool.map(_run_candidate, tasks))


def spot_perp_engine(
    dataset: Any, params: StrategyParams, fees: FeeSchedule, settings: BacktestSettings
) -> Runner:
    return BacktestEngine(dataset, params, fees, settings)


# --- main entry -----------------------------------------------------------------------------
def run_walk_forward(
    dataset: Dataset,
    params: StrategyParams,
    fees: FeeSchedule,
    settings: BacktestSettings,
    *,
    jobs: int = 1,
    start_idx: int = 0,
    end_idx: int | None = None,
    factory: EngineFactory = spot_perp_engine,
) -> WalkForwardResult:
    end = len(dataset) if end_idx is None else min(end_idx, len(dataset))
    index = dataset.index_ms[start_idx:end]
    wf = settings.walk_forward
    folds = [
        Fold(
            f.number,
            f.train_start + start_idx,
            f.train_end + start_idx,
            f.test_start + start_idx,
            f.test_end + start_idx,
        )
        for f in make_folds(index, wf.train_months, wf.test_months)
    ]
    candidates = grid_candidates(wf.grid.model_dump())
    out = WalkForwardResult(grid_size=len(candidates))
    if not folds:
        out.skipped_reason = (
            f"less than {wf.train_months + wf.test_months} months of data, "
            "walk-forward is not possible"
        )
        return out

    chained: list[pd.Series] = []
    level = settings.initial_capital_usd
    all_trades: list[TradeRecord] = []
    totals = {"hard_stops": 0, "margin_topups": 0, "reductions": 0, "rejected": 0, "bars": 0}
    for fold in folds:
        scored = _evaluate_grid(
            dataset, params, fees, settings, factory, candidates,
            fold.train_start, fold.train_end, jobs,
        )  # fmt: skip
        best_overrides, best_train = max(scored, key=lambda item: score(item[1]))
        note = ""
        if best_train.trades == 0:
            best_overrides, note = (
                {},
                "no grid candidate traded in training, defaults used",
            )
            best_train = compute_metrics(
                factory(dataset, params, fees, settings).run(fold.train_start, fold.train_end)
            )
        tuned = params.with_overrides(**best_overrides)
        test = factory(dataset, tuned, fees, settings).run(fold.test_start, fold.test_end)
        test_metrics = compute_metrics(test)
        out.folds.append(
            FoldReport(
                fold,
                best_overrides,
                best_train,
                test_metrics,
                test.equity,
                test.trades,
                len(candidates),
                note,
            )
        )
        if not test.equity.empty:
            factor = level / float(test.equity.iloc[0])
            chained.append(test.equity * factor)
            level = float(chained[-1].iloc[-1])
        all_trades.extend(test.trades)
        totals["hard_stops"] += test.hard_stops
        totals["margin_topups"] += test.margin_topups
        totals["reductions"] += test.reductions
        totals["rejected"] += test.rejected_entries
        totals["bars"] += test.bars_in_market

    if chained:
        out.oos_equity = pd.concat(chained)
        oos = BacktestResult(
            params,
            settings.initial_capital_usd,
            out.oos_equity,
            trades=all_trades,
            hard_stops=totals["hard_stops"],
            margin_topups=totals["margin_topups"],
            reductions=totals["reductions"],
            bars_in_market=totals["bars"],
            rejected_entries=totals["rejected"],
        )
        out.oos_metrics = compute_metrics(oos)
    return out
