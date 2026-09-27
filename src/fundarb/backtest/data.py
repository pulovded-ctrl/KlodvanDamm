"""Turns stored history into aligned numpy arrays on one hourly index, with coverage checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

import numpy as np
import numpy.typing as npt
import pandas as pd

from fundarb.core.models import HOURS_PER_YEAR, InstrumentRules
from fundarb.marketdata.history import TIMEFRAME_MS
from fundarb.marketdata.store import ParquetStore

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]


@dataclass(slots=True)
class CoinSeries:
    rules: InstrumentRules
    spot_close: FloatArray  # NaN where missing
    perp_close: FloatArray
    spot_vol_usd: FloatArray  # per-bar volume in USD, 0 where missing
    perp_vol_usd: FloatArray
    funding_rate_at: FloatArray  # NaN except at bars where funding settles
    funding_rates: FloatArray  # settled rates in time order
    funding_count_at: IntArray  # number of settled fundings up to and including bar i
    funding_gap_hours: FloatArray  # hours since the previous settlement, per settlement

    def history_upto(self, i: int, span: int) -> FloatArray:
        count = int(self.funding_count_at[i])
        return self.funding_rates[max(0, count - span) : count]

    def interval_upto(self, i: int, span: int) -> float:
        """Funding interval in force at bar i: median gap of the last ``span`` settlements."""
        count = int(self.funding_count_at[i])
        if count == 0:
            return self.rules.funding_interval_hours
        gaps = self.funding_gap_hours[max(0, count - span) : count]
        return float(np.median(gaps))

    def rules_at(self, i: int, span: int) -> InstrumentRules:
        interval = self.interval_upto(i, span)
        if interval == self.rules.funding_interval_hours:
            return self.rules
        return replace(self.rules, funding_interval_hours=interval)


@dataclass(slots=True)
class CoinCoverage:
    base: str
    first_ts: datetime | None
    last_ts: datetime | None
    bars: int
    missing_bars: int
    funding_events: int
    funding_interval_hours: float


@dataclass(slots=True)
class DataCoverage:
    coins: list[CoinCoverage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source: dict[str, object] = field(default_factory=dict)

    @property
    def usable_bases(self) -> list[str]:
        return [c.base for c in self.coins if c.bars > 0 and c.funding_events > 0]


@dataclass(slots=True)
class BacktestDataset:
    index_ms: IntArray
    tf_ms: int
    coins: dict[str, CoinSeries]
    coverage: DataCoverage

    def __len__(self) -> int:
        return int(self.index_ms.shape[0])

    def ts(self, i: int) -> datetime:
        return datetime.fromtimestamp(int(self.index_ms[i]) / 1000, tz=UTC)

    def index_of(self, when: datetime) -> int:
        """First bar at or after ``when`` (clamped to the index)."""
        target = int(when.timestamp() * 1000)
        return int(np.searchsorted(self.index_ms, target, side="left"))


def _rolling_sum(values: FloatArray, window: int) -> FloatArray:
    """Trailing sum over ``window`` bars including the current one; NaN treated as 0."""
    clean = np.nan_to_num(values, nan=0.0)
    csum = np.concatenate(([0.0], np.cumsum(clean)))
    idx = np.arange(1, len(clean) + 1)
    lo = np.maximum(idx - window, 0)
    return np.asarray(csum[idx] - csum[lo], dtype=np.float64)


def build_dataset(
    store: ParquetStore,
    bases: Sequence[str] | None = None,
    *,
    min_bars: int = 24 * 30,
) -> BacktestDataset:
    tf_ms = TIMEFRAME_MS[store.timeframe]
    rules_by_base = store.load_instruments()
    chosen = [b for b in sorted(rules_by_base) if bases is None or b in bases]
    coverage = DataCoverage(source=store.load_meta())
    raw: dict[str, tuple[InstrumentRules, pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}
    lo, hi = None, None
    for base in chosen:
        spot = store.read_ohlcv("spot", base)
        perp = store.read_ohlcv("perp", base)
        funding = store.read_funding(base)
        if spot.empty or perp.empty:
            coverage.coins.append(CoinCoverage(base, None, None, 0, 0, len(funding), 0.0))
            coverage.warnings.append(f"{base}: no spot or perp candles, coin excluded")
            continue
        first = max(int(spot["ts_ms"].iloc[0]), int(perp["ts_ms"].iloc[0]))
        last = min(int(spot["ts_ms"].iloc[-1]), int(perp["ts_ms"].iloc[-1]))
        lo = first if lo is None else min(lo, first)
        hi = last if hi is None else max(hi, last)
        raw[base] = (rules_by_base[base], spot, perp, funding)
    if lo is None or hi is None:
        return BacktestDataset(np.zeros(0, dtype=np.int64), tf_ms, {}, coverage)
    lo = (lo // tf_ms) * tf_ms
    index_ms = np.arange(lo, hi + tf_ms, tf_ms, dtype=np.int64)
    n = len(index_ms)
    coins: dict[str, CoinSeries] = {}
    for base, (rules, spot, perp, funding) in raw.items():
        spot_close, spot_vol = align_candles(index_ms, spot, tf_ms)
        perp_close, perp_vol = align_candles(index_ms, perp, tf_ms)
        f_ts = funding["ts_ms"].to_numpy(dtype=np.int64)
        f_rate = funding["rate"].to_numpy(dtype=np.float64)
        f_idx = (f_ts - lo) // tf_ms
        keep = (f_idx >= 0) & (f_idx < n)
        f_idx, f_rate = f_idx[keep], f_rate[keep]
        rate_at = np.full(n, np.nan)
        rate_at[f_idx] = f_rate
        count_at = np.cumsum(~np.isnan(rate_at)).astype(np.int64)
        f_ts_kept = f_ts[keep]
        gaps = np.full(len(f_ts_kept), rules.funding_interval_hours, dtype=np.float64)
        if len(f_ts_kept) > 1:
            gaps[1:] = np.diff(f_ts_kept) / 3_600_000.0
            gaps[0] = gaps[1]
        if len(f_ts_kept) >= 10:
            typical = float(np.median(gaps))
            if abs(typical - rules.funding_interval_hours) > 0.25 * rules.funding_interval_hours:
                coverage.warnings.append(
                    f"{base}: funding interval from data is {typical:g}h, "
                    f"reference says {rules.funding_interval_hours:g}h; using the data"
                )
        valid = ~np.isnan(spot_close) & ~np.isnan(perp_close)
        bars = int(valid.sum())
        if bars < min_bars:
            coverage.coins.append(
                CoinCoverage(base, None, None, bars, 0, len(f_rate), rules.funding_interval_hours)
            )
            coverage.warnings.append(
                f"{base}: only {bars} hours of data, below the minimum, excluded"
            )
            continue
        vidx = np.flatnonzero(valid)
        span_bars = int(vidx[-1] - vidx[0] + 1)
        missing = span_bars - bars
        coins[base] = CoinSeries(
            rules=rules,
            spot_close=spot_close,
            perp_close=perp_close,
            spot_vol_usd=spot_vol,
            perp_vol_usd=perp_vol,
            funding_rate_at=rate_at,
            funding_rates=f_rate,
            funding_count_at=count_at,
            funding_gap_hours=gaps,
        )
        coverage.coins.append(
            CoinCoverage(
                base=base,
                first_ts=datetime.fromtimestamp(int(index_ms[vidx[0]]) / 1000, tz=UTC),
                last_ts=datetime.fromtimestamp(int(index_ms[vidx[-1]]) / 1000, tz=UTC),
                bars=bars,
                missing_bars=missing,
                funding_events=len(f_rate),
                funding_interval_hours=rules.funding_interval_hours,
            )
        )
        if missing > 0.02 * span_bars:
            coverage.warnings.append(f"{base}: {missing} of {span_bars} hours missing")
        if len(f_rate) == 0:
            coverage.warnings.append(f"{base}: no funding history")
    return BacktestDataset(index_ms, tf_ms, coins, coverage)


def align_candles(
    index_ms: IntArray, df: pd.DataFrame, tf_ms: int
) -> tuple[FloatArray, FloatArray]:
    ts = df["ts_ms"].to_numpy(dtype=np.int64)
    close = df["close"].to_numpy(dtype=np.float64)
    volume = df["volume"].to_numpy(dtype=np.float64)
    n = len(index_ms)
    pos = (ts - int(index_ms[0])) // tf_ms
    keep = (pos >= 0) & (pos < n) & (ts % tf_ms == 0)
    out_close = np.full(n, np.nan)
    out_vol = np.zeros(n)
    out_close[pos[keep]] = close[keep]
    out_vol[pos[keep]] = volume[keep] * close[keep]
    return out_close, out_vol


def rolling_volume_usd(values: FloatArray, window: int) -> FloatArray:
    return _rolling_sum(values, window)


def coverage_summary(coverage: DataCoverage) -> Mapping[str, object]:
    usable = [c for c in coverage.coins if c.bars > 0 and c.funding_events > 0 and c.first_ts]
    return {
        "coins_total": len(coverage.coins),
        "coins_usable": len(usable),
        "first_ts": min((c.first_ts for c in usable if c.first_ts), default=None),
        "last_ts": max((c.last_ts for c in usable if c.last_ts), default=None),
        "warnings": list(coverage.warnings),
    }


@dataclass(frozen=True, slots=True)
class FundingEnv:
    base: str
    settlements: int
    mean_apr: float
    median_apr: float
    share_above_15: float
    share_above_50: float
    share_negative: float


class HasFunding(Protocol):
    funding_rates: FloatArray
    funding_gap_hours: FloatArray


class HasCoins(Protocol):
    @property
    def coins(self) -> Mapping[str, HasFunding]: ...


def funding_environment(dataset: HasCoins) -> list[FundingEnv]:
    """What the funding market looked like per coin: the context every result must be read in."""
    out: list[FundingEnv] = []
    for base, coin in sorted(dataset.coins.items()):
        if len(coin.funding_rates) == 0:
            continue
        apr = coin.funding_rates * (HOURS_PER_YEAR / np.maximum(coin.funding_gap_hours, 1e-9))
        out.append(
            FundingEnv(
                base=base,
                settlements=len(apr),
                mean_apr=float(apr.mean()),
                median_apr=float(np.median(apr)),
                share_above_15=float(np.mean(apr > 0.15)),
                share_above_50=float(np.mean(apr > 0.50)),
                share_negative=float(np.mean(apr < 0.0)),
            )
        )
    return out
