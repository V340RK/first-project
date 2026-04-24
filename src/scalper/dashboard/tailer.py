"""JournalTailer — асинхронний «tail -f» над JSONL журналом.

Модель роботи:
  • фонова задача кожні poll_interval_ms перечитує новий хвіст сьогоднішнього файлу;
  • парсить кожен ЦІЛИЙ рядок (тобто з `\\n` на кінці) і розсилає підписникам;
  • якщо настала нова UTC-доба — перемикається на новий файл.

Чому не `watchfiles`/inotify: Windows підтримує нестабільно, polling надійніший і простіший.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scalper.common import time as _time

logger = logging.getLogger(__name__)

EventDict = dict[str, Any]
Subscriber = Callable[[EventDict], Awaitable[None]]
ClockFn = Callable[[], int]


class JournalTailer:
    """Один екземпляр — один каталог журналу. Багато підписників."""

    def __init__(
        self,
        journal_dir: Path,
        *,
        poll_interval_ms: int = 150,
        clock_fn: ClockFn | None = None,
    ) -> None:
        self._dir = journal_dir
        self._interval_s = poll_interval_ms / 1000
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())

        self._subscribers: set[Subscriber] = set()
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        # Стан читача
        self._current_date: str = ""
        self._current_path: Path | None = None
        self._current_pos: int = 0
        self._pending: str = ""  # хвіст без `\n` від попереднього читання

    # === Lifecycle ===

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("JournalTailer вже запущений")
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="dashboard.tailer")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    # === Subscribe API ===

    def subscribe(self, cb: Subscriber) -> Callable[[], None]:
        """Повертає функцію-unsubscribe."""
        self._subscribers.add(cb)
        return lambda: self._subscribers.discard(cb)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # === Read-only helpers (для backfill при connect) ===

    def read_recent(self, limit: int = 200) -> list[EventDict]:
        """Повертає останні `limit` валідних рядків з сьогоднішнього файлу.
        Використовується для відновлення стану при підключенні нового клієнта.
        """
        today = self._today_str()
        path = self._dir / f"{today}.jsonl"
        if not path.exists():
            return []
        # Простий варіант: читаємо увесь файл. Файл за добу рідко > кількох МБ.
        # Якщо стане вузьким місцем — замінимо на seek з кінця.
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        tail = lines[-limit:] if limit > 0 else lines
        out: list[EventDict] = []
        for raw in tail:
            line = raw.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # === Internal: poll loop ===

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("JournalTailer tick failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        today = self._today_str()
        if today != self._current_date:
            self._switch_to(today)

        if self._current_path is None or not self._current_path.exists():
            return

        # Якщо файл «вкоротився» (рідко, але можливо при ручному втручанні) — рестартуємо з 0.
        size = self._current_path.stat().st_size
        if size < self._current_pos:
            self._current_pos = 0
            self._pending = ""

        if size == self._current_pos:
            return

        with self._current_path.open("rb") as f:
            f.seek(self._current_pos)
            chunk = f.read(size - self._current_pos)
            self._current_pos = f.tell()

        # Додаємо до незавершеного хвоста, ділимо по `\n`, залишок — новий хвіст.
        text = self._pending + chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._pending = lines.pop()  # все після останнього `\n` (може бути пустим)

        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping corrupt line in %s", self._current_path.name)
                continue
            await self._broadcast(event)

    async def _broadcast(self, event: EventDict) -> None:
        if not self._subscribers:
            return
        # Копія, щоб можна було безпечно unsubscribe з колбеку.
        for cb in list(self._subscribers):
            try:
                await cb(event)
            except Exception:
                logger.exception("Subscriber callback raised; keeping subscription")

    def _switch_to(self, date_str: str) -> None:
        """Перемикаємось на файл нової дати.

        Дві ситуації:
          • Перший старт (current_date==""): файл може бути вже великий — читати з кінця,
            щоб не бомбити підписників історією. Бекфіл надається окремо через read_recent().
          • Ротація (current_date != ""): нова доба, читаємо з початку, бо це «свіжий» файл,
            а все що в ньому встигло з'явитись — події нової доби, які ми хочемо побачити.
        """
        is_initial = self._current_date == ""
        self._current_date = date_str
        self._current_path = self._dir / f"{date_str}.jsonl"
        self._pending = ""
        if is_initial and self._current_path.exists():
            try:
                self._current_pos = self._current_path.stat().st_size
            except OSError:
                self._current_pos = 0
        else:
            self._current_pos = 0

    def _today_str(self) -> str:
        ms = self._clock()
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    # === Testing hooks ===

    def _force_switch_for_test(self, date_str: str, start_from_zero: bool = True) -> None:
        """Використовується тестами — симулює зміну дати без клокера."""
        self._current_date = date_str
        self._current_path = self._dir / f"{date_str}.jsonl"
        self._current_pos = 0 if start_from_zero else (
            self._current_path.stat().st_size if self._current_path.exists() else 0
        )
        self._pending = ""


__all__ = ["JournalTailer", "Subscriber"]
