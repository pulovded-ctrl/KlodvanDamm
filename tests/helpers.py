"""Test helpers: instrument rules, a deterministic in-memory exchange, time utils."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

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

HOUR_MS = 3_600_000


def make_rules(base: str = "BTC", funding_interval_hours: float = 8.0) -> InstrumentRules:
    return InstrumentRules(
        base=base,
        spot_symbol=f"{base}/USDT",
        perp_symbol=f"{base}/USDT:USDT",
        spot_amount_step=0.000001,
        spot_min_amount=0.00001,
        spot_min_notional_usd=1.0,
        perp_amount_step=0.001,
        perp_min_amount=0.001,
        perp_min_notional_usd=5.0,
        spot_price_tick=0.01,
        perp_price_tick=0.1,
        funding_interval_hours=funding_interval_hours,
    )


class FakeAdapter(ExchangeAdapter):
    """Serves synthetic funding and candles; records how it was called."""

    name = "fake"

    def __init__(
        self,
        rules: Mapping[str, InstrumentRules],
        *,
        start_ms: int,
        end_ms: int,
        volumes: Mapping[str, tuple[float, float]] | None = None,
        funding_rate: Callable[[str, int], float] | None = None,
        price: Callable[[str, int], float] | None = None,
        fail_bases: frozenset[str] = frozenset(),
    ) -> None:
        self.rules = dict(rules)
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.volumes = dict(volumes) if volumes else dict.fromkeys(rules, (1e9, 1e9))
        self.funding_rate = funding_rate or (lambda _base, _ts: 0.0001)
        self.price = price or (lambda _base, _ts: 100.0)
        self.fail_bases = fail_bases
        self.calls: list[tuple[str, str, int, int, int]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def load_instruments(self) -> dict[str, InstrumentRules]:
        return dict(self.rules)

    async def fetch_24h_volumes(self) -> Mapping[str, tuple[float, float]]:
        return dict(self.volumes)

    def funding_timestamps(self, base: str) -> list[int]:
        step = int(self.rules[base].funding_interval_hours * HOUR_MS)
        first = ((self.start_ms + step - 1) // step) * step
        return list(range(first, self.end_ms + 1, step))

    async def fetch_funding_history(
        self, rules: InstrumentRules, since_ms: int, until_ms: int, limit: int
    ) -> list[FundingRecord]:
        if rules.base in self.fail_bases:
            raise RuntimeError("simulated exchange failure")
        self.calls.append(("funding", rules.base, since_ms, until_ms, limit))
        rows = [
            FundingRecord(ts_ms=ts, rate=self.funding_rate(rules.base, ts))
            for ts in self.funding_timestamps(rules.base)
            if since_ms <= ts <= until_ms
        ]
        return rows[:limit]

    def candle_timestamps(self) -> list[int]:
        first = ((self.start_ms + HOUR_MS - 1) // HOUR_MS) * HOUR_MS
        return list(range(first, self.end_ms + 1, HOUR_MS))

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int, limit: int
    ) -> list[Candle]:
        base = symbol.split("/")[0]
        self.calls.append(("ohlcv:" + symbol, base, since_ms, until_ms, limit))
        rows = []
        for ts in self.candle_timestamps():
            if not since_ms <= ts <= until_ms:
                continue
            px = self.price(base, ts)
            rows.append(Candle(ts_ms=ts, open=px, high=px, low=px, close=px, volume=1000.0))
        return rows[:limit]

    async def fetch_funding_info(self, rules: InstrumentRules) -> FundingInfo:
        return FundingInfo(
            base=rules.base,
            predicted_rate=self.funding_rate(rules.base, self.end_ms),
            next_funding_ts_ms=None,
            interval_hours=rules.funding_interval_hours,
        )

    async def fetch_order_book(self, symbol: str, depth: int) -> OrderBook:
        raise NotImplementedError

    async def fetch_balances(self) -> Mapping[str, float]:
        raise NotImplementedError

    async def fetch_positions(self) -> list[PerpPosition]:
        raise NotImplementedError

    async def create_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        raise NotImplementedError

    async def transfer(self, asset: str, amount: float, from_wallet: str, to_wallet: str) -> None:
        raise NotImplementedError


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)
