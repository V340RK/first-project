"""Сирі payload-и Gateway + канонічний SymbolFilters / ExchangeInfo."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RawAggTrade:
    timestamp_ms: int
    symbol: str
    price: float
    quantity: float
    is_buyer_maker: bool      # True → ринок продав; False → ринок купив
    agg_id: int               # для детекту gap


@dataclass(frozen=True)
class RawDepthDiff:
    symbol: str
    first_update_id: int      # 'U' з Binance
    final_update_id: int      # 'u'
    bids: list[tuple[float, float]]      # (price, qty)
    asks: list[tuple[float, float]]


@dataclass(frozen=True)
class RawKline:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool


@dataclass(frozen=True)
class RawBookTicker:
    symbol: str
    timestamp_ms: int
    best_bid: float
    best_bid_qty: float
    best_ask: float
    best_ask_qty: float


@dataclass(frozen=True)
class RawUserEvent:
    event_type: Literal[
        "ORDER_TRADE_UPDATE",
        "ACCOUNT_UPDATE",
        "MARGIN_CALL",
        "listenKeyExpired",
    ]
    timestamp_ms: int
    payload: dict[str, object]


@dataclass(frozen=True)
class DepthSnapshot:
    """REST-знімок (GET /fapi/v1/depth), із якого стартує OrderBookEngine."""

    symbol: str
    last_update_id: int
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    timestamp_ms: int


@dataclass(frozen=True)
class SymbolFilters:
    """Нормалізовані Binance filters для одного символу.

    ⚠ Source of truth для round-ування цін/кількостей — ЦЕЙ тип.
    RiskEngine / ExecutionEngine беруть значення звідси, НЕ з конфігу.
    """

    tick_size: float            # PRICE_FILTER.tickSize
    step_size: float            # LOT_SIZE.stepSize
    min_qty: float              # LOT_SIZE.minQty
    max_qty: float              # LOT_SIZE.maxQty
    min_notional: float         # MIN_NOTIONAL.notional (USDT)
    price_precision: int
    qty_precision: int


@dataclass(frozen=True)
class ExchangeInfo:
    """Нормалізована відповідь GET /fapi/v1/exchangeInfo. Кешується Gateway-ем."""

    server_time_ms: int
    fetched_at_ms: int
    symbols: dict[str, SymbolFilters]
    rate_limits: dict[str, int]     # {'REQUEST_WEIGHT_1M': 2400, 'ORDERS_10S': 300, ...}
