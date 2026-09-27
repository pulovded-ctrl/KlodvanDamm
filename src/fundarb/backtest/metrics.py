"""Performance metrics on an equity curve and its trades."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from fundarb.backtest.engine import BacktestResult, TradeRecord


@dataclass(frozen=True, slots=True)
class Metrics:
    start: datetime | None
    end: datetime | None
    days: float
    initial_usd: float
    final_usd: float
    total_return: float
    annual_return: float
    sharpe: float
    max_drawdown: float
    trades: int
    partial_closes: int
    win_rate: float
    avg_hold_hours: float
    turnover_annual: float
    time_in_market: float
    funding_usd: float
    basis_pnl_usd: float
    fees_usd: float
    spread_slippage_usd: float
    fees_share_of_gross: float  # (fees + spread + slippage) / (funding + basis)
    hard_stops: int
    margin_topups: int
    reductions: int
    rejected_entries: int

    def as_dict(self) -> dict[str, object]:
        out = asdict(self)
        for key in ("start", "end"):
            value = out[key]
            out[key] = value.isoformat() if isinstance(value, datetime) else None
        return out


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(-dd.min())


def sharpe_ratio(equity: pd.Series) -> float:
    """Annualised Sharpe of daily returns, risk-free rate 0."""
    if equity.empty:
        return 0.0
    daily = equity.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    if len(rets) < 2:
        return 0.0
    std = float(rets.std(ddof=1))
    if std == 0.0 or math.isnan(std):
        return 0.0
    return float(rets.mean() / std * math.sqrt(365.0))


def compute_metrics(result: BacktestResult, trades: list[TradeRecord] | None = None) -> Metrics:
    eq = result.equity
    trades = result.trades if trades is None else trades
    initial = result.initial_capital_usd
    if eq.empty:
        return Metrics(
            None, None, 0.0, initial, initial, 0.0, 0.0, 0.0, 0.0, 0, 0, 0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 0.0, 0.0, result.hard_stops, result.margin_topups, result.reductions,
            result.rejected_entries,
        )  # fmt: skip
    start = eq.index[0].to_pydatetime()
    end = eq.index[-1].to_pydatetime()
    days = max((end - start).total_seconds() / 86400.0, 1e-9)
    final = float(eq.iloc[-1])
    total_return = final / initial - 1.0
    annual_return = (final / initial) ** (365.0 / days) - 1.0 if days >= 1 and final > 0 else 0.0
    full = [t for t in trades if not t.partial]
    wins = sum(1 for t in full if t.net_pnl_usd > 0)
    traded_notional = sum(t.notional_entry_usd for t in trades) * 2.0  # in and out
    avg_equity = float(eq.mean())
    turnover = traded_notional / avg_equity / (days / 365.0) if avg_equity > 0 else 0.0
    funding = sum(t.funding_usd for t in trades)
    basis = sum(t.basis_pnl_usd for t in trades)
    fees = sum(t.fees_usd for t in trades)
    friction = sum(t.spread_slippage_usd for t in trades)
    gross = funding + basis
    fees_share = (fees + friction) / gross if gross > 0 else float("nan")
    return Metrics(
        start=start,
        end=end,
        days=days,
        initial_usd=initial,
        final_usd=final,
        total_return=total_return,
        annual_return=annual_return,
        sharpe=sharpe_ratio(eq),
        max_drawdown=max_drawdown(eq),
        trades=len(full),
        partial_closes=len(trades) - len(full),
        win_rate=wins / len(full) if full else 0.0,
        avg_hold_hours=float(np.mean([t.hours_held for t in full])) if full else 0.0,
        turnover_annual=turnover,
        time_in_market=result.bars_in_market / len(eq) if len(eq) else 0.0,
        funding_usd=funding,
        basis_pnl_usd=basis,
        fees_usd=fees,
        spread_slippage_usd=friction,
        fees_share_of_gross=fees_share,
        hard_stops=result.hard_stops,
        margin_topups=result.margin_topups,
        reductions=result.reductions,
        rejected_entries=result.rejected_entries,
    )
