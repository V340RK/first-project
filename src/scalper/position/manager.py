"""PositionManager — state machine життєвого циклу однієї позиції.

Стани: PENDING_ENTRY → ACTIVE → TP1_HIT → TP2_HIT → CLOSING → CLOSED.
Primary exit — на зрив сигналу (invalidation_conditions); time stop — резервний.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from scalper.common import time as _time
from scalper.common.enums import Direction, SetupType
from scalper.common.types import InvalidationCondition, InvalidationKind, TradePlan
from scalper.execution import (
    ExecutionEngine,
    FillEvent,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)
from scalper.features.types import Features
from scalper.position.config import PositionConfig
from scalper.risk import RiskEngine, TradeOutcome

logger = logging.getLogger(__name__)

ClockFn = Callable[[], int]


class PositionState(str, Enum):
    PENDING_ENTRY = "pending_entry"
    ACTIVE = "active"
    TP1_HIT = "tp1_hit"
    TP2_HIT = "tp2_hit"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass
class OpenPosition:
    plan: TradePlan
    symbol: str
    direction: Direction
    setup_type: SetupType
    state: PositionState
    opened_at_ms: int

    entry_coid: str
    sl_coid: str | None = None
    tp_coids: list[str] = field(default_factory=list)

    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_entry_price: float = 0.0

    realized_pnl_usd: float = 0.0
    realized_r: float = 0.0
    fees_usd: float = 0.0

    current_stop_price: float = 0.0
    trailing_active: bool = False
    trailing_high: float = 0.0
    trailing_low: float = 0.0

    max_favorable_r: float = 0.0
    max_adverse_r: float = 0.0

    time_stop_deadline_ms: int | None = None
    invalidation_triggered: InvalidationCondition | None = None

    trade_id: str = ""


class PositionManager:
    def __init__(
        self,
        config: PositionConfig,
        execution: ExecutionEngine,
        risk: RiskEngine,
        *,
        clock_fn: ClockFn | None = None,
    ) -> None:
        self._config = config
        self._execution = execution
        self._risk = risk
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())
        self._positions: dict[str, OpenPosition] = {}

        execution.on_fill(self._on_fill)

    # === Public ===

    async def open(self, plan: TradePlan) -> bool:
        if self.has_open_position(plan.symbol):
            logger.warning("open rejected: %s already open", plan.symbol)
            return False
        if plan.position_size is None or plan.position_size <= 0:
            logger.warning("open rejected: no position_size in plan")
            return False

        entry_req = self._build_entry(plan)
        result = await self._execution.place_order(entry_req)
        if not result.success:
            logger.error("entry order rejected: %s", result.error_msg)
            return False

        now = self._clock()
        pos = OpenPosition(
            plan=plan,
            symbol=plan.symbol,
            direction=plan.direction,
            setup_type=plan.setup_type,
            state=PositionState.PENDING_ENTRY,
            opened_at_ms=now,
            entry_coid=result.client_order_id,
            remaining_qty=0.0,
            current_stop_price=plan.stop_price,
            time_stop_deadline_ms=(now + plan.time_stop_ms) if plan.time_stop_ms else None,
            trade_id=result.client_order_id,
        )
        self._positions[plan.symbol] = pos

        # Якщо entry відразу виконаний (MARKET → FILLED) — виставити захист
        if result.status == "FILLED" and result.filled_qty > 0:
            pos.filled_qty = result.filled_qty
            pos.remaining_qty = result.filled_qty
            pos.avg_entry_price = result.avg_fill_price or plan.entry_price
            pos.state = PositionState.ACTIVE
            await self._place_protection(pos)
        return True

    def has_open_position(self, symbol: str) -> bool:
        p = self._positions.get(symbol)
        return p is not None and p.state != PositionState.CLOSED

    def get(self, symbol: str) -> OpenPosition | None:
        return self._positions.get(symbol)

    def all_open(self) -> list[OpenPosition]:
        return [p for p in self._positions.values() if p.state != PositionState.CLOSED]

    async def on_features(self, features: Features) -> None:
        pos = self._positions.get(features.snapshot.symbol)
        if not pos or pos.state in (
            PositionState.PENDING_ENTRY, PositionState.CLOSING, PositionState.CLOSED,
        ):
            return

        last_price = features.snapshot.last_price
        self._update_extremes(pos, last_price)

        ic = self._check_invalidations(pos, features)
        if ic is not None:
            pos.invalidation_triggered = ic
            await self._close_fully(
                pos, was_stopped=False, reason=f"signal_invalidated:{ic.kind.value}",
            )
            return

        if pos.time_stop_deadline_ms and self._clock() > pos.time_stop_deadline_ms:
            if not (self._config.disable_time_stop_after_tp1
                    and pos.state in (PositionState.TP1_HIT, PositionState.TP2_HIT)):
                await self._close_fully(pos, was_stopped=False, reason="time_stop")
                return

        if pos.state == PositionState.TP2_HIT and pos.trailing_active:
            await self._update_trailing(pos, last_price)

    async def force_close(self, symbol: str, reason: str) -> bool:
        pos = self._positions.get(symbol)
        if not pos or pos.state in (PositionState.CLOSING, PositionState.CLOSED):
            return False
        await self._close_fully(pos, was_stopped=False, reason=reason)
        return True

    # === Fill handling ===

    async def _on_fill(self, fill: FillEvent) -> None:
        pos = self._find_by_coid(fill.client_order_id)
        if pos is None:
            return

        pos.fees_usd += fill.commission_usd

        if fill.client_order_id == pos.entry_coid:
            await self._handle_entry_fill(pos, fill)
        elif fill.client_order_id == pos.sl_coid:
            await self._handle_sl_fill(pos, fill)
        elif fill.client_order_id in pos.tp_coids:
            idx = pos.tp_coids.index(fill.client_order_id)
            await self._handle_tp_fill(pos, fill, idx)

    async def _handle_entry_fill(self, pos: OpenPosition, fill: FillEvent) -> None:
        notional_old = pos.avg_entry_price * pos.filled_qty
        pos.filled_qty += fill.qty
        pos.avg_entry_price = (notional_old + fill.price * fill.qty) / pos.filled_qty
        pos.remaining_qty += fill.qty

        if fill.order_status == "FILLED" and pos.state == PositionState.PENDING_ENTRY:
            pos.state = PositionState.ACTIVE
            await self._place_protection(pos)

    async def _handle_sl_fill(self, pos: OpenPosition, fill: FillEvent) -> None:
        # R від поточного стопу відносно entry і початкового R-distance
        r = self._compute_realized_r(pos, fill.price, fill.qty)
        pos.realized_r += r
        pos.realized_pnl_usd += fill.realized_pnl_usd
        pos.remaining_qty -= fill.qty
        if pos.remaining_qty <= 1e-12:
            await self._finalize(pos, was_stopped=True, reason="sl_hit")

    async def _handle_tp_fill(self, pos: OpenPosition, fill: FillEvent, idx: int) -> None:
        r = self._compute_realized_r(pos, fill.price, fill.qty)
        pos.realized_r += r
        pos.realized_pnl_usd += fill.realized_pnl_usd
        pos.remaining_qty -= fill.qty

        if idx == 0:
            pos.state = PositionState.TP1_HIT
            await self._move_stop_to_breakeven(pos)
        elif idx == 1:
            pos.state = PositionState.TP2_HIT
            pos.trailing_active = True
            pos.trailing_high = max(pos.trailing_high, fill.price)
            pos.trailing_low = min(pos.trailing_low or fill.price, fill.price)
        if pos.remaining_qty <= 1e-12 or idx == 2:
            await self._finalize(pos, was_stopped=False, reason=f"tp{idx+1}_hit")

    # === Protection / BE / trailing ===

    async def _place_protection(self, pos: OpenPosition) -> None:
        plan = pos.plan
        side_close = OrderSide.SELL if pos.direction == Direction.LONG else OrderSide.BUY

        sl_req = OrderRequest(
            symbol=plan.symbol, side=side_close, type=OrderType.STOP_MARKET,
            qty=pos.filled_qty, stop_price=plan.stop_price, reduce_only=True,
        )
        sl_res = await self._execution.place_order(sl_req)
        if not sl_res.success:
            logger.critical("SL placement failed: %s — EMERGENCY close", sl_res.error_msg)
            await self._emergency_market_close(pos)
            return
        pos.sl_coid = sl_res.client_order_id

        sizes = [pos.filled_qty * s for s in self._config.tp_split]
        prices = [plan.tp1_price, plan.tp2_price, plan.tp3_price]
        for sz, pr in zip(sizes, prices, strict=True):
            tp_req = OrderRequest(
                symbol=plan.symbol, side=side_close, type=OrderType.TAKE_PROFIT_MARKET,
                qty=sz, stop_price=pr, reduce_only=True,
            )
            tp_res = await self._execution.place_order(tp_req)
            if tp_res.success:
                pos.tp_coids.append(tp_res.client_order_id)
            else:
                logger.error("TP placement failed: %s", tp_res.error_msg)

    async def _move_stop_to_breakeven(self, pos: OpenPosition) -> None:
        if pos.sl_coid is None:
            return
        await self._execution.cancel_order(pos.symbol, pos.sl_coid)

        buffer = self._config.breakeven_buffer_ticks * self._config.tick_size
        new_stop = (
            pos.avg_entry_price + buffer if pos.direction == Direction.LONG
            else pos.avg_entry_price - buffer
        )
        pos.current_stop_price = new_stop

        side_close = OrderSide.SELL if pos.direction == Direction.LONG else OrderSide.BUY
        new_req = OrderRequest(
            symbol=pos.symbol, side=side_close, type=OrderType.STOP_MARKET,
            qty=pos.remaining_qty, stop_price=new_stop, reduce_only=True,
        )
        res = await self._execution.place_order(new_req)
        if res.success:
            pos.sl_coid = res.client_order_id
            logger.info("%s: SL moved to BE at %.4f", pos.symbol, new_stop)

    async def _update_trailing(self, pos: OpenPosition, last_price: float) -> None:
        trail = self._config.trailing_distance_ticks * self._config.tick_size
        min_move = self._config.trailing_min_move_ticks * self._config.tick_size

        if pos.direction == Direction.LONG:
            desired = last_price - trail
            if desired - pos.current_stop_price >= min_move:
                await self._move_stop(pos, desired)
        else:
            desired = last_price + trail
            if pos.current_stop_price - desired >= min_move:
                await self._move_stop(pos, desired)

    async def _move_stop(self, pos: OpenPosition, new_stop: float) -> None:
        if pos.sl_coid is None:
            return
        await self._execution.cancel_order(pos.symbol, pos.sl_coid)
        side_close = OrderSide.SELL if pos.direction == Direction.LONG else OrderSide.BUY
        req = OrderRequest(
            symbol=pos.symbol, side=side_close, type=OrderType.STOP_MARKET,
            qty=pos.remaining_qty, stop_price=new_stop, reduce_only=True,
        )
        res = await self._execution.place_order(req)
        if res.success:
            pos.sl_coid = res.client_order_id
            pos.current_stop_price = new_stop

    # === Invalidation / extremes ===

    def _check_invalidations(
        self, pos: OpenPosition, f: Features,
    ) -> InvalidationCondition | None:
        if self._config.disable_invalidation_after_tp1 and pos.state in (
            PositionState.TP1_HIT, PositionState.TP2_HIT,
        ):
            return None

        price = f.snapshot.last_price
        for ic in pos.plan.invalidation_conditions:
            if ic.kind == InvalidationKind.PRICE_BEYOND_LEVEL:
                level = float(ic.params.get("price", pos.plan.stop_price))
                if pos.direction == Direction.LONG and price <= level:
                    return ic
                if pos.direction == Direction.SHORT and price >= level:
                    return ic
            elif ic.kind == InvalidationKind.OPPOSITE_ABSORPTION:
                if pos.direction == Direction.LONG and f.absorption_score > 0.6 and f.absorption_side == "ASK":
                    return ic
                if pos.direction == Direction.SHORT and f.absorption_score > 0.6 and f.absorption_side == "BID":
                    return ic
            elif ic.kind == InvalidationKind.DELTA_FLIP:
                threshold = float(ic.params.get("threshold", 20_000))
                if pos.direction == Direction.LONG and f.delta_2s < -threshold:
                    return ic
                if pos.direction == Direction.SHORT and f.delta_2s > threshold:
                    return ic
            elif ic.kind == InvalidationKind.BOOK_IMBALANCE_FLIP:
                limit = float(ic.params.get("threshold", 0.4))
                if pos.direction == Direction.LONG and f.weighted_imbalance < -limit:
                    return ic
                if pos.direction == Direction.SHORT and f.weighted_imbalance > limit:
                    return ic
        return None

    def _update_extremes(self, pos: OpenPosition, price: float) -> None:
        r_distance = abs(pos.plan.entry_price - pos.plan.stop_price)
        if r_distance <= 0:
            return
        move = (price - pos.avg_entry_price) if pos.direction == Direction.LONG \
            else (pos.avg_entry_price - price)
        r = move / r_distance
        if r > pos.max_favorable_r:
            pos.max_favorable_r = r
        if r < pos.max_adverse_r:
            pos.max_adverse_r = r

    def _compute_realized_r(self, pos: OpenPosition, exit_price: float, qty: float) -> float:
        total_size = pos.plan.position_size or pos.filled_qty or 1
        r_distance = abs(pos.plan.entry_price - pos.plan.stop_price)
        if r_distance <= 0:
            return 0.0
        move = (exit_price - pos.avg_entry_price) if pos.direction == Direction.LONG \
            else (pos.avg_entry_price - exit_price)
        r_per_unit = move / r_distance
        return r_per_unit * (qty / total_size)

    # === Close / finalize ===

    async def _close_fully(
        self, pos: OpenPosition, *, was_stopped: bool, reason: str,
    ) -> None:
        if pos.state in (PositionState.CLOSING, PositionState.CLOSED):
            return
        pos.state = PositionState.CLOSING

        await self._execution.cancel_all(pos.symbol)

        if pos.remaining_qty > 0 and not was_stopped:
            side = OrderSide.SELL if pos.direction == Direction.LONG else OrderSide.BUY
            close_req = OrderRequest(
                symbol=pos.symbol, side=side, type=OrderType.MARKET,
                qty=pos.remaining_qty, reduce_only=True,
            )
            close_res = await self._execution.place_order(close_req)
            if close_res.success and close_res.status == "FILLED" and close_res.avg_fill_price:
                r = self._compute_realized_r(pos, close_res.avg_fill_price, pos.remaining_qty)
                pos.realized_r += r
                pos.remaining_qty = 0

        await self._finalize(pos, was_stopped=was_stopped, reason=reason)

    async def _emergency_market_close(self, pos: OpenPosition) -> None:
        await self._execution.cancel_all(pos.symbol)
        side = OrderSide.SELL if pos.direction == Direction.LONG else OrderSide.BUY
        if pos.filled_qty > 0:
            await self._execution.place_order(OrderRequest(
                symbol=pos.symbol, side=side, type=OrderType.MARKET,
                qty=pos.filled_qty, reduce_only=True,
            ))
        await self._finalize(pos, was_stopped=False, reason="emergency_close")

    async def _finalize(
        self, pos: OpenPosition, *, was_stopped: bool, reason: str,
    ) -> None:
        pos.state = PositionState.CLOSED

        outcome = TradeOutcome(
            plan=pos.plan,
            trade_id=pos.trade_id,
            symbol=pos.symbol,
            setup_type=pos.setup_type,
            direction=pos.direction,
            closed_at_ms=self._clock(),
            realized_r=pos.realized_r,
            realized_usd=pos.realized_pnl_usd,
            max_favorable_r=pos.max_favorable_r,
            max_adverse_r=pos.max_adverse_r,
            was_stopped=was_stopped,
            fees_usd=pos.fees_usd,
        )
        try:
            self._risk.on_position_closed(outcome)
        except Exception as e:
            logger.exception("risk.on_position_closed failed: %s", e)

        logger.info(
            "closed %s: %+.2fR (%s)", pos.symbol, pos.realized_r, reason,
        )
        self._positions.pop(pos.symbol, None)

    # === Helpers ===

    def _build_entry(self, plan: TradePlan) -> OrderRequest:
        side = OrderSide.BUY if plan.direction == Direction.LONG else OrderSide.SELL
        if self._config.entry_as_market:
            return OrderRequest(
                symbol=plan.symbol, side=side, type=OrderType.MARKET,
                qty=float(plan.position_size or 0),
            )
        tick = self._config.tick_size
        offset = self._config.entry_ioc_offset_ticks * tick
        limit_price = (
            plan.entry_price + offset if plan.direction == Direction.LONG
            else plan.entry_price - offset
        )
        return OrderRequest(
            symbol=plan.symbol, side=side, type=OrderType.LIMIT,
            qty=float(plan.position_size or 0), price=limit_price,
            time_in_force=TimeInForce.IOC,
        )

    def _find_by_coid(self, coid: str) -> OpenPosition | None:
        for pos in self._positions.values():
            if coid == pos.entry_coid or coid == pos.sl_coid or coid in pos.tp_coids:
                return pos
        return None


__all__ = ["OpenPosition", "PositionConfig", "PositionManager", "PositionState"]
