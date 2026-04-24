"""TapeAnalyzer — підписка на Gateway.on_agg_trade, агрегація у вікна, CVD, fan-out.

Архітектурне рішення з doc-у:
  trade-події фан-аутяться ЧЕРЕЗ TapeAnalyzer (а не безпосередньо з Gateway), щоб
  порядок гарантовано був "tape оновлено → OB footprint оновлено → downstream бачить
  узгоджений знімок". Тому OrderBookEngine має підписуватися на TapeAnalyzer.on_trade(),
  а НЕ на Gateway.on_agg_trade(). Поки існуючий код підписує OB напряму на Gateway —
  міграція в Orchestrator-кроці.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from scalper.common import time as _time
from scalper.gateway.types import RawAggTrade
from scalper.tape.config import TapeConfig
from scalper.tape.rolling import _RollingWindow, _TradeContribution, add, evict_until, snapshot
from scalper.tape.types import TapeWindow, TapeWindowsState

logger = logging.getLogger(__name__)

TradeCallback = Callable[[RawAggTrade], Awaitable[None]]
ClockFn = Callable[[], int]

_EMPTY_WINDOW_CACHE: dict[int, TapeWindow] = {}


def _empty_window(duration_ms: int) -> TapeWindow:
    cached = _EMPTY_WINDOW_CACHE.get(duration_ms)
    if cached is not None:
        return cached
    win = TapeWindow(
        duration_ms=duration_ms, trade_count=0,
        buy_volume_qty=0.0, sell_volume_qty=0.0,
        buy_volume_usd=0.0, sell_volume_usd=0.0,
        delta_qty=0.0, delta_usd=0.0,
        last_trade_price=0.0, first_trade_ms=0, last_trade_ms=0,
    )
    _EMPTY_WINDOW_CACHE[duration_ms] = win
    return win


@dataclass
class _SymbolTapeState:
    symbol: str
    short_w: _RollingWindow
    medium_w: _RollingWindow
    long_w: _RollingWindow
    trades: deque[RawAggTrade]
    price_path: deque[tuple[int, float]]
    cvd: float = 0.0
    cvd_unreliable_until_ms: int = 0
    last_agg_id: int = 0
    last_trade_ms: int = 0
    last_price: float = 0.0


class TapeAnalyzer:
    """Один екземпляр на процес. Per-symbol стан створюється в `start()`."""

    def __init__(
        self,
        config: TapeConfig,
        gateway: object,
        *,
        clock_fn: ClockFn | None = None,
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())

        self._states: dict[str, _SymbolTapeState] = {}
        self._trade_cbs: list[TradeCallback] = []
        self._started = False
        self._unreliable_window_ms = config.tape_gap.unreliable_window_min * 60_000

    # === Lifecycle ===

    async def start(self, symbols: list[str]) -> None:
        if self._started:
            raise RuntimeError("TapeAnalyzer already started")
        for sym in symbols:
            sym = sym.upper()
            self._states[sym] = _SymbolTapeState(
                symbol=sym,
                short_w=_RollingWindow(duration_ms=self._config.windows.short_ms),
                medium_w=_RollingWindow(duration_ms=self._config.windows.medium_ms),
                long_w=_RollingWindow(duration_ms=self._config.windows.long_ms),
                trades=deque(maxlen=self._config.trade_buffer_maxlen),
                price_path=deque(maxlen=self._config.price_path_maxlen),
            )
        self._gateway.on_agg_trade(self._on_trade)  # type: ignore[attr-defined]
        self._started = True

    async def stop(self) -> None:
        self._started = False
        # State лишаємо, бо хтось ще може робити post-mortem read; новий start() пере-створить.

    # === Subscribe ===

    def on_trade(self, cb: TradeCallback) -> None:
        self._trade_cbs.append(cb)

    # === Query ===

    def get_windows(self, symbol: str) -> TapeWindowsState:
        sym = symbol.upper()
        s = self._states.get(sym)
        if s is None:
            return self._empty_state(sym)
        now = self._clock()
        # Force-evict перед read — критично, бо без свіжих trade'ів вікна «зависнуть».
        evict_until(s.short_w, now)
        evict_until(s.medium_w, now)
        evict_until(s.long_w, now)
        last_price = s.last_price
        cvd_ok = now > s.cvd_unreliable_until_ms
        return TapeWindowsState(
            symbol=sym, timestamp_ms=now,
            window_500ms=snapshot(s.short_w, last_price),
            window_2s=snapshot(s.medium_w, last_price),
            window_10s=snapshot(s.long_w, last_price),
            cvd=s.cvd, cvd_reliable=cvd_ok,
            delta_500ms=s.short_w.sum_buy_usd - s.short_w.sum_sell_usd,
            delta_2s=s.medium_w.sum_buy_usd - s.medium_w.sum_sell_usd,
            delta_10s=s.long_w.sum_buy_usd - s.long_w.sum_sell_usd,
            price_path=list(s.price_path),
        )

    def get_recent_trades(self, symbol: str, n: int) -> list[RawAggTrade]:
        s = self._states.get(symbol.upper())
        if s is None or not s.trades:
            return []
        if n <= 0:
            return []
        if n >= len(s.trades):
            return list(s.trades)
        return list(s.trades)[-n:]

    def get_cvd(self, symbol: str) -> float:
        s = self._states.get(symbol.upper())
        return s.cvd if s else 0.0

    def is_cvd_reliable(self, symbol: str) -> bool:
        s = self._states.get(symbol.upper())
        if s is None:
            return True
        return self._clock() > s.cvd_unreliable_until_ms

    # === Internal ===

    async def _on_trade(self, trade: RawAggTrade) -> None:
        s = self._states.get(trade.symbol)
        if s is None:
            return

        # 1) Gap detection. Перший trade — просто запам'ятати, не вважати gap-ом.
        if s.last_agg_id != 0:
            expected = s.last_agg_id + 1
            if trade.agg_id < s.last_agg_id:
                # agg_id поїхав назад (рестарт біржі/maintenance) — лог + ресет без unreliable.
                logger.warning("tape agg_id reset on %s: %d -> %d", trade.symbol, s.last_agg_id, trade.agg_id)
            elif trade.agg_id != expected:
                missing = trade.agg_id - expected
                logger.warning("tape_gap %s missing=%d (last=%d, got=%d)",
                               trade.symbol, missing, s.last_agg_id, trade.agg_id)
                s.cvd_unreliable_until_ms = trade.timestamp_ms + self._unreliable_window_ms
        s.last_agg_id = trade.agg_id

        # 2) Buffer + price_path
        s.trades.append(trade)
        s.price_path.append((trade.timestamp_ms, trade.price))
        s.last_trade_ms = trade.timestamp_ms
        s.last_price = trade.price

        # 3) Rolling windows
        contrib = _TradeContribution(
            ts_ms=trade.timestamp_ms, price=trade.price,
            qty=trade.quantity, is_buyer_maker=trade.is_buyer_maker,
        )
        for w in (s.short_w, s.medium_w, s.long_w):
            add(w, contrib)
            evict_until(w, trade.timestamp_ms)

        # 4) CVD (USD)
        qty_usd = trade.price * trade.quantity
        s.cvd += -qty_usd if trade.is_buyer_maker else qty_usd

        # 5) Fan-out — після оновлення стану, щоб підписники бачили новий знімок.
        for cb in self._trade_cbs:
            try:
                await cb(trade)
            except Exception:
                logger.exception("tape on_trade callback raised")

    def _empty_state(self, symbol: str) -> TapeWindowsState:
        return TapeWindowsState(
            symbol=symbol, timestamp_ms=self._clock(),
            window_500ms=_empty_window(self._config.windows.short_ms),
            window_2s=_empty_window(self._config.windows.medium_ms),
            window_10s=_empty_window(self._config.windows.long_ms),
            cvd=0.0, cvd_reliable=True,
            delta_500ms=0.0, delta_2s=0.0, delta_10s=0.0,
            price_path=[],
        )


# Backward-compat alias (overview/smoke-test очікує цю назву).
TapeFlowAnalyzer = TapeAnalyzer

__all__ = ["TapeAnalyzer", "TapeFlowAnalyzer"]
