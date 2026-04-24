"""BinanceOrderTransport — адаптер _RestTransport → OrderTransport Protocol.

ExecutionEngine працює через абстракцію OrderTransport з трьома методами.
Цей модуль — тонкий шар, який мапить їх на REST-ендпоїнти Binance Futures:
  place_order  → POST /fapi/v1/order
  cancel_order → DELETE /fapi/v1/order
  get_open_orders → GET /fapi/v1/openOrders

Binance-помилки конвертуються в ExchangeError(code, msg) — ExecutionEngine
уже обробляє коди (-1021 retry, -2010/-2019 no-retry, -2011 not-found, тощо).
"""

from __future__ import annotations

import logging
from typing import Any

from scalper.execution.types import ExchangeError
from scalper.gateway.transport import RestError, _RestTransport

logger = logging.getLogger(__name__)


PLACE_ORDER_PATH = "/fapi/v1/order"
CANCEL_ORDER_PATH = "/fapi/v1/order"
OPEN_ORDERS_PATH = "/fapi/v1/openOrders"


class BinanceOrderTransport:
    """Implements OrderTransport Protocol using _RestTransport.

    Весь HMAC-підпис + rate-limit + retry живе у _RestTransport. Тут лише
    мапінг контрактів і конвертація помилок.
    """

    def __init__(self, transport: _RestTransport) -> None:
        self._transport = transport

    async def place_order(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._transport.private_post(
                PLACE_ORDER_PATH, params=params, weight=1,
            )
        except RestError as e:
            raise ExchangeError(e.code or -1, e.message) from e

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        try:
            return await self._transport.private_delete(
                CANCEL_ORDER_PATH,
                params={"symbol": symbol, "origClientOrderId": client_order_id},
                weight=1,
            )
        except RestError as e:
            raise ExchangeError(e.code or -1, e.message) from e

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        try:
            result = await self._transport.private_get(
                OPEN_ORDERS_PATH, params={"symbol": symbol}, weight=1,
            )
        except RestError as e:
            raise ExchangeError(e.code or -1, e.message) from e
        if isinstance(result, list):
            return result
        return []


__all__ = ["BinanceOrderTransport"]
