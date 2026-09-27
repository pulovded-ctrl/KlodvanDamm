"""History-only adapter over Hyperliquid's public info API (no keys, no trading).

Funding settles hourly; ``fundingRate`` is the hourly rate. ``fundingHistory`` pages 500
records per call and goes back to 2023. ``candleSnapshot`` only serves the most recent
~5000 candles, so for long backtests prices must come from another venue. Requests are paced
to stay under the documented weight limit (1200 per minute, 20 per info request).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

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

INFO_URL = "https://api.hyperliquid.xyz/info"
QUOTE = "USDC"
FUNDING_PAGE = 500
CANDLE_PAGE = 5000
MIN_ORDER_USD = 10.0
_NOT_A_VENUE = "hyperliquid info adapter is history-only, it cannot trade"


class HyperliquidInfoAdapter(ExchangeAdapter):
    name = "hyperliquid"
    funding_page_size = FUNDING_PAGE  # the syncer asks for pages this big
    ohlcv_page_size = CANDLE_PAGE

    def __init__(
        self,
        *,
        info_url: str = INFO_URL,
        min_interval_sec: float = 1.05,
        transport: httpx.AsyncBaseTransport | None = None,
        now: datetime | None = None,
    ) -> None:
        self.info_url = info_url
        self.min_interval_sec = min_interval_sec
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._now = now
        self._meta: list[dict[str, Any]] = []

    # --- lifecycle -------------------------------------------------------------------------
    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            trust_env=True, timeout=httpx.Timeout(60.0), transport=self._transport
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

    async def _post(self, payload: Mapping[str, Any]) -> Any:
        """Paced POST with retries on rate limiting and server errors."""
        delay = 2.0
        for attempt in range(6):
            async with self._lock:
                wait = self.min_interval_sec - (time.monotonic() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.monotonic()
            resp = await self.client.post(self.info_url, json=dict(payload))
            if resp.status_code == 429 or resp.status_code >= 500:
                log.warning("hyperliquid_retry", status=resp.status_code, attempt=attempt)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"hyperliquid info request failed after retries: {payload.get('type')}")

    # --- reference data ----------------------------------------------------------------------
    async def load_instruments(self) -> dict[str, InstrumentRules]:
        meta = await self._post({"type": "meta"})
        self._meta = list(meta.get("universe", []))
        rules: dict[str, InstrumentRules] = {}
        for asset in self._meta:
            if asset.get("isDelisted"):
                continue
            name = str(asset["name"])
            step = 10.0 ** (-int(asset.get("szDecimals", 0)))
            rules[name] = InstrumentRules(
                base=name,
                spot_symbol="",  # perp-only venue for our purposes
                perp_symbol=f"{name}/{QUOTE}:{QUOTE}",
                spot_amount_step=step,
                spot_min_amount=0.0,
                spot_min_notional_usd=0.0,
                perp_amount_step=step,
                perp_min_amount=step,
                perp_min_notional_usd=MIN_ORDER_USD,
                spot_price_tick=0.0,
                perp_price_tick=0.0,
                funding_interval_hours=1.0,
            )
        return rules

    async def fetch_24h_volumes(self) -> Mapping[str, tuple[float, float]]:
        universe, contexts = await self._post({"type": "metaAndAssetCtxs"})
        out: dict[str, tuple[float, float]] = {}
        for asset, ctx in zip(universe.get("universe", []), contexts, strict=False):
            if asset.get("isDelisted"):
                continue
            volume = float(ctx.get("dayNtlVlm", 0.0) or 0.0)
            out[str(asset["name"])] = (volume, volume)  # no spot leg: perp volume on both
        return out

    # --- history -----------------------------------------------------------------------------
    async def fetch_funding_history(
        self, rules: InstrumentRules, since_ms: int, until_ms: int, limit: int
    ) -> list[FundingRecord]:
        rows = await self._post(
            {
                "type": "fundingHistory",
                "coin": rules.base,
                "startTime": int(since_ms),
                "endTime": int(until_ms),
            }
        )
        records = [
            FundingRecord(ts_ms=int(row["time"]), rate=float(row["fundingRate"]))
            for row in rows
            if since_ms <= int(row["time"]) <= until_ms
        ]
        records.sort(key=lambda r: r.ts_ms)
        return records[:limit]

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int, limit: int
    ) -> list[Candle]:
        coin = symbol.split("/")[0]
        rows = await self._post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": timeframe,
                    "startTime": int(since_ms),
                    "endTime": int(until_ms),
                },
            }
        )
        candles = [
            Candle(
                ts_ms=int(row["t"]),
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=float(row["v"]),
            )
            for row in rows
            if since_ms <= int(row["t"]) <= until_ms
        ]
        candles.sort(key=lambda c: c.ts_ms)
        return candles[:limit]

    async def fetch_funding_info(self, rules: InstrumentRules) -> FundingInfo:
        universe, contexts = await self._post({"type": "metaAndAssetCtxs"})
        rate: float | None = None
        for asset, ctx in zip(universe.get("universe", []), contexts, strict=False):
            if asset.get("name") == rules.base:
                rate = float(ctx.get("funding", 0.0))
                break
        now = self._now or datetime.now(UTC)
        next_hour = (int(now.timestamp() * 1000) // 3_600_000 + 1) * 3_600_000
        return FundingInfo(
            base=rules.base, predicted_rate=rate, next_funding_ts_ms=next_hour, interval_hours=1.0
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
