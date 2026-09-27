"""The only door to any exchange. Backtest, paper and live code never import ccxt directly."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

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


class ExchangeAdapter(ABC):
    name: str = "abstract"

    # --- lifecycle -------------------------------------------------------------------------
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # --- reference data and history (phase 0) -----------------------------------------------
    @abstractmethod
    async def load_instruments(self) -> dict[str, InstrumentRules]:
        """Base coin -> rules, only for coins that have BOTH a USDT spot pair and a USDT perp."""

    @abstractmethod
    async def fetch_24h_volumes(self) -> Mapping[str, tuple[float, float]]:
        """Base coin -> (spot 24h volume in USD, perp 24h volume in USD)."""

    @abstractmethod
    async def fetch_funding_history(
        self, rules: InstrumentRules, since_ms: int, until_ms: int, limit: int
    ) -> list[FundingRecord]:
        """Settled funding rates in ``[since_ms, until_ms]``, oldest first, at most ``limit``."""

    @abstractmethod
    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since_ms: int, until_ms: int, limit: int
    ) -> list[Candle]:
        """Candles starting at or after ``since_ms``, oldest first, at most ``limit``."""

    @abstractmethod
    async def fetch_funding_info(self, rules: InstrumentRules) -> FundingInfo:
        """Rate to be paid at the next settlement (exchange's predicted funding)."""

    # --- market data and trading (phase 1) --------------------------------------------------
    @abstractmethod
    async def fetch_order_book(self, symbol: str, depth: int) -> OrderBook: ...

    @abstractmethod
    async def fetch_balances(self) -> Mapping[str, float]: ...

    @abstractmethod
    async def fetch_positions(self) -> list[PerpPosition]: ...

    @abstractmethod
    async def create_order(self, request: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> None: ...

    @abstractmethod
    async def transfer(
        self, asset: str, amount: float, from_wallet: str, to_wallet: str
    ) -> None: ...
