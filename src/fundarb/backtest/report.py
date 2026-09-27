"""Backtest report: markdown (Russian, for the owner) plus an equity/drawdown PNG and JSON."""

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

from fundarb.backtest.data import DataCoverage, coverage_summary
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


def pct(value: float, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "н/д"
    return f"{value * 100:.{digits}f}%"


def usd(value: float) -> str:
    return f"{value:,.0f} $".replace(",", " ")


def _dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "н/д"


def metrics_table(m: Metrics) -> str:
    rows = [
        ("Период", f"{_dt(m.start)} → {_dt(m.end)} ({m.days:.0f} дней)"),
        ("Капитал в начале и в конце", f"{usd(m.initial_usd)} → {usd(m.final_usd)}"),
        ("Доходность за период", pct(m.total_return)),
        ("Доходность в годовых", pct(m.annual_return)),
        ("Sharpe (по дневным данным)", f"{m.sharpe:.2f}"),
        ("Максимальная просадка", pct(m.max_drawdown)),
        ("Сделок (полных кругов)", f"{m.trades}, частичных сокращений {m.partial_closes}"),
        ("Доля прибыльных сделок", pct(m.win_rate)),
        ("Средняя длительность позиции", f"{m.avg_hold_hours / 24:.1f} дней"),
        ("Время в рынке", pct(m.time_in_market)),
        ("Оборот в год (к капиталу)", f"{m.turnover_annual:.1f}x"),
        ("Получено фандинга", usd(m.funding_usd)),
        ("Результат по базису (спот минус перп)", usd(m.basis_pnl_usd)),
        ("Комиссии, спред и проскальзывание", usd(m.fees_usd)),
        ("Доля издержек в валовом доходе", pct(m.fees_share_of_gross)),
        (
            "Стоп-краны, пополнения маржи, сокращения",
            f"{m.hard_stops}, {m.margin_topups}, {m.reductions}",
        ),
        ("Отклонённых входов (не хватило денег или размера)", str(m.rejected_entries)),
    ]
    lines = ["| Показатель | Значение |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    return "\n".join(lines)


def _fold_table(wf: WalkForwardResult) -> str:
    lines = [
        "| Отрезок | Обучение | Проверка | Выбранные параметры | Sharpe обуч. | Доход обуч. "
        "| Доход провер. | Просадка провер. | Сделок провер. |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for fr in wf.folds:
        tr, te = fr.train_metrics, fr.test_metrics
        chosen = ", ".join(f"{k}={v}" for k, v in fr.overrides.items()) or "по умолчанию"
        if fr.note:
            chosen += f" ({fr.note})"
        lines.append(
            f"| {fr.fold.number + 1} | {_dt(tr.start)} → {_dt(tr.end)} | {_dt(te.start)} → "
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
        return "Сделок не было."
    ranked = sorted(totals.items(), key=lambda kv: kv[1]["net"], reverse=True)
    lines = ["| Монета | Сделок | Фандинг | Издержки | Итог |", "|---|---|---|---|---|"]
    for base, agg in ranked[:limit]:
        lines.append(
            f"| {base} | {int(agg['n'])} | {usd(agg['funding'])} | {usd(agg['fees'])} | "
            f"{usd(agg['net'])} |"
        )
    if len(ranked) > limit:
        rest = sum(agg["net"] for _, agg in ranked[limit:])
        lines.append(f"| остальные {len(ranked) - limit} | | | | {usd(rest)} |")
    return "\n".join(lines)


def _coverage_section(cov: DataCoverage) -> str:
    summary = coverage_summary(cov)
    src = cov.source
    lines = [
        f"- Источник: биржа `{src.get('exchange', 'н/д')}`, "
        f"таймфрейм `{src.get('timeframe', 'н/д')}`, "
        f"последняя докачка `{src.get('last_sync_at', 'н/д')}`.",
        f"- Правило отбора монет: {src.get('universe_rule', 'н/д')}.",
        f"- Монет в данных: {summary['coins_total']}, пригодных: {summary['coins_usable']}.",
        f"- Диапазон: {_dt(summary['first_ts'])} → {_dt(summary['last_ts'])}.",  # type: ignore[arg-type]
    ]
    warnings = summary["warnings"]
    assert isinstance(warnings, list)
    if warnings:
        lines.append(f"- **Предупреждения ({len(warnings)}):**")
        lines += [f"  - {w}" for w in warnings[:40]]
        if len(warnings) > 40:
            lines.append(f"  - ... и ещё {len(warnings) - 40}")
    else:
        lines.append("- Предупреждений нет.")
    return "\n".join(lines)


def _assumptions(inp: ReportInputs) -> str:
    s, f = inp.settings, inp.fees
    note = [f"- **Источник данных: {inp.exchange_note}**"] if inp.exchange_note else []
    return "\n".join(
        [
            *note,
            f"- Комиссии: спот maker {f.spot_maker_bps} bps, taker {f.spot_taker_bps} bps; "
            f"перп maker {f.perp_maker_bps} bps, taker {f.perp_taker_bps} bps. "
            "Проверьте свой тариф на бирже, по умолчанию стоят публичные ставки без VIP.",
            f"- Спред: истории стакана нет, взят {s.assumed_spread_bps} bps на каждой ноге.",
            f"- Проскальзывание: {s.slippage_coef_bps} bps на каждые 100% минутного объёма.",
            f"- Маржа шорт-ноги изолированная, поддерживающая ставка "
            f"{s.maintenance_margin_rate * 100:.2f}%. Это строже unified-аккаунта.",
            "- Predicted funding биржи в бэктесте недоступен, прогноз только по истории. "
            "В живой торговле он будет ограничивать прогноз сверху.",
            "- Вселенная монет выбрана по объёму на момент скачивания: есть ошибка выжившего, "
            "результат может быть завышен.",
            "- Позиции в конце периода закрываются принудительно, чтобы учесть все издержки.",
        ]
    )


def _oos_section(wf: WalkForwardResult | None) -> str:
    if wf is None:
        return "Walk-forward отключён флагом."
    if wf.skipped_reason:
        return f"Walk-forward не проводился: {wf.skipped_reason}."
    if wf.oos_metrics is None:
        return "Walk-forward не дал ни одного проверочного отрезка."
    parts = [
        f"Сетка из {wf.grid_size} комбинаций, {len(wf.folds)} отрезков. Параметры подбирались "
        "только на обучающем отрезке и проверялись на следующем за ним.",
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
        basis = "по всему периоду с параметрами по умолчанию (walk-forward не было)"
    else:
        m = wf.oos_metrics
        basis = "по out-of-sample отрезкам walk-forward"
    if m.trades == 0:
        return f"Сделок не было {basis}: фандинг не превышал порог входа после издержек."
    tone = "положительный" if m.total_return > 0 else "отрицательный"
    return (
        f"Результат {basis}: {tone}, {pct(m.annual_return)} в годовых при просадке "
        f"{pct(m.max_drawdown)} и {m.trades} сделках. Издержки съели {pct(m.fees_share_of_gross)} "
        "валового дохода."
    )


def render_markdown(inp: ReportInputs) -> str:
    when = inp.generated_at or datetime.now(UTC)
    sections = [
        "# Отчёт бэктеста: фандинг-арбитраж",
        "",
        f"Сформирован {when.strftime('%Y-%m-%d %H:%M UTC')}. Биржа: `{inp.exchange}`. "
        f"Стартовый капитал: {usd(inp.settings.initial_capital_usd)}.",
        "",
        "## Коротко",
        "",
        _verdict(inp),
        "",
        "## Out-of-sample (главные цифры)",
        "",
        _oos_section(inp.walk_forward),
        "",
        "## Весь период с параметрами по умолчанию (для справки, in-sample)",
        "",
        metrics_table(inp.default_metrics),
        "",
        "### По монетам (весь период, параметры по умолчанию)",
        "",
        _per_coin_table(inp.default_result),
        "",
        "## Покрытие данных",
        "",
        _coverage_section(inp.coverage),
        "",
        "## Допущения и оговорки",
        "",
        _assumptions(inp),
        "",
        "## Параметры стратегии",
        "",
        "```yaml",
        _params_yaml(inp.params),
        "```",
        "",
        "![Кривая капитала](latest.png)",
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
            label="Весь период, параметры по умолчанию (in-sample)",
        )
    if not oos_eq.empty:
        ax1.plot(
            oos_eq.index,
            oos_eq.to_numpy(dtype=float) / initial * 100.0,
            color=SERIES_1,
            linewidth=2,
            label="Out-of-sample (walk-forward)",
        )
    ax1.set_ylabel("Капитал, % от начального", color=TEXT_SECONDARY)
    ax1.set_title("Кривая капитала", color=TEXT_PRIMARY, loc="left", fontsize=12)
    if not default_eq.empty or not oos_eq.empty:
        ax1.legend(frameon=False, labelcolor=TEXT_PRIMARY, fontsize=9, loc="upper left")
    dd_source = oos_eq if not oos_eq.empty else default_eq
    if not dd_source.empty:
        dd = (dd_source / dd_source.cummax() - 1.0) * 100.0
        ax2.fill_between(dd.index, dd.values, 0, color=SERIES_1, alpha=0.25, linewidth=0)
        ax2.plot(dd.index, dd.values, color=SERIES_1, linewidth=1.5)
    ax2.set_ylabel("Просадка, %", color=TEXT_SECONDARY)
    ax2.set_title(
        "Просадка " + ("out-of-sample" if not oos_eq.empty else "in-sample"),
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
