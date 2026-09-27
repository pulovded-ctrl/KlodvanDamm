from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fundarb.core.models import FundingRecord
from fundarb.marketdata.history import HistorySync
from fundarb.marketdata.store import ParquetStore
from helpers import HOUR_MS, FakeAdapter, make_rules, ms


def test_store_merges_and_dedupes(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path, "fake")
    a = [FundingRecord(ts_ms=1, rate=0.1), FundingRecord(ts_ms=2, rate=0.2)]
    b = [FundingRecord(ts_ms=2, rate=0.25), FundingRecord(ts_ms=3, rate=0.3)]
    assert store.append_funding("BTC", a) == 2
    assert store.append_funding("BTC", b) == 1
    df = store.read_funding("BTC")
    assert df["ts_ms"].tolist() == [1, 2, 3]
    assert df["rate"].tolist() == [0.1, 0.25, 0.3]
    assert store.last_funding_ts("BTC") == 3
    assert store.last_funding_ts("ETH") is None


def test_instruments_roundtrip(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path, "fake")
    rules = {"BTC": make_rules("BTC"), "ETH": make_rules("ETH", funding_interval_hours=4.0)}
    store.save_instruments(rules)
    assert store.load_instruments() == rules


@pytest.mark.asyncio
async def test_full_then_incremental_sync(tmp_path: Path, start_dt: datetime) -> None:
    rules = {"BTC": make_rules("BTC")}
    now = start_dt + timedelta(days=100, hours=0, minutes=30)  # mid-hour: last candle incomplete
    adapter = FakeAdapter(rules, start_ms=ms(start_dt), end_ms=ms(now))
    store = ParquetStore(tmp_path, "fake")
    sync = HistorySync(
        adapter, store, start=start_dt, now_ms=lambda: ms(now), funding_page=50, ohlcv_page=100
    )
    result = await sync.sync_all()
    assert result.ok
    expected_funding = 100 * 3 + 1  # every 8h from start inclusive
    assert result.funding_rows["BTC"] == expected_funding
    expected_candles = 100 * 24  # hourly, last closed candle is at now - 1h..; started at start
    assert result.spot_rows["BTC"] == expected_candles
    assert result.perp_rows["BTC"] == expected_candles
    funding_calls = [c for c in adapter.calls if c[0] == "funding"]
    assert len(funding_calls) == 8  # 301 rows / 50 per page -> 7 pages plus one empty page
    # the current, still-open hour must not be stored
    last_ts = store.last_ohlcv_ts("spot", "BTC")
    assert last_ts is not None
    assert last_ts + HOUR_MS <= ms(now)

    # nothing new: no rows added
    adapter.calls.clear()
    again = await sync.sync_all(rules_by_base=rules)
    assert again.funding_rows["BTC"] == 0
    assert again.spot_rows["BTC"] == 0

    # time moves 1 day: exactly the new rows are added
    later = now + timedelta(days=1)
    adapter.end_ms = ms(later)
    sync2 = HistorySync(adapter, store, start=start_dt, now_ms=lambda: ms(later))
    third = await sync2.sync_all(rules_by_base=rules)
    assert third.funding_rows["BTC"] == 3
    assert third.spot_rows["BTC"] == 24
    df = store.read_ohlcv("perp", "BTC")
    assert df["ts_ms"].is_monotonic_increasing
    assert df["ts_ms"].is_unique


@pytest.mark.asyncio
async def test_universe_selection_ranks_by_min_volume(tmp_path: Path, start_dt: datetime) -> None:
    rules = {b: make_rules(b) for b in ("AAA", "BBB", "CCC", "DDD")}
    volumes = {
        "AAA": (5e7, 9e9),  # min 5e7
        "BBB": (3e8, 2e8),  # min 2e8
        "CCC": (1e7, 1e7),  # below min volume filter
        "DDD": (1e9, 1e9),
    }
    adapter = FakeAdapter(rules, start_ms=ms(start_dt), end_ms=ms(start_dt), volumes=volumes)
    store = ParquetStore(tmp_path, "fake")
    sync = HistorySync(adapter, store, start=start_dt, max_symbols=2, min_volume_usd=2e7)
    chosen = await sync.select_universe()
    assert list(chosen) == ["DDD", "BBB"]
    assert set(store.load_instruments()) == {"DDD", "BBB"}
    assert store.load_meta()["bases"] == ["BBB", "DDD"]


@pytest.mark.asyncio
async def test_one_failing_symbol_does_not_stop_others(tmp_path: Path, start_dt: datetime) -> None:
    rules = {"BTC": make_rules("BTC"), "ETH": make_rules("ETH")}
    now = start_dt + timedelta(days=2)
    adapter = FakeAdapter(
        rules, start_ms=ms(start_dt), end_ms=ms(now), fail_bases=frozenset({"ETH"})
    )
    store = ParquetStore(tmp_path, "fake")
    sync = HistorySync(adapter, store, start=start_dt, now_ms=lambda: ms(now))
    result = await sync.sync_all()
    assert not result.ok
    assert "ETH" in result.errors
    assert result.funding_rows["BTC"] == 7
    assert store.load_meta()["last_sync_errors"] == result.errors


def test_now_is_utc_aware() -> None:
    assert datetime.now(UTC).tzinfo is not None
