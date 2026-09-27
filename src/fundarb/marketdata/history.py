"""Incremental history download: funding rates and candles, paged forward, resumable."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fundarb.core.logging import get_logger
from fundarb.core.models import InstrumentRules
from fundarb.exchanges.base import ExchangeAdapter
from fundarb.marketdata.store import Kind, ParquetStore

log = get_logger(__name__)

TIMEFRAME_MS: dict[str, int] = {"1m": 60_000, "1h": 3_600_000}


@dataclass(slots=True)
class SyncResult:
    bases: list[str] = field(default_factory=list)
    funding_rows: dict[str, int] = field(default_factory=dict)
    spot_rows: dict[str, int] = field(default_factory=dict)
    perp_rows: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


class HistorySync:
    def __init__(
        self,
        adapter: ExchangeAdapter,
        store: ParquetStore,
        *,
        start: datetime,
        timeframe: str = "1h",
        max_symbols: int = 60,
        min_volume_usd: float = 0.0,
        now_ms: Callable[[], int] | None = None,
        funding_page: int = 200,
        ohlcv_page: int = 1000,
        max_pages: int = 20_000,
    ) -> None:
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"unsupported timeframe {timeframe!r}")
        self.adapter = adapter
        self.store = store
        self.start_ms = int(start.timestamp() * 1000)
        self.timeframe = timeframe
        self.tf_ms = TIMEFRAME_MS[timeframe]
        self.max_symbols = max_symbols
        self.min_volume_usd = min_volume_usd
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        # adapters that know their own page sizes win over the defaults
        self.funding_page = int(getattr(adapter, "funding_page_size", funding_page))
        self.ohlcv_page = int(getattr(adapter, "ohlcv_page_size", ohlcv_page))
        self.max_pages = max_pages

    # --- universe ----------------------------------------------------------------------------
    async def select_universe(self) -> dict[str, InstrumentRules]:
        """Top ``max_symbols`` coins by min(spot, perp) 24h volume; saved to the store."""
        instruments = await self.adapter.load_instruments()
        volumes = await self.adapter.fetch_24h_volumes()
        ranked = sorted(
            (
                (min(volumes[base]), base)
                for base in instruments
                if base in volumes and min(volumes[base]) >= self.min_volume_usd
            ),
            reverse=True,
        )
        chosen = {base: instruments[base] for _, base in ranked[: self.max_symbols]}
        self.store.save_instruments(chosen)
        self.store.save_meta(
            {
                "exchange": self.adapter.name,
                "timeframe": self.timeframe,
                "universe_selected_at": datetime.now(UTC).isoformat(),
                "universe_rule": "top by min(spot, perp) 24h volume at selection time",
                "history_start_ms": self.start_ms,
                "bases": sorted(chosen),
                "volumes_24h_usd": {b: list(volumes[b]) for b in sorted(chosen)},
            }
        )
        return chosen

    # --- funding -----------------------------------------------------------------------------
    async def sync_funding(self, rules: InstrumentRules) -> int:
        last = self.store.last_funding_ts(rules.base)
        cursor = self.start_ms if last is None else last + 1
        until = self._now_ms()
        added = 0
        for _ in range(self.max_pages):
            if cursor > until:
                break
            rows = await self.adapter.fetch_funding_history(rules, cursor, until, self.funding_page)
            if not rows:
                break
            added += self.store.append_funding(rules.base, rows)
            next_cursor = rows[-1].ts_ms + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        return added

    # --- candles -----------------------------------------------------------------------------
    async def sync_ohlcv(self, kind: Kind, rules: InstrumentRules) -> int:
        symbol = rules.spot_symbol if kind == "spot" else rules.perp_symbol
        last = self.store.last_ohlcv_ts(kind, rules.base)
        cursor = self.start_ms if last is None else last + self.tf_ms
        now = self._now_ms()
        # only candles that are fully closed
        until = ((now // self.tf_ms) * self.tf_ms) - self.tf_ms
        added = 0
        for _ in range(self.max_pages):
            if cursor > until:
                break
            candles = await self.adapter.fetch_ohlcv(
                symbol, self.timeframe, cursor, until, self.ohlcv_page
            )
            candles = [c for c in candles if c.ts_ms <= until]
            if not candles:
                break
            added += self.store.append_ohlcv(kind, rules.base, candles)
            next_cursor = candles[-1].ts_ms + self.tf_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        return added

    # --- everything --------------------------------------------------------------------------
    async def sync_all(
        self,
        rules_by_base: Mapping[str, InstrumentRules] | None = None,
        bases: Sequence[str] | None = None,
        progress: Callable[[str], None] | None = None,
        concurrency: int = 4,
    ) -> SyncResult:
        """Sync every selected coin; ``concurrency`` coins are downloaded at the same time.
        Adapters keep their own rate limits, so this only overlaps network latency."""
        result = SyncResult()
        universe = rules_by_base if rules_by_base is not None else await self.select_universe()
        selected = [b for b in sorted(universe) if bases is None or b in bases]
        result.bases = selected
        gate = asyncio.Semaphore(max(1, concurrency))

        async def one(base: str) -> None:
            rules = universe[base]
            async with gate:
                if progress:
                    progress(base)
                try:
                    result.funding_rows[base] = await self.sync_funding(rules)
                    if rules.spot_symbol:  # perp-only venues have no spot leg
                        result.spot_rows[base] = await self.sync_ohlcv("spot", rules)
                    else:
                        result.spot_rows[base] = 0
                    result.perp_rows[base] = await self.sync_ohlcv("perp", rules)
                except Exception as exc:  # one bad symbol must not stop the others
                    result.errors[base] = f"{type(exc).__name__}: {exc}"
                    log.warning("history_sync_failed", base=base, error=result.errors[base])

        await asyncio.gather(*(one(b) for b in selected))
        result.finished_at = datetime.now(UTC)
        meta = self.store.load_meta()
        meta["last_sync_at"] = result.finished_at.isoformat()
        meta["last_sync_errors"] = result.errors
        self.store.save_meta(meta)
        return result
