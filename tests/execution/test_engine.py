"""ExecutionEngine — rounding, coid, retry, reject-коди, user-stream hook."""

from __future__ import annotations

from typing import Any

import pytest

from scalper.execution import (
    ExchangeError,
    ExecConfig,
    ExecutionEngine,
    FillEvent,
    OrderRequest,
    OrderSide,
    OrderType,
    OrderUpdate,
    SymbolFilters,
    TimeInForce,
)


# ============================================================
# Fake transport
# ============================================================

class FakeTransport:
    def __init__(self) -> None:
        self.place_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.place_responses: list[Any] = []     # dict or ExchangeError
        self.cancel_responses: list[Any] = []

    async def place_order(self, params: dict[str, Any]) -> dict[str, Any]:
        self.place_calls.append(params)
        if not self.place_responses:
            return {
                "clientOrderId": params["newClientOrderId"],
                "orderId": 1001,
                "status": "NEW",
                "executedQty": "0",
                "avgPrice": "0",
            }
        resp = self.place_responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        self.cancel_calls.append((symbol, client_order_id))
        if not self.cancel_responses:
            return {
                "clientOrderId": client_order_id, "orderId": 1001,
                "status": "CANCELED", "executedQty": "0",
            }
        resp = self.cancel_responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return []


def _eng() -> tuple[ExecutionEngine, FakeTransport]:
    t = FakeTransport()
    e = ExecutionEngine(
        ExecConfig(max_retries=3, retry_delay_ms=0),
        t,
        clock_fn=lambda: 1_700_000_000_000,
    )
    e.register_symbol(SymbolFilters(
        symbol="BTCUSDT", tick_size=0.1, step_size=0.001,
        min_qty=0.001, max_qty=100.0, min_notional=5.0,
    ))
    return e, t


def _req(**kwargs: Any) -> OrderRequest:
    defaults: dict[str, Any] = dict(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT,
        qty=0.05, price=100.0, time_in_force=TimeInForce.GTC,
    )
    defaults.update(kwargs)
    return OrderRequest(**defaults)


# ============================================================
# Rounding
# ============================================================

def test_round_qty_floors_to_step() -> None:
    f = SymbolFilters("BTCUSDT", 0.1, 0.001, 0.001, 100.0, 5.0)
    assert ExecutionEngine._round_qty(f, 16.6789) == 16.678


def test_round_price_nearest_tick() -> None:
    f = SymbolFilters("BTCUSDT", 0.1, 0.001, 0.001, 100.0, 5.0)
    assert ExecutionEngine._round_price(f, 100.123) == 100.1
    assert ExecutionEngine._round_price(f, 100.16) == 100.2


def test_check_notional() -> None:
    f = SymbolFilters("BTCUSDT", 0.1, 0.001, 0.001, 100.0, 10.0)
    assert ExecutionEngine._check_notional(f, 0.2, 100.0) is True    # 20 >= 10
    assert ExecutionEngine._check_notional(f, 0.05, 100.0) is False  # 5 < 10


# ============================================================
# place_order
# ============================================================

@pytest.mark.asyncio
async def test_place_order_happy_path() -> None:
    e, t = _eng()
    r = await e.place_order(_req(qty=0.0578, price=100.123))
    assert r.success is True
    assert r.status == "NEW"
    # округлилось
    call = t.place_calls[0]
    assert call["quantity"] == 0.057
    assert call["price"] == 100.1
    assert len(call["newClientOrderId"]) <= 36


@pytest.mark.asyncio
async def test_place_order_no_symbol_filters() -> None:
    t = FakeTransport()
    e = ExecutionEngine(ExecConfig(), t)  # без register_symbol
    r = await e.place_order(_req())
    assert r.success is False
    assert r.error_msg == "no_symbol_filters"


@pytest.mark.asyncio
async def test_place_order_qty_rounded_to_zero() -> None:
    e, t = _eng()
    r = await e.place_order(_req(qty=0.0005, price=100.0))
    assert r.success is False
    assert r.error_code == -4003


@pytest.mark.asyncio
async def test_place_order_notional_below_min() -> None:
    e, t = _eng()
    # qty=0.001, price=4 → notional 0.004 < 5
    r = await e.place_order(_req(qty=0.001, price=4.0))
    assert r.success is False
    assert r.error_code == -4164


@pytest.mark.asyncio
async def test_place_order_qty_clamped_to_max() -> None:
    e, t = _eng()
    r = await e.place_order(_req(qty=500.0, price=100.0))
    assert r.success is True
    assert t.place_calls[0]["quantity"] == 100.0    # max_qty


@pytest.mark.asyncio
async def test_client_order_id_preserved_when_given() -> None:
    e, t = _eng()
    r = await e.place_order(_req(client_order_id="my-fixed-id"))
    assert r.client_order_id == "my-fixed-id"
    assert t.place_calls[0]["newClientOrderId"] == "my-fixed-id"


@pytest.mark.asyncio
async def test_generated_coid_is_within_length() -> None:
    e, t = _eng()
    await e.place_order(_req())
    coid = t.place_calls[0]["newClientOrderId"]
    assert 1 <= len(coid) <= 36


# ============================================================
# Retry / errors
# ============================================================

@pytest.mark.asyncio
async def test_retry_on_timestamp_error_then_success() -> None:
    e, t = _eng()
    t.place_responses = [
        ExchangeError(-1021, "timestamp"),
        {"clientOrderId": "x", "orderId": 1, "status": "NEW", "executedQty": "0"},
    ]
    r = await e.place_order(_req())
    assert r.success is True
    assert len(t.place_calls) == 2


@pytest.mark.asyncio
async def test_insufficient_balance_no_retry() -> None:
    e, t = _eng()
    t.place_responses = [ExchangeError(-2010, "insufficient balance")]
    r = await e.place_order(_req())
    assert r.success is False
    assert r.error_code == -2010
    assert len(t.place_calls) == 1   # без retry


@pytest.mark.asyncio
async def test_qty_too_small_no_retry() -> None:
    e, t = _eng()
    t.place_responses = [ExchangeError(-4003, "qty too small")]
    r = await e.place_order(_req())
    assert r.success is False
    assert r.error_code == -4003
    assert len(t.place_calls) == 1


@pytest.mark.asyncio
async def test_max_retries_exhausted() -> None:
    e, t = _eng()
    t.place_responses = [ExchangeError(-1001, "disconnected")] * 5
    r = await e.place_order(_req())
    assert r.success is False
    assert r.error_msg == "max_retries"


# ============================================================
# cancel
# ============================================================

@pytest.mark.asyncio
async def test_cancel_unknown_returns_not_found() -> None:
    e, t = _eng()
    t.cancel_responses = [ExchangeError(-2011, "unknown order")]
    r = await e.cancel_order("BTCUSDT", "absent-coid")
    assert r.success is True
    assert r.status == "NOT_FOUND"


@pytest.mark.asyncio
async def test_cancel_happy_path() -> None:
    e, t = _eng()
    await e.place_order(_req(client_order_id="abc"))
    r = await e.cancel_order("BTCUSDT", "abc")
    assert r.status == "CANCELED"
    assert t.cancel_calls == [("BTCUSDT", "abc")]


@pytest.mark.asyncio
async def test_cancel_all_iterates_active_by_symbol() -> None:
    e, t = _eng()
    await e.place_order(_req(client_order_id="a"))
    await e.place_order(_req(client_order_id="b"))
    results = await e.cancel_all("BTCUSDT")
    assert len(results) == 2
    assert all(r.status == "CANCELED" for r in results)


# ============================================================
# User-stream hook → FillEvent
# ============================================================

@pytest.mark.asyncio
async def test_handle_user_event_emits_fill() -> None:
    e, t = _eng()
    await e.place_order(_req(client_order_id="fill-me"))

    fills: list[FillEvent] = []

    async def on_fill(f: FillEvent) -> None:
        fills.append(f)

    updates: list[OrderUpdate] = []

    async def on_upd(u: OrderUpdate) -> None:
        updates.append(u)

    e.on_fill(on_fill)
    e.on_order_update(on_upd)

    await e.handle_user_event({
        "T": 1_700_000_000_100,
        "o": {
            "c": "fill-me", "s": "BTCUSDT", "i": 42, "S": "BUY",
            "x": "TRADE", "X": "FILLED",
            "l": "0.05", "L": "100.1", "z": "0.05",
            "m": False, "n": "0.01", "rp": "0.5",
        },
    })

    assert len(fills) == 1
    assert fills[0].price == 100.1
    assert fills[0].qty == 0.05
    assert fills[0].order_status == "FILLED"
    assert len(updates) == 1
    assert updates[0].new_status == "FILLED"
    # термінальний статус → active вичищено
    assert "fill-me" not in e._state.active_orders   # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_handle_user_event_unknown_coid_noop() -> None:
    e, t = _eng()
    # активних ордерів нема → подія ігнорується без винятків
    await e.handle_user_event({"o": {"c": "ghost"}})
