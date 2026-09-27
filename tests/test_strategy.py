from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fundarb.core.config import FeeSchedule, StrategyParams, load_strategy
from fundarb.core.models import (
    ActionKind,
    InstrumentSnapshot,
    KillSwitch,
    MarketSnapshot,
    PositionSnapshot,
)
from fundarb.strategy.costs import CostModel
from fundarb.strategy.forecast import annualize, ewma, forecast_funding
from fundarb.strategy.funding_arb import FundingArbStrategy
from fundarb.strategy.signal import evaluate_instrument
from fundarb.strategy.sizing import max_entry_notional
from helpers import make_rules

ROOT = Path(__file__).resolve().parents[1]
FEES = FeeSchedule(spot_maker_bps=10, spot_taker_bps=10, perp_maker_bps=2, perp_taker_bps=5.5)
NOW = datetime(2025, 6, 1, tzinfo=UTC)


@pytest.fixture
def params() -> StrategyParams:
    return load_strategy(ROOT / "config" / "strategy.yaml")


def cost_model(assumed_spread: float | None = 5.0) -> CostModel:
    return CostModel(FEES, slippage_coef_bps=100.0, assumed_spread_bps=assumed_spread)


def make_inst(
    base: str = "BTC",
    rates: list[float] | None = None,
    seq: int = 6,
    price: float = 100.0,
    minute_vol: float = 1e6,
    vol24: float = 1e9,
    predicted: float | None = None,
    spread_bps: float = 2.0,
    event_at: datetime | None = None,
    interval_hours: float = 8.0,
) -> InstrumentSnapshot:
    half = price * spread_bps * 1e-4 / 2
    return InstrumentSnapshot(
        rules=make_rules(base, funding_interval_hours=interval_hours),
        funding_history=rates if rates is not None else [0.001] * 6,
        funding_seq=seq,
        predicted_funding=predicted,
        spot_bid=price - half,
        spot_ask=price + half,
        perp_bid=price - half,
        perp_ask=price + half,
        spot_minute_volume_usd=minute_vol,
        perp_minute_volume_usd=minute_vol,
        spot_volume_24h_usd=vol24,
        perp_volume_24h_usd=vol24,
        event_at=event_at,
    )


def snapshot(
    insts: list[InstrumentSnapshot],
    positions: dict[str, PositionSnapshot] | None = None,
    equity: float = 10_000.0,
    cash: float | None = None,
    kill: KillSwitch = KillSwitch.NONE,
    ts: datetime = NOW,
) -> MarketSnapshot:
    return MarketSnapshot(
        ts=ts,
        equity_usd=equity,
        free_cash_usd=equity if cash is None else cash,
        instruments={i.base: i for i in insts},
        positions=positions or {},
        kill_switch=kill,
    )


# --- forecast --------------------------------------------------------------------------------


def test_ewma_constant_and_weighting() -> None:
    assert ewma([0.01] * 6, 6) == pytest.approx(0.01)
    rising = ewma([0.0, 0.0, 0.0, 0.0, 0.0, 0.01], 6)
    assert 0.0 < rising < 0.01
    assert rising == pytest.approx(2 / 7 * 0.01)


def test_forecast_needs_enough_history_and_caps_by_predicted() -> None:
    assert forecast_funding([0.01] * 5, 6, None) is None
    assert forecast_funding([0.01] * 6, 6, None) == pytest.approx(0.01)
    assert forecast_funding([0.01] * 6, 6, 0.002) == pytest.approx(0.002)
    assert forecast_funding([0.01] * 6, 6, 0.05) == pytest.approx(0.01)
    assert annualize(0.0001, 8.0) == pytest.approx(0.1095)


# --- costs -----------------------------------------------------------------------------------


def test_round_trip_cost_breakdown(params: StrategyParams) -> None:
    cm = cost_model()
    inst = make_inst(minute_vol=100_000.0)
    bd = cm.round_trip(inst, notional_usd=1000.0)
    assert bd.fees_frac == pytest.approx(27.5e-4)
    assert bd.spread_frac == pytest.approx(10e-4)  # 5 bps assumed on each leg
    assert bd.slippage_frac == pytest.approx(2e-4)  # 1 bps on each taker leg
    assert bd.total_frac == pytest.approx(39.5e-4)
    apr = cm.cost_apr(bd.total_frac, 8.0, params.hold_horizon_periods)
    assert apr == pytest.approx(39.5e-4 * 8760 / 72)


def test_live_spread_used_when_no_assumption() -> None:
    cm = cost_model(assumed_spread=None)
    inst = make_inst(spread_bps=4.0)
    bd = cm.round_trip(inst, notional_usd=1000.0)
    assert bd.spread_frac == pytest.approx(8e-4)


def test_slippage_infinite_without_volume() -> None:
    cm = cost_model()
    assert cm.slippage_bps(100.0, 0.0) == float("inf")
    assert cm.slippage_bps(0.0, 0.0) == 0.0


# --- sizing ----------------------------------------------------------------------------------


def test_sizing_takes_smallest_cap(params: StrategyParams) -> None:
    cm = cost_model()
    inst = make_inst(minute_vol=10_000.0)  # depth cap: 5% -> 500; slip cap: 10/100*10000 -> 1000
    n = max_entry_notional(10_000.0, 10_000.0, inst, params, cm)
    assert n == pytest.approx(500.0)
    n = max_entry_notional(10_000.0, 10_000.0, make_inst(minute_vol=1e9), params, cm)
    assert n == pytest.approx(1000.0)  # asset cap 10%
    n = max_entry_notional(10_000.0, 300.0, make_inst(minute_vol=1e9), params, cm)
    assert n == pytest.approx(300.0 / 1.5)  # cash cap: notional + margin at 2x
    assert max_entry_notional(10_000.0, 50.0, make_inst(minute_vol=1e9), params, cm) == 0.0


# --- signal ----------------------------------------------------------------------------------


def test_signal_reasons(params: StrategyParams) -> None:
    cm = cost_model()
    good = evaluate_instrument(make_inst(), params, cm, NOW, 1000.0)
    assert good.eligible and good.expected_net_apr > 0
    thin = evaluate_instrument(make_inst(vol24=1e6), params, cm, NOW, 1000.0)
    assert "volume_below_min" in thin.reasons
    short = evaluate_instrument(make_inst(rates=[0.001] * 3), params, cm, NOW, 1000.0)
    assert "insufficient_funding_history" in short.reasons
    neg = evaluate_instrument(make_inst(rates=[-0.001] * 6), params, cm, NOW, 1000.0)
    assert "forecast_negative" in neg.reasons
    soon = evaluate_instrument(
        make_inst(event_at=NOW + timedelta(hours=5)), params, cm, NOW, 1000.0
    )
    assert "event_soon" in soon.reasons
    later = evaluate_instrument(
        make_inst(event_at=NOW + timedelta(hours=50)), params, cm, NOW, 1000.0
    )
    assert later.eligible
    tiny = evaluate_instrument(make_inst(), params, cm, NOW, 0.0)
    assert "size_below_min" in tiny.reasons
    banned = evaluate_instrument(
        make_inst(), params.with_overrides(blacklist=["BTC"]), cm, NOW, 1000.0
    )
    assert "blacklisted" in banned.reasons


# --- strategy state machine -------------------------------------------------------------------


def test_entry_requires_confirmations_on_new_fundings(params: StrategyParams) -> None:
    strat = FundingArbStrategy(params, cost_model())
    # same funding_seq repeated: counters must not advance
    for _ in range(5):
        assert strat.evaluate(snapshot([make_inst(seq=6)])) == []
    # new observations: confirm_periods = 3 -> entry on the 3rd new observation
    assert strat.evaluate(snapshot([make_inst(seq=7)])) == []
    actions = strat.evaluate(snapshot([make_inst(seq=8)]))
    assert len(actions) == 1
    assert actions[0].kind is ActionKind.ENTER
    assert actions[0].base == "BTC"
    assert actions[0].notional_usd == pytest.approx(1000.0)


def test_exit_on_negative_forecast_and_after_confirmations(params: StrategyParams) -> None:
    strat = FundingArbStrategy(params, cost_model())
    pos = {"BTC": PositionSnapshot("BTC", qty=10.0, notional_usd=1000.0, opened_at=NOW)}
    # healthy funding: hold
    assert strat.evaluate(snapshot([make_inst(seq=6)], pos)) == []
    # weak but positive funding (0.00001 per 8h ~ 1% apr): exit only after 3 new observations
    weak = [0.00001] * 6
    assert strat.evaluate(snapshot([make_inst(rates=weak, seq=7)], pos)) == []
    assert strat.evaluate(snapshot([make_inst(rates=weak, seq=8)], pos)) == []
    actions = strat.evaluate(snapshot([make_inst(rates=weak, seq=9)], pos))
    assert [a.kind for a in actions] == [ActionKind.EXIT]
    assert actions[0].reason == "below_exit_threshold"
    # negative forecast: immediate exit regardless of counters
    fresh = FundingArbStrategy(params, cost_model())
    actions = fresh.evaluate(snapshot([make_inst(rates=[0.001] * 6, predicted=-0.001)], pos))
    assert actions[0].reason == "forecast_negative"


def test_kill_switches(params: StrategyParams) -> None:
    strat = FundingArbStrategy(params, cost_model())
    pos = {"BTC": PositionSnapshot("BTC", qty=10.0, notional_usd=1000.0, opened_at=NOW)}
    for seq in (6, 7, 8):
        strat.evaluate(snapshot([make_inst(seq=seq), make_inst("ETH", seq=seq)], pos))
    # ETH is confirmed; soft kill blocks the entry, hard kill exits BTC too
    soft = strat.evaluate(
        snapshot([make_inst(seq=8), make_inst("ETH", seq=8)], pos, kill=KillSwitch.SOFT)
    )
    assert soft == []
    hard = strat.evaluate(
        snapshot([make_inst(seq=8), make_inst("ETH", seq=8)], pos, kill=KillSwitch.HARD)
    )
    assert [(a.base, a.kind, a.reason) for a in hard] == [
        ("BTC", ActionKind.EXIT, "kill_switch_hard")
    ]


def test_rotation_replaces_worst_when_full(params: StrategyParams) -> None:
    p = params.with_overrides(max_positions=1, rotation_margin_apr=0.05)
    strat = FundingArbStrategy(p, cost_model())
    pos = {"BTC": PositionSnapshot("BTC", qty=10.0, notional_usd=1000.0, opened_at=NOW)}
    btc = [0.0006] * 6  # ~66% gross apr, ~20% net after costs
    eth = [0.002] * 6  # ~219% gross apr
    actions: list[str] = []
    for seq in (6, 7, 8):
        acts = strat.evaluate(
            snapshot([make_inst(rates=btc, seq=seq), make_inst("ETH", eth, seq)], pos)
        )
        actions = [f"{a.base}:{a.kind}:{a.reason}" for a in acts]
    assert actions == ["BTC:exit:rotation_for_ETH"]


def test_no_rotation_when_margin_not_covered(params: StrategyParams) -> None:
    p = params.with_overrides(max_positions=1, rotation_margin_apr=5.0)
    strat = FundingArbStrategy(p, cost_model())
    pos = {"BTC": PositionSnapshot("BTC", qty=10.0, notional_usd=1000.0, opened_at=NOW)}
    for seq in (6, 7, 8):
        acts = strat.evaluate(
            snapshot(
                [make_inst(rates=[0.0006] * 6, seq=seq), make_inst("ETH", [0.002] * 6, seq)], pos
            )
        )
    assert acts == []


def test_entries_limited_by_cash_and_slots(params: StrategyParams) -> None:
    p = params.with_overrides(max_positions=2)
    strat = FundingArbStrategy(p, cost_model())
    insts = [make_inst(b, seq=s) for b in ("AAA", "BBB", "CCC") for s in (8,)]
    for seq in (6, 7, 8):
        acts = strat.evaluate(snapshot([make_inst(b, seq=seq) for b in ("AAA", "BBB", "CCC")]))
    assert len(acts) == 2  # slots
    assert len(insts) == 3
    strat2 = FundingArbStrategy(p, cost_model())
    for seq in (6, 7, 8):
        acts = strat2.evaluate(
            snapshot([make_inst(b, seq=seq) for b in ("AAA", "BBB", "CCC")], cash=1200.0)
        )
    # first entry takes min(asset cap 1000, cash 1200/1.5=800)=800, leaves 0 -> second skipped
    assert [round(a.notional_usd) for a in acts] == [800]
