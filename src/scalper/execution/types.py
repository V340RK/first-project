"""Типи Execution — OrderRequest/Result/FillEvent, SymbolFilters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTX = "GTX"


OrderStatus = Literal[
    "NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "EXPIRED", "NOT_FOUND",
]


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    tick_size: float
    step_size: float
    min_qty: float
    max_qty: float
    min_notional: float


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    type: OrderType
    qty: float
    price: float | None = None
    stop_price: float | None = None
    time_in_force: TimeInForce | None = None
    reduce_only: bool = False
    close_position: bool = False
    client_order_id: str | None = None


@dataclass(frozen=True)
class OrderResult:
    success: bool
    client_order_id: str
    exchange_order_id: int | None
    status: OrderStatus
    filled_qty: float
    avg_fill_price: float | None
    error_code: int | None
    error_msg: str | None
    request_sent_ms: int
    response_received_ms: int


@dataclass(frozen=True)
class FillEvent:
    symbol: str
    client_order_id: str
    exchange_order_id: int
    side: OrderSide
    qty: float
    price: float
    is_maker: bool
    commission_usd: float
    filled_cumulative: float
    order_status: OrderStatus
    timestamp_ms: int
    realized_pnl_usd: float


@dataclass(frozen=True)
class OrderUpdate:
    symbol: str
    client_order_id: str
    exchange_order_id: int
    old_status: OrderStatus
    new_status: OrderStatus
    timestamp_ms: int


class ExchangeError(Exception):
    """Помилка від біржі з кодом (Binance-style)."""

    def __init__(self, code: int, msg: str) -> None:
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg


__all__ = [
    "ExchangeError", "FillEvent", "OrderRequest", "OrderResult", "OrderSide",
    "OrderStatus", "OrderType", "OrderUpdate", "SymbolFilters", "TimeInForce",
]
