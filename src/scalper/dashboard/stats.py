"""SessionStats — агрегує журнал у лічильники ПО СИМВОЛАХ.

Кожна пара торгується окремим процесом бота → кожна має свою сесію:
час роботи, трейди, PnL. Підписуємось на один JournalTailer, але події
маршрутизуємо по `event.symbol`.

startup event з `payload.symbols=[X]` → reset сесії для символу X.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from scalper.common import time as _time
from scalper.dashboard.tailer import JournalTailer

logger = logging.getLogger(__name__)


@dataclass
class SessionSnapshot:
    """Статистика для ОДНОЇ пари. Глобальний знімок — dict[symbol, SessionSnapshot]."""

    symbol: str
    session_started_ms: int | None
    uptime_ms: int
    trades_closed: int
    open_positions: int
    realized_r: float
    realized_usd: float
    last_event_ms: int | None
    kinds_counter: dict[str, int]


class _SymbolState:
    """Лічильники для однієї пари."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.session_started_ms: int | None = None
        self.trades_closed: int = 0
        self.open_positions: int = 0
        self.realized_r: float = 0.0
        self.realized_usd: float = 0.0
        self.last_event_ms: int | None = None
        self.kinds: dict[str, int] = {}

    def reset(self) -> None:
        self.session_started_ms = None
        self.trades_closed = 0
        self.open_positions = 0
        self.realized_r = 0.0
        self.realized_usd = 0.0
        self.last_event_ms = None
        self.kinds.clear()


class SessionStats:
    """Per-symbol агрегатор подій."""

    def __init__(self, tailer: JournalTailer) -> None:
        self._tailer = tailer
        self._per_symbol: dict[str, _SymbolState] = {}
        self._unsubscribe: Any = None

    async def start(self) -> None:
        for ev in self._tailer.read_recent(limit=5000):
            self._ingest(ev)
        self._unsubscribe = self._tailer.subscribe(self._on_event)

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def snapshot(self, symbol: str) -> SessionSnapshot | None:
        sym = symbol.upper()
        state = self._per_symbol.get(sym)
        if state is None:
            return None
        return self._to_snapshot(state)

    def snapshot_all(self) -> dict[str, SessionSnapshot]:
        return {sym: self._to_snapshot(s) for sym, s in self._per_symbol.items()}

    def reset(self, symbol: str) -> None:
        """Викликається з API /start, щоб лічильники почались з нуля для сесії."""
        sym = symbol.upper()
        if sym in self._per_symbol:
            self._per_symbol[sym].reset()

    # === Internal ===

    def _to_snapshot(self, s: _SymbolState) -> SessionSnapshot:
        now = _time.clock()
        uptime = (
            now - s.session_started_ms if s.session_started_ms is not None else 0
        )
        return SessionSnapshot(
            symbol=s.symbol,
            session_started_ms=s.session_started_ms,
            uptime_ms=uptime,
            trades_closed=s.trades_closed,
            open_positions=s.open_positions,
            realized_r=s.realized_r,
            realized_usd=s.realized_usd,
            last_event_ms=s.last_event_ms,
            kinds_counter=dict(s.kinds),
        )

    def _get_or_create(self, symbol: str) -> _SymbolState:
        sym = symbol.upper()
        if sym not in self._per_symbol:
            self._per_symbol[sym] = _SymbolState(sym)
        return self._per_symbol[sym]

    async def _on_event(self, event: dict[str, Any]) -> None:
        self._ingest(event)

    def _ingest(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind is None:
            return
        ts = event.get("timestamp_ms")
        payload = event.get("payload") or {}

        # startup події з payload.symbols=[X] стосуються саме цих символів —
        # reset їх як нова сесія. Подія без symbols — це bot PID, ігноруємо.
        if kind == "startup":
            syms = payload.get("symbols")
            if isinstance(syms, list):
                for sym in syms:
                    state = self._get_or_create(sym)
                    state.reset()
                    state.session_started_ms = ts if isinstance(ts, int) else _time.clock()
                    state.last_event_ms = state.session_started_ms
                    state.kinds[kind] = state.kinds.get(kind, 0) + 1
            return

        # Решта подій мають event.symbol → до цього слоту
        event_sym = event.get("symbol")
        if not isinstance(event_sym, str):
            return

        state = self._get_or_create(event_sym)
        if isinstance(ts, int):
            state.last_event_ms = ts
        state.kinds[kind] = state.kinds.get(kind, 0) + 1

        if kind == "position_opened":
            state.open_positions += 1
        elif kind == "position_closed":
            state.trades_closed += 1
            state.open_positions = max(0, state.open_positions - 1)
        elif kind == "trade_outcome":
            r = payload.get("realized_r")
            usd = payload.get("realized_usd")
            if isinstance(r, (int, float)):
                state.realized_r += float(r)
            if isinstance(usd, (int, float)):
                state.realized_usd += float(usd)


__all__ = ["SessionSnapshot", "SessionStats"]
