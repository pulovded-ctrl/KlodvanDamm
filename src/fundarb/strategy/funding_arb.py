"""The strategy: decides what the target portfolio should be. Same code for backtest, paper, live.

It never talks to an exchange. It receives a ``MarketSnapshot`` and returns ``TargetAction``s.
Confirmation counters advance only when a coin has a NEW settled funding payment, so calling
``evaluate`` more often than fundings settle does not shorten the confirmation window.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fundarb.core.config import StrategyParams
from fundarb.core.models import (
    ActionKind,
    KillSwitch,
    MarketSnapshot,
    Signal,
    TargetAction,
)
from fundarb.strategy.costs import CostModel
from fundarb.strategy.signal import evaluate_instrument
from fundarb.strategy.sizing import capital_per_notional, max_entry_notional


@dataclass(slots=True)
class _CoinState:
    last_seq: int | None = None
    entry_streak: int = 0
    exit_streak: int = 0


@dataclass(slots=True)
class FundingArbStrategy:
    params: StrategyParams
    cost_model: CostModel
    _state: dict[str, _CoinState] = field(default_factory=dict, init=False)
    last_signals: dict[str, Signal] = field(default_factory=dict, init=False)

    def state_of(self, base: str) -> _CoinState:
        return self._state.setdefault(base, _CoinState())

    def evaluate(self, snap: MarketSnapshot) -> list[TargetAction]:
        p = self.params
        signals: dict[str, Signal] = {}
        for base, inst in snap.instruments.items():
            held = snap.positions.get(base)
            if held is not None:
                notional = held.notional_usd
            else:
                notional = max_entry_notional(
                    snap.equity_usd, snap.free_cash_usd, inst, p, self.cost_model
                )
            sig = evaluate_instrument(inst, p, self.cost_model, snap.ts, notional)
            signals[base] = sig
            state = self.state_of(base)
            if state.last_seq is None or inst.funding_seq > state.last_seq:
                state.last_seq = inst.funding_seq
                if sig.eligible and sig.expected_net_apr > p.entry_threshold_apr:
                    state.entry_streak += 1
                else:
                    state.entry_streak = 0
                if sig.expected_net_apr < p.exit_threshold_apr:
                    state.exit_streak += 1
                else:
                    state.exit_streak = 0
        self.last_signals = signals

        actions: list[TargetAction] = []
        exiting: set[str] = set()
        for base, pos in snap.positions.items():
            maybe_sig = signals.get(base)
            reason = self._exit_reason(base, maybe_sig, snap.kill_switch)
            if reason is not None:
                net = maybe_sig.expected_net_apr if maybe_sig is not None else float("nan")
                actions.append(TargetAction(base, ActionKind.EXIT, pos.notional_usd, reason, net))
                exiting.add(base)

        if snap.kill_switch is not KillSwitch.NONE:
            return actions

        candidates = sorted(
            (
                sig
                for base, sig in signals.items()
                if base not in snap.positions
                and sig.eligible
                and self.state_of(base).entry_streak >= p.confirm_periods
            ),
            key=lambda s: s.expected_net_apr,
            reverse=True,
        )
        open_after_exits = len(snap.positions) - len(exiting)
        slots = p.max_positions - open_after_exits
        cash = snap.free_cash_usd
        remaining: list[Signal] = []
        for cand in candidates:
            if slots <= 0:
                remaining.append(cand)
                continue
            inst = snap.instruments[cand.base]
            notional = max_entry_notional(snap.equity_usd, cash, inst, p, self.cost_model)
            if notional <= 0:
                continue
            actions.append(
                TargetAction(
                    cand.base, ActionKind.ENTER, notional, "entry_confirmed", cand.expected_net_apr
                )
            )
            slots -= 1
            cash -= notional * capital_per_notional(p)

        if remaining and slots <= 0:
            held_signals = [
                signals[b]
                for b in snap.positions
                if b not in exiting and b in signals and signals[b].forecast_funding is not None
            ]
            if held_signals:
                worst = min(held_signals, key=lambda s: s.expected_net_apr)
                best = remaining[0]
                if best.expected_net_apr - worst.expected_net_apr > p.rotation_margin_apr:
                    actions.append(
                        TargetAction(
                            worst.base,
                            ActionKind.EXIT,
                            snap.positions[worst.base].notional_usd,
                            f"rotation_for_{best.base}",
                            worst.expected_net_apr,
                        )
                    )
        return actions

    def _exit_reason(self, base: str, sig: Signal | None, kill: KillSwitch) -> str | None:
        if kill is KillSwitch.HARD:
            return "kill_switch_hard"
        if sig is None:
            return None  # no data for this coin right now; the risk layer handles stale data
        if sig.forecast_funding is not None and sig.forecast_funding < 0:
            return "forecast_negative"
        if "event_soon" in sig.reasons:
            return "exchange_event"
        if self.state_of(base).exit_streak >= self.params.confirm_periods:
            return "below_exit_threshold"
        return None
