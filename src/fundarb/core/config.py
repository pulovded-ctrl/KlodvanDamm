"""Typed configuration loaded from YAML. Unknown keys are rejected so typos fail loudly."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_SETTINGS_PATH = Path("config/settings.yaml")
DEFAULT_STRATEGY_PATH = Path("config/strategy.yaml")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExchangeSettings(_Strict):
    id: str = "bybit"
    testnet: bool = False
    unified_account: bool = True
    note: str = ""  # free text shown in reports, e.g. "archive data used as a proxy"


class FeeSchedule(_Strict):
    """Fees in basis points. 1 bps = 0.01%."""

    spot_maker_bps: float = Field(ge=0)
    spot_taker_bps: float = Field(ge=0)
    perp_maker_bps: float = Field(ge=0)
    perp_taker_bps: float = Field(ge=0)

    @property
    def round_trip_fee_frac(self) -> float:
        """First leg maker, second leg taker, on entry and on exit (see execution rules)."""
        total_bps = (
            self.spot_maker_bps + self.perp_taker_bps + self.perp_maker_bps + self.spot_taker_bps
        )
        return total_bps * 1e-4


class DataSettings(_Strict):
    dir: Path = Path("data")
    timeframe: Literal["1h", "1m"] = "1h"
    history_start: datetime
    max_symbols: int = Field(default=60, ge=1, le=500)

    @field_validator("history_start", mode="before")
    @classmethod
    def _parse_start(cls, value: object) -> object:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class TelegramSettings(_Strict):
    enabled: bool = False


class LogSettings(_Strict):
    level: str = "INFO"
    json_output: bool = True


class VenueFees(_Strict):
    maker_bps: float = Field(ge=0)
    taker_bps: float = Field(ge=0)
    assumed_spread_bps: float = Field(default=5.0, ge=0)  # no order-book history in backtests


class PairSettings(_Strict):
    """Cross-venue perp-perp study: long a perp on one venue, short the same perp on the other."""

    venues: list[str] = Field(min_length=2, max_length=2)
    fees: dict[str, VenueFees]
    price_venue: str  # venue whose perp candles price both legs
    capital_split: dict[str, float] | None = None  # share of capital per venue, equal if omitted
    period_hours: int = Field(default=8, ge=1)  # funding spreads are summed per period
    venue_rebalance_hours: int = Field(default=24, ge=1)  # cash equalised between venues

    def split_for(self, venue: str) -> float:
        if self.capital_split is None:
            return 1.0 / len(self.venues)
        return self.capital_split[venue]


class Settings(_Strict):
    mode: Literal["paper", "live"] = "paper"
    exchange: ExchangeSettings = ExchangeSettings()
    fees: FeeSchedule
    data: DataSettings
    reports_dir: Path = Path("reports")
    ledger_db: Path = Path("data/ledger.sqlite")
    telegram: TelegramSettings = TelegramSettings()
    log: LogSettings = LogSettings()
    pair: PairSettings | None = None


class ErrorBreaker(_Strict):
    max_errors: int = Field(ge=1)
    window_sec: int = Field(ge=1)


class ExchangeEvent(_Strict):
    base: str
    kind: Literal["delisting", "maintenance"]
    at: datetime

    @field_validator("at", mode="before")
    @classmethod
    def _parse_at(cls, value: object) -> object:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class WalkForwardGrid(_Strict):
    entry_threshold_apr: list[float] = Field(min_length=1)
    exit_threshold_apr: list[float] = Field(min_length=1)
    hold_horizon_periods: list[int] = Field(min_length=1)


class WalkForwardSettings(_Strict):
    train_months: int = Field(default=6, ge=1)
    test_months: int = Field(default=3, ge=1)
    grid: WalkForwardGrid


class BacktestSettings(_Strict):
    initial_capital_usd: float = Field(gt=0)
    assumed_spread_bps: float = Field(ge=0)
    slippage_coef_bps: float = Field(ge=0)
    maintenance_margin_rate: float = Field(ge=0, lt=1)
    walk_forward: WalkForwardSettings


class StrategyParams(_Strict):
    entry_threshold_apr: float
    exit_threshold_apr: float
    rotation_margin_apr: float = Field(ge=0)
    confirm_periods: int = Field(ge=1)
    funding_ewma_span: int = Field(ge=1)
    hold_horizon_periods: int = Field(ge=1)
    min_24h_volume_usd: float = Field(ge=0)
    max_spread_bps: float = Field(ge=0)
    max_order_pct_of_1min_volume: float = Field(gt=0, le=100)
    max_hedge_slippage_bps: float = Field(ge=0)
    leg_timeout_sec: float = Field(gt=0)
    unhedged_max_sec: float = Field(gt=0)
    max_asset_pct: float = Field(gt=0, le=100)
    max_positions: int = Field(ge=1)
    min_notional_usd: float = Field(ge=0)
    max_leverage_perp: float = Field(gt=0)
    min_liq_distance_pct: float = Field(ge=0)
    cash_reserve_pct: float = Field(default=15.0, ge=0, le=90)  # kept free for margin top-ups
    long_leg: Literal["spot", "perp"] = "spot"  # perp = cross-venue perp-perp pair
    stale_data_sec: float = Field(gt=0)
    daily_dd_hard_stop_pct: float = Field(gt=0, le=100)
    reconcile_interval_sec: int = Field(ge=1)
    api_error_rate_breaker: ErrorBreaker
    stablecoin_depeg_pct: float = Field(gt=0)
    event_close_before_hours: float = Field(ge=0)
    rebalance_interval_hours: int = Field(default=8, ge=1)
    blacklist: list[str] = Field(default_factory=list)
    exchange_events: list[ExchangeEvent] = Field(default_factory=list)
    backtest: BacktestSettings

    def with_overrides(self, **overrides: object) -> StrategyParams:
        return self.model_copy(update=overrides)


def _read_yaml(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected a mapping at top level")
    return loaded


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    settings = Settings.model_validate(_read_yaml(path))
    pair = settings.pair
    if pair is not None:
        missing = [v for v in pair.venues if v not in pair.fees]
        if missing:
            raise ValueError(f"pair.fees missing for venues: {missing}")
        if pair.price_venue not in pair.venues:
            raise ValueError("pair.price_venue must be one of pair.venues")
        if pair.capital_split is not None and set(pair.capital_split) != set(pair.venues):
            raise ValueError("pair.capital_split must name exactly the pair venues")
    return settings


def load_strategy(path: Path = DEFAULT_STRATEGY_PATH) -> StrategyParams:
    params = StrategyParams.model_validate(_read_yaml(path))
    if params.exit_threshold_apr >= params.entry_threshold_apr:
        raise ValueError("exit_threshold_apr must be below entry_threshold_apr")
    return params
