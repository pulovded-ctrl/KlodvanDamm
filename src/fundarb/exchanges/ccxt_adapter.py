"""ExchangeAdapter backed by ccxt (async). Phase 0 implements reference data and history only."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import ccxt.async_support as ccxt

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

DEFAULT_FUNDING_INTERVAL_HOURS = 8.0
_PHASE1 = "trading methods arrive in phase 1"


def _num(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _funding_interval_hours(market: Mapping[str, Any]) -> float:
    info = market.get("info") or {}
    minutes = _num(info.get("fundingInterval"), 0.0)
    return minutes / 60.0 if minutes > 0 else DEFAULT_FUNDING_INTERVAL_HOURS


def _parse_interval(text: object) -> float | None:
    """ccxt reports the interval as e.g. ``'8h'``."""
    if not isinstance(text, str) or not text.endswith("h"):
        return None
    try:
        return float(text[:-1])
    except ValueError:
        return None


class CcxtAdapter(ExchangeAdapter):
    def __init__(
        self,
        exchange_id: str = "bybit",
        *,
        testnet: bool = False,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        self.name = exchange_id
        self._testnet = testnet
        self._api_key = api_key
        self._api_secret = api_secret
        self._ex: Any = None

    @property
    def raw(self) -> Any:
        if self._ex is None:
            raise RuntimeError("adapter not connected; call connect() first")
        return self._ex

    async def connect(self) -> None:
        cls = getattr(ccxt, self.name)
        # aiohttp_trust_env: honour HTTPS_PROXY / SSL_CERT_FILE like every other tool does
        options: dict[str, Any] = {"enableRateLimit": True, "aiohttp_trust_env": True}
        if self._api_key and self._api_secret:
            options["apiKey"] = self._api_key
            options["secret"] = self._api_secret
        self._ex = cls(options)
        if self._testnet:
            self._ex.set_sandbox_mode(True)
        try:
            await self._ex.load_markets()
        except Exception:
            await self._ex.close()  # release the HTTP session even when the exchange is unreachable
            self._ex = None
            raise

    async def close(self) -> None:
        if self._ex is not None:
            await self._ex.close()
            self._ex = None

    async def load_instruments(self) -> dict[str, InstrumentRules]:
        markets: Mapping[str, Mapping[str, Any]] = self.raw.markets
        spots: dict[str, Mapping[str, Any]] = {}
        perps: dict[str, Mapping[str, Any]] = {}
        for market in markets.values():
            if not market.get("active", True):
                continue
            base = market.get("base")
            if not isinstance(base, str):
                continue
            if market.get("spot") and market.get("quote") == "USDT":
                spots[base] = market
            elif market.get("swap") and market.get("linear") and market.get("settle") == "USDT":
                perps[base] = market
        rules: dict[str, InstrumentRules] = {}
        for base in sorted(spots.keys() & perps.keys()):
            spot, perp = spots[base], perps[base]
            rules[base] = InstrumentRules(
                base=base,
                spot_symbol=str(spot["symbol"]),
                perp_symbol=str(perp["symbol"]),
                spot_amount_step=_num(spot["precision"].get("amount"), 0.0),
                spot_min_amount=_num(spot["limits"].get("amount", {}).get("min"), 0.0),
                spot_min_notional_usd=_num(spot["limits"].get("cost", {}).get("min"), 0.0),
                perp_amount_step=_num(perp["precision"].get("amount"), 0.0),
                perp_min_amount=_num(perp["limits"].get("amount", {}).get("min"), 0.0),
                perp_min_notional_usd=_num(perp["limits"].get("cost", {}).get("min"), 0.0),
                spot_price_tick=_num(spot["precision"].get("price"), 0.0),
                perp_price_tick=_num(perp["precision"].get("price"), 0.0),
                funding_interval_hours=_funding_interval_hours(perp),
                contract_size=_num(perp.get("contractSize"), 1.0) or 1.0,
            )
        return rules

    async def fetch_24h_volumes(self) -> Mapping[str, tuple[float, float]]:
        spot_tickers = await self.raw.fetch_tickers(params={"type": "spot"})
        perp_tickers = await self.raw.fetch_tickers(params={"type": "swap", "subType": "linear"})
        markets: Mapping[str, Mapping[str, Any]] = self.raw.markets
        volumes: dict[str, tuple[float, float]] = {}
        spot_by_base: dict[str, float] = {}
        for symbol, ticker in spot_tickers.items():
            market = markets.get(symbol)
            if market and market.get("quote") == "USDT":
                spot_by_base[str(market["base"])] = _num(ticker.get("quoteVolume"), 0.0)
        for symbol, ticker in perp_tickers.items():
            market = markets.get(symbol)
            if not market or market.get("settle") != "USDT":
                continue
            base = str(market["base"])
            if base in spot_by_base:
                volumes[base] = (spot_by_base[base], _num(ticker.get("quoteVolume"), 0.0))
        return volumes

    async def fetch_funding_history(
        self, rules: InstrumentRules, since_ms: int, until_ms: int, limit: int
    ) -> list[FundingRecord]:
        rows = await self.raw.fetch_funding_rate_history(
            rules.perp_symbol, since=since_ms, limit=limit, params={"until": until_ms}
        )
        records = [
            FundingRecord(ts_ms=int(row["timestamp"]), rate=float(row["fundingRate"]))
            for row in rows
            if row.get("timestamp") is not None and row.get("fundingRate") is not None
        ]
        records.sort(key=lambda r: r.ts_ms)
        return [r for r in records if since_ms <= r.ts_ms <= until_ms]

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int, limit: int
    ) -> list[Candle]:
        rows = await self.raw.fetch_ohlcv(
            symbol, timeframe, since=since_ms, limit=limit, params={"until": until_ms}
        )
        candles = [
            Candle(
                ts_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
            for row in rows
            if row[0] is not None and row[4] is not None
        ]
        candles.sort(key=lambda c: c.ts_ms)
        return [c for c in candles if since_ms <= c.ts_ms <= until_ms]

    async def fetch_funding_info(self, rules: InstrumentRules) -> FundingInfo:
        data = await self.raw.fetch_funding_rate(rules.perp_symbol)
        rate = data.get("fundingRate")
        next_ts = data.get("fundingTimestamp")
        interval = _parse_interval(data.get("interval")) or rules.funding_interval_hours
        return FundingInfo(
            base=rules.base,
            predicted_rate=float(rate) if rate is not None else None,
            next_funding_ts_ms=int(next_ts) if next_ts is not None else None,
            interval_hours=interval,
        )

    # --- phase 1 ---------------------------------------------------------------------------
    async def fetch_order_book(self, symbol: str, depth: int) -> OrderBook:
        raise NotImplementedError(_PHASE1)

    async def fetch_balances(self) -> Mapping[str, float]:
        raise NotImplementedError(_PHASE1)

    async def fetch_positions(self) -> list[PerpPosition]:
        raise NotImplementedError(_PHASE1)

    async def create_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError(_PHASE1)

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        raise NotImplementedError(_PHASE1)

    async def transfer(self, asset: str, amount: float, from_wallet: str, to_wallet: str) -> None:
        raise NotImplementedError(_PHASE1)
