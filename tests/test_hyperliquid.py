from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from fundarb.exchanges.hyperliquid_info import HyperliquidInfoAdapter
from fundarb.marketdata.history import HistorySync
from fundarb.marketdata.store import ParquetStore

HOUR = 3_600_000
START = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
NOW = datetime(2025, 2, 1, 0, 30, tzinfo=UTC)


class FakeInfo:
    """Hourly funding for BTC and kPEPE since START, 500 rows per page, recent candles only."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        kind = payload["type"]
        self.calls.append(kind)
        if kind == "meta":
            return httpx.Response(
                200,
                json={
                    "universe": [
                        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                        {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10},
                        {"name": "OLD", "szDecimals": 1, "isDelisted": True},
                    ]
                },
            )
        if kind == "metaAndAssetCtxs":
            universe = {
                "universe": [
                    {"name": "BTC"},
                    {"name": "kPEPE"},
                    {"name": "OLD", "isDelisted": True},
                ]
            }
            ctxs = [
                {"funding": "0.0000125", "dayNtlVlm": "1000000000"},
                {"funding": "-0.0001", "dayNtlVlm": "5000000"},
                {"funding": "0", "dayNtlVlm": "0"},
            ]
            return httpx.Response(200, json=[universe, ctxs])
        if kind == "fundingHistory":
            start, end = int(payload["startTime"]), int(payload.get("endTime", 10**15))
            rows = []
            t = ((start + HOUR - 1) // HOUR) * HOUR
            while t <= end and len(rows) < 500 and t < int(NOW.timestamp() * 1000):
                rows.append(
                    {
                        "coin": payload["coin"],
                        "fundingRate": "0.0000125",
                        "premium": "0",
                        "time": t + 151,
                    }
                )
                t += HOUR
            return httpx.Response(200, json=rows)
        if kind == "candleSnapshot":
            req = payload["req"]
            start, end = int(req["startTime"]), int(req["endTime"])
            recent_from = int(NOW.timestamp() * 1000) - 100 * HOUR  # only the last 100 hours exist
            rows = []
            t = max(((start + HOUR - 1) // HOUR) * HOUR, recent_from)
            while t <= end and t + HOUR <= int(NOW.timestamp() * 1000):
                rows.append(
                    {
                        "t": t,
                        "T": t + HOUR - 1,
                        "s": req["coin"],
                        "i": "1h",
                        "o": "1",
                        "c": "2",
                        "h": "3",
                        "l": "0.5",
                        "v": "10",
                        "n": 1,
                    }
                )
                t += HOUR
            return httpx.Response(200, json=rows)
        return httpx.Response(400)


@pytest.mark.asyncio
async def test_instruments_volumes_funding_and_candles(tmp_path: Path) -> None:
    fake = FakeInfo()
    adapter = HyperliquidInfoAdapter(
        transport=httpx.MockTransport(fake.handler), min_interval_sec=0.0, now=NOW
    )
    await adapter.connect()
    try:
        rules = await adapter.load_instruments()
        assert sorted(rules) == ["BTC", "kPEPE"]  # delisted asset dropped
        assert rules["BTC"].spot_symbol == "" and rules["BTC"].funding_interval_hours == 1.0
        assert rules["BTC"].perp_amount_step == pytest.approx(1e-5)
        volumes = await adapter.fetch_24h_volumes()
        assert volumes["BTC"] == (1e9, 1e9)
        page = await adapter.fetch_funding_history(rules["BTC"], START, START + 10**12, 500)
        assert len(page) == 500 and page[0].ts_ms == START + 151
        assert page[0].rate == pytest.approx(0.0000125)
        info = await adapter.fetch_funding_info(rules["kPEPE"])
        assert info.predicted_rate == pytest.approx(-0.0001) and info.interval_hours == 1.0
        old = await adapter.fetch_ohlcv("BTC/USDC:USDC", "1h", START, START + 48 * HOUR, 5000)
        assert old == []  # history beyond the recent window is not served
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_sync_perp_only_venue(tmp_path: Path) -> None:
    fake = FakeInfo()
    adapter = HyperliquidInfoAdapter(
        transport=httpx.MockTransport(fake.handler), min_interval_sec=0.0, now=NOW
    )
    await adapter.connect()
    try:
        store = ParquetStore(tmp_path, "hyperliquid")
        sync = HistorySync(
            adapter,
            store,
            start=datetime(2025, 1, 1, tzinfo=UTC),
            now_ms=lambda: int(NOW.timestamp() * 1000),
            funding_page=500,
        )
        result = await sync.sync_all(bases=["BTC"])
        assert result.ok, result.errors
        assert result.bases == ["BTC"]
        assert (
            result.funding_rows["BTC"] == 31 * 24 + 1
        )  # hourly January plus the Feb 1 00:00 settlement
        assert result.spot_rows["BTC"] == 0  # no spot leg on this venue
        assert result.perp_rows["BTC"] == 99  # only the recent window exists (99 closed candles)
        assert fake.calls.count("fundingHistory") == 3  # 745 rows: 500 + 245 + one empty page
        df = store.read_funding("BTC")
        assert df["ts_ms"].is_unique and len(df) == 745
    finally:
        await adapter.close()
