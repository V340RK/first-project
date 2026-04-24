"""01 Market Data Gateway — єдина точка доступу до Binance WS + REST.

Див. DOCS/architecture/01-market-data-gateway.md.
"""

from scalper.gateway.config import GatewayConfig
from scalper.gateway.gateway import MarketDataGateway
from scalper.gateway.transport import RateLimitBlocked, RestError
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

__all__ = [
    "DepthSnapshot",
    "ExchangeInfo",
    "GatewayConfig",
    "MarketDataGateway",
    "RateLimitBlocked",
    "RawAggTrade",
    "RawBookTicker",
    "RawDepthDiff",
    "RawKline",
    "RawUserEvent",
    "RestError",
    "SymbolFilters",
]
