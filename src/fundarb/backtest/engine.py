"""Event-driven backtest on hourly bars. Runs the SAME strategy object as paper and live.

Per bar: apply funding payments, maintain margin, mark to market, check the daily drawdown
stop, and on rebalance bars build a ``MarketSnapshot`` and execute the strategy's actions
with fees, spread and slippage. Margin is isolated per position, which is stricter than a
unified account, so results are not flattered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from fundarb.backtest.data import BacktestDataset, CoinSeries, rolling_volume_usd
from fundarb.core.config import BacktestSettings, FeeSchedule, StrategyParams
from fundarb.core.models import (
    BPS,
    ActionKind,
    InstrumentSnapshot,
    KillSwitch,
    MarketSnapshot,
    PositionSnapshot,
    TargetAction,
)
from fundarb.core.rounding import hedged_qty
from fundarb.strategy.costs import CostModel
from fundarb.strategy.funding_arb import FundingArbStrategy
from fundarb.strategy.sizing import capital_per_notional

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
LIQ_TARGET_BUFFER_PCT = 10.0  # restore liquidation distance to min + this buffer


@dataclass(slots=True)
class OpenPosition:
    base: str
    qty: float
    spot_entry: float
    perp_entry: float
    margin_usd: float
    opened_idx: int
    funding_received: float = 0.0
    fees_paid: float = 0.0
    last_spot: float = 0.0
    last_perp: float = 0.0

    def liq_price(self, mmr: float) -> float:
        return (self.margin_usd + self.perp_entry * self.qty) / (self.qty * (1.0 + mmr))

    def liq_distance_pct(self, price: float, mmr: float) -> float:
        return (self.liq_price(mmr) / price - 1.0) * 100.0


@dataclass(frozen=True, slots=True)
class TradeRecord:
    base: str
    opened_at: datetime
    closed_at: datetime
    qty: float
    notional_entry_usd: float
    funding_usd: float
    basis_pnl_usd: float
    fees_usd: float
    reason: str
    partial: bool

    @property
    def net_pnl_usd(self) -> float:
        return self.funding_usd + self.basis_pnl_usd - self.fees_usd

    @property
    def hours_held(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds() / 3600.0


@dataclass(slots=True)
class BacktestResult:
    params: StrategyParams
    initial_capital_usd: float
    equity: pd.Series
    trades: list[TradeRecord] = field(default_factory=list)
    hard_stops: int = 0
    margin_topups: int = 0
    reductions: int = 0
    bars_in_market: int = 0
    rejected_entries: int = 0

    @property
    def funding_total(self) -> float:
        return sum(t.funding_usd for t in self.trades)

    @property
    def basis_total(self) -> float:
        return sum(t.basis_pnl_usd for t in self.trades)

    @property
    def fees_total(self) -> float:
        return sum(t.fees_usd for t in self.trades)


class BacktestEngine:
    def __init__(
        self,
        dataset: BacktestDataset,
        params: StrategyParams,
        fees: FeeSchedule,
        settings: BacktestSettings,
    ) -> None:
        self.data = dataset
        self.params = params
        self.fees = fees
        self.settings = settings
        self.cost_model = CostModel(fees, settings.slippage_coef_bps, settings.assumed_spread_bps)
        self._half_spread = settings.assumed_spread_bps * BPS / 2.0
        self._mmr = settings.maintenance_margin_rate
        bars_24h = DAY_MS // dataset.tf_ms
        self._vol24: dict[str, tuple[np.ndarray, np.ndarray]] = {
            base: (
                rolling_volume_usd(c.spot_vol_usd, bars_24h),
                rolling_volume_usd(c.perp_vol_usd, bars_24h),
            )
            for base, c in dataset.coins.items()
        }
        self._events: dict[str, datetime] = {}
        for ev in params.exchange_events:
            at = ev.at if ev.at.tzinfo else ev.at.replace(tzinfo=UTC)
            if ev.base not in self._events or at < self._events[ev.base]:
                self._events[ev.base] = at

    # --- public --------------------------------------------------------------------------------
    def run(self, start_idx: int = 0, end_idx: int | None = None) -> BacktestResult:
        data = self.data
        n = len(data)
        end = n if end_idx is None else min(end_idx, n)
        if end <= start_idx:
            empty = pd.Series(dtype="float64")
            return BacktestResult(self.params, self.settings.initial_capital_usd, empty)
        strategy = FundingArbStrategy(self.params, self.cost_model)
        cash = self.settings.initial_capital_usd
        positions: dict[str, OpenPosition] = {}
        trades: list[TradeRecord] = []
        result = BacktestResult(
            self.params, self.settings.initial_capital_usd, pd.Series(dtype="float64")
        )
        equity_values = np.empty(end - start_idx)
        current_day = -1
        day_start_equity = cash
        halted_until = -1
        rebalance_hours = self.params.rebalance_interval_hours
        dd_limit = self.params.daily_dd_hard_stop_pct / 100.0

        for i in range(start_idx, end):
            ts_ms = int(data.index_ms[i])
            # 1. funding payments and price refresh for open positions
            for base, pos in list(positions.items()):
                coin = data.coins[base]
                spot_px, perp_px = float(coin.spot_close[i]), float(coin.perp_close[i])
                if np.isnan(spot_px) or np.isnan(perp_px):
                    cash += self._close(pos, pos.last_spot, pos.last_perp, i, "data_gap", trades)
                    del positions[base]
                    continue
                pos.last_spot, pos.last_perp = spot_px, perp_px
                rate = float(coin.funding_rate_at[i])
                if not np.isnan(rate):
                    pay = pos.qty * perp_px * rate
                    cash += pay
                    pos.funding_received += pay
                # 2. margin maintenance
                cash = self._maintain_margin(pos, perp_px, spot_px, cash, i, trades, result)
                if pos.qty <= 0:
                    del positions[base]
            # 3. equity and daily drawdown stop
            equity = self._equity(cash, positions)
            day = ts_ms // DAY_MS
            if day != current_day:
                current_day = day
                day_start_equity = equity
            if equity < day_start_equity * (1.0 - dd_limit) and positions:
                for base, pos in list(positions.items()):
                    cash += self._close(
                        pos, pos.last_spot, pos.last_perp, i, "kill_switch_hard", trades
                    )
                    del positions[base]
                result.hard_stops += 1
                halted_until = ((day + 1) * DAY_MS - int(data.index_ms[0])) // data.tf_ms
                equity = self._equity(cash, positions)
            # 4. strategy on rebalance bars
            hour = (ts_ms // HOUR_MS) % 24
            if hour % rebalance_hours == 0 and i >= halted_until:
                snap = self._snapshot(i, cash, equity, positions)
                actions = strategy.evaluate(snap)
                cash = self._execute(actions, i, cash, positions, trades, result)
                equity = self._equity(cash, positions)
            if positions:
                result.bars_in_market += 1
            equity_values[i - start_idx] = equity

        # close everything at the end so every trade is accounted for
        last = end - 1
        for base, pos in list(positions.items()):
            cash += self._close(pos, pos.last_spot, pos.last_perp, last, "end_of_backtest", trades)
            del positions[base]
        equity_values[-1] = self._equity(cash, positions)
        index = pd.to_datetime(data.index_ms[start_idx:end], unit="ms", utc=True)
        result.equity = pd.Series(equity_values, index=index, name="equity")
        result.trades = trades
        return result

    # --- internals ---------------------------------------------------------------------------
    def _equity(self, cash: float, positions: dict[str, OpenPosition]) -> float:
        total = cash
        for pos in positions.values():
            total += pos.qty * pos.last_spot
            total += pos.margin_usd + (pos.perp_entry - pos.last_perp) * pos.qty
        return total

    def _snapshot(
        self, i: int, cash: float, equity: float, positions: dict[str, OpenPosition]
    ) -> MarketSnapshot:
        data = self.data
        now = data.ts(i)
        span = self.params.funding_ewma_span
        instruments: dict[str, InstrumentSnapshot] = {}
        for base, coin in data.coins.items():
            spot_px, perp_px = float(coin.spot_close[i]), float(coin.perp_close[i])
            if np.isnan(spot_px) or np.isnan(perp_px) or spot_px <= 0 or perp_px <= 0:
                continue
            if coin.funding_count_at[i] == 0:
                continue
            spot24, perp24 = self._vol24[base]
            instruments[base] = InstrumentSnapshot(
                rules=coin.rules,
                funding_history=coin.history_upto(i, span).tolist(),
                funding_seq=int(coin.funding_count_at[i]),
                predicted_funding=None,  # not known without look-ahead; see DECISIONS.md
                spot_bid=spot_px * (1.0 - self._half_spread),
                spot_ask=spot_px * (1.0 + self._half_spread),
                perp_bid=perp_px * (1.0 - self._half_spread),
                perp_ask=perp_px * (1.0 + self._half_spread),
                spot_minute_volume_usd=float(spot24[i]) / 1440.0,
                perp_minute_volume_usd=float(perp24[i]) / 1440.0,
                spot_volume_24h_usd=float(spot24[i]),
                perp_volume_24h_usd=float(perp24[i]),
                event_at=self._events.get(base),
            )
        pos_snaps = {
            base: PositionSnapshot(
                base=base,
                qty=pos.qty,
                notional_usd=pos.qty * pos.last_spot,
                opened_at=data.ts(pos.opened_idx),
                liq_distance_pct=pos.liq_distance_pct(pos.last_perp, self._mmr),
            )
            for base, pos in positions.items()
        }
        return MarketSnapshot(
            ts=now,
            equity_usd=equity,
            free_cash_usd=cash,
            instruments=instruments,
            positions=pos_snaps,
            kill_switch=KillSwitch.NONE,
        )

    def _execute(
        self,
        actions: list[TargetAction],
        i: int,
        cash: float,
        positions: dict[str, OpenPosition],
        trades: list[TradeRecord],
        result: BacktestResult,
    ) -> float:
        for action in actions:
            if action.kind is ActionKind.EXIT and action.base in positions:
                pos = positions.pop(action.base)
                cash += self._close(pos, pos.last_spot, pos.last_perp, i, action.reason, trades)
        for action in actions:
            if action.kind is not ActionKind.ENTER or action.base in positions:
                continue
            coin = self.data.coins[action.base]
            opened, cash = self._open(coin, action.notional_usd, i, cash)
            if opened is None:
                result.rejected_entries += 1
            else:
                positions[action.base] = opened
        return cash

    def _open(
        self, coin: CoinSeries, notional_usd: float, i: int, cash: float
    ) -> tuple[OpenPosition | None, float]:
        spot_px, perp_px = float(coin.spot_close[i]), float(coin.perp_close[i])
        spot_fill = spot_px * (1.0 + self._half_spread)  # first leg: spot, maker, pays half spread
        perp_minute_vol = float(self._vol24[coin.rules.base][1][i]) / 1440.0
        slip = self.cost_model.slippage_bps(notional_usd, perp_minute_vol) * BPS
        perp_fill = perp_px * (1.0 - self._half_spread - slip)  # second leg: perp short, taker
        fee_frac = (self.fees.spot_maker_bps + self.fees.perp_taker_bps) * BPS
        unit_cost = capital_per_notional(self.params) + fee_frac + self._half_spread
        affordable = max(cash, 0.0) / unit_cost
        notional = min(notional_usd, affordable)
        if notional < self.params.min_notional_usd:
            return None, cash
        qty = hedged_qty(notional, spot_fill, coin.rules)
        if qty <= 0:
            return None, cash
        spot_cost = qty * spot_fill
        fee_spot = spot_cost * self.fees.spot_maker_bps * BPS
        fee_perp = qty * perp_fill * self.fees.perp_taker_bps * BPS
        margin = qty * perp_fill / self.params.max_leverage_perp
        total = spot_cost + fee_spot + fee_perp + margin
        if total > cash:
            return None, cash
        cash -= total
        pos = OpenPosition(
            base=coin.rules.base,
            qty=qty,
            spot_entry=spot_fill,
            perp_entry=perp_fill,
            margin_usd=margin,
            opened_idx=i,
            fees_paid=fee_spot + fee_perp,
            last_spot=spot_px,
            last_perp=perp_px,
        )
        return pos, cash

    def _close(
        self,
        pos: OpenPosition,
        spot_px: float,
        perp_px: float,
        i: int,
        reason: str,
        trades: list[TradeRecord],
        qty: float | None = None,
        release_margin: bool = True,
    ) -> float:
        """Close ``qty`` coins (default all). Returns the cash released. Partial closes keep
        the margin on the remaining position unless ``release_margin``."""
        close_qty = pos.qty if qty is None else min(qty, pos.qty)
        partial = close_qty < pos.qty
        perp_fill = perp_px * (1.0 + self._half_spread)  # first leg: buy back perp, maker
        spot_minute_vol = float(self._vol24[pos.base][0][i]) / 1440.0
        slip = self.cost_model.slippage_bps(close_qty * spot_px, spot_minute_vol) * BPS
        spot_fill = spot_px * (1.0 - self._half_spread - slip)  # second leg: sell spot, taker
        fee_perp = close_qty * perp_fill * self.fees.perp_maker_bps * BPS
        fee_spot = close_qty * spot_fill * self.fees.spot_taker_bps * BPS
        perp_pnl = (pos.perp_entry - perp_fill) * close_qty
        spot_pnl = (spot_fill - pos.spot_entry) * close_qty
        share = close_qty / pos.qty
        margin_released = pos.margin_usd * share if (release_margin or not partial) else 0.0
        entry_fees = pos.fees_paid * share
        funding = pos.funding_received * share
        trades.append(
            TradeRecord(
                base=pos.base,
                opened_at=self.data.ts(pos.opened_idx),
                closed_at=self.data.ts(i),
                qty=close_qty,
                notional_entry_usd=close_qty * pos.spot_entry,
                funding_usd=funding,
                basis_pnl_usd=perp_pnl + spot_pnl,
                fees_usd=entry_fees + fee_perp + fee_spot,
                reason=reason,
                partial=partial,
            )
        )
        cash_back = margin_released + perp_pnl + close_qty * spot_fill - fee_perp - fee_spot
        pos.qty -= close_qty
        pos.margin_usd -= margin_released
        pos.fees_paid -= entry_fees
        pos.funding_received -= funding
        return cash_back

    def _maintain_margin(
        self,
        pos: OpenPosition,
        perp_px: float,
        spot_px: float,
        cash: float,
        i: int,
        trades: list[TradeRecord],
        result: BacktestResult,
    ) -> float:
        distance = pos.liq_distance_pct(perp_px, self._mmr)
        if distance >= self.params.min_liq_distance_pct:
            return cash
        target = (self.params.min_liq_distance_pct + LIQ_TARGET_BUFFER_PCT) / 100.0
        per_unit = perp_px * (1.0 + target) * (1.0 + self._mmr) - pos.perp_entry
        needed = pos.qty * per_unit - pos.margin_usd
        if needed <= 0:
            return cash
        if cash >= needed:
            cash -= needed
            pos.margin_usd += needed
            result.margin_topups += 1
            return cash
        # not enough cash: keep all margin, shrink the position until it is safe
        keep_qty = pos.margin_usd / per_unit if per_unit > 0 else 0.0
        close_qty = pos.qty - keep_qty
        if close_qty <= 0:
            return cash
        result.reductions += 1
        cash += self._close(
            pos, spot_px, perp_px, i, "margin_reduce", trades, qty=close_qty, release_margin=False
        )
        return cash
