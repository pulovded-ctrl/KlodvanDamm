"""Funding forecast: exponential average of recent settled fundings, capped by the exchange's
predicted rate when one is available. Conservative by construction."""

from __future__ import annotations

from collections.abc import Sequence

from fundarb.core.models import HOURS_PER_YEAR


def ewma(values: Sequence[float], span: int) -> float:
    """Exponentially weighted average over ``values`` (oldest first), alpha = 2 / (span + 1)."""
    if not values:
        raise ValueError("ewma of empty sequence")
    alpha = 2.0 / (span + 1.0)
    estimate = float(values[0])
    for value in values[1:]:
        estimate = alpha * float(value) + (1.0 - alpha) * estimate
    return estimate


def forecast_funding(history: Sequence[float], span: int, predicted: float | None) -> float | None:
    """Forecast of the next funding payment, or ``None`` when history is too short to trust."""
    if len(history) < span:
        return None
    estimate = ewma(history[-span:], span)
    if predicted is not None:
        estimate = min(estimate, predicted)
    return estimate


def periods_per_year(interval_hours: float) -> float:
    if interval_hours <= 0:
        raise ValueError("funding interval must be positive")
    return HOURS_PER_YEAR / interval_hours


def annualize(rate_per_period: float, interval_hours: float) -> float:
    return rate_per_period * periods_per_year(interval_hours)
