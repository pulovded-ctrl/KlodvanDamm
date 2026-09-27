"""Cross-venue pair dataset: the same coin's perpetual on two venues, both directions.

For direction ``A>B`` the bot is long the perp on venue A and short the perp on venue B.
The strategy sees the pair as one instrument whose "funding" is the net spread per period:
short-venue funding minus long-venue funding, summed over ``period_hours``. Prices for both
legs come from one venue (``price_venue``): basis between venues is not modelled, only the
spread, slippage and fees of each venue.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from fundarb.backtest.data import (
    CoinCoverage,
    DataCoverage,
    FloatArray,
    IntArray,
    align_candles,
)
from fundarb.core.config import PairSettings
from fundarb.core.models import InstrumentRules
from fundarb.marketdata.history import TIMEFRAME_MS
from fundarb.marketdata.store import ParquetStore

HOUR_MS = 3_600_000


@dataclass(slots=True)
class PairSeries:
    key: str
    coin: str
    long_venue: str
    short_venue: str
    rules: InstrumentRules
    price: FloatArray
    long_vol_usd: FloatArray  # per-bar volume proxy on the long venue
    short_vol_usd: FloatArray
    long_rate_at: FloatArray  # funding settled at bar i on the long venue, NaN if none
    short_rate_at: FloatArray
    funding_rates: FloatArray  # net spread (short minus long) per completed period
    funding_gap_hours: FloatArray  # constant: period_hours
    funding_count_at: IntArray  # completed periods up to and including bar i

    def history_upto(self, i: int, span: int) -> FloatArray:
        count = int(self.funding_count_at[i])
        return self.funding_rates[max(0, count - span) : count]

    def rules_at(self, i: int, span: int) -> InstrumentRules:
        return self.rules


@dataclass(slots=True)
class PairDataset:
    index_ms: IntArray
    tf_ms: int
    period_hours: int
    venues: tuple[str, str]
    pairs: dict[str, PairSeries] = field(default_factory=dict)
    coverage: DataCoverage = field(default_factory=DataCoverage)

    @property
    def coins(self) -> Mapping[str, PairSeries]:
        return self.pairs

    def __len__(self) -> int:
        return int(self.index_ms.shape[0])

    def ts(self, i: int) -> datetime:
        return datetime.fromtimestamp(int(self.index_ms[i]) / 1000, tz=UTC)

    def index_of(self, when: datetime) -> int:
        target = int(when.timestamp() * 1000)
        return int(np.searchsorted(self.index_ms, target, side="left"))


def pair_key(coin: str, long_venue: str, short_venue: str) -> str:
    return f"{coin}:{long_venue}>{short_venue}"


def _rate_at(index_ms: IntArray, ts: IntArray, rates: FloatArray, tf_ms: int) -> FloatArray:
    """Settlement rates mapped onto bars (timestamps floored to the bar)."""
    n = len(index_ms)
    out = np.full(n, np.nan)
    if len(ts) == 0:
        return out
    pos = (ts - int(index_ms[0])) // tf_ms
    keep = (pos >= 0) & (pos < n)
    for p_, r in zip(pos[keep], rates[keep], strict=True):
        out[p_] = r if np.isnan(out[p_]) else out[p_] + r  # two settlements in one bar add up
    return out


def _period_sums(ts: IntArray, rates: FloatArray, period_ms: int) -> dict[int, float]:
    """Sum of rates per period id. A settlement on a boundary belongs to the period ending there."""
    sums: dict[int, float] = {}
    for t, r in zip(ts, rates, strict=True):
        pid = int((int(t) - 1) // period_ms)
        sums[pid] = sums.get(pid, 0.0) + float(r)
    return sums


def _volume_proxy(index_ms: IntArray, store: ParquetStore, base: str, tf_ms: int) -> FloatArray:
    """Per-bar USD volume from the venue's perp candles where they exist, else a constant
    from the sync-time 24h volume so the strategy's depth caps still have something to use."""
    n = len(index_ms)
    out = np.zeros(n)
    candles = store.read_ohlcv("perp", base)
    if not candles.empty:
        _, vol = align_candles(index_ms, candles, tf_ms)
        out = vol
    meta_vols = store.load_meta().get("volumes_24h_usd")
    constant = 0.0
    if isinstance(meta_vols, dict) and base in meta_vols:
        pair = meta_vols[base]
        if isinstance(pair, list) and len(pair) == 2:
            constant = float(pair[1]) / (86_400_000 / tf_ms)
    if constant > 0:
        out = np.where(out > 0, out, constant)
    return out


def build_pair_dataset(
    stores: Mapping[str, ParquetStore], pair: PairSettings, *, min_bars: int = 24 * 30
) -> PairDataset:
    a, b = pair.venues
    tf_ms = TIMEFRAME_MS[stores[a].timeframe]
    period_ms = pair.period_hours * HOUR_MS
    rules_a, rules_b = stores[a].load_instruments(), stores[b].load_instruments()
    coins = sorted(set(rules_a) & set(rules_b))
    coverage = DataCoverage(
        source={
            "exchange": f"{a} + {b}",
            "timeframe": stores[a].timeframe,
            "universe_rule": "coins present on both venues",
            "price_venue": pair.price_venue,
            "last_sync_at": f"{stores[a].load_meta().get('last_sync_at')} / "
            f"{stores[b].load_meta().get('last_sync_at')}",
        }
    )
    price_store = stores[pair.price_venue]
    # master index from the price venue's perp candles
    lo: int | None = None
    hi: int | None = None
    prices: dict[str, pd.DataFrame] = {}
    for coin in coins:
        df = price_store.read_ohlcv("perp", coin)
        if df.empty:
            coverage.coins.append(CoinCoverage(coin, None, None, 0, 0, 0, float(pair.period_hours)))
            coverage.warnings.append(f"{coin}: no price candles on {pair.price_venue}, excluded")
            continue
        first, last = int(df["ts_ms"].iloc[0]), int(df["ts_ms"].iloc[-1])
        lo = first if lo is None else min(lo, first)
        hi = last if hi is None else max(hi, last)
        prices[coin] = df
    if lo is None or hi is None:
        return PairDataset(
            np.zeros(0, dtype=np.int64), tf_ms, pair.period_hours, (a, b), {}, coverage
        )
    lo = (lo // tf_ms) * tf_ms
    index_ms = np.arange(lo, hi + tf_ms, tf_ms, dtype=np.int64)
    dataset = PairDataset(index_ms, tf_ms, pair.period_hours, (a, b), {}, coverage)
    for coin, df in prices.items():
        price, _ = align_candles(index_ms, df, tf_ms)
        valid = ~np.isnan(price)
        bars = int(valid.sum())
        fund_a, fund_b = stores[a].read_funding(coin), stores[b].read_funding(coin)
        if bars < min_bars or fund_a.empty or fund_b.empty:
            coverage.coins.append(
                CoinCoverage(
                    coin, None, None, bars, 0, len(fund_a) + len(fund_b), float(pair.period_hours)
                )
            )
            reason = "too few price bars" if bars < min_bars else "funding missing on one venue"
            coverage.warnings.append(f"{coin}: {reason}, excluded")
            continue
        ts_a = fund_a["ts_ms"].to_numpy(dtype=np.int64)
        ts_b = fund_b["ts_ms"].to_numpy(dtype=np.int64)
        r_a = fund_a["rate"].to_numpy(dtype=np.float64)
        r_b = fund_b["rate"].to_numpy(dtype=np.float64)
        rate_at_a = _rate_at(index_ms, ts_a, r_a, tf_ms)
        rate_at_b = _rate_at(index_ms, ts_b, r_b, tf_ms)
        sums_a, sums_b = _period_sums(ts_a, r_a, period_ms), _period_sums(ts_b, r_b, period_ms)
        last_period = int((int(index_ms[-1]) + tf_ms - 1) // period_ms)  # completed by the end
        valid_pids = sorted(
            pid
            for pid in set(sums_a) & set(sums_b)
            if (pid + 1) * period_ms <= (last_period + 1) * period_ms
        )
        valid_pids = [
            pid for pid in valid_pids if (pid + 1) * period_ms <= int(index_ms[-1]) + tf_ms
        ]
        spread_ab = np.array([sums_b[pid] - sums_a[pid] for pid in valid_pids], dtype=np.float64)
        period_end = np.array([(pid + 1) * period_ms for pid in valid_pids], dtype=np.int64)
        count_at = np.searchsorted(period_end, index_ms, side="right").astype(np.int64)
        gaps = np.full(len(valid_pids), float(pair.period_hours))
        vol_a = _volume_proxy(index_ms, stores[a], coin, tf_ms)
        vol_b = _volume_proxy(index_ms, stores[b], coin, tf_ms)
        vidx = np.flatnonzero(valid)
        coverage.coins.append(
            CoinCoverage(
                base=coin,
                first_ts=datetime.fromtimestamp(int(index_ms[vidx[0]]) / 1000, tz=UTC),
                last_ts=datetime.fromtimestamp(int(index_ms[vidx[-1]]) / 1000, tz=UTC),
                bars=bars,
                missing_bars=int(vidx[-1] - vidx[0] + 1) - bars,
                funding_events=len(valid_pids),
                funding_interval_hours=float(pair.period_hours),
            )
        )
        if len(valid_pids) < 30:
            coverage.warnings.append(
                f"{coin}: only {len(valid_pids)} periods with funding on both venues"
            )
        for long_v, short_v, spread, rate_long, rate_short, vol_l, vol_s in (
            (a, b, spread_ab, rate_at_a, rate_at_b, vol_a, vol_b),
            (b, a, -spread_ab, rate_at_b, rate_at_a, vol_b, vol_a),
        ):
            key = pair_key(coin, long_v, short_v)
            ra, rb = rules_a[coin], rules_b[coin]
            rules = InstrumentRules(
                base=key,
                spot_symbol=(ra if long_v == a else rb).perp_symbol,
                perp_symbol=(rb if short_v == b else ra).perp_symbol,
                spot_amount_step=max(ra.perp_amount_step, rb.perp_amount_step),
                spot_min_amount=max(ra.perp_min_amount, rb.perp_min_amount),
                spot_min_notional_usd=max(ra.perp_min_notional_usd, rb.perp_min_notional_usd),
                perp_amount_step=max(ra.perp_amount_step, rb.perp_amount_step),
                perp_min_amount=max(ra.perp_min_amount, rb.perp_min_amount),
                perp_min_notional_usd=max(ra.perp_min_notional_usd, rb.perp_min_notional_usd),
                spot_price_tick=0.0,
                perp_price_tick=0.0,
                funding_interval_hours=float(pair.period_hours),
            )
            dataset.pairs[key] = PairSeries(
                key=key,
                coin=coin,
                long_venue=long_v,
                short_venue=short_v,
                rules=rules,
                price=price,
                long_vol_usd=vol_l,
                short_vol_usd=vol_s,
                long_rate_at=rate_long,
                short_rate_at=rate_short,
                funding_rates=spread,
                funding_gap_hours=gaps,
                funding_count_at=count_at,
            )
    return dataset
