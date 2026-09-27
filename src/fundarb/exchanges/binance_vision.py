"""History-only adapter over Binance's public archive (data.binance.vision).

Used when the target exchange's API is unreachable (e.g. Bybit blocks the region the
backtest runs from). Monthly zip files: funding rates (with the funding interval per row),
1h klines for spot and USDT-M perpetuals. Files are cached on disk; missing months are 404.
Trading methods are intentionally unsupported: this is a data source, not a venue.
"""

from __future__ import annotations

import asyncio
import csv
import io
import statistics
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import httpx

from fundarb.core.logging import get_logger
from fundarb.core.models import (
    Candle,
    FundingInfo,
    FundingRecord,
    InstrumentRules,
    OrderBook,
    OrderRequest,
    OrderResult,
    PerpPosition,
)
from fundarb.exchanges.base import ExchangeAdapter

log = get_logger(__name__)

ARCHIVE_URL = "https://data.binance.vision"
LISTING_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FUNDING_PREFIX = "data/futures/um/monthly/fundingRate/"
SPOT_KLINES_PREFIX = "data/spot/monthly/klines/"
PERP_KLINES_PREFIX = "data/futures/um/monthly/klines/"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
QUOTE = "USDT"
_NOT_A_VENUE = "binance_vision is a history archive, it cannot trade"


def month_key(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    return f"{dt.year:04d}-{dt.month:02d}"


def months_between(since_ms: int, until_ms: int) -> list[str]:
    """Inclusive list of YYYY-MM keys covering [since_ms, until_ms]."""
    if until_ms < since_ms:
        return []
    start = datetime.fromtimestamp(since_ms / 1000, tz=UTC)
    end = datetime.fromtimestamp(until_ms / 1000, tz=UTC)
    year, month = start.year, start.month
    out: list[str] = []
    while (year, month) <= (end.year, end.month):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


def previous_month_key(now: datetime) -> str:
    year, month = now.year, now.month - 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"


def _normalize_ts(raw: str) -> int:
    """Spot archives switched to microseconds in 2025; funding and futures use milliseconds."""
    value = int(raw)
    return value // 1000 if value > 10**14 else value


def parse_funding_csv(text: str) -> list[tuple[int, float, float]]:
    """Rows of (calc_time_ms, interval_hours, rate)."""
    rows: list[tuple[int, float, float]] = []
    for fields in csv.reader(io.StringIO(text)):
        if len(fields) < 3 or not fields[0].strip().isdigit():
            continue
        rows.append((_normalize_ts(fields[0]), float(fields[1]), float(fields[2])))
    rows.sort(key=lambda r: r[0])
    return rows


def parse_klines_csv(text: str) -> list[Candle]:
    candles: list[Candle] = []
    for fields in csv.reader(io.StringIO(text)):
        if len(fields) < 6 or not fields[0].strip().isdigit():
            continue
        candles.append(
            Candle(
                ts_ms=_normalize_ts(fields[0]),
                open=float(fields[1]),
                high=float(fields[2]),
                low=float(fields[3]),
                close=float(fields[4]),
                volume=float(fields[5]),
            )
        )
    candles.sort(key=lambda c: c.ts_ms)
    return candles


def parse_klines_quote_volume(text: str) -> float:
    total = 0.0
    for fields in csv.reader(io.StringIO(text)):
        if len(fields) < 8 or not fields[0].strip().isdigit():
            continue
        total += float(fields[7])
    return total


def unzip_single(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if not names:
            return ""
        return archive.read(names[0]).decode("utf-8")


def _approx_rules(base: str, interval_hours: float) -> InstrumentRules:
    """Lot rules are not in the archive: rounding is effectively disabled, min notional kept."""
    return InstrumentRules(
        base=base,
        spot_symbol=f"{base}/{QUOTE}",
        perp_symbol=f"{base}/{QUOTE}:{QUOTE}",
        spot_amount_step=1e-8,
        spot_min_amount=0.0,
        spot_min_notional_usd=5.0,
        perp_amount_step=1e-8,
        perp_min_amount=0.0,
        perp_min_notional_usd=5.0,
        spot_price_tick=0.0,
        perp_price_tick=0.0,
        funding_interval_hours=interval_hours,
    )


class BinanceVisionAdapter(ExchangeAdapter):
    name = "binance_vision"

    def __init__(
        self,
        cache_dir: Path,
        *,
        timeframe: str = "1h",
        concurrency: int = 8,
        archive_url: str = ARCHIVE_URL,
        listing_url: str = LISTING_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        now: datetime | None = None,
    ) -> None:
        if timeframe != "1h":
            raise ValueError("binance_vision adapter supports only the 1h timeframe")
        self.cache_dir = Path(cache_dir)
        self.timeframe = timeframe
        self.archive_url = archive_url.rstrip("/")
        self.listing_url = listing_url.rstrip("/")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(concurrency)
        self._now = now
        self._intervals: dict[str, float] = {}

    # --- lifecycle -------------------------------------------------------------------------
    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            trust_env=True,
            timeout=httpx.Timeout(90.0),
            follow_redirects=True,
            transport=self._transport,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("adapter not connected; call connect() first")
        return self._client

    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    # --- archive access ----------------------------------------------------------------------
    async def _list_symbols(self, prefix: str) -> list[str]:
        symbols: list[str] = []
        marker = ""
        for _ in range(100):
            params = {"delimiter": "/", "prefix": prefix}
            if marker:
                params["marker"] = marker
            resp = await self.client.get(self.listing_url, params=params)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.text)
            for node in root.iter(f"{S3_NS}CommonPrefixes"):
                value = node.findtext(f"{S3_NS}Prefix") or ""
                name = value[len(prefix) :].strip("/")
                if name:
                    symbols.append(name)
            if (root.findtext(f"{S3_NS}IsTruncated") or "false").lower() != "true":
                break
            marker = root.findtext(f"{S3_NS}NextMarker") or (
                f"{prefix}{symbols[-1]}/" if symbols else ""
            )
            if not marker:
                break
        return symbols

    async def _download(self, rel_path: str, *, permanent_miss: bool) -> bytes | None:
        """Cached GET of an archive file. Returns None on 404."""
        cached = self.cache_dir / rel_path
        missing_marker = cached.with_suffix(cached.suffix + ".missing")
        if cached.exists():
            return cached.read_bytes()
        if missing_marker.exists():
            return None
        async with self._sem:
            resp = await self.client.get(f"{self.archive_url}/{rel_path}")
        if resp.status_code == 404:
            if permanent_miss:
                missing_marker.parent.mkdir(parents=True, exist_ok=True)
                missing_marker.touch()
            return None
        resp.raise_for_status()
        cached.parent.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(cached.suffix + ".tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(cached)
        return resp.content

    def _is_closed_month(self, key: str) -> bool:
        return key <= previous_month_key(self.now())

    async def _funding_month(self, symbol: str, key: str) -> list[tuple[int, float, float]]:
        rel = f"{FUNDING_PREFIX}{symbol}/{symbol}-fundingRate-{key}.zip"
        data = await self._download(rel, permanent_miss=self._is_closed_month(key))
        return parse_funding_csv(unzip_single(data)) if data else []

    async def _klines_month(self, kind: str, symbol: str, key: str) -> str | None:
        prefix = SPOT_KLINES_PREFIX if kind == "spot" else PERP_KLINES_PREFIX
        rel = f"{prefix}{symbol}/{self.timeframe}/{symbol}-{self.timeframe}-{key}.zip"
        data = await self._download(rel, permanent_miss=self._is_closed_month(key))
        return unzip_single(data) if data else None

    # --- ExchangeAdapter: reference data and history -----------------------------------------
    async def load_instruments(self) -> dict[str, InstrumentRules]:
        perps, spots = await asyncio.gather(
            self._list_symbols(FUNDING_PREFIX), self._list_symbols(SPOT_KLINES_PREFIX)
        )
        spot_set = set(spots)
        bases = sorted(
            p[: -len(QUOTE)]
            for p in perps
            if p.endswith(QUOTE) and p[: -len(QUOTE)] and p in spot_set
        )
        latest = previous_month_key(self.now())

        async def interval_for(base: str) -> tuple[str, float | None]:
            rows = await self._funding_month(f"{base}{QUOTE}", latest)
            if not rows:
                return base, None
            return base, float(statistics.median(r[1] for r in rows))

        found = await asyncio.gather(*(interval_for(b) for b in bases))
        rules: dict[str, InstrumentRules] = {}
        for base, interval in found:
            if interval is None or interval <= 0:
                continue  # no funding last month: dead or delisted
            self._intervals[base] = interval
            rules[base] = _approx_rules(base, interval)
        log.info("binance_vision_instruments", pairs=len(rules), candidates=len(bases))
        return rules

    async def fetch_24h_volumes(self) -> Mapping[str, tuple[float, float]]:
        latest = previous_month_key(self.now())
        year, month = (int(x) for x in latest.split("-"))
        days = (
            datetime(year + (month // 12), month % 12 + 1, 1, tzinfo=UTC)
            - datetime(year, month, 1, tzinfo=UTC)
        ).days

        async def volume_for(base: str) -> tuple[str, float, float]:
            symbol = f"{base}{QUOTE}"
            spot_text, perp_text = await asyncio.gather(
                self._klines_month("spot", symbol, latest),
                self._klines_month("perp", symbol, latest),
            )
            spot = parse_klines_quote_volume(spot_text) / days if spot_text else 0.0
            perp = parse_klines_quote_volume(perp_text) / days if perp_text else 0.0
            return base, spot, perp

        results = await asyncio.gather(*(volume_for(b) for b in sorted(self._intervals)))
        return {base: (spot, perp) for base, spot, perp in results}

    async def fetch_funding_history(
        self, rules: InstrumentRules, since_ms: int, until_ms: int, limit: int
    ) -> list[FundingRecord]:
        symbol = f"{rules.base}{QUOTE}"
        months = months_between(since_ms, until_ms)
        chunks = await asyncio.gather(*(self._funding_month(symbol, m) for m in months))
        records = [
            FundingRecord(ts_ms=ts, rate=rate)
            for rows in chunks
            for ts, _interval, rate in rows
            if since_ms <= ts <= until_ms
        ]
        records.sort(key=lambda r: r.ts_ms)
        return records  # all rows in range: paging is done by month, not by count

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int, limit: int
    ) -> list[Candle]:
        if timeframe != self.timeframe:
            raise ValueError(f"archive adapter serves {self.timeframe} only")
        kind = "perp" if ":" in symbol else "spot"
        archive_symbol = symbol.split(":")[0].replace("/", "")
        months = months_between(since_ms, until_ms)
        texts = await asyncio.gather(*(self._klines_month(kind, archive_symbol, m) for m in months))
        candles = [
            c
            for text in texts
            if text
            for c in parse_klines_csv(text)
            if since_ms <= c.ts_ms <= until_ms
        ]
        candles.sort(key=lambda c: c.ts_ms)
        return candles

    async def fetch_funding_info(self, rules: InstrumentRules) -> FundingInfo:
        return FundingInfo(
            base=rules.base,
            predicted_rate=None,
            next_funding_ts_ms=None,
            interval_hours=rules.funding_interval_hours,
        )

    # --- trading: never --------------------------------------------------------------------
    async def fetch_order_book(self, symbol: str, depth: int) -> OrderBook:
        raise NotImplementedError(_NOT_A_VENUE)

    async def fetch_balances(self) -> Mapping[str, float]:
        raise NotImplementedError(_NOT_A_VENUE)

    async def fetch_positions(self) -> list[PerpPosition]:
        raise NotImplementedError(_NOT_A_VENUE)

    async def create_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError(_NOT_A_VENUE)

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        raise NotImplementedError(_NOT_A_VENUE)

    async def transfer(self, asset: str, amount: float, from_wallet: str, to_wallet: str) -> None:
        raise NotImplementedError(_NOT_A_VENUE)


def iter_symbols(pairs: Iterable[str]) -> list[str]:
    return sorted(set(pairs))
