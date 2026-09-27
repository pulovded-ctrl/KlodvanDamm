"""Parquet storage for history: funding rates and candles per base coin, plus instrument rules."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Literal

import pandas as pd

from fundarb.core.models import Candle, FundingRecord, InstrumentRules

Kind = Literal["spot", "perp"]

FUNDING_COLUMNS = {"ts_ms": "int64", "rate": "float64"}
OHLCV_COLUMNS = {
    "ts_ms": "int64",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "float64",
}


def _empty(columns: Mapping[str, str]) -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype=dtype) for name, dtype in columns.items()})


def _merge(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        merged = new
    elif new.empty:
        merged = existing
    else:
        merged = pd.concat([existing, new], ignore_index=True)
    merged = merged.drop_duplicates(subset="ts_ms", keep="last").sort_values("ts_ms")
    return merged.reset_index(drop=True)


class ParquetStore:
    def __init__(self, root: Path, exchange_id: str, timeframe: str = "1h") -> None:
        self.root = Path(root) / exchange_id
        self.timeframe = timeframe

    # --- paths -----------------------------------------------------------------------------
    def instruments_path(self) -> Path:
        return self.root / "instruments.json"

    def meta_path(self) -> Path:
        return self.root / "meta.json"

    def funding_path(self, base: str) -> Path:
        return self.root / "funding" / f"{base}.parquet"

    def ohlcv_path(self, kind: Kind, base: str) -> Path:
        return self.root / f"ohlcv_{self.timeframe}" / kind / f"{base}.parquet"

    # --- instruments and meta ----------------------------------------------------------------
    def save_instruments(self, rules: Mapping[str, InstrumentRules]) -> None:
        self.instruments_path().parent.mkdir(parents=True, exist_ok=True)
        payload = {base: asdict(r) for base, r in sorted(rules.items())}
        self.instruments_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load_instruments(self) -> dict[str, InstrumentRules]:
        path = self.instruments_path()
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {base: InstrumentRules(**fields) for base, fields in payload.items()}

    def save_meta(self, meta: Mapping[str, object]) -> None:
        self.meta_path().parent.mkdir(parents=True, exist_ok=True)
        self.meta_path().write_text(json.dumps(dict(meta), indent=2, default=str), encoding="utf-8")

    def load_meta(self) -> dict[str, object]:
        path = self.meta_path()
        if not path.exists():
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return dict(loaded) if isinstance(loaded, dict) else {}

    # --- funding -----------------------------------------------------------------------------
    def read_funding(self, base: str) -> pd.DataFrame:
        path = self.funding_path(base)
        if not path.exists():
            return _empty(FUNDING_COLUMNS)
        return pd.read_parquet(path).astype(FUNDING_COLUMNS)

    def append_funding(self, base: str, records: Sequence[FundingRecord]) -> int:
        if not records:
            return 0
        new = pd.DataFrame([asdict(r) for r in records]).astype(FUNDING_COLUMNS)
        existing = self.read_funding(base)
        merged = _merge(existing, new)
        self._write(self.funding_path(base), merged)
        return len(merged) - len(existing)

    def last_funding_ts(self, base: str) -> int | None:
        df = self.read_funding(base)
        return None if df.empty else int(df["ts_ms"].iloc[-1])

    # --- candles -----------------------------------------------------------------------------
    def read_ohlcv(self, kind: Kind, base: str) -> pd.DataFrame:
        path = self.ohlcv_path(kind, base)
        if not path.exists():
            return _empty(OHLCV_COLUMNS)
        return pd.read_parquet(path).astype(OHLCV_COLUMNS)

    def append_ohlcv(self, kind: Kind, base: str, candles: Sequence[Candle]) -> int:
        if not candles:
            return 0
        new = pd.DataFrame([asdict(c) for c in candles]).astype(OHLCV_COLUMNS)
        existing = self.read_ohlcv(kind, base)
        merged = _merge(existing, new)
        self._write(self.ohlcv_path(kind, base), merged)
        return len(merged) - len(existing)

    def last_ohlcv_ts(self, kind: Kind, base: str) -> int | None:
        df = self.read_ohlcv(kind, base)
        return None if df.empty else int(df["ts_ms"].iloc[-1])

    # --- helpers -----------------------------------------------------------------------------
    @staticmethod
    def _write(path: Path, df: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.parquet")
        df.to_parquet(tmp, index=False)
        tmp.replace(path)
