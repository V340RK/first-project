"""SimulatedExecutionEngine — drop-in заміна ExecutionEngine для backtest/replay.

Той самий публічний API (place_order/cancel_order/cancel_all/on_fill/on_order_update/
register_symbol), але замість транспорту до Binance — внутрішня модель філів
за `book.bid/ask/last_trade_price` + slippage + commission. Детермінований.
"""

from __future__ import annotations

import logging
import math
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from scalper.common import time as _time
from scalper.execution.types import (
    FillEvent,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderUpdate,
    SymbolFilters,
)

logger = logging.getLogger(__name__)

ClockFn = Callable[[], int]
FillCb = Callable[[FillEvent], Awaitable[None]]
UpdateCb = Callable[[OrderUpdate], Awaitable[None]]


class FillPolicy(str, Enum):
    TOUCH = "touch"
    CROSS = "cross"


class SlippageModel(str, Enum):
    ZERO = "zero"
    FIXED_TICKS = "fixed_ticks"
    SPREAD_BASED = "spread_based"


@dataclass(frozen=True)
class SimulatorConfig:
    limit_fill_policy: FillPolicy = FillPolicy.TOUCH
    taker_fee_rate: float = 0.0004
    maker_fee_rate: float = 0.0002
    slippage_model: SlippageModel = SlippageModel.SPREAD_BASED
    slippage_fixed_ticks: int = 1
    latency_ms: int = 50


@dataclass
class _BookSnapshot:
    bid: float
    ask: float
    last_trade_price: float
    tick_size: float
    updated_ms: int = 0


@dataclass
class _SimOrder:
    request: OrderRequest
    client_order_id: str
    exchange_order_id: int
    submitted_ms: int
    trigger_ms: int
    status: OrderStatus = "NEW"
    filled_qty: float = 0.0
    avg_fill_price: float | None = None


@dataclass
class _SimState:
    pending: dict[str, _SimOrder] = field(default_factory=dict)
    filters: dict[str, SymbolFilters] = field(default_factory=dict)
    book: dict[str, _BookSnapshot] = field(default_factory=dict)
    next_order_id: int = 1


class SimulatedExecutionEngine:
    """Симулятор виконання ордерів. Drop-in для ExecutionEngine у Replay pipeline."""

    def __init__(
        self,
        config: SimulatorConfig,
        *,
        clock_fn: ClockFn | None = None,
    ) -> None:
        self._config = config
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())
        self._state = _SimState()
        self._fill_cbs: list[FillCb] = []
        self._update_cbs: list[UpdateCb] = []

    # === Setup ===

    def register_symbol(self, filters: SymbolFilters) -> None:
        self._state.filters[filters.symbol] = filters

    def on_fill(self, cb: FillCb) -> None:
        self._fill_cbs.append(cb)

    def on_order_update(self, cb: UpdateCb) -> None:
        self._update_cbs.append(cb)

    def update_book(
        self, symbol: str, *, bid: float, ask: float,
        last_trade_price: float, tick_size: float | None = None,
        ts_ms: int | None = None,
    ) -> None:
        """Оновити стан орбука/тику для символу. Викликається ззовні на кожен тик."""
        prev = self._state.book.get(symbol)
        tick = tick_size if tick_size is not None else (prev.tick_size if prev else 0.0)
        self._state.book[symbol] = _BookSnapshot(
            bid=bid, ask=ask, last_trade_price=last_trade_price,
            tick_size=tick, updated_ms=ts_ms if ts_ms is not None else self._clock(),
        )

    # === Public order API (match ExecutionEngine) ===

    async def place_order(self, req: OrderRequest) -> OrderResult:
        filters = self._state.filters.get(req.symbol)
        sent_ms = self._clock()
        if filters is None:
            return self._reject(req.client_order_id or "?", -1, "no_symbol_filters", sent_ms)

        qty = self._round_qty(filters, req.qty)
        price = self._round_price(filters, req.price) if req.price is not None else None
        stop_price = (
            self._round_price(filters, req.stop_price) if req.stop_price is not None else None
        )
        coid = req.client_order_id or self._gen_coid(self._hint_for(req))

        if qty <= 0 or qty < filters.min_qty:
            return self._reject(coid, -4003, f"qty_below_min ({qty})", sent_ms)
        if qty > filters.max_qty:
            qty = filters.max_qty
        if price is not None and qty * price < filters.min_notional:
            return self._reject(coid, -4164, "notional_below_min", sent_ms)

        normalized = OrderRequest(
            symbol=req.symbol, side=req.side, type=req.type, qty=qty,
            price=price, stop_price=stop_price, time_in_force=req.time_in_force,
            reduce_only=req.reduce_only, close_position=req.close_position,
            client_order_id=coid,
        )
        order_id = self._state.next_order_id
        self._state.next_order_id += 1

        sim = _SimOrder(
            request=normalized, client_order_id=coid, exchange_order_id=order_id,
            submitted_ms=sent_ms, trigger_ms=sent_ms + self._config.latency_ms, status="NEW",
        )
        self._state.pending[coid] = sim

        # Спроба філу одразу, якщо latency=0 і умови виконані — щоб MARKET повертало FILLED
        if self._config.latency_ms == 0:
            await self._attempt_fill(sim, sent_ms)

        return OrderResult(
            success=True, client_order_id=coid, exchange_order_id=order_id,
            status=sim.status, filled_qty=sim.filled_qty, avg_fill_price=sim.avg_fill_price,
            error_code=None, error_msg=None,
            request_sent_ms=sent_ms, response_received_ms=self._clock(),
        )

    async def cancel_order(self, symbol: str, client_order_id: str) -> OrderResult:
        sent_ms = self._clock()
        sim = self._state.pending.pop(client_order_id, None)
        if sim is None:
            return OrderResult(
                success=True, client_order_id=client_order_id, exchange_order_id=None,
                status="NOT_FOUND", filled_qty=0.0, avg_fill_price=None,
                error_code=-2011, error_msg="unknown_order",
                request_sent_ms=sent_ms, response_received_ms=self._clock(),
            )
        old = sim.status
        sim.status = "CANCELED"
        await self._emit_update(sim, old)
        return OrderResult(
            success=True, client_order_id=client_order_id,
            exchange_order_id=sim.exchange_order_id,
            status="CANCELED", filled_qty=sim.filled_qty, avg_fill_price=sim.avg_fill_price,
            error_code=None, error_msg=None,
            request_sent_ms=sent_ms, response_received_ms=self._clock(),
        )

    async def cancel_all(self, symbol: str) -> list[OrderResult]:
        coids = [c for c, s in self._state.pending.items() if s.request.symbol == symbol]
        return [await self.cancel_order(symbol, c) for c in coids]

    async def get_open_orders(self, symbol: str) -> list[dict]:
        """Для compatibility з PositionManager.reconcile_pending_entries."""
        return [
            {"clientOrderId": c, "symbol": s.request.symbol,
             "side": s.request.side.value, "type": s.request.type.value,
             "origQty": s.request.qty, "status": s.status}
            for c, s in self._state.pending.items()
            if s.request.symbol == symbol
        ]

    async def get_position_risk(self, symbol: str) -> list[dict]:
        """Симулятор не тримає реальні позиції біржі — повертає пусто.
        PositionManager._reconcile тоді трактує як "expired without fill"."""
        return []

    # === Clock / tick hook ===

    async def on_clock_tick(self, sim_time_ms: int) -> None:
        """ReplayRunner викликає на кожну нову подію. Перевіряє філ очікуючих ордерів."""
        for coid in list(self._state.pending.keys()):
            sim = self._state.pending.get(coid)
            if sim is None or sim.status in ("FILLED", "CANCELED"):
                continue
            if sim_time_ms < sim.trigger_ms:
                continue
            await self._attempt_fill(sim, sim_time_ms)

    # === Fill logic ===

    async def _attempt_fill(self, sim: _SimOrder, t_ms: int) -> None:
        book = self._state.book.get(sim.request.symbol)
        if book is None:
            return
        fill = self._try_fill(sim, book)
        if fill is None:
            return
        price, is_maker = fill
        await self._execute_fill(sim, price, is_maker, t_ms)

    def _try_fill(self, sim: _SimOrder, book: _BookSnapshot) -> tuple[float, bool] | None:
        req = sim.request
        side = req.side

        if req.type == OrderType.MARKET:
            base = book.ask if side == OrderSide.BUY else book.bid
            slip = self._slippage(book, side)
            return (base + slip if side == OrderSide.BUY else base - slip), False

        if req.type == OrderType.LIMIT:
            if req.price is None:
                return None
            strict = self._config.limit_fill_policy == FillPolicy.CROSS
            if side == OrderSide.BUY:
                touched = book.ask < req.price if strict else book.ask <= req.price
            else:
                touched = book.bid > req.price if strict else book.bid >= req.price
            if touched:
                return req.price, True
            return None

        if req.type in (OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET):
            if req.stop_price is None:
                return None
            if req.type == OrderType.STOP_MARKET:
                # SL: триггер — рух у невигідний бік
                if side == OrderSide.BUY:
                    triggered = book.last_trade_price >= req.stop_price
                else:
                    triggered = book.last_trade_price <= req.stop_price
            else:
                # TP: триггер — рух у вигідний бік
                if side == OrderSide.BUY:
                    triggered = book.last_trade_price <= req.stop_price
                else:
                    triggered = book.last_trade_price >= req.stop_price
            if not triggered:
                return None
            base = book.ask if side == OrderSide.BUY else book.bid
            slip = self._slippage(book, side)
            return (base + slip if side == OrderSide.BUY else base - slip), False

        return None

    def _slippage(self, book: _BookSnapshot, side: OrderSide) -> float:
        m = self._config.slippage_model
        if m == SlippageModel.ZERO:
            return 0.0
        if m == SlippageModel.FIXED_TICKS:
            return self._config.slippage_fixed_ticks * book.tick_size
        if m == SlippageModel.SPREAD_BASED:
            spread = max(0.0, book.ask - book.bid)
            return spread / 2
        return 0.0

    async def _execute_fill(
        self, sim: _SimOrder, price: float, is_maker: bool, t_ms: int,
    ) -> None:
        req = sim.request
        old_status = sim.status
        sim.filled_qty = req.qty
        sim.avg_fill_price = price
        sim.status = "FILLED"
        fee_rate = self._config.maker_fee_rate if is_maker else self._config.taker_fee_rate
        commission = req.qty * price * fee_rate

        fill = FillEvent(
            symbol=req.symbol, client_order_id=sim.client_order_id,
            exchange_order_id=sim.exchange_order_id, side=req.side,
            qty=req.qty, price=price, is_maker=is_maker,
            commission_usd=commission, filled_cumulative=req.qty,
            order_status="FILLED", timestamp_ms=t_ms, realized_pnl_usd=0.0,
        )
        for cb in self._fill_cbs:
            await cb(fill)
        await self._emit_update(sim, old_status)
        self._state.pending.pop(sim.client_order_id, None)

    async def _emit_update(self, sim: _SimOrder, old_status: OrderStatus) -> None:
        if old_status == sim.status:
            return
        update = OrderUpdate(
            symbol=sim.request.symbol, client_order_id=sim.client_order_id,
            exchange_order_id=sim.exchange_order_id,
            old_status=old_status, new_status=sim.status, timestamp_ms=self._clock(),
        )
        for cb in self._update_cbs:
            await cb(update)

    # === Helpers ===

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
    def _hint_for(req: OrderRequest) -> str:
        if req.type == OrderType.STOP_MARKET:
            return "sl"
        if req.type == OrderType.TAKE_PROFIT_MARKET:
            return "tp"
        return "e"

    def _gen_coid(self, hint: str) -> str:
        ts_tail = str(self._clock())[-6:]
        suffix = secrets.token_hex(3)
        return f"{hint}-{ts_tail}-{suffix}"[:36]

    def _reject(
        self, coid: str, error_code: int, error_msg: str, sent_ms: int,
    ) -> OrderResult:
        return OrderResult(
            success=False, client_order_id=coid, exchange_order_id=None,
            status="REJECTED", filled_qty=0.0, avg_fill_price=None,
            error_code=error_code, error_msg=error_msg,
            request_sent_ms=sent_ms, response_received_ms=self._clock(),
        )


__all__ = [
    "FillPolicy", "SimulatedExecutionEngine", "SimulatorConfig", "SlippageModel",
]
