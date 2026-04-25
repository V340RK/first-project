"""ExecutionEngine — "рука" для place/cancel ордерів.

Знає тільки про OrderRequest, фільтри біржі та транспорт. Нічого про сетапи/R/TradePlan.
Ідемпотентність — через client_order_id (≤ 36 символів, стабільний між retry).

Транспорт абстрагований як Protocol, щоб у тестах підставити FakeTransport
без REST-виклику. У проді — тонкий адаптер над aiohttp → Binance /fapi.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from scalper.common import time as _time
from scalper.execution.config import ExecConfig
from scalper.execution.types import (
    ExchangeError,
    FillEvent,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderUpdate,
    SymbolFilters,
    TimeInForce,
)

logger = logging.getLogger(__name__)

ClockFn = Callable[[], int]
FillCb = Callable[[FillEvent], Awaitable[None]]
UpdateCb = Callable[[OrderUpdate], Awaitable[None]]


class OrderTransport(Protocol):
    """Абстракція над біржовим REST.

    У проді — обгортка над aiohttp → Binance; у тестах — FakeTransport.
    Викидає `ExchangeError(code, msg)` для помилок біржі.
    """

    async def place_order(self, params: dict[str, Any]) -> dict[str, Any]: ...
    async def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]: ...
    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]: ...
    async def get_position_risk(self, symbol: str) -> list[dict[str, Any]]: ...


@dataclass
class _LocalOrderState:
    request: OrderRequest | None
    exchange_order_id: int | None
    status: OrderStatus
    filled_qty: float
    avg_price: float
    created_ms: int
    updated_ms: int


@dataclass
class _ExecState:
    active_orders: dict[str, _LocalOrderState] = field(default_factory=dict)
    symbol_filters: dict[str, SymbolFilters] = field(default_factory=dict)


class ExecutionEngine:
    def __init__(
        self,
        config: ExecConfig,
        transport: OrderTransport,
        *,
        clock_fn: ClockFn | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())
        self._state = _ExecState()
        self._fill_cbs: list[FillCb] = []
        self._update_cbs: list[UpdateCb] = []

    # === Setup ===

    def register_symbol(self, filters: SymbolFilters) -> None:
        self._state.symbol_filters[filters.symbol] = filters

    def on_fill(self, cb: FillCb) -> None:
        self._fill_cbs.append(cb)

    def on_order_update(self, cb: UpdateCb) -> None:
        self._update_cbs.append(cb)

    # === Public ===

    async def place_order(self, req: OrderRequest) -> OrderResult:
        filters = self._state.symbol_filters.get(req.symbol)
        if filters is None:
            return self._reject(
                req.client_order_id or "?", None, "no_symbol_filters",
                -1, sent_ms=self._clock(),
            )

        qty = self._round_qty(filters, req.qty)
        price = self._round_price(filters, req.price) if req.price is not None else None
        stop_price = (
            self._round_price(filters, req.stop_price) if req.stop_price is not None else None
        )
        coid = req.client_order_id or self._gen_coid(self._hint_for(req))

        sent_ms = self._clock()

        if qty <= 0:
            return self._reject(coid, -4003, "qty_rounded_to_zero", -4003, sent_ms=sent_ms)
        if qty < filters.min_qty:
            return self._reject(coid, -4003, f"qty_below_min ({qty})", -4003, sent_ms=sent_ms)
        if qty > filters.max_qty:
            qty = filters.max_qty
        if price is not None and not self._check_notional(filters, qty, price):
            return self._reject(coid, -4164, "notional_below_min", -4164, sent_ms=sent_ms)

        params = self._build_params(req, qty, price, stop_price, coid)

        for attempt in range(max(1, self._config.max_retries)):
            try:
                data = await self._transport.place_order(params)
                resp_ms = self._clock()
                result = self._parse_order_result(data, coid, sent_ms, resp_ms)
                if result.success:
                    self._track_active(coid, req, result)
                return result
            except ExchangeError as e:
                if e.code == -1021:   # timestamp drift — одна додаткова спроба
                    continue
                if e.code in (-2010, -2019):  # balance/margin — не retry
                    logger.error("insufficient: %s", e.msg)
                    return self._reject(coid, e.code, e.msg, e.code, sent_ms=sent_ms)
                if e.code == -4003:
                    logger.error("qty filter despite rounding: %s", e.msg)
                    return self._reject(coid, e.code, e.msg, e.code, sent_ms=sent_ms)
                # Інші — retry з паузою
                await asyncio.sleep(self._config.retry_delay_ms / 1000.0)
            except (TimeoutError, ConnectionError) as e:
                logger.warning("network error attempt %d: %s", attempt, e)
                await asyncio.sleep(self._config.retry_delay_ms / 1000.0 * (attempt + 1))

        return self._reject(coid, -1, "max_retries", -1, sent_ms=sent_ms)

    async def cancel_order(self, symbol: str, client_order_id: str) -> OrderResult:
        sent_ms = self._clock()
        try:
            data = await self._transport.cancel_order(symbol, client_order_id)
            result = self._parse_order_result(data, client_order_id, sent_ms, self._clock())
            self._state.active_orders.pop(client_order_id, None)
            return result
        except ExchangeError as e:
            if e.code == -2011:   # unknown order — вже виконано/скасовано
                self._state.active_orders.pop(client_order_id, None)
                return OrderResult(
                    success=True, client_order_id=client_order_id, exchange_order_id=None,
                    status="NOT_FOUND", filled_qty=0.0, avg_fill_price=None,
                    error_code=e.code, error_msg=e.msg,
                    request_sent_ms=sent_ms, response_received_ms=self._clock(),
                )
            return self._reject(client_order_id, e.code, e.msg, e.code, sent_ms=sent_ms)

    async def cancel_all(self, symbol: str) -> list[OrderResult]:
        to_cancel = [c for c, s in self._state.active_orders.items()
                     if s.request is not None and s.request.symbol == symbol]
        results: list[OrderResult] = []
        for coid in to_cancel:
            results.append(await self.cancel_order(symbol, coid))
        return results

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        """Proxy до transport — для PositionManager.reconcile_pending_entries."""
        try:
            return await self._transport.get_open_orders(symbol)
        except ExchangeError as e:
            logger.warning("get_open_orders failed: %s", e)
            return []

    async def get_position_risk(self, symbol: str) -> list[dict[str, Any]]:
        """Proxy: реальна позиція на біржі (positionAmt). Потрібно reconcile,
        щоб виставляти protection із саме тим qty що Binance тримає, а не з
        plan.position_size (LIMIT IOC міг частково виконатися)."""
        try:
            return await self._transport.get_position_risk(symbol)
        except (ExchangeError, AttributeError) as e:
            logger.warning("get_position_risk failed: %s", e)
            return []

    # === User stream hook ===

    async def handle_user_event(self, event: dict[str, Any]) -> None:
        """Викликається зовні (Orchestrator) при ORDER_TRADE_UPDATE."""
        order = event.get("o", {})
        coid = order.get("c")
        if not coid:
            return
        local = self._state.active_orders.get(coid)
        if not local:
            return

        old_status = local.status
        new_status: OrderStatus = order.get("X", old_status)
        local.status = new_status
        local.filled_qty = float(order.get("z", local.filled_qty))
        local.updated_ms = int(event.get("T", self._clock()))

        if order.get("x") == "TRADE":
            fill = FillEvent(
                symbol=order["s"],
                client_order_id=coid,
                exchange_order_id=int(order.get("i", 0)),
                side=OrderSide(order["S"]),
                qty=float(order["l"]),
                price=float(order["L"]),
                is_maker=bool(order.get("m", False)),
                commission_usd=float(order.get("n", 0) or 0),
                filled_cumulative=local.filled_qty,
                order_status=new_status,
                timestamp_ms=local.updated_ms,
                realized_pnl_usd=float(order.get("rp", 0) or 0),
            )
            for cb in self._fill_cbs:
                await cb(fill)

        if new_status != old_status:
            update = OrderUpdate(
                symbol=order.get("s", ""),
                client_order_id=coid,
                exchange_order_id=int(order.get("i", 0)),
                old_status=old_status,
                new_status=new_status,
                timestamp_ms=local.updated_ms,
            )
            for cb in self._update_cbs:
                await cb(update)

        if new_status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            self._state.active_orders.pop(coid, None)

    # === Rounding / filters ===

    @staticmethod
    def _round_qty(filters: SymbolFilters, qty: float) -> float:
        step = filters.step_size
        if step <= 0:
            return qty
        return round(math.floor(qty / step) * step, 12)

    @staticmethod
    def _round_price(filters: SymbolFilters, price: float) -> float:
        tick = filters.tick_size
        if tick <= 0:
            return price
        return round(round(price / tick) * tick, 12)

    @staticmethod
    def _check_notional(filters: SymbolFilters, qty: float, price: float) -> bool:
        return qty * price >= filters.min_notional

    # === coid ===

    def _gen_coid(self, hint: str) -> str:
        prefix = hint[: self._config.coid_prefix_max_len]
        suffix = secrets.token_hex(3)   # 6 chars
        ts_tail = str(self._clock())[-6:]
        coid = f"{prefix}-{ts_tail}-{suffix}"
        return coid[: self._config.coid_max_len]

    @staticmethod
    def _hint_for(req: OrderRequest) -> str:
        if req.type == OrderType.STOP_MARKET:
            return "sl"
        if req.type == OrderType.TAKE_PROFIT_MARKET:
            return "tp"
        return "e"

    # === Params / parse ===

    @staticmethod
    def _build_params(
        req: OrderRequest, qty: float, price: float | None,
        stop_price: float | None, coid: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": req.symbol,
            "side": req.side.value,
            "type": req.type.value,
            "quantity": qty,
            "newClientOrderId": coid,
        }
        if price is not None:
            params["price"] = price
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if req.time_in_force is not None:
            params["timeInForce"] = req.time_in_force.value
        if req.reduce_only:
            params["reduceOnly"] = "true"
        if req.close_position:
            params["closePosition"] = "true"
        return params

    @staticmethod
    def _parse_order_result(
        data: dict[str, Any], coid: str, sent_ms: int, resp_ms: int,
    ) -> OrderResult:
        status: OrderStatus = data.get("status", "NEW")
        filled = float(data.get("executedQty", 0) or 0)
        # EXPIRED з частковим fill (IOC LIMIT часто дає PARTIALLY → EXPIRED) —
        # це УСПІШНИЙ partial fill, НЕ rejection. Без цього бот накопичує
        # orphan-позиції на біржі без SL/TP.
        success = (status not in ("REJECTED", "EXPIRED")) or filled > 0
        avg = data.get("avgPrice") or data.get("avg_fill_price")
        avg_price = float(avg) if avg not in (None, 0, "0", "") else None
        return OrderResult(
            success=success,
            client_order_id=data.get("clientOrderId", coid),
            exchange_order_id=data.get("orderId"),
            status=status,
            filled_qty=filled,
            avg_fill_price=avg_price,
            error_code=None,
            error_msg=None,
            request_sent_ms=sent_ms,
            response_received_ms=resp_ms,
        )

    def _reject(
        self, coid: str, error_code: int | None, error_msg: str,
        raw_code: int, *, sent_ms: int,
    ) -> OrderResult:
        return OrderResult(
            success=False, client_order_id=coid, exchange_order_id=None,
            status="REJECTED", filled_qty=0.0, avg_fill_price=None,
            error_code=error_code, error_msg=error_msg,
            request_sent_ms=sent_ms, response_received_ms=self._clock(),
        )

    def _track_active(self, coid: str, req: OrderRequest, result: OrderResult) -> None:
        if result.status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
            return
        self._state.active_orders[coid] = _LocalOrderState(
            request=req,
            exchange_order_id=result.exchange_order_id,
            status=result.status,
            filled_qty=result.filled_qty,
            avg_price=result.avg_fill_price or 0.0,
            created_ms=result.request_sent_ms,
            updated_ms=result.response_received_ms,
        )


__all__ = [
    "ExchangeError", "ExecutionEngine", "FillEvent", "OrderRequest", "OrderResult",
    "OrderSide", "OrderType", "OrderUpdate", "OrderTransport", "SymbolFilters",
    "TimeInForce",
]
