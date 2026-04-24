"""NotificationService — fire-and-forget сповіщення з rate-limit + async queue.

MVP: console + опційний Telegram через HTTP POST. Black-box rate-limit per minute.
Помилка доставки → лог + drop (не падаємо). Hot loop не блокується: всі send-и
кладуться в queue, окремий consumer task відправляє.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

from scalper.common import time as _time
from scalper.common.enums import AlertLevel
from scalper.notifications.config import NotificationConfig

logger = logging.getLogger(__name__)

_LEVEL_ORDER = {"info": 0, "warning": 1, "error": 2, "critical": 3}


@dataclass(frozen=True)
class _Message:
    text: str
    level: AlertLevel
    ts_ms: int


class NotificationService:
    def __init__(self, config: NotificationConfig) -> None:
        self._config = config
        self._queue: asyncio.Queue[_Message] | None = None
        self._task: asyncio.Task[None] | None = None
        self._sent_timestamps: deque[int] = deque(maxlen=config.rate_limit_per_minute * 2)
        self._http_session: Any = None
        self._stopping = False

    async def start(self) -> None:
        if not self._config.enabled:
            return
        self._queue = asyncio.Queue(maxsize=self._config.queue_size)
        self._stopping = False
        if self._config.telegram_bot_token:
            try:
                import aiohttp
                self._http_session = aiohttp.ClientSession()
            except Exception as e:
                logger.warning("notification: failed to init aiohttp session: %s", e)
        self._task = asyncio.create_task(self._consumer(), name="notification-consumer")

    async def stop(self) -> None:
        self._stopping = True
        if self._queue is not None:
            await self._queue.put(_Message(text="__shutdown__", level=AlertLevel.INFO, ts_ms=0))
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    async def send(self, text: str, level: AlertLevel) -> None:
        if not self._config.enabled or self._queue is None:
            return
        if _LEVEL_ORDER[level.value] < _LEVEL_ORDER[self._config.min_level]:
            return
        msg = _Message(text=text, level=level, ts_ms=_time.clock())
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning("notification queue full, dropping: %s", text[:80])

    # === Internals ===

    async def _consumer(self) -> None:
        assert self._queue is not None
        while True:
            msg = await self._queue.get()
            if msg.text == "__shutdown__":
                return
            if not self._allow_by_rate_limit(msg.ts_ms):
                continue
            if self._config.console:
                self._emit_console(msg)
            if self._config.telegram_bot_token and self._http_session is not None:
                await self._emit_telegram(msg)

    def _allow_by_rate_limit(self, ts_ms: int) -> bool:
        cutoff = ts_ms - 60_000
        while self._sent_timestamps and self._sent_timestamps[0] < cutoff:
            self._sent_timestamps.popleft()
        if len(self._sent_timestamps) >= self._config.rate_limit_per_minute:
            return False
        self._sent_timestamps.append(ts_ms)
        return True

    @staticmethod
    def _emit_console(msg: _Message) -> None:
        level_map = {
            AlertLevel.INFO: logger.info,
            AlertLevel.WARNING: logger.warning,
            AlertLevel.ERROR: logger.error,
            AlertLevel.CRITICAL: logger.critical,
        }
        level_map[msg.level]("[NOTIFY/%s] %s", msg.level.value.upper(), msg.text)

    async def _emit_telegram(self, msg: _Message) -> None:
        assert self._http_session is not None
        token = self._config.telegram_bot_token
        chat_id = self._config.telegram_chat_id
        if not chat_id:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        prefix = {"info": "", "warning": "⚠️ ", "error": "❌ ", "critical": "🚨 "}
        payload = {"chat_id": chat_id, "text": f"{prefix[msg.level.value]}{msg.text}"}
        try:
            async with self._http_session.post(url, json=payload, timeout=5) as resp:
                if resp.status != 200:
                    logger.warning("telegram send failed status=%s", resp.status)
        except Exception as e:
            logger.warning("telegram send exception: %s", e)


__all__ = ["NotificationConfig", "NotificationService"]
