"""Core data structures shared by the backtest, paper and live code paths.

The strategy only ever sees a ``MarketSnapshot`` and answers with ``TargetAction``s.
Whoever builds the snapshot (backtest engine, paper engine, live engine) is responsible
for filling it from its own data source. This is what keeps the strategy code identical
across all three modes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

HOURS_PER_YEAR = 24 * 365
BPS = 1e-4


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    """Exchange trading rules for one base coin: its USDT spot pair and its linear perpetual."""

    base: str
    spot_symbol: str
    perp_symbol: str
    spot_amount_step: float
    spot_min_amount: float
    spot_min_notional_usd: float
    perp_amount_step: float
    perp_min_amount: float
    perp_min_notional_usd: float
    spot_price_tick: float
    perp_price_tick: float
    funding_interval_hours: float
    contract_size: float = 1.0


@dataclass(frozen=True, slots=True)
class FundingRecord:
    ts_ms: int
    rate: float


@dataclass(frozen=True, slots=True)
class Candle:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float  # base-asset volume


@dataclass(frozen=True, slots=True)
class FundingInfo:
    """Current funding state of a perpetual: the rate to be paid at the next settlement."""

    base: str
    predicted_rate: float | None
    next_funding_ts_ms: int | None
    interval_hours: float


@dataclass(slots=True)
class InstrumentSnapshot:
    """Everything the strategy needs to know about one coin at one moment."""

    rules: InstrumentRules
    funding_history: Sequence[float]  # settled funding rates, oldest first, newest last
    funding_seq: int  # number of settled fundings observed so far; grows monotonically
    predicted_funding: float | None
    spot_bid: float
    spot_ask: float
    perp_bid: float
    perp_ask: float
    spot_minute_volume_usd: float
    perp_minute_volume_usd: float
    spot_volume_24h_usd: float
    perp_volume_24h_usd: float
    tradable: bool = True
    event_at: datetime | None = None  # nearest delisting / maintenance event, if any

    @property
    def base(self) -> str:
        return self.rules.base

    @property
    def spot_mid(self) -> float:
        return (self.spot_bid + self.spot_ask) / 2.0

    @property
    def perp_mid(self) -> float:
        return (self.perp_bid + self.perp_ask) / 2.0

    @property
    def spot_spread_bps(self) -> float:
        mid = self.spot_mid
        return (self.spot_ask - self.spot_bid) / mid / BPS if mid > 0 else float("inf")

    @property
    def perp_spread_bps(self) -> float:
        mid = self.perp_mid
        return (self.perp_ask - self.perp_bid) / mid / BPS if mid > 0 else float("inf")


@dataclass(slots=True)
class PositionSnapshot:
    """An open hedged position: ``qty`` coins long on spot, the same ``qty`` short on the perp."""

    base: str
    qty: float
    notional_usd: float
    opened_at: datetime
    liq_distance_pct: float | None = None


class KillSwitch(StrEnum):
    NONE = "none"
    SOFT = "soft"  # no new entries
    HARD = "hard"  # flatten everything


@dataclass(slots=True)
class MarketSnapshot:
    ts: datetime
    equity_usd: float
    free_cash_usd: float
    instruments: dict[str, InstrumentSnapshot] = field(default_factory=dict)
    positions: dict[str, PositionSnapshot] = field(default_factory=dict)
    kill_switch: KillSwitch = KillSwitch.NONE


class ActionKind(StrEnum):
    ENTER = "enter"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class TargetAction:
    base: str
    kind: ActionKind
    notional_usd: float
    reason: str
    expected_net_apr: float


@dataclass(frozen=True, slots=True)
class Signal:
    """Result of evaluating one coin: forecast, costs and whether it may be traded."""

    base: str
    forecast_funding: float | None
    gross_apr: float
    cost_apr: float
    expected_net_apr: float
    eligible: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: float
    qty: float


@dataclass(frozen=True, slots=True)
class OrderBook:
    symbol: str
    ts_ms: int
    bids: tuple[OrderBookLevel, ...]  # best first
    asks: tuple[OrderBookLevel, ...]  # best first


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT_POST_ONLY = "limit_post_only"
    IOC = "ioc"
    MARKET = "market"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    qty: float
    order_type: OrderType
    client_order_id: str
    price: float | None = None
    reduce_only: bool = False


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: str
    client_order_id: str
    symbol: str
    filled_qty: float
    avg_price: float | None
    fee_usd: float
    status: str


@dataclass(frozen=True, slots=True)
class PerpPosition:
    symbol: str
    qty: float  # signed: negative for short
    entry_price: float
    liquidation_price: float | None
    margin_usd: float | None
