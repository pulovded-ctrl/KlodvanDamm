from __future__ import annotations

import dataclasses
import os
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fundarb.backtest.data import build_dataset
from fundarb.backtest.engine import BacktestEngine
from fundarb.backtest.metrics import compute_metrics
from fundarb.backtest.report import ReportInputs, render_markdown, write_report
from fundarb.backtest.walkforward import WalkForwardResult
from fundarb.cli import EXIT_LIVE_REFUSED, EXIT_NO_DATA, EXIT_NOT_IMPLEMENTED, app
from fundarb.control.safety import LIVE_CONFIRMATION_PHRASE, live_gate_error
from fundarb.core.config import FeeSchedule, StrategyParams, load_strategy
from fundarb.marketdata.store import ParquetStore
from helpers import synthetic_store

ROOT = Path(__file__).resolve().parents[1]
FEES = FeeSchedule(spot_maker_bps=10, spot_taker_bps=10, perp_maker_bps=2, perp_taker_bps=5.5)
runner = CliRunner()


@pytest.fixture
def params() -> StrategyParams:
    return load_strategy(ROOT / "config" / "strategy.yaml")


def test_report_files_and_content(
    tmp_path: Path, start_dt: datetime, params: StrategyParams
) -> None:
    synthetic_store(tmp_path / "data", start_dt, days=40, funding_rate=lambda _b, _t: 0.001)
    dataset = build_dataset(ParquetStore(tmp_path / "data", "fake"))
    result = BacktestEngine(dataset, params, FEES, params.backtest).run()
    metrics = compute_metrics(result)
    wf = WalkForwardResult(skipped_reason="too little data")
    inputs = ReportInputs(
        exchange="fake",
        coverage=dataset.coverage,
        default_result=result,
        default_metrics=metrics,
        walk_forward=wf,
        params=params,
        fees=FEES,
        settings=params.backtest,
    )
    paths = write_report(inputs, tmp_path / "reports")
    for p in (paths.markdown, paths.png, paths.json, paths.latest_markdown, paths.latest_png):
        assert p.exists() and p.stat().st_size > 0
    text = paths.latest_markdown.read_text(encoding="utf-8")
    assert "Walk-forward skipped: too little data" in text
    assert "Data coverage" in text
    assert "BTC" in text
    assert "Sharpe" in text
    md = render_markdown(dataclasses.replace(inputs, walk_forward=None))
    assert "Walk-forward disabled" in md


def _write_settings(tmp_path: Path) -> Path:
    src = (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    src = src.replace("id: bybit", "id: fake").replace("dir: data", f"dir: {tmp_path / 'data'}")
    src = src.replace("reports_dir: reports", f"reports_dir: {tmp_path / 'reports'}")
    path = tmp_path / "settings.yaml"
    path.write_text(src, encoding="utf-8")
    return path


def test_cli_backtest_without_data_exits_2(tmp_path: Path) -> None:
    settings = _write_settings(tmp_path)
    res = runner.invoke(app, ["backtest", "--settings", str(settings)])
    assert res.exit_code == EXIT_NO_DATA
    assert "No data" in res.output


def test_cli_backtest_on_synthetic_store(tmp_path: Path, start_dt: datetime) -> None:
    settings = _write_settings(tmp_path)
    synthetic_store(tmp_path / "data", start_dt, days=45, funding_rate=lambda _b, _t: 0.001)
    res = runner.invoke(
        app, ["backtest", "--settings", str(settings), "--no-walk-forward", "--jobs", "1"]
    )
    assert res.exit_code == 0, res.output
    assert "Report:" in res.output
    assert (tmp_path / "reports" / "latest.md").exists()
    res2 = runner.invoke(
        app,
        ["backtest", "--settings", str(settings), "--jobs", "1", "--start", "2025-01-10",
         "--end", "2025-02-01"],
    )  # fmt: skip
    assert res2.exit_code == 0, res2.output
    assert "walk-forward" in res2.output.lower()
    bad = runner.invoke(
        app, ["backtest", "--settings", str(settings), "--start", "2025-03-01", "--jobs", "1"]
    )
    assert bad.exit_code == EXIT_NO_DATA


def test_cli_data_sync_connection_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fundarb.exchanges.ccxt_adapter as mod

    class Broken(mod.CcxtAdapter):
        async def connect(self) -> None:
            raise ConnectionError("blocked")

    monkeypatch.setattr(mod, "CcxtAdapter", Broken)  # the factory imports it lazily
    settings = _write_settings(tmp_path)
    res = runner.invoke(app, ["data", "sync", "--settings", str(settings)])
    assert res.exit_code == EXIT_NO_DATA
    assert "Could not connect" in res.output


def test_stubs_report_phase(tmp_path: Path) -> None:
    for cmd in ("paper", "status", "pause", "resume", "flatten"):
        res = runner.invoke(app, [cmd])
        assert res.exit_code == EXIT_NOT_IMPLEMENTED, cmd
        assert "not implemented" in res.output


def test_live_gate_function() -> None:
    assert live_gate_error(flag=False, env_value="true", typed_phrase=LIVE_CONFIRMATION_PHRASE)
    assert live_gate_error(flag=True, env_value="false", typed_phrase=LIVE_CONFIRMATION_PHRASE)
    assert live_gate_error(flag=True, env_value="true", typed_phrase=None)
    assert live_gate_error(flag=True, env_value="true", typed_phrase="yes")
    assert (
        live_gate_error(flag=True, env_value="TRUE ", typed_phrase=LIVE_CONFIRMATION_PHRASE) is None
    )


def test_cli_live_is_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    res = runner.invoke(app, ["live"])
    assert res.exit_code == EXIT_LIVE_REFUSED and "--live flag" in res.output
    res = runner.invoke(app, ["live", "--live"])
    assert res.exit_code == EXIT_LIVE_REFUSED and "LIVE_TRADING" in res.output
    monkeypatch.setenv("LIVE_TRADING", "true")
    res = runner.invoke(app, ["live", "--live"], input="wrong phrase\n")
    assert res.exit_code == EXIT_LIVE_REFUSED and "did not match" in res.output
    res = runner.invoke(app, ["live", "--live"], input=LIVE_CONFIRMATION_PHRASE + "\n")
    assert res.exit_code == EXIT_NOT_IMPLEMENTED  # gate open, phase 2 not built yet
    assert os.environ["LIVE_TRADING"] == "true"


def test_cli_pair_backtest(tmp_path: Path, start_dt: datetime) -> None:
    from helpers import synthetic_venue_store

    root = tmp_path / "data"
    synthetic_venue_store(root, "va", start_dt, days=45, funding_rate=lambda _b, _t: 0.0002)
    synthetic_venue_store(
        root,
        "vb",
        start_dt,
        days=45,
        funding_interval_hours=1.0,
        funding_rate=lambda _b, _t: 0.0001,
        perp_only=True,
    )
    settings_src = (ROOT / "config" / "settings.pair.yaml").read_text(encoding="utf-8")
    settings_src = settings_src.replace("dir: data", f"dir: {root}").replace(
        "reports_dir: reports", f"reports_dir: {tmp_path / 'reports'}"
    )
    settings_src = settings_src.replace("venues: [binance_vision, hyperliquid]", "venues: [va, vb]")
    settings_src = settings_src.replace("price_venue: binance_vision", "price_venue: va")
    settings_src = settings_src.replace("    binance_vision: {", "    va: {").replace(
        "    hyperliquid: {", "    vb: {"
    )
    settings_path = tmp_path / "settings.pair.yaml"
    settings_path.write_text(settings_src, encoding="utf-8")
    strategy_src = (ROOT / "config" / "strategy.pair.yaml").read_text(encoding="utf-8")
    strategy_path = tmp_path / "strategy.pair.yaml"
    strategy_path.write_text(
        strategy_src.replace("min_24h_volume_usd: 5000000", "min_24h_volume_usd: 1000000"),
        encoding="utf-8",
    )
    res = runner.invoke(
        app,
        ["backtest", "--settings", str(settings_path), "--strategy", str(strategy_path),
         "--no-walk-forward", "--jobs", "1"],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert "Pairs: 2 (1 coins, both directions)" in res.output
    text = (tmp_path / "reports" / "latest.md").read_text(encoding="utf-8")
    assert "cross-venue perp-perp" in text and "BTC:va>vb" in text
    # spot strategy file against pair settings is refused
    bad = runner.invoke(app, ["backtest", "--settings", str(settings_path), "--jobs", "1"])
    assert bad.exit_code == EXIT_NO_DATA and "long_leg: perp" in bad.output


def test_cli_data_sync_bases_from_venue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _write_settings(tmp_path)
    res = runner.invoke(
        app, ["data", "sync", "--settings", str(settings), "--bases-from-venue", "nothing"]
    )
    assert res.exit_code == EXIT_NO_DATA and "No coins downloaded yet" in res.output
