"""Per-coin evaluation: forecast, costs and eligibility with explicit reasons."""

from __future__ import annotations

from datetime import datetime, timedelta

from fundarb.core.config import StrategyParams
from fundarb.core.models import InstrumentSnapshot, Signal
from fundarb.strategy.costs import CostModel
from fundarb.strategy.forecast import annualize, forecast_funding


def evaluate_instrument(
    inst: InstrumentSnapshot,
    params: StrategyParams,
    cost_model: CostModel,
    now: datetime,
    notional_usd: float,
) -> Signal:
    reasons: list[str] = []
    base = inst.base
    if base in params.blacklist:
        reasons.append("blacklisted")
    if not inst.tradable:
        reasons.append("not_tradable")
    if inst.event_at is not None:
        horizon = timedelta(hours=params.event_close_before_hours)
        if inst.event_at - now <= horizon:
            reasons.append("event_soon")
    if (
        inst.spot_volume_24h_usd < params.min_24h_volume_usd
        or inst.perp_volume_24h_usd < params.min_24h_volume_usd
    ):
        reasons.append("volume_below_min")
    if cost_model.assumed_spread_bps is None and (
        inst.spot_spread_bps > params.max_spread_bps or inst.perp_spread_bps > params.max_spread_bps
    ):
        reasons.append("spread_too_wide")

    forecast = forecast_funding(
        inst.funding_history, params.funding_ewma_span, inst.predicted_funding
    )
    if forecast is None:
        reasons.append("insufficient_funding_history")
        return Signal(base, None, 0.0, 0.0, float("-inf"), False, tuple(reasons))

    interval = inst.rules.funding_interval_hours
    gross_apr = annualize(forecast, interval)
    if notional_usd <= 0:
        reasons.append("size_below_min")
        cost_apr = float("inf")
    else:
        breakdown = cost_model.round_trip(inst, notional_usd)
        cost_apr = cost_model.cost_apr(breakdown.total_frac, interval, params.hold_horizon_periods)
    net_apr = gross_apr - cost_apr
    if forecast < 0:
        reasons.append("forecast_negative")
    return Signal(
        base=base,
        forecast_funding=forecast,
        gross_apr=gross_apr,
        cost_apr=cost_apr,
        expected_net_apr=net_apr,
        eligible=not reasons,
        reasons=tuple(reasons),
    )
