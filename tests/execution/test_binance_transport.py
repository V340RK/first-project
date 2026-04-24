"""BinanceOrderTransport — мапінг методів + конвертація RestError → ExchangeError."""

from __future__ import annotations

from typing import Any

import pytest

from scalper.execution import BinanceOrderTransport, ExchangeError
from scalper.gateway.transport import RestError


class FakeRest:
    def __init__(self, responses: dict[str, Any] | None = None, raise_on: dict[str, RestError] | None = None) -> None:
        self._responses = responses or {}
        self._raise_on = raise_on or {}
        self.calls: list[tuple[str, str, dict]] = []

    async def private_post(self, endpoint: str, *, params: dict, weight: int = 1) -> Any:
        self.calls.append(("POST", endpoint, params))
        if endpoint in self._raise_on:
            raise self._raise_on[endpoint]
        return self._responses.get(endpoint, {})

    async def private_get(self, endpoint: str, *, params: dict, weight: int = 1) -> Any:
        self.calls.append(("GET", endpoint, params))
        if endpoint in self._raise_on:
            raise self._raise_on[endpoint]
        return self._responses.get(endpoint, [])

    async def private_delete(self, endpoint: str, *, params: dict, weight: int = 1) -> Any:
        self.calls.append(("DELETE", endpoint, params))
        if endpoint in self._raise_on:
            raise self._raise_on[endpoint]
        return self._responses.get(endpoint, {})


@pytest.mark.asyncio
async def test_place_order_calls_post_to_fapi_order() -> None:
    rest = FakeRest(responses={"/fapi/v1/order": {"status": "NEW", "orderId": 123}})
    transport = BinanceOrderTransport(rest)   # type: ignore[arg-type]
    out = await transport.place_order({"symbol": "BTCUSDT", "side": "BUY", "quantity": 0.1})
    assert out["status"] == "NEW"
    assert rest.calls[0] == ("POST", "/fapi/v1/order", {"symbol": "BTCUSDT", "side": "BUY", "quantity": 0.1})


@pytest.mark.asyncio
async def test_cancel_order_calls_delete_with_coid() -> None:
    rest = FakeRest(responses={"/fapi/v1/order": {"status": "CANCELED"}})
    transport = BinanceOrderTransport(rest)   # type: ignore[arg-type]
    out = await transport.cancel_order("BTCUSDT", "my-coid-1")
    assert out["status"] == "CANCELED"
    method, path, params = rest.calls[0]
    assert method == "DELETE"
    assert params == {"symbol": "BTCUSDT", "origClientOrderId": "my-coid-1"}


@pytest.mark.asyncio
async def test_get_open_orders_returns_list() -> None:
    rest = FakeRest(responses={"/fapi/v1/openOrders": [{"orderId": 1}, {"orderId": 2}]})
    transport = BinanceOrderTransport(rest)   # type: ignore[arg-type]
    out = await transport.get_open_orders("BTCUSDT")
    assert len(out) == 2


@pytest.mark.asyncio
async def test_get_open_orders_handles_dict_response() -> None:
    rest = FakeRest(responses={"/fapi/v1/openOrders": {"err": "?"}})
    transport = BinanceOrderTransport(rest)   # type: ignore[arg-type]
    out = await transport.get_open_orders("BTCUSDT")
    assert out == []


@pytest.mark.asyncio
async def test_rest_error_becomes_exchange_error() -> None:
    rest = FakeRest(raise_on={"/fapi/v1/order": RestError(400, -2010, "Account has insufficient balance")})
    transport = BinanceOrderTransport(rest)   # type: ignore[arg-type]
    with pytest.raises(ExchangeError) as e:
        await transport.place_order({"symbol": "BTCUSDT"})
    assert e.value.code == -2010
    assert "insufficient" in e.value.msg.lower()


@pytest.mark.asyncio
async def test_rest_error_without_code_defaults_to_minus_one() -> None:
    rest = FakeRest(raise_on={"/fapi/v1/order": RestError(500, None, "Internal")})
    transport = BinanceOrderTransport(rest)   # type: ignore[arg-type]
    with pytest.raises(ExchangeError) as e:
        await transport.cancel_order("BTCUSDT", "c")
    assert e.value.code == -1
