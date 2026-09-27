"""Backtest report: markdown plus an equity/drawdown PNG and a JSON with the metrics."""

from __future__ import annotations

import json
import math
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import pandas as pd

from fundarb.backtest.data import DataCoverage, FundingEnv, coverage_summary
from fundarb.backtest.engine import BacktestResult
from fundarb.backtest.metrics import Metrics
from fundarb.backtest.walkforward import WalkForwardResult
from fundarb.core.config import BacktestSettings, FeeSchedule, StrategyParams

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# reference palette (validated, see dataviz notes): slot 1 blue, slot 2 orange
SERIES_1 = "#2a78d6"
SERIES_2 = "#eb6834"
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e6e5e1"


@dataclass(frozen=True, slots=True)
class ReportPaths:
    markdown: Path
    png: Path
    json: Path
    latest_markdown: Path
    latest_png: Path


@dataclass(slots=True)
class ReportInputs:
    exchange: str
    coverage: DataCoverage
    default_result: BacktestResult
    default_metrics: Metrics
    walk_forward: WalkForwardResult | None
    params: StrategyParams
    fees: FeeSchedule
    settings: BacktestSettings
    generated_at: datetime | None = None
    exchange_note: str = ""
    funding_env: list[FundingEnv] | None = None
    mode: str = "spot long + perp short"


def pct(value: float, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def usd(value: float) -> str:
    return f"{value:,.0f} $".replace(",", " ")


def _dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "n/a"


def metrics_table(m: Metrics) -> str:
    rows = [
        ("Period", f"{_dt(m.start)} to {_dt(m.end)} ({m.days:.0f} days)"),
        ("Capital, start to end", f"{usd(m.initial_usd)} to {usd(m.final_usd)}"),
        ("Return over the period", pct(m.total_return)),
        ("Annualised return", pct(m.annual_return)),
        ("Sharpe (daily returns)", f"{m.sharpe:.2f}"),
        ("Max drawdown", pct(m.max_drawdown)),
        ("Trades (full round trips)", f"{m.trades}, partial reductions {m.partial_closes}"),
        ("Winning trades", pct(m.win_rate)),
        ("Average holding time", f"{m.avg_hold_hours / 24:.1f} days"),
        ("Time in market", pct(m.time_in_market)),
        ("Annual turnover (x capital)", f"{m.turnover_annual:.1f}x"),
        ("Funding received", usd(m.funding_usd)),
        ("Basis P&L (spot minus perp, at mid prices)", usd(m.basis_pnl_usd)),
        ("Exchange fees", usd(m.fees_usd)),
        ("Spread and slippage", usd(m.spread_slippage_usd)),
        ("All costs as a share of gross income", pct(m.fees_share_of_gross)),
        (
            "Hard stops, margin top-ups, reductions",
            f"{m.hard_stops}, {m.margin_topups}, {m.reductions}",
        ),
        ("Rejected entries (not enough cash or size)", str(m.rejected_entries)),
    ]
    lines = ["| Metric | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines)


def _fold_table(wf: WalkForwardResult) -> str:
    lines = [
        "| Fold | Training | Test | Chosen parameters | Train Sharpe | Train return "
        "| Test return | Test drawdown | Test trades |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for fr in wf.folds:
        tr, te = fr.train_metrics, fr.test_metrics
        chosen = ", ".join(f"{k}={v}" for k, v in fr.overrides.items()) or "defaults"
        if fr.note:
            chosen += f" ({fr.note})"
        lines.append(
            f"| {fr.fold.number + 1} | {_dt(tr.start)} to {_dt(tr.end)} | {_dt(te.start)} to "
            f"{_dt(te.end)} | {chosen} | {tr.sharpe:.2f} | {pct(tr.total_return)} | "
            f"{pct(te.total_return)} | {pct(te.max_drawdown)} | {te.trades} |"
        )
    return "\n".join(lines)


def _per_coin_table(result: BacktestResult, limit: int = 12) -> str:
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in result.trades:
        totals[t.base]["net"] += t.net_pnl_usd
        totals[t.base]["funding"] += t.funding_usd
        totals[t.base]["fees"] += t.fees_usd
        totals[t.base]["n"] += 0 if t.partial else 1
    if not totals:
        return "No trades."
    ranked = sorted(totals.items(), key=lambda kv: kv[1]["net"], reverse=True)
    lines = ["| Coin | Trades | Funding | Costs | Net |", "|---|---|---|---|---|"]
    for base, agg in ranked[:limit]:
        lines.append(
            f"| {base} | {int(agg['n'])} | {usd(agg['funding'])} | {usd(agg['fees'])} | "
            f"{usd(agg['net'])} |"
        )
    if len(ranked) > limit:
        rest = sum(agg["net"] for _, agg in ranked[limit:])
        lines.append(f"| other {len(ranked) - limit} | | | | {usd(rest)} |")
    return "\n".join(lines)


def _coverage_section(cov: DataCoverage) -> str:
    summary = coverage_summary(cov)
    src = cov.source
    lines = [
        f"- Source: exchange `{src.get('exchange', 'n/a')}`, "
        f"timeframe `{src.get('timeframe', 'n/a')}`, "
        f"last sync `{src.get('last_sync_at', 'n/a')}`.",
        f"- Coin selection rule: {src.get('universe_rule', 'n/a')}.",
        f"- Coins in data: {summary['coins_total']}, usable: {summary['coins_usable']}.",
        f"- Range: {_dt(summary['first_ts'])} to {_dt(summary['last_ts'])}.",  # type: ignore[arg-type]
    ]
    warnings = summary["warnings"]
    assert isinstance(warnings, list)
    if warnings:
        lines.append(f"- **Warnings ({len(warnings)}):**")
        lines += [f"  - {w}" for w in warnings[:40]]
        if len(warnings) > 40:
            lines.append(f"  - ... and {len(warnings) - 40} more")
    else:
        lines.append("- No warnings.")
    return "\n".join(lines)


def _assumptions(inp: ReportInputs) -> str:
    s, f = inp.settings, inp.fees
    note = [f"- **Data source: {inp.exchange_note}**"] if inp.exchange_note else []
    return "\n".join(
        [
            *note,
            f"- Fees: spot maker {f.spot_maker_bps} bps, taker {f.spot_taker_bps} bps; "
            f"perp maker {f.perp_maker_bps} bps, taker {f.perp_taker_bps} bps. "
            "Check your own tier on the exchange; defaults are public non-VIP rates.",
            f"- Spread: no order-book history, {s.assumed_spread_bps} bps assumed on each leg.",
            f"- Slippage: {s.slippage_coef_bps} bps per 100% of one minute's volume.",
            f"- Short-leg margin is isolated, maintenance rate "
            f"{s.maintenance_margin_rate * 100:.2f}%. Stricter than a unified account.",
            "- The exchange's predicted funding is not available in the backtest; the forecast "
            "uses history only. Live trading caps the forecast with it.",
            "- The coin universe was picked by volume at download time: survivorship bias, "
            "results may be flattered.",
            "- Positions are force-closed at the end of the period so every cost is counted.",
        ]
    )


def _funding_env_section(env: list[FundingEnv] | None) -> str:
    if not env:
        return "No funding data."
    total = sum(e.settlements for e in env)
    if total == 0:
        return "No funding data."
    above15 = sum(e.share_above_15 * e.settlements for e in env) / total
    above50 = sum(e.share_above_50 * e.settlements for e in env) / total
    negative = sum(e.share_negative * e.settlements for e in env) / total
    lines = [
        f"Across all coins and settlements: funding above 15% annualised in {pct(above15)} "
        f"of settlements, above 50% in {pct(above50)}, negative in {pct(negative)}. "
        "The strategy only earns on settlements above the entry threshold after costs; "
        "the rest of the time it waits in cash.",
        "",
        "| Coin | Settlements | Mean funding, annualised | Median | Share above 15% "
        "| Share negative |",
        "|---|---|---|---|---|---|",
    ]
    for e in sorted(env, key=lambda x: x.mean_apr, reverse=True)[:15]:
        lines.append(
            f"| {e.base} | {e.settlements} | {pct(e.mean_apr)} | {pct(e.median_apr)} | "
            f"{pct(e.share_above_15)} | {pct(e.share_negative)} |"
        )
    if len(env) > 15:
        lines.append(f"| ... {len(env) - 15} more coins | | | | | |")
    return "\n".join(lines)


def _oos_section(wf: WalkForwardResult | None) -> str:
    if wf is None:
        return "Walk-forward disabled by flag."
    if wf.skipped_reason:
        return f"Walk-forward skipped: {wf.skipped_reason}."
    if wf.oos_metrics is None:
        return "Walk-forward produced no test fold."
    parts = [
        f"Grid of {wf.grid_size} combinations, {len(wf.folds)} folds. Parameters were tuned "
        "on the training window only and evaluated on the window that follows it.",
        "",
        metrics_table(wf.oos_metrics),
        "",
        _fold_table(wf),
    ]
    return "\n".join(parts)


def _verdict(inp: ReportInputs) -> str:
    wf = inp.walk_forward
    if wf is None or wf.oos_metrics is None:
        m = inp.default_metrics
        basis = "over the full period with default parameters (no walk-forward)"
    else:
        m = wf.oos_metrics
        basis = "on the out-of-sample walk-forward folds"
    if m.trades == 0:
        return f"No trades {basis}: funding never exceeded the entry threshold after costs."
    tone = "positive" if m.total_return > 0 else "negative"
    return (
        f"Result {basis}: {tone}, {pct(m.annual_return)} annualised with a max drawdown of "
        f"{pct(m.max_drawdown)} over {m.trades} trades. Costs took {pct(m.fees_share_of_gross)} "
        "of gross income."
    )


def render_markdown(inp: ReportInputs) -> str:
    when = inp.generated_at or datetime.now(UTC)
    sections = [
        "# Backtest report: funding-rate arbitrage",
        "",
        f"Generated {when.strftime('%Y-%m-%d %H:%M UTC')}. Exchange: `{inp.exchange}`. "
        f"Mode: {inp.mode}. Initial capital: {usd(inp.settings.initial_capital_usd)}.",
        "",
        "## Summary",
        "",
        _verdict(inp),
        "",
        "## Out-of-sample (the numbers that matter)",
        "",
        _oos_section(inp.walk_forward),
        "",
        "## Full period with default parameters (reference, in-sample)",
        "",
        metrics_table(inp.default_metrics),
        "",
        "### By coin (full period, default parameters)",
        "",
        _per_coin_table(inp.default_result),
        "",
        "## Funding environment: what the market actually paid",
        "",
        _funding_env_section(inp.funding_env),
        "",
        "## Data coverage",
        "",
        _coverage_section(inp.coverage),
        "",
        "## Assumptions and caveats",
        "",
        _assumptions(inp),
        "",
        "## Strategy parameters",
        "",
        "```yaml",
        _params_yaml(inp.params),
        "```",
        "",
        "![Equity curve](latest.png)",
        "",
    ]
    return "\n".join(sections)


def _params_yaml(params: StrategyParams) -> str:
    data = params.model_dump(exclude={"backtest", "exchange_events"})
    return "\n".join(f"{k}: {v}" for k, v in data.items())


def render_png(inp: ReportInputs, path: Path) -> None:
    default_eq = inp.default_result.equity
    oos_eq = (
        inp.walk_forward.oos_equity if inp.walk_forward is not None else pd.Series(dtype="float64")
    )
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    fig.patch.set_facecolor(SURFACE)
    for ax in (ax1, ax2):
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.8)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(GRID)
        ax.tick_params(colors=TEXT_SECONDARY, labelsize=9)
    initial = inp.settings.initial_capital_usd
    if not default_eq.empty:
        ax1.plot(
            default_eq.index,
            default_eq.to_numpy(dtype=float) / initial * 100.0,
            color=SERIES_2,
            linewidth=2,
            label="Full period, default parameters (in-sample)",
        )
    if not oos_eq.empty:
        ax1.plot(
            oos_eq.index,
            oos_eq.to_numpy(dtype=float) / initial * 100.0,
            color=SERIES_1,
            linewidth=2,
            label="Out-of-sample (walk-forward)",
        )
    ax1.set_ylabel("Capital, % of initial", color=TEXT_SECONDARY)
    ax1.set_title("Equity curve", color=TEXT_PRIMARY, loc="left", fontsize=12)
    if not default_eq.empty or not oos_eq.empty:
        ax1.legend(frameon=False, labelcolor=TEXT_PRIMARY, fontsize=9, loc="upper left")
    dd_source = oos_eq if not oos_eq.empty else default_eq
    if not dd_source.empty:
        dd = (dd_source / dd_source.cummax() - 1.0) * 100.0
        ax2.fill_between(dd.index, dd.values, 0, color=SERIES_1, alpha=0.25, linewidth=0)
        ax2.plot(dd.index, dd.values, color=SERIES_1, linewidth=1.5)
    ax2.set_ylabel("Drawdown, %", color=TEXT_SECONDARY)
    ax2.set_title(
        "Drawdown " + ("out-of-sample" if not oos_eq.empty else "in-sample"),
        color=TEXT_PRIMARY,
        loc="left",
        fontsize=11,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor=SURFACE)
    plt.close(fig)


def write_report(inp: ReportInputs, out_dir: Path) -> ReportPaths:
    out_dir.mkdir(parents=True, exist_ok=True)
    when = inp.generated_at or datetime.now(UTC)
    stem = f"backtest_{when.strftime('%Y%m%d_%H%M%S')}"
    md_path, png_path, json_path = (out_dir / f"{stem}.{ext}" for ext in ("md", "png", "json"))
    latest_md, latest_png = out_dir / "latest.md", out_dir / "latest.png"
    render_png(inp, png_path)
    shutil.copyfile(png_path, latest_png)
    markdown = render_markdown(inp).replace("latest.png", png_path.name)
    md_path.write_text(markdown, encoding="utf-8")
    latest_md.write_text(render_markdown(inp), encoding="utf-8")
    payload = {
        "generated_at": when.isoformat(),
        "exchange": inp.exchange,
        "default": inp.default_metrics.as_dict(),
        "oos": inp.walk_forward.oos_metrics.as_dict()
        if inp.walk_forward and inp.walk_forward.oos_metrics
        else None,
        "walk_forward_skipped": inp.walk_forward.skipped_reason if inp.walk_forward else "disabled",
        "folds": [
            {
                "fold": fr.fold.number,
                "overrides": fr.overrides,
                "train": fr.train_metrics.as_dict(),
                "test": fr.test_metrics.as_dict(),
                "note": fr.note,
            }
            for fr in (inp.walk_forward.folds if inp.walk_forward else [])
        ],
        "coverage": coverage_summary(inp.coverage),
        "params": inp.params.model_dump(mode="json"),
    }
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return ReportPaths(md_path, png_path, json_path, latest_md, latest_png)
