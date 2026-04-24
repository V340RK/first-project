"""ReplayGateway — підміна MarketDataGateway. Грає історичні дані з файлу.

Merge-sort стрімів кількох символів за event_time_ms. Той самий callback API.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from scalper.gateway.types import (
    DepthSnapshot,
    ExchangeInfo,
    RawAggTrade,
    RawBookTicker,
    RawDepthDiff,
    RawKline,
    RawUserEvent,
    SymbolFilters,
)


class ReplayGateway:
    """Drop-in заміна MarketDataGateway. Той самий публічний інтерфейс.

    Дані: pre-recorded JSONL/parquet з aggTrade + depthDiff + kline за період.
    """

    def __init__(self, data_dir: Path, speed: float = 0.0) -> None:
        # speed=0 → max швидкість (як можна швидше); speed=1.0 → реальний час.
        raise NotImplementedError

    async def start(self, symbols: list[str]) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    # Той самий API що у MarketDataGateway:
    def on_agg_trade(self, callback: Callable[[RawAggTrade], Awaitable[None]]) -> None:
        raise NotImplementedError

    def on_depth_diff(self, callback: Callable[[RawDepthDiff], Awaitable[None]]) -> None:
        raise NotImplementedError

    def on_kline_close(self, callback: Callable[[RawKline], Awaitable[None]]) -> None:
        raise NotImplementedError

    def on_book_ticker(self, callback: Callable[[RawBookTicker], Awaitable[None]]) -> None:
        raise NotImplementedError

    def on_user_event(self, callback: Callable[[RawUserEvent], Awaitable[None]]) -> None:
        raise NotImplementedError

    async def fetch_depth_snapshot(self, symbol: str, limit: int = 1000) -> DepthSnapshot:
        raise NotImplementedError

    async def fetch_klines(self, symbol: str, interval: str, limit: int) -> list[RawKline]:
        raise NotImplementedError

    async def fetch_exchange_info(self) -> ExchangeInfo:
        raise NotImplementedError

    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        raise NotImplementedError
