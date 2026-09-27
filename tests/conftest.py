"""Shared fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest


@pytest.fixture
def start_dt() -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC)
