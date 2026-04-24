"""SessionStats — агрегує журнал у лічильники для UI.

Підписується на JournalTailer і тримає поточний snapshot:
  - uptime — з моменту startup події сесії
  - trades_closed — кількість position_closed / trade_outcome
  - realized_r — сума реалізованих R
  - realized_usd — сума реалізованого PnL у $
  - last_event_ms — для "живий/мертвий" індикатора
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from scalper.common import time as _time
from scalper.dashboard.tailer import JournalTailer

logger = logging.getLogger(__name__)


@dataclass
class SessionSnapshot:
    session_started_ms: int | None
    uptime_ms: int
    trades_closed: int
    open_positions: int
    realized_r: float
    realized_usd: float
    current_equity_usd: float | None
    last_event_ms: int | None
    kinds_counter: dict[str, int]


class SessionStats:
    """Тонкий агрегатор подій — читає журнал через JournalTailer і рахує."""

    def __init__(self, tailer: JournalTailer) -> None:
        self._tailer = tailer
        self._session_started_ms: int | None = None
        self._trades_closed: int = 0
        self._open_positions: int = 0
        self._realized_r: float = 0.0
        self._realized_usd: float = 0.0
        self._current_equity_usd: float | None = None
        self._last_event_ms: int | None = None
        self._kinds: dict[str, int] = {}
        self._unsubscribe: Any = None

    async def start(self) -> None:
        # Початковий backfill — останні події, щоб не починати з нуля при рестарті UI
        for ev in self._tailer.read_recent(limit=2000):
            self._ingest(ev)
        self._unsubscribe = self._tailer.subscribe(self._on_event)

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def snapshot(self) -> SessionSnapshot:
        now = _time.clock()
        uptime = (
            now - self._session_started_ms
            if self._session_started_ms is not None else 0
        )
        return SessionSnapshot(
            session_started_ms=self._session_started_ms,
            uptime_ms=uptime,
            trades_closed=self._trades_closed,
            open_positions=self._open_positions,
            realized_r=self._realized_r,
            realized_usd=self._realized_usd,
            current_equity_usd=self._current_equity_usd,
            last_event_ms=self._last_event_ms,
            kinds_counter=dict(self._kinds),
        )

    def reset(self) -> None:
        self._session_started_ms = None
        self._trades_closed = 0
        self._open_positions = 0
        self._realized_r = 0.0
        self._realized_usd = 0.0
        self._current_equity_usd = None
        self._last_event_ms = None
        self._kinds.clear()

    # === Internal ===

    async def _on_event(self, event: dict[str, Any]) -> None:
        self._ingest(event)

    def _ingest(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind is None:
            return
        ts = event.get("timestamp_ms")
        if isinstance(ts, int):
            self._last_event_ms = ts
        payload = event.get("payload") or {}
        self._kinds[kind] = self._kinds.get(kind, 0) + 1

        if kind == "startup" and "symbols" in payload:
            # Це подія з симолами = сесія стартувала. Reset and record.
            self.reset()
            self._session_started_ms = ts if isinstance(ts, int) else _time.clock()
            self._last_event_ms = self._session_started_ms
        elif kind == "position_opened":
            self._open_positions += 1
        elif kind == "position_closed":
            self._trades_closed += 1
            self._open_positions = max(0, self._open_positions - 1)
        elif kind == "trade_outcome":
            r = payload.get("realized_r")
            usd = payload.get("realized_usd")
            if isinstance(r, (int, float)):
                self._realized_r += float(r)
            if isinstance(usd, (int, float)):
                self._realized_usd += float(usd)
        elif kind == "heartbeat":
            eq = payload.get("equity_usd")
            if isinstance(eq, (int, float)):
                self._current_equity_usd = float(eq)


__all__ = ["SessionSnapshot", "SessionStats"]
