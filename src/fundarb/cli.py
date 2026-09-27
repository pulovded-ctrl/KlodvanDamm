"""Command line: `fundarb data sync`, `fundarb backtest`, and the phase 1/2 stubs."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from fundarb.core.config import (
    DEFAULT_SETTINGS_PATH,
    DEFAULT_STRATEGY_PATH,
    Settings,
    StrategyParams,
    load_settings,
    load_strategy,
)
from fundarb.core.logging import configure_logging

app = typer.Typer(help="fundarb: бот на фандинг-арбитраже", no_args_is_help=True)
data_app = typer.Typer(help="История с биржи (фандинг и свечи).", no_args_is_help=True)
app.add_typer(data_app, name="data")

EXIT_NOT_IMPLEMENTED = 3
SYNC_VOLUME_FRACTION = 0.25
EXIT_NO_DATA = 2
EXIT_LIVE_REFUSED = 4

SettingsOpt = Annotated[Path, typer.Option("--settings", help="Путь к settings.yaml")]
StrategyOpt = Annotated[Path, typer.Option("--strategy", help="Путь к strategy.yaml")]


def _load(settings_path: Path, strategy_path: Path) -> tuple[Settings, StrategyParams]:
    settings = load_settings(settings_path)
    params = load_strategy(strategy_path)
    configure_logging(settings.log.level, settings.log.json_output)
    return settings, params


def _parse_dt(text: str | None) -> datetime | None:
    if text is None:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- data ---------------------------------------------------------------------------------------
@data_app.command("sync")
def data_sync(
    settings_path: SettingsOpt = DEFAULT_SETTINGS_PATH,
    strategy_path: StrategyOpt = DEFAULT_STRATEGY_PATH,
    max_symbols: Annotated[int | None, typer.Option(help="Сколько монет качать")] = None,
    bases: Annotated[list[str] | None, typer.Option("--base", help="Только эти монеты")] = None,
) -> None:
    """Скачать или докачать историю фандинга и свечей."""
    from fundarb.exchanges.factory import history_adapter
    from fundarb.marketdata.history import HistorySync
    from fundarb.marketdata.store import ParquetStore

    settings, params = _load(settings_path, strategy_path)
    store = ParquetStore(settings.data.dir, settings.exchange.id, settings.data.timeframe)
    adapter = history_adapter(settings)

    async def _run() -> int:
        try:
            await adapter.connect()
        except Exception as exc:
            typer.echo(
                f"Не удалось подключиться к бирже {settings.exchange.id}: "
                f"{type(exc).__name__}: {exc}\n"
                "Проверьте интернет и доступ к API биржи."
            )
            return EXIT_NO_DATA
        try:
            sync = HistorySync(
                adapter,
                store,
                start=settings.data.history_start,
                timeframe=settings.data.timeframe,
                max_symbols=max_symbols or settings.data.max_symbols,
                # looser than the strategy's own volume rule: the strategy re-checks volume on
                # every bar, the sync must not drop coins that were liquid in the past
                min_volume_usd=params.min_24h_volume_usd * SYNC_VOLUME_FRACTION,
            )
            typer.echo(
                f"Выбираю монеты и качаю историю с {settings.data.history_start:%Y-%m-%d}..."
            )
            result = await sync.sync_all(
                bases=bases, progress=lambda b: typer.echo(f"  {b}", nl=True)
            )
        except Exception as exc:
            typer.echo(f"Ошибка при скачивании: {type(exc).__name__}: {exc}")
            return EXIT_NO_DATA
        finally:
            await adapter.close()
        ok = [b for b in result.bases if b not in result.errors]
        typer.echo(
            f"Готово: монет {len(result.bases)}, успешно {len(ok)}, "
            f"с ошибками {len(result.errors)}. "
            f"Новых строк фандинга {sum(result.funding_rows.values())}, "
            f"свечей спота {sum(result.spot_rows.values())}, "
            f"перпа {sum(result.perp_rows.values())}."
        )
        for base, err in result.errors.items():
            typer.echo(f"  ошибка {base}: {err}")
        return 0 if ok else EXIT_NO_DATA

    raise typer.Exit(code=asyncio.run(_run()))


# --- backtest -------------------------------------------------------------------------------------
@app.command()
def backtest(
    settings_path: SettingsOpt = DEFAULT_SETTINGS_PATH,
    strategy_path: StrategyOpt = DEFAULT_STRATEGY_PATH,
    jobs: Annotated[int, typer.Option(help="Параллельных процессов, 0 = все ядра")] = 0,
    no_walk_forward: Annotated[bool, typer.Option("--no-walk-forward")] = False,
    start: Annotated[str | None, typer.Option(help="Начало, например 2024-06-01")] = None,
    end: Annotated[str | None, typer.Option(help="Конец, например 2025-06-01")] = None,
    bases: Annotated[list[str] | None, typer.Option("--base", help="Только эти монеты")] = None,
) -> None:
    """Прогнать стратегию на скачанной истории и записать отчёт в reports/."""
    from fundarb.backtest.data import build_dataset, funding_environment
    from fundarb.backtest.engine import BacktestEngine
    from fundarb.backtest.metrics import compute_metrics
    from fundarb.backtest.report import ReportInputs, write_report
    from fundarb.backtest.walkforward import run_walk_forward
    from fundarb.marketdata.store import ParquetStore

    settings, params = _load(settings_path, strategy_path)
    store = ParquetStore(settings.data.dir, settings.exchange.id, settings.data.timeframe)
    dataset = build_dataset(store, bases)
    if not dataset.coins:
        typer.echo(
            "Нет данных для бэктеста: история не скачана или монеты не прошли проверку.\n"
            "Сначала выполните `make data` (нужен доступ к api.bybit.com)."
        )
        for warning in dataset.coverage.warnings[:20]:
            typer.echo(f"  {warning}")
        raise typer.Exit(code=EXIT_NO_DATA)
    start_dt, end_dt = _parse_dt(start), _parse_dt(end)
    start_idx = dataset.index_of(start_dt) if start_dt else 0
    end_idx = dataset.index_of(end_dt) if end_dt else len(dataset)
    if end_idx <= start_idx:
        typer.echo("Конец периода раньше начала или вне данных.")
        raise typer.Exit(code=EXIT_NO_DATA)
    workers = jobs if jobs > 0 else (os.cpu_count() or 1)
    typer.echo(
        f"Монет: {len(dataset.coins)}, часов: {end_idx - start_idx}, "
        f"{dataset.ts(start_idx):%Y-%m-%d} → {dataset.ts(end_idx - 1):%Y-%m-%d}."
    )
    engine = BacktestEngine(dataset, params, settings.fees, params.backtest)
    typer.echo("Прогон с параметрами по умолчанию...")
    default_result = engine.run(start_idx, end_idx)
    default_metrics = compute_metrics(default_result)
    wf = None
    if not no_walk_forward:
        typer.echo(f"Walk-forward на {workers} процессах...")
        wf = run_walk_forward(
            dataset, params, settings.fees, params.backtest,
            jobs=workers, start_idx=start_idx, end_idx=end_idx,
        )  # fmt: skip
    paths = write_report(
        ReportInputs(
            exchange=settings.exchange.id,
            coverage=dataset.coverage,
            default_result=default_result,
            default_metrics=default_metrics,
            walk_forward=wf,
            params=params,
            fees=settings.fees,
            settings=params.backtest,
            exchange_note=settings.exchange.note,
            funding_env=funding_environment(dataset),
        ),
        settings.reports_dir,
    )
    headline = wf.oos_metrics if wf is not None and wf.oos_metrics is not None else default_metrics
    kind = "out-of-sample" if wf is not None and wf.oos_metrics is not None else "in-sample"
    typer.echo(
        f"Результат ({kind}): доходность {headline.total_return * 100:.1f}% за период, "
        f"{headline.annual_return * 100:.1f}% в годовых, "
        f"просадка {headline.max_drawdown * 100:.1f}%, сделок {headline.trades}."
    )
    typer.echo(
        f"Отчёт: {paths.latest_markdown} (копия {paths.markdown.name}), график {paths.latest_png}"
    )


# --- phase 1 / 2 stubs ---------------------------------------------------------------------------
def _not_implemented(what: str, phase: int) -> None:
    typer.echo(
        f"{what}: это этап {phase}, он ещё не реализован. Сейчас доступны `data sync` и `backtest`."
    )
    raise typer.Exit(code=EXIT_NOT_IMPLEMENTED)


@app.command()
def paper() -> None:
    """Бумажная торговля на живых данных (этап 1)."""
    _not_implemented("Paper-режим", 1)


@app.command()
def live(
    live_flag: Annotated[bool, typer.Option("--live", help="Подтверждение флагом")] = False,
) -> None:
    """Реальная торговля (этап 2). Требует --live, LIVE_TRADING=true и фразу в консоли."""
    from fundarb.control.safety import LIVE_CONFIRMATION_PHRASE, LIVE_ENV_VAR, live_gate_error

    typed: str | None = None
    env_value = os.environ.get(LIVE_ENV_VAR)
    if live_flag and (env_value or "").strip().lower() == "true":
        typed = typer.prompt(f"Введите фразу «{LIVE_CONFIRMATION_PHRASE}» для подтверждения")
    error = live_gate_error(flag=live_flag, env_value=env_value, typed_phrase=typed)
    if error is not None:
        typer.echo(f"Реальная торговля НЕ включена: {error}.")
        raise typer.Exit(code=EXIT_LIVE_REFUSED)
    _not_implemented("Лайв-режим", 2)


@app.command()
def status() -> None:
    """Состояние бота (этап 1)."""
    _not_implemented("Статус", 1)


@app.command()
def pause() -> None:
    """Мягкий kill switch: запретить новые входы (этап 1)."""
    _not_implemented("Пауза", 1)


@app.command()
def resume() -> None:
    """Снять паузу (этап 1)."""
    _not_implemented("Снятие паузы", 1)


@app.command()
def flatten() -> None:
    """Жёсткий kill switch: закрыть все позиции (этап 1)."""
    _not_implemented("Закрытие всех позиций", 1)


if __name__ == "__main__":
    app()
