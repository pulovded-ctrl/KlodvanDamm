install:
	uv sync --all-groups

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest -q

data:
	uv run fundarb data sync

backtest: data
	uv run fundarb backtest

paper:
	uv run fundarb paper

live:
	uv run fundarb live --live

status:
	uv run fundarb status

flatten:
	uv run fundarb flatten

.PHONY: install lint format test data backtest paper live status flatten

data-archive:
	uv run fundarb data sync --settings config/settings.binance_vision.yaml

backtest-archive: data-archive
	uv run fundarb backtest --settings config/settings.binance_vision.yaml

.PHONY: data-archive backtest-archive
