"""Backtest engine for the cross-venue perp-perp pair: long a perp on one venue, short the
same perp on another. Both legs carry isolated margin; each venue has its own cash pool and
cash is equalised between venues on a fixed schedule (a transfer we assume takes less than
that interval). Prices for both legs come from one venue, so venue basis is not modelled.
The strategy object is the same one used for spot+perp; it sees the pair as one instrument
whose funding is the net spread per period.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from fundarb.backtest.data import rolling_volume_usd
from fundarb.backtest.engine import (
    DAY_MS,
    HOUR_MS,
    LIQ_TARGET_BUFFER_PCT,
    BacktestResult,
    TradeRecord,
)
from fundarb.backtest.pair_data import PairDataset, PairSeries
from fundarb.core.config import BacktestSettings, FeeSchedule, PairSettings, StrategyParams
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


@dataclass(slots=True)
class PairPosition:
    key: str
    long_venue: str
    short_venue: str
    maker_venue: str  # maker on entry, taker on exit
    qty: float
    long_entry: float  # fill prices
    short_entry: float
    entry_mid: float
    long_margin: float
    short_margin: float
    opened_idx: int
    funding_net: float = 0.0
    fees_paid: float = 0.0
    friction_paid: float = 0.0
    last_price: float = 0.0

    def short_liq_distance_pct(self, price: float, mmr: float) -> float:
        liq = (self.short_margin + self.short_entry * self.qty) / (self.qty * (1.0 + mmr))
        return (liq / price - 1.0) * 100.0

    def long_liq_distance_pct(self, price: float, mmr: float) -> float:
        liq = (self.long_entry * self.qty - self.long_margin) / (self.qty * (1.0 - mmr))
        if liq <= 0:
            return 100.0
        return (1.0 - liq / price) * 100.0


@dataclass(slots=True)
class _Book:
    cash: dict[str, float]
    positions: dict[str, PairPosition] = field(default_factory=dict)
    trades: list[TradeRecord] = field(default_factory=list)


class PairEngine:
    def __init__(
        self,
        dataset: PairDataset,
        params: StrategyParams,
        fees: FeeSchedule,
        settings: BacktestSettings,
        pair: PairSettings,
    ) -> None:
        if params.long_leg != "perp":
            raise ValueError("PairEngine needs strategy params with long_leg: perp")
        self.data = dataset
        self.params = params
        self.settings = settings
        self.pair = pair
        self.fees = fees
        # per-venue spreads live in the snapshot, so the cost model reads them from there
        self.cost_model = CostModel(fees, settings.slippage_coef_bps, assumed_spread_bps=None)
        self._mmr = settings.maintenance_margin_rate
        bars_24h = DAY_MS // dataset.tf_ms
        self._vol24: dict[str, tuple[np.ndarray, np.ndarray]] = {
            key: (
                rolling_volume_usd(p.long_vol_usd, bars_24h),
                rolling_volume_usd(p.short_vol_usd, bars_24h),
            )
            for key, p in dataset.pairs.items()
        }

    # --- venue helpers -----------------------------------------------------------------------
    def _half_spread(self, venue: str) -> float:
        return self.pair.fees[venue].assumed_spread_bps * BPS / 2.0

    def _fee(self, venue: str, maker: bool) -> float:
        fees = self.pair.fees[venue]
        return (fees.maker_bps if maker else fees.taker_bps) * BPS

    # --- public --------------------------------------------------------------------------------
    def run(self, start_idx: int = 0, end_idx: int | None = None) -> BacktestResult:
        data = self.data
        end = len(data) if end_idx is None else min(end_idx, len(data))
        initial = self.settings.initial_capital_usd
        if end <= start_idx:
            return BacktestResult(self.params, initial, pd.Series(dtype="float64"))
        strategy = FundingArbStrategy(self.params, self.cost_model)
        book = _Book(cash={v: initial * self.pair.split_for(v) for v in self.pair.venues})
        result = BacktestResult(self.params, initial, pd.Series(dtype="float64"))
        equity_values = np.empty(end - start_idx)
        current_day = -1
        day_start_equity = initial
        halted_until = -1
        dd_limit = self.params.daily_dd_hard_stop_pct / 100.0

        for i in range(start_idx, end):
            ts_ms = int(data.index_ms[i])
            hour = (ts_ms // HOUR_MS) % 24
            for key, pos in list(book.positions.items()):
                pair = data.pairs[key]
                price = float(pair.price[i])
                if np.isnan(price) or price <= 0:
                    self._close(book, pos, pos.last_price, i, "data_gap")
                    del book.positions[key]
                    continue
                pos.last_price = price
                rate_l = float(pair.long_rate_at[i])
                if not np.isnan(rate_l):  # long pays positive funding, receives negative
                    pay = pos.qty * price * rate_l
                    book.cash[pos.long_venue] -= pay
                    pos.funding_net -= pay
                rate_s = float(pair.short_rate_at[i])
                if not np.isnan(rate_s):  # short receives positive funding, pays negative
                    recv = pos.qty * price * rate_s
                    book.cash[pos.short_venue] += recv
                    pos.funding_net += recv
                self._maintain_margin(book, pos, price, i, result)
                if pos.qty <= 0:
                    del book.positions[key]
            equity = self._equity(book)
            day = ts_ms // DAY_MS
            if day != current_day:
                current_day = day
                day_start_equity = equity
            if hour % self.pair.venue_rebalance_hours == 0:
                self._equalise_cash(book)
            if equity < day_start_equity * (1.0 - dd_limit) and book.positions:
                for key, pos in list(book.positions.items()):
                    self._close(book, pos, pos.last_price, i, "kill_switch_hard")
                    del book.positions[key]
                result.hard_stops += 1
                halted_until = ((day + 1) * DAY_MS - int(data.index_ms[0])) // data.tf_ms
                equity = self._equity(book)
            if hour % self.params.rebalance_interval_hours == 0 and i >= halted_until:
                snap = self._snapshot(i, book, equity)
                actions = strategy.evaluate(snap)
                self._execute(book, actions, i, equity, result)
                equity = self._equity(book)
            if book.positions:
                result.bars_in_market += 1
            equity_values[i - start_idx] = equity

        last = end - 1
        for key, pos in list(book.positions.items()):
            self._close(book, pos, pos.last_price, last, "end_of_backtest")
            del book.positions[key]
        equity_values[-1] = self._equity(book)
        index = pd.to_datetime(data.index_ms[start_idx:end], unit="ms", utc=True)
        result.equity = pd.Series(equity_values, index=index, name="equity")
        result.trades = book.trades
        return result

    # --- internals ---------------------------------------------------------------------------
    def _equity(self, book: _Book) -> float:
        total = sum(book.cash.values())
        for pos in book.positions.values():
            total += pos.long_margin + (pos.last_price - pos.long_entry) * pos.qty
            total += pos.short_margin + (pos.short_entry - pos.last_price) * pos.qty
        return total

    def _equalise_cash(self, book: _Book) -> None:
        """Move free cash between venues to the configured split (transfer assumed instant)."""
        total = sum(book.cash.values())
        for venue in self.pair.venues:
            book.cash[venue] = total * self.pair.split_for(venue)

    def _free_cash_for_sizing(self, book: _Book) -> float:
        """A pair needs margin on both venues: the smaller pool binds, scaled to the whole."""
        smallest = min(book.cash[v] / self.pair.split_for(v) for v in self.pair.venues)
        return max(smallest, 0.0)

    def _snapshot(self, i: int, book: _Book, equity: float) -> MarketSnapshot:
        data = self.data
        span = self.params.funding_ewma_span
        instruments: dict[str, InstrumentSnapshot] = {}
        for key, pair in data.pairs.items():
            price = float(pair.price[i])
            if np.isnan(price) or price <= 0 or pair.funding_count_at[i] == 0:
                continue
            hs_l, hs_s = self._half_spread(pair.long_venue), self._half_spread(pair.short_venue)
            vol_l, vol_s = self._vol24[key]
            instruments[key] = InstrumentSnapshot(
                rules=pair.rules,
                funding_history=pair.history_upto(i, span).tolist(),
                funding_seq=int(pair.funding_count_at[i]),
                predicted_funding=None,
                spot_bid=price * (1.0 - hs_l),
                spot_ask=price * (1.0 + hs_l),
                perp_bid=price * (1.0 - hs_s),
                perp_ask=price * (1.0 + hs_s),
                spot_minute_volume_usd=float(vol_l[i]) / 1440.0,
                perp_minute_volume_usd=float(vol_s[i]) / 1440.0,
                spot_volume_24h_usd=float(vol_l[i]),
                perp_volume_24h_usd=float(vol_s[i]),
            )
        positions = {
            key: PositionSnapshot(
                base=key,
                qty=pos.qty,
                notional_usd=pos.qty * pos.last_price,
                opened_at=data.ts(pos.opened_idx),
                liq_distance_pct=min(
                    pos.short_liq_distance_pct(pos.last_price, self._mmr),
                    pos.long_liq_distance_pct(pos.last_price, self._mmr),
                ),
            )
            for key, pos in book.positions.items()
        }
        return MarketSnapshot(
            ts=data.ts(i),
            equity_usd=equity,
            free_cash_usd=self._free_cash_for_sizing(book),
            instruments=instruments,
            positions=positions,
            kill_switch=KillSwitch.NONE,
        )

    def _execute(
        self,
        book: _Book,
        actions: list[TargetAction],
        i: int,
        equity: float,
        result: BacktestResult,
    ) -> None:
        for action in actions:
            if action.kind is ActionKind.EXIT and action.base in book.positions:
                pos = book.positions.pop(action.base)
                self._close(book, pos, pos.last_price, i, action.reason)
        reserve = equity * self.params.cash_reserve_pct / 100.0
        for action in actions:
            if action.kind is not ActionKind.ENTER or action.base in book.positions:
                continue
            opened = self._open(book, self.data.pairs[action.base], action.notional_usd, i, reserve)
            if opened is None:
                result.rejected_entries += 1
            else:
                book.positions[action.base] = opened

    def _open(
        self, book: _Book, pair: PairSeries, notional_usd: float, i: int, reserve_usd: float
    ) -> PairPosition | None:
        price = float(pair.price[i])
        lev = self.params.max_leverage_perp
        vol_l, vol_s = self._vol24[pair.key]
        # the less liquid venue goes first as maker, the other hedges as taker
        maker_venue = pair.long_venue if vol_l[i] <= vol_s[i] else pair.short_venue
        legs = {}
        for venue, side_sign, minute_vol in (
            (pair.long_venue, +1.0, float(vol_l[i]) / 1440.0),
            (pair.short_venue, -1.0, float(vol_s[i]) / 1440.0),
        ):
            maker = venue == maker_venue
            slip = 0.0 if maker else self.cost_model.slippage_bps(notional_usd, minute_vol) * BPS
            fill = price * (1.0 + side_sign * (self._half_spread(venue) + slip))
            legs[venue] = (fill, self._fee(venue, maker), slip)
        # affordability per venue: margin plus fee plus the friction baked into the fill
        notional = notional_usd
        for venue, (_fill, fee, slip) in legs.items():
            unit = 1.0 / lev + fee + self._half_spread(venue) + slip
            available = book.cash[venue] - reserve_usd * self.pair.split_for(venue)
            notional = min(notional, max(available, 0.0) / unit)
        if notional < self.params.min_notional_usd:
            return None
        qty = hedged_qty(notional, price, pair.rules)
        if qty <= 0:
            return None
        long_fill, long_fee, _ = legs[pair.long_venue]
        short_fill, short_fee, _ = legs[pair.short_venue]
        long_margin = qty * long_fill / lev
        short_margin = qty * short_fill / lev
        fee_l = qty * long_fill * long_fee
        fee_s = qty * short_fill * short_fee
        if (
            book.cash[pair.long_venue] < long_margin + fee_l
            or book.cash[pair.short_venue] < short_margin + fee_s
        ):
            return None
        book.cash[pair.long_venue] -= long_margin + fee_l
        book.cash[pair.short_venue] -= short_margin + fee_s
        return PairPosition(
            key=pair.key,
            long_venue=pair.long_venue,
            short_venue=pair.short_venue,
            maker_venue=maker_venue,
            qty=qty,
            long_entry=long_fill,
            short_entry=short_fill,
            entry_mid=price,
            long_margin=long_margin,
            short_margin=short_margin,
            opened_idx=i,
            fees_paid=fee_l + fee_s,
            friction_paid=qty * (long_fill - price) + qty * (price - short_fill),
            last_price=price,
        )

    def _close(
        self,
        book: _Book,
        pos: PairPosition,
        price: float,
        i: int,
        reason: str,
        qty: float | None = None,
        keep_margin_on: str | None = None,
    ) -> None:
        close_qty = pos.qty if qty is None else min(qty, pos.qty)
        partial = close_qty < pos.qty
        share = close_qty / pos.qty
        vol_l, vol_s = self._vol24[pos.key]
        cash_back: dict[str, float] = {}
        friction = 0.0
        fees = 0.0
        for venue, side_sign, minute_vol, entry, margin in (
            (pos.long_venue, -1.0, float(vol_l[i]) / 1440.0, pos.long_entry, pos.long_margin),
            (pos.short_venue, +1.0, float(vol_s[i]) / 1440.0, pos.short_entry, pos.short_margin),
        ):
            maker = venue != pos.maker_venue  # roles swap on exit
            slip = (
                0.0 if maker else self.cost_model.slippage_bps(close_qty * price, minute_vol) * BPS
            )
            fill = price * (1.0 + side_sign * (self._half_spread(venue) + slip))
            fee = close_qty * fill * self._fee(venue, maker)
            pnl = (fill - entry) * close_qty if side_sign < 0 else (entry - fill) * close_qty
            release = margin * share if (not partial or keep_margin_on != venue) else 0.0
            cash_back[venue] = release + pnl - fee
            friction += abs(fill - price) * close_qty
            fees += fee
        for venue, amount in cash_back.items():
            book.cash[venue] += amount
        entry_fees = pos.fees_paid * share
        entry_friction = pos.friction_paid * share
        funding = pos.funding_net * share
        book.trades.append(
            TradeRecord(
                base=pos.key,
                opened_at=self.data.ts(pos.opened_idx),
                closed_at=self.data.ts(i),
                qty=close_qty,
                notional_entry_usd=close_qty * pos.entry_mid,
                funding_usd=funding,
                basis_pnl_usd=0.0,  # one price series for both legs: no venue basis modelled
                fees_usd=entry_fees + fees,
                spread_slippage_usd=entry_friction + friction,
                reason=reason,
                partial=partial,
            )
        )
        if not partial or keep_margin_on != pos.long_venue:
            pos.long_margin -= pos.long_margin * share if partial else pos.long_margin
        if not partial or keep_margin_on != pos.short_venue:
            pos.short_margin -= pos.short_margin * share if partial else pos.short_margin
        pos.qty -= close_qty
        pos.fees_paid -= entry_fees
        pos.friction_paid -= entry_friction
        pos.funding_net -= funding

    def _maintain_margin(
        self, book: _Book, pos: PairPosition, price: float, i: int, result: BacktestResult
    ) -> None:
        min_dist = self.params.min_liq_distance_pct
        target = (min_dist + LIQ_TARGET_BUFFER_PCT) / 100.0
        mmr = self._mmr
        # short leg: hurt by rallies
        if pos.short_liq_distance_pct(price, mmr) < min_dist:
            per_unit = price * (1.0 + target) * (1.0 + mmr) - pos.short_entry
            needed = pos.qty * per_unit - pos.short_margin
            if needed > 0:
                if book.cash[pos.short_venue] >= needed:
                    book.cash[pos.short_venue] -= needed
                    pos.short_margin += needed
                    result.margin_topups += 1
                else:
                    keep_qty = pos.short_margin / per_unit if per_unit > 0 else 0.0
                    close_qty = pos.qty - keep_qty
                    if close_qty > 0:
                        result.reductions += 1
                        self._close(
                            book,
                            pos,
                            price,
                            i,
                            "margin_reduce",
                            qty=close_qty,
                            keep_margin_on=pos.short_venue,
                        )
                        if pos.qty <= 0:
                            return
        # long leg: hurt by crashes
        if pos.long_liq_distance_pct(price, mmr) < min_dist:
            per_unit = pos.long_entry - price * (1.0 - target) * (1.0 - mmr)
            needed = pos.qty * per_unit - pos.long_margin
            if needed > 0 and per_unit > 0:
                if book.cash[pos.long_venue] >= needed:
                    book.cash[pos.long_venue] -= needed
                    pos.long_margin += needed
                    result.margin_topups += 1
                else:
                    keep_qty = pos.long_margin / per_unit
                    close_qty = pos.qty - keep_qty
                    if close_qty > 0:
                        result.reductions += 1
                        self._close(
                            book,
                            pos,
                            price,
                            i,
                            "margin_reduce",
                            qty=close_qty,
                            keep_margin_on=pos.long_venue,
                        )


def pair_engine_factory(
    pair: PairSettings,
) -> Callable[[PairDataset, StrategyParams, FeeSchedule, BacktestSettings], PairEngine]:
    def make(
        dataset: PairDataset, params: StrategyParams, fees: FeeSchedule, settings: BacktestSettings
    ) -> PairEngine:
        return PairEngine(dataset, params, fees, settings, pair)

    return make


def fee_schedule_for_pair(pair: PairSettings) -> FeeSchedule:
    """The strategy's cost model needs one schedule; the sum is symmetric across directions."""
    a, b = pair.venues
    return FeeSchedule(
        spot_maker_bps=pair.fees[a].maker_bps,
        spot_taker_bps=pair.fees[a].taker_bps,
        perp_maker_bps=pair.fees[b].maker_bps,
        perp_taker_bps=pair.fees[b].taker_bps,
    )
