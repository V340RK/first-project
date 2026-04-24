"""OrderBookEngine — тримає книгу + будує footprint, кожен символ незалежно.

Життєвий цикл символу:
  1. start() → буферизуємо diff-и, паралельно REST snapshot
  2. replay буферу поверх snapshot-у (з перевіркою sequence)
  3. live: apply diff, при gap → reinit
  4. кожен agg-trade оновлює footprint (1m/5m/15m одночасно)
  5. на межі бару (chrono або по timestamp trade-у) → close + persist в історію + callback

Engine підписується на Gateway: по ОДНОМУ callback-у для diff і trade, мультиплексує
всередині по symbol. Це пасує існуючому контракту Gateway (`on_depth_diff` ставить ОДИН
callback — ми робимо dispatcher).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from scalper.book.book_state import SequenceGapError, _BookState
from scalper.book.config import OBConfig
from scalper.book.footprint import close_bar, new_bar, tf_to_ms, update_bar
from scalper.book.types import FootprintBar, OrderBookState
from scalper.common import time as _time
from scalper.gateway.types import RawAggTrade, RawDepthDiff

logger = logging.getLogger(__name__)


BarCloseCallback = Callable[[FootprintBar], Awaitable[None]]
ClockFn = Callable[[], int]


@dataclass
class _SymbolCtx:
    """Per-symbol стан. Живе лише всередині OrderBookEngine."""

    symbol: str
    book: _BookState
    warmup_buffer: list[RawDepthDiff] = field(default_factory=list)
    active_bars: dict[str, FootprintBar] = field(default_factory=dict)              # tf → current bar
    closed_history: dict[str, deque[FootprintBar]] = field(default_factory=dict)    # tf → ring
    reinit_task: asyncio.Task[None] | None = None


class GatewayLike:
    """Мінімальний контракт Gateway, потрібний OrderBook. Визначаємо Protocol нижче."""


class OrderBookEngine:
    """Один екземпляр на процес. Працює над N символами та M таймфреймами."""

    def __init__(
        self,
        config: OBConfig,
        gateway: object,  # MarketDataGateway — не імпортуємо, щоб не мати циклів
        *,
        clock_fn: ClockFn | None = None,
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())

        self._ctxs: dict[str, _SymbolCtx] = {}
        self._symbols: list[str] = []
        self._timeframes: list[str] = list(config.timeframes)

        self._bar_close_cbs: list[BarCloseCallback] = []
        self._started = False
        self._shutdown = asyncio.Event()

        # Службова задача, що закриває «застряглі» бари при відсутності трейдів
        # (щоб TF-границі залишались синхронні з годинником).
        self._chrono_task: asyncio.Task[None] | None = None

    # === Lifecycle ===

    async def start(self, symbols: list[str]) -> None:
        if self._started:
            raise RuntimeError("OrderBookEngine already started")
        self._symbols = [s.upper() for s in symbols]

        # Підписуємось на Gateway — один глобальний callback, роутимо по symbol.
        self._gateway.on_depth_diff(self._on_depth_diff)  # type: ignore[attr-defined]
        self._gateway.on_agg_trade(self._on_agg_trade)    # type: ignore[attr-defined]

        # Підготовка контекстів + паралельний warmup.
        for sym in self._symbols:
            book = _BookState(symbol=sym)
            self._ctxs[sym] = _SymbolCtx(
                symbol=sym, book=book,
                closed_history={tf: deque(maxlen=self._config.closed_history_size) for tf in self._timeframes},
            )
        await asyncio.gather(*(self._init_symbol(sym) for sym in self._symbols))

        self._chrono_task = asyncio.create_task(self._chrono_close_loop(), name="book.chrono")
        self._started = True

    async def stop(self) -> None:
        self._shutdown.set()
        if self._chrono_task:
            self._chrono_task.cancel()
            try:
                await self._chrono_task
            except (asyncio.CancelledError, Exception):
                pass
            self._chrono_task = None
        for ctx in self._ctxs.values():
            if ctx.reinit_task:
                ctx.reinit_task.cancel()
        self._started = False

    def on_bar_close(self, cb: BarCloseCallback) -> None:
        self._bar_close_cbs.append(cb)

    # === Query (для FeatureEngine/SetupDetector) ===

    def get_book(self, symbol: str) -> OrderBookState:
        ctx = self._require_ctx(symbol)
        return ctx.book.top_snapshot(self._config.levels_to_keep, self._clock())

    def get_current_footprint(self, symbol: str, tf: str) -> FootprintBar:
        ctx = self._require_ctx(symbol)
        bar = ctx.active_bars.get(tf)
        if bar is None:
            # Стартовий порожній бар, щоб споживач не отримав None раніше першого трейду.
            bar = new_bar(symbol.upper(), tf, self._clock())
            ctx.active_bars[tf] = bar
        return bar

    def get_recent_footprints(self, symbol: str, tf: str, n: int) -> list[FootprintBar]:
        ctx = self._require_ctx(symbol)
        history = ctx.closed_history.get(tf)
        if not history:
            return []
        return list(history)[-n:]

    # === Internal: init / reinit ===

    async def _init_symbol(self, symbol: str) -> None:
        ctx = self._ctxs[symbol]
        cfg = self._config.reinit
        for attempt in range(1, cfg.max_attempts + 1):
            try:
                await self._init_once(ctx)
                return
            except SequenceGapError as e:
                logger.warning("Reinit %s attempt %d failed: %s", symbol, attempt, e)
                await asyncio.sleep(cfg.backoff_ms / 1000)
            except Exception:
                logger.exception("Reinit %s attempt %d crashed", symbol, attempt)
                await asyncio.sleep(cfg.backoff_ms / 1000)
        logger.error("Failed to init book for %s after %d attempts", symbol, cfg.max_attempts)

    async def _init_once(self, ctx: _SymbolCtx) -> None:
        ctx.book.initialized = False
        ctx.warmup_buffer.clear()

        # 1. Дочекатись першого diff-у (його зібрав _on_depth_diff у warmup_buffer).
        t0 = self._clock()
        while not ctx.warmup_buffer:
            if self._clock() - t0 > self._config.reinit.warmup_diff_timeout_ms:
                raise SequenceGapError(f"{ctx.symbol}: no diff within warmup timeout")
            await asyncio.sleep(0.05)

        # 2. REST snapshot.
        snap = await self._gateway.fetch_depth_snapshot(  # type: ignore[attr-defined]
            ctx.symbol, limit=self._config.reinit.snapshot_limit,
        )
        ctx.book.load_snapshot(snap)

        # 3. Replay буферу з перевірками sequence.
        snap_last = snap.last_update_id
        try:
            for diff in list(ctx.warmup_buffer):
                ctx.book.apply_warmup_diff(diff, snap_last)
        except SequenceGapError:
            if self._config.reinit.relaxed_sync:
                # Testnet / дрифтанутий snapshot: беремо поточний snapshot як
                # good-enough state, скидаємо buffered diffs і приймаємо лише
                # свіжі, у яких u > snap.U. Коротка початкова розсинхронізація
                # допустима (≤ секунди).
                logger.warning(
                    "%s: relaxed_sync — snapshot застарілий, приймаємо book з "
                    "snapshot + live diffs з u > %d", ctx.symbol, snap_last,
                )
                ctx.book.last_update_id = snap_last
            else:
                raise
        ctx.warmup_buffer.clear()
        ctx.book.initialized = True
        logger.info(
            "OrderBook %s initialized at u=%d (%d bids / %d asks)",
            ctx.symbol, ctx.book.last_update_id, len(ctx.book.bids), len(ctx.book.asks),
        )

    async def _schedule_reinit(self, symbol: str) -> None:
        ctx = self._ctxs.get(symbol)
        if ctx is None or (ctx.reinit_task and not ctx.reinit_task.done()):
            return
        ctx.reinit_task = asyncio.create_task(self._init_symbol(symbol), name=f"book.reinit.{symbol}")

    # === Internal: live feed handlers ===

    async def _on_depth_diff(self, diff: RawDepthDiff) -> None:
        ctx = self._ctxs.get(diff.symbol)
        if ctx is None:
            return
        if not ctx.book.initialized:
            ctx.warmup_buffer.append(diff)
            return
        # У relaxed mode: skip diffs що вже в snapshot, а SequenceGapError
        # трактуємо як одноразовий resync (для testnet).
        if self._config.reinit.relaxed_sync and diff.final_update_id <= ctx.book.last_update_id:
            return
        try:
            ctx.book.apply_diff(diff)
        except SequenceGapError as e:
            if self._config.reinit.relaxed_sync:
                logger.debug("%s: relaxed resync on gap: %s", diff.symbol, e)
                ctx.book.apply_diff_relaxed(diff)
                return
            logger.error("Sequence gap on %s: %s — scheduling reinit", diff.symbol, e)
            ctx.book.initialized = False
            await self._schedule_reinit(diff.symbol)

    async def _on_agg_trade(self, trade: RawAggTrade) -> None:
        ctx = self._ctxs.get(trade.symbol)
        if ctx is None:
            return
        tick_size = self._resolve_tick_size(trade.symbol)
        for tf in self._timeframes:
            bar = ctx.active_bars.get(tf)
            if bar is None:
                bar = new_bar(trade.symbol, tf, trade.timestamp_ms)
                ctx.active_bars[tf] = bar
            # Переходимо через кордони TF — закриваємо старий, відкриваємо новий (і наступний якщо знову wrap).
            while trade.timestamp_ms >= bar.close_time_ms:
                bar = await self._close_and_start_next(ctx, tf, bar, trade.timestamp_ms)
            update_bar(bar, trade, tick_size)

    async def _close_and_start_next(
        self, ctx: _SymbolCtx, tf: str, bar: FootprintBar, now_ms: int,
    ) -> FootprintBar:
        close_bar(bar)
        ctx.closed_history[tf].append(bar)
        for cb in self._bar_close_cbs:
            try:
                await cb(bar)
            except Exception:
                logger.exception("bar_close callback failed (tf=%s)", tf)
        fresh = new_bar(ctx.symbol, tf, max(bar.close_time_ms, now_ms))
        ctx.active_bars[tf] = fresh
        return fresh

    # === Internal: chrono close ===

    async def _chrono_close_loop(self) -> None:
        """Закриває бари коли немає трейдів (щоб TF-сітка не відставала)."""
        # Прокидаємось кожні 250мс — дешево і достатньо точно.
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
            if self._shutdown.is_set():
                return
            now = self._clock()
            for ctx in self._ctxs.values():
                for tf in self._timeframes:
                    bar = ctx.active_bars.get(tf)
                    if bar is None:
                        continue
                    if now >= bar.close_time_ms and bar.trade_count > 0:
                        # Є що закривати — створюємо наступний (лінк у ланцюгу)
                        await self._close_and_start_next(ctx, tf, bar, now)
                    elif now >= bar.close_time_ms and bar.trade_count == 0:
                        # Порожній бар — проштовхуємо вперед, без close-callback-у.
                        ctx.active_bars[tf] = new_bar(ctx.symbol, tf, now)

    # === Helpers ===

    def _resolve_tick_size(self, symbol: str) -> float:
        try:
            filters = self._gateway.get_symbol_filters(symbol)  # type: ignore[attr-defined]
            return float(filters.tick_size)
        except Exception:
            # Фолбек: 0 → round_to_tick поверне ціну як є (без кластеризації по тіку).
            return 0.0

    def _require_ctx(self, symbol: str) -> _SymbolCtx:
        ctx = self._ctxs.get(symbol.upper())
        if ctx is None:
            raise RuntimeError(f"Unknown symbol: {symbol!r} (start() not called or symbol not in list)")
        return ctx


# Зберігаємо старий експорт OrderBookState для сумісності з існуючим __init__.
from scalper.book.types import OrderBookState as _PubState  # noqa: E402

OrderBookState = _PubState

__all__ = ["OrderBookEngine", "OrderBookState", "BarCloseCallback"]
