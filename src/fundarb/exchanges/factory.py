"""Builds the right adapter for the configured exchange id."""

from __future__ import annotations

from pathlib import Path

from fundarb.core.config import Settings
from fundarb.exchanges.base import ExchangeAdapter


def history_adapter(settings: Settings) -> ExchangeAdapter:
    exchange_id = settings.exchange.id
    if exchange_id == "binance_vision":
        from fundarb.exchanges.binance_vision import BinanceVisionAdapter

        cache = Path(settings.data.dir) / exchange_id / "raw"
        return BinanceVisionAdapter(cache, timeframe=settings.data.timeframe)
    if exchange_id == "hyperliquid":
        from fundarb.exchanges.hyperliquid_info import HyperliquidInfoAdapter

        return HyperliquidInfoAdapter()
    from fundarb.exchanges.ccxt_adapter import CcxtAdapter

    return CcxtAdapter(exchange_id, testnet=settings.exchange.testnet)
