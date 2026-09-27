"""Position sizing: the smallest of every cap wins."""

from __future__ import annotations

from fundarb.core.config import StrategyParams
from fundarb.core.models import InstrumentSnapshot
from fundarb.strategy.costs import CostModel


def capital_per_notional(params: StrategyParams) -> float:
    """USD of capital consumed per 1 USD of hedged notional: spot cash plus perp margin."""
    return 1.0 + 1.0 / params.max_leverage_perp


def max_entry_notional(
    equity_usd: float,
    free_cash_usd: float,
    inst: InstrumentSnapshot,
    params: StrategyParams,
    cost_model: CostModel,
) -> float:
    """Largest notional allowed by asset, depth, slippage and cash caps. 0 if below minimum."""
    cap_asset = equity_usd * params.max_asset_pct / 100.0
    min_minute_volume = min(inst.spot_minute_volume_usd, inst.perp_minute_volume_usd)
    cap_depth = min_minute_volume * params.max_order_pct_of_1min_volume / 100.0
    if cost_model.slippage_coef_bps > 0:
        cap_slip = params.max_hedge_slippage_bps / cost_model.slippage_coef_bps * min_minute_volume
    else:
        cap_slip = float("inf")
    reserve = equity_usd * params.cash_reserve_pct / 100.0
    cap_cash = max(free_cash_usd - reserve, 0.0) / capital_per_notional(params)
    notional = min(cap_asset, cap_depth, cap_slip, cap_cash)
    if notional < params.min_notional_usd or notional <= 0:
        return 0.0
    return notional
