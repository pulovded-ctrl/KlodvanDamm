from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from fundarb.exchanges.binance_vision import (
    FUNDING_PREFIX,
    PERP_KLINES_PREFIX,
    SPOT_KLINES_PREFIX,
    BinanceVisionAdapter,
    months_between,
    parse_funding_csv,
    parse_klines_csv,
    previous_month_key,
)
from fundarb.marketdata.history import HistorySync
from fundarb.marketdata.store import ParquetStore

HOUR = 3_600_000
NOW = datetime(2025, 3, 10, tzinfo=UTC)  # latest closed month: 2025-02


def zip_csv(rows: list[list[object]], header: list[str] | None = None) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    if header:
        writer.writerow(header)
    writer.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.csv", buf.getvalue())
    return out.getvalue()


def listing_xml(prefix: str, names: list[str], truncated: bool = False) -> str:
    items = "".join(
        f"<CommonPrefixes><Prefix>{prefix}{n}/</Prefix></CommonPrefixes>" for n in names
    )
    trunc = "true" if truncated else "false"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Prefix>{prefix}</Prefix><IsTruncated>{trunc}</IsTruncated>{items}</ListBucketResult>"
    )


def month_start_ms(key: str) -> int:
    year, month = (int(x) for x in key.split("-"))
    return int(datetime(year, month, 1, tzinfo=UTC).timestamp() * 1000)


class Archive:
    """Fake data.binance.vision: BTC (8h funding) and ETH (4h funding) from 2025-01, AAA no spot."""

    def __init__(self) -> None:
        self.hits: list[str] = []

    def funding_rows(self, symbol: str, key: str) -> list[list[object]]:
        interval = 4 if symbol.startswith("ETH") else 8
        start, end = month_start_ms(key), month_start_ms(next_month(key))
        return [[ts, interval, 0.0001] for ts in range(start, end, interval * HOUR)]

    def kline_rows(self, kind: str, key: str) -> list[list[object]]:
        start, end = month_start_ms(key), month_start_ms(next_month(key))
        scale = 1000 if kind == "spot" else 1  # spot archives use microseconds
        rows: list[list[object]] = []
        for ts in range(start, end, HOUR):
            rows.append(
                [ts * scale, 100, 101, 99, 100, 10, (ts + HOUR - 1) * scale, 1000, 5, 5, 500, 0]
            )
        return rows

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits.append(url)
        if "amazonaws.com" in url:
            prefix = request.url.params["prefix"]
            if prefix == FUNDING_PREFIX:
                return httpx.Response(
                    200, text=listing_xml(prefix, ["AAAUSDT", "BTCUSDT", "ETHUSDT", "XYZUSDC"])
                )
            if prefix == SPOT_KLINES_PREFIX:
                marker = request.url.params.get("marker")
                if marker is None:
                    return httpx.Response(
                        200, text=listing_xml(prefix, ["BTCUSDT"], truncated=True)
                    )
                return httpx.Response(200, text=listing_xml(prefix, ["ETHUSDT", "XYZUSDT"]))
            return httpx.Response(200, text=listing_xml(prefix, []))
        path = request.url.path.lstrip("/")
        name = path.rsplit("/", 1)[-1].removesuffix(".zip")
        symbol, _, key = name.split("-", 2)[0], None, name[-7:]
        if key < "2025-01" or key > "2025-02":
            return httpx.Response(404)
        if path.startswith(FUNDING_PREFIX):
            if symbol == "AAAUSDT":
                return httpx.Response(404)
            header = ["calc_time", "funding_interval_hours", "last_funding_rate"]
            return httpx.Response(200, content=zip_csv(self.funding_rows(symbol, key), header))
        if path.startswith(SPOT_KLINES_PREFIX):
            return httpx.Response(200, content=zip_csv(self.kline_rows("spot", key)))
        if path.startswith(PERP_KLINES_PREFIX):
            header = [
                "open_time", "open", "high", "low", "close", "volume", "close_time",
                "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
            ]  # fmt: skip
            return httpx.Response(200, content=zip_csv(self.kline_rows("perp", key), header))
        return httpx.Response(404)


def next_month(key: str) -> str:
    year, month = (int(x) for x in key.split("-"))
    month += 1
    if month > 12:
        month, year = 1, year + 1
    return f"{year:04d}-{month:02d}"


def test_helpers() -> None:
    assert months_between(month_start_ms("2024-11"), month_start_ms("2025-02") + 5) == [
        "2024-11", "2024-12", "2025-01", "2025-02",
    ]  # fmt: skip
    assert months_between(10, 5) == []
    assert previous_month_key(datetime(2025, 1, 15, tzinfo=UTC)) == "2024-12"
    rows = parse_funding_csv(
        "calc_time,funding_interval_hours,last_funding_rate\n1704067200000,8,0.0003\n"
    )
    assert rows == [(1704067200000, 8.0, 0.0003)]
    candles = parse_klines_csv("1735689600000000,1,2,0.5,1.5,7,1735693199999999,10,1,1,1,0\n")
    assert candles[0].ts_ms == 1735689600000 and candles[0].close == 1.5 and candles[0].volume == 7


@pytest.mark.asyncio
async def test_instruments_volumes_and_history(tmp_path: Path) -> None:
    archive = Archive()
    adapter = BinanceVisionAdapter(
        tmp_path / "raw", transport=httpx.MockTransport(archive.handler), now=NOW, concurrency=3
    )
    await adapter.connect()
    try:
        rules = await adapter.load_instruments()
        assert sorted(rules) == ["BTC", "ETH"]  # AAA has no funding file, XYZ has no USDT perp
        assert rules["BTC"].funding_interval_hours == 8.0
        assert rules["ETH"].funding_interval_hours == 4.0
        volumes = await adapter.fetch_24h_volumes()
        assert volumes["BTC"][0] == pytest.approx(
            1000 * 24
        )  # quote volume per day (28 days in Feb)
        assert volumes["ETH"][1] == pytest.approx(1000 * 24)

        since, until = month_start_ms("2024-12"), month_start_ms("2025-03") + 10 * HOUR
        funding = await adapter.fetch_funding_history(rules["ETH"], since, until, limit=5)
        assert len(funding) == (31 + 28) * 6  # 4h funding, two months, limit ignored on purpose
        assert funding[0].ts_ms == month_start_ms("2025-01")
        spot = await adapter.fetch_ohlcv("BTC/USDT", "1h", since, until, limit=10)
        perp = await adapter.fetch_ohlcv("BTC/USDT:USDT", "1h", since, until, limit=10)
        assert len(spot) == len(perp) == (31 + 28) * 24
        assert spot[0].ts_ms == perp[0].ts_ms == month_start_ms("2025-01")
        with pytest.raises(ValueError):
            await adapter.fetch_ohlcv("BTC/USDT", "1m", since, until, limit=10)

        # cache: a second identical fetch only re-asks for the still-open month (2025-03)
        hits_before = len(archive.hits)
        await adapter.fetch_ohlcv("BTC/USDT", "1h", since, until, limit=10)
        assert len(archive.hits) == hits_before + 1
        assert archive.hits[-1].endswith("2025-03.zip")
        # 404 for a closed month is remembered, for the open month it is retried
        assert any(p.name.endswith(".missing") for p in (tmp_path / "raw").rglob("*.missing"))
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_history_sync_with_archive(tmp_path: Path) -> None:
    archive = Archive()
    adapter = BinanceVisionAdapter(
        tmp_path / "raw", transport=httpx.MockTransport(archive.handler), now=NOW
    )
    await adapter.connect()
    try:
        store = ParquetStore(tmp_path / "data", "binance_vision")
        sync = HistorySync(
            adapter,
            store,
            start=datetime(2024, 12, 1, tzinfo=UTC),
            now_ms=lambda: int(NOW.timestamp() * 1000),
        )
        result = await sync.sync_all()
        assert result.ok, result.errors
        assert result.bases == ["BTC", "ETH"]
        assert result.funding_rows["BTC"] == (31 + 28) * 3
        assert result.spot_rows["ETH"] == (31 + 28) * 24
        again = await sync.sync_all(rules_by_base=store.load_instruments())
        assert again.funding_rows["BTC"] == 0 and again.perp_rows["BTC"] == 0
    finally:
        await adapter.close()
