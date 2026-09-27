"""Rounding to exchange rules. Decimal arithmetic keeps float noise out of order sizes."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from fundarb.core.models import InstrumentRules


def _dec(value: float) -> Decimal:
    return Decimal(repr(value))


def round_down_to_step(value: float, step: float) -> float:
    """Largest multiple of ``step`` that is <= ``value``. A non-positive step returns ``value``."""
    if step <= 0:
        return value
    if value <= 0:
        return 0.0
    quantized = (_dec(value) / _dec(step)).to_integral_value(rounding=ROUND_DOWN) * _dec(step)
    return float(quantized)


def round_to_tick(price: float, tick: float) -> float:
    """Nearest multiple of ``tick``."""
    if tick <= 0:
        return price
    quantized = (_dec(price) / _dec(tick)).to_integral_value(rounding=ROUND_HALF_UP) * _dec(tick)
    return float(quantized)


def is_multiple(value: float, step: float) -> bool:
    if step <= 0:
        return True
    return (_dec(value) % _dec(step)) == 0


def hedged_qty(notional_usd: float, price: float, rules: InstrumentRules) -> float:
    """Coin quantity for a hedged pair that is valid on BOTH legs, rounded down.

    Returns 0.0 when no valid quantity exists (below minimum amount or minimum notional
    on either leg, or the two lot steps are incompatible).
    """
    if notional_usd <= 0 or price <= 0:
        return 0.0
    raw = notional_usd / price
    coarse, fine = sorted((rules.spot_amount_step, rules.perp_amount_step), reverse=True)
    qty = round_down_to_step(raw, coarse)
    if not is_multiple(qty, fine):
        qty = round_down_to_step(raw, fine)
        if not is_multiple(qty, coarse):
            return 0.0
    if qty <= 0:
        return 0.0
    if qty < rules.spot_min_amount or qty < rules.perp_min_amount:
        return 0.0
    notional = qty * price
    if notional < rules.spot_min_notional_usd or notional < rules.perp_min_notional_usd:
        return 0.0
    return qty
