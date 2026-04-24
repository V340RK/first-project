"""JournalLogger — append-only JSONL writer + read-only query API.

Архітектура та edge cases — DOCS/architecture/11-journal-logger.md.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import shutil
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from scalper.common import time as _time
from scalper.journal.config import JournalConfig
from scalper.journal.serialization import (
    event_from_dict,
    event_to_dict,
    event_to_jsonl,
)
from scalper.journal.types import EventKind, JournalEvent, TradeRecord

logger = logging.getLogger(__name__)

ClockFn = Callable[[], int]


class JournalLogger:
    """Один writer на процес. Sink для всіх 12 модулів."""

    def __init__(self, config: JournalConfig, clock_fn: ClockFn | None = None) -> None:
        self._config = config
        # Лямбда тримає посилання на модуль, тож monkeypatch у тестах працює.
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())

        self._config.journal_dir.mkdir(parents=True, exist_ok=True)

        self._queue: asyncio.Queue[JournalEvent] = asyncio.Queue(maxsize=config.queue_size)
        self._shutdown = asyncio.Event()
        self._writer_task: asyncio.Task[None] | None = None

        # Стан writer-а — змінюється тільки в _writer_loop і допоміжних.
        self._current_file: IO[str] | None = None
        self._current_date: str = ""
        self._current_seq: int = 0
        self._unflushed_bytes: int = 0
        self._last_fsync_ms: int = 0
        self._dropped_count: int = 0

    # === Lifecycle ===

    async def start(self) -> None:
        if self._writer_task is not None:
            raise RuntimeError("JournalLogger вже запущений")
        self._shutdown.clear()
        self._open_today_file()
        self._writer_task = asyncio.create_task(self._writer_loop(), name="journal.writer")
        # Стартова подія йде через звичайний log() — щоб бачити рестарти у журналі.
        self.log(JournalEvent(
            seq=0,
            timestamp_ms=self._clock(),
            kind=EventKind.STARTUP,
            trade_id=None,
            symbol=None,
            payload={"pid": os.getpid()},
        ))

    async def stop(self) -> None:
        """Graceful shutdown: дописати все з черги, fsync, закрити файл."""
        if self._writer_task is None:
            return
        # Останній log — щоб у файлі залишився маркер чистого зупину.
        self.log(JournalEvent(
            seq=0,
            timestamp_ms=self._clock(),
            kind=EventKind.SHUTDOWN,
            trade_id=None,
            symbol=None,
            payload={"dropped": self._dropped_count},
        ))
        self._shutdown.set()
        try:
            await self._writer_task
        except asyncio.CancelledError:
            pass
        self._writer_task = None
        self._close_current_file()

    # === Sink API ===

    def log(self, event: JournalEvent) -> None:
        """Non-blocking. Заповнення черги → drop counter, не падаємо."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped_count += 1
            logger.error(
                "Journal queue full, dropping event (kind=%s, total_dropped=%d)",
                event.kind.value, self._dropped_count,
            )

    async def log_async(self, event: JournalEvent) -> None:
        """Awaitable варіант — блокує до звільнення місця у черзі.
        Використовуй ТІЛЬКИ якщо втрата події неприйнятна (наприклад, фінальний TRADE_OUTCOME).
        """
        await self._queue.put(event)

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    # === Internal: writer loop ===

    async def _writer_loop(self) -> None:
        """Тягне з черги батчами, пише, періодично fsync, ротує опівночі UTC."""
        timeout_s = self._config.flush_interval_ms / 1000
        while True:
            batch: list[JournalEvent] = []
            # Чекаємо хоча б одну подію, або вийдемо при shutdown.
            try:
                first = await asyncio.wait_for(self._queue.get(), timeout=timeout_s)
                batch.append(first)
            except asyncio.TimeoutError:
                pass

            # Дренируємо чергу до batch_max за одну ітерацію (без awaits).
            while len(batch) < self._config.batch_max and not self._queue.empty():
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            # Ротація — ПЕРЕД записом, інакше події нової доби потраплять у старий файл.
            await self._maybe_rotate()

            if batch:
                try:
                    self._write_batch(batch)
                except OSError:
                    logger.exception("Journal write failed (диск повний?), drop batch")
                    self._dropped_count += len(batch)

            self._maybe_fsync()

            if self._shutdown.is_set() and self._queue.empty():
                # Фінальний fsync перед виходом
                self._maybe_fsync(force=True)
                break

    def _write_batch(self, batch: list[JournalEvent]) -> None:
        if self._current_file is None:
            raise RuntimeError("writer не ініціалізовано (start() не викликано)")
        chunks: list[str] = []
        for event in batch:
            seq = self._current_seq
            self._current_seq += 1
            stamped = replace(event, seq=seq)
            chunks.append(event_to_jsonl(stamped))
        data = "".join(chunks)
        self._current_file.write(data)
        self._unflushed_bytes += len(data)

    def _maybe_fsync(self, force: bool = False) -> None:
        if self._current_file is None:
            return
        now = self._clock()
        time_due = (now - self._last_fsync_ms) > self._config.fsync_interval_ms
        bytes_due = self._unflushed_bytes > self._config.fsync_bytes_threshold
        if force or time_due or bytes_due:
            self._current_file.flush()
            try:
                os.fsync(self._current_file.fileno())
            except OSError:
                # На Windows fsync для деяких filesystems може кинути EINVAL — некритично
                pass
            self._last_fsync_ms = now
            self._unflushed_bytes = 0

    async def _maybe_rotate(self) -> None:
        today = self._today_str()
        if today == self._current_date:
            return
        # Закриваємо старий файл, gzip у фоні, відкриваємо новий.
        old_date = self._current_date
        self._maybe_fsync(force=True)
        self._close_current_file()
        if self._config.gzip_old_files:
            old_path = self._path_for(old_date)
            asyncio.create_task(self._gzip_async(old_path), name="journal.gzip")
        self._open_file_for(today)
        # Логуємо ротацію вже у НОВИЙ файл — синхронно, в обхід черги.
        self._write_batch([JournalEvent(
            seq=0,
            timestamp_ms=self._clock(),
            kind=EventKind.DAILY_ROLLOVER,
            trade_id=None,
            symbol=None,
            payload={"previous_date": old_date},
        )])

    # === Internal: file management ===

    def _path_for(self, date_str: str) -> Path:
        return self._config.journal_dir / f"{date_str}.jsonl"

    def _today_str(self) -> str:
        ms = self._clock()
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    def _open_today_file(self) -> None:
        today = self._today_str()
        self._open_file_for(today)

    def _open_file_for(self, date_str: str) -> None:
        path = self._path_for(date_str)
        self._current_date = date_str
        if path.exists() and path.stat().st_size > 0:
            self._current_seq = self._read_last_seq(path) + 1
            # `a` mode + utf-8; line buffering не використовуємо (буферизуємо самі)
            self._current_file = path.open("a", encoding="utf-8")
        else:
            self._current_file = path.open("w", encoding="utf-8")
            self._current_seq = 1
        self._unflushed_bytes = 0
        self._last_fsync_ms = self._clock()

    def _close_current_file(self) -> None:
        if self._current_file is not None:
            try:
                self._current_file.flush()
                try:
                    os.fsync(self._current_file.fileno())
                except OSError:
                    pass
            finally:
                self._current_file.close()
                self._current_file = None

    @staticmethod
    def _read_last_seq(path: Path) -> int:
        """Швидкий tail-read: скачуємо останні 4KB і парсимо останній цілий рядок.

        Едж-кейс: якщо процес впав між write і `\\n`, останній рядок — обірваний.
        Тоді беремо ПЕРЕДостанній.
        """
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return 0
            chunk = min(size, 4096)
            f.seek(-chunk, os.SEEK_END)
            tail = f.read(chunk).decode("utf-8", errors="ignore")
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        # Якщо файл не закінчується на \n — останній рядок підозріло обрізаний; пробуємо передостанній.
        candidates = lines[::-1]
        for line in candidates:
            try:
                return int(json.loads(line).get("seq", 0))
            except (json.JSONDecodeError, ValueError):
                continue
        return 0

    async def _gzip_async(self, path: Path) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._gzip_sync, path)

    @staticmethod
    def _gzip_sync(path: Path) -> None:
        if not path.exists():
            return
        gz_path = path.with_suffix(".jsonl.gz")
        with path.open("rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        path.unlink()

    # === Read-only query API ===

    def iter_events(
        self,
        date_utc: str,
        *,
        kinds: set[EventKind] | None = None,
    ) -> Iterator[JournalEvent]:
        """Lazy iterator — читає файл рядок за рядком, не вантажить все в RAM."""
        path = self._path_for(date_utc)
        if not path.exists():
            gz_path = path.with_suffix(".jsonl.gz")
            if not gz_path.exists():
                return iter(())
            return self._iter_file(gz_path, kinds, gzipped=True)
        return self._iter_file(path, kinds, gzipped=False)

    def _iter_file(
        self, path: Path, kinds: set[EventKind] | None, *, gzipped: bool
    ) -> Iterator[JournalEvent]:
        opener = gzip.open if gzipped else open
        with opener(path, "rt", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = event_from_dict(json.loads(line))
                except (json.JSONDecodeError, ValueError, KeyError) as e:
                    logger.warning("Skipping corrupt journal line in %s: %s", path.name, e)
                    continue
                if kinds is not None and event.kind not in kinds:
                    continue
                yield event

    def get_trade(self, trade_id: str) -> TradeRecord | None:
        """Лінійний скан сьогоднішнього + вчорашнього файлу. Повертає None якщо нема."""
        events: list[JournalEvent] = []
        # Скануємо кілька найсвіжіших днів — більшість угод закриваються в межах доби-двох.
        # Можна розширити цикл якщо потрібно, але це slow path для post-mortem.
        candidates = sorted(self._config.journal_dir.glob("*.jsonl"), reverse=True)[:7]
        candidates += sorted(self._config.journal_dir.glob("*.jsonl.gz"), reverse=True)[:7]
        seen_dates: set[str] = set()
        for path in candidates:
            date_str = path.name.split(".")[0]
            if date_str in seen_dates:
                continue
            seen_dates.add(date_str)
            for ev in self.iter_events(date_str):
                if ev.trade_id == trade_id:
                    events.append(ev)
        if not events:
            return None
        events.sort(key=lambda e: e.seq)
        return _build_trade_record(trade_id, events)

    def iter_closed_trades(self, date_utc: str) -> Iterator[TradeRecord]:
        """Усі угоди, які закрилися (мають TRADE_OUTCOME) у вказану дату."""
        outcomes = [e for e in self.iter_events(date_utc, kinds={EventKind.TRADE_OUTCOME})]
        for outcome in outcomes:
            if outcome.trade_id is None:
                continue
            record = self.get_trade(outcome.trade_id)
            if record is not None:
                yield record


def _build_trade_record(trade_id: str, events: list[JournalEvent]) -> TradeRecord:
    """Зводимо ланцюжок подій у TradeRecord. Деталі беремо з найсемантичнішої події."""
    symbol = next((e.symbol for e in events if e.symbol is not None), "")
    opened = next(
        (e.timestamp_ms for e in events if e.kind == EventKind.POSITION_OPENED), None
    )
    closed = next(
        (e.timestamp_ms for e in events if e.kind == EventKind.POSITION_CLOSED), None
    )
    decision = next(
        (e for e in events if e.kind == EventKind.DECISION_ACCEPTED), None
    )
    outcome = next((e for e in events if e.kind == EventKind.TRADE_OUTCOME), None)

    setup_type = decision.payload.get("setup_type") if decision else None
    direction = decision.payload.get("direction") if decision else None
    realized_r = outcome.payload.get("realized_r") if outcome else None

    return TradeRecord(
        trade_id=trade_id,
        symbol=symbol,
        opened_at_ms=opened,
        closed_at_ms=closed,
        setup_type=str(setup_type) if setup_type is not None else None,
        direction=str(direction) if direction is not None else None,
        realized_r=float(realized_r) if realized_r is not None else None,
        events=events,
    )
