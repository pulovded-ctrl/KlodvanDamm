"""Round-trip cost model for a hedged pair: fees, spreads, slippage, amortised over the holding
horizon so it can be compared with an annualised funding rate."""

from __future__ import annotations

from dataclasses import dataclass

from fundarb.core.config import FeeSchedule
from fundarb.core.models import BPS, HOURS_PER_YEAR, InstrumentSnapshot


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    fees_frac: float
    spread_frac: float
    slippage_frac: float

    @property
    def total_frac(self) -> float:
        return self.fees_frac + self.spread_frac + self.slippage_frac


class CostModel:
    """``assumed_spread_bps`` replaces live bid/ask when there is no order book (backtest)."""

    def __init__(
        self,
        fees: FeeSchedule,
        slippage_coef_bps: float,
        assumed_spread_bps: float | None = None,
    ) -> None:
        self.fees = fees
        self.slippage_coef_bps = slippage_coef_bps
        self.assumed_spread_bps = assumed_spread_bps

    def slippage_bps(self, notional_usd: float, minute_volume_usd: float) -> float:
        """Linear impact: taking x% of one minute's volume costs coef * x bps."""
        if notional_usd <= 0:
            return 0.0
        if minute_volume_usd <= 0:
            return float("inf")
        return self.slippage_coef_bps * notional_usd / minute_volume_usd

    def round_trip(self, inst: InstrumentSnapshot, notional_usd: float) -> CostBreakdown:
        if self.assumed_spread_bps is not None:
            spot_spread_bps = perp_spread_bps = self.assumed_spread_bps
        else:
            spot_spread_bps, perp_spread_bps = inst.spot_spread_bps, inst.perp_spread_bps
        # each leg pays half the spread on entry and half on exit: one full spread per leg
        spread_frac = (spot_spread_bps + perp_spread_bps) * BPS
        # taker legs: perp on entry (hedge), spot on exit (see execution rules)
        slippage_frac = (
            self.slippage_bps(notional_usd, inst.perp_minute_volume_usd)
            + self.slippage_bps(notional_usd, inst.spot_minute_volume_usd)
        ) * BPS
        return CostBreakdown(
            fees_frac=self.fees.round_trip_fee_frac,
            spread_frac=spread_frac,
            slippage_frac=slippage_frac,
        )

    @staticmethod
    def cost_apr(total_frac: float, interval_hours: float, hold_horizon_periods: int) -> float:
        hold_hours = hold_horizon_periods * interval_hours
        if hold_hours <= 0:
            raise ValueError("holding horizon must be positive")
        return total_frac * HOURS_PER_YEAR / hold_hours
