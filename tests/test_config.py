from pathlib import Path

import pytest

from fundarb.core.config import load_settings, load_strategy

ROOT = Path(__file__).resolve().parents[1]


def test_load_repo_configs() -> None:
    settings = load_settings(ROOT / "config" / "settings.yaml")
    params = load_strategy(ROOT / "config" / "strategy.yaml")
    assert settings.mode == "paper"
    assert settings.exchange.id == "bybit"
    assert params.entry_threshold_apr > params.exit_threshold_apr
    assert params.backtest.initial_capital_usd > 0
    assert settings.fees.round_trip_fee_frac == pytest.approx((10 + 5.5 + 2 + 10) * 1e-4)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "strategy.yaml"
    src = (ROOT / "config" / "strategy.yaml").read_text(encoding="utf-8")
    bad.write_text(src + "\nentry_treshold_apr: 0.5\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_strategy(bad)


def test_exit_must_be_below_entry(tmp_path: Path) -> None:
    bad = tmp_path / "strategy.yaml"
    src = (ROOT / "config" / "strategy.yaml").read_text(encoding="utf-8")
    bad.write_text(
        src.replace("exit_threshold_apr: 0.04", "exit_threshold_apr: 0.5"), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_strategy(bad)


def test_with_overrides_keeps_other_fields() -> None:
    params = load_strategy(ROOT / "config" / "strategy.yaml")
    tuned = params.with_overrides(entry_threshold_apr=0.2)
    assert tuned.entry_threshold_apr == 0.2
    assert tuned.confirm_periods == params.confirm_periods
