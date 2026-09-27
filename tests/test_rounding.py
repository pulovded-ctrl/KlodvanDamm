from fundarb.core.models import InstrumentRules
from fundarb.core.rounding import hedged_qty, is_multiple, round_down_to_step, round_to_tick


def make_rules(**overrides: float) -> InstrumentRules:
    base: dict[str, object] = {
        "base": "BTC",
        "spot_symbol": "BTC/USDT",
        "perp_symbol": "BTC/USDT:USDT",
        "spot_amount_step": 0.000001,
        "spot_min_amount": 0.00005,
        "spot_min_notional_usd": 1.0,
        "perp_amount_step": 0.001,
        "perp_min_amount": 0.001,
        "perp_min_notional_usd": 5.0,
        "spot_price_tick": 0.01,
        "perp_price_tick": 0.1,
        "funding_interval_hours": 8.0,
    }
    base.update(overrides)
    return InstrumentRules(**base)  # type: ignore[arg-type]


def test_round_down_to_step_avoids_float_noise() -> None:
    assert round_down_to_step(0.3, 0.1) == 0.3
    assert round_down_to_step(0.29999, 0.1) == 0.2
    assert round_down_to_step(123.456, 0.01) == 123.45
    assert round_down_to_step(-1.0, 0.1) == 0.0
    assert round_down_to_step(5.0, 0.0) == 5.0


def test_round_to_tick() -> None:
    assert round_to_tick(100.004, 0.01) == 100.0
    assert round_to_tick(100.005, 0.01) == 100.01
    assert round_to_tick(7.0, 0.0) == 7.0


def test_is_multiple() -> None:
    assert is_multiple(0.3, 0.1)
    assert not is_multiple(0.35, 0.1)
    assert is_multiple(1.0, 0.0)


def test_hedged_qty_uses_coarser_step() -> None:
    rules = make_rules()
    qty = hedged_qty(notional_usd=1000.0, price=60000.0, rules=rules)
    # 1000 / 60000 = 0.016666.. -> rounded down to perp step 0.001
    assert qty == 0.016
    assert is_multiple(qty, rules.spot_amount_step)


def test_hedged_qty_below_minimums_returns_zero() -> None:
    rules = make_rules()
    assert hedged_qty(notional_usd=30.0, price=60000.0, rules=rules) == 0.0  # 0.0005 -> 0.000
    assert hedged_qty(notional_usd=0.0, price=60000.0, rules=rules) == 0.0
    assert hedged_qty(notional_usd=100.0, price=0.0, rules=rules) == 0.0


def test_hedged_qty_respects_min_notional() -> None:
    rules = make_rules(perp_min_notional_usd=500.0)
    assert hedged_qty(notional_usd=300.0, price=100.0, rules=rules) == 0.0
    assert hedged_qty(notional_usd=600.0, price=100.0, rules=rules) == 6.0


def test_hedged_qty_incompatible_steps() -> None:
    rules = make_rules(
        spot_amount_step=0.3, perp_amount_step=0.5, spot_min_amount=0.0, perp_min_amount=0.0
    )
    # 1.5 is a multiple of both 0.3 and 0.5
    assert hedged_qty(notional_usd=150.0, price=100.0, rules=rules) == 1.5
    # 1.2: coarse step 0.5 gives 1.0, not a multiple of 0.3; fine step gives 1.2, not of 0.5
    assert hedged_qty(notional_usd=120.0, price=100.0, rules=rules) == 0.0
