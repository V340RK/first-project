"""FastAPI сервер дашборда.

Endpoints:
  GET  /               → index.html
  GET  /api/status     → JSON знімок (конфіг, лічильники, список підписників)
  WS   /ws/events      → стрім подій: backfill + live

Сервер свідомо «тупий»: усе що бачить — це JSONL журнал через JournalTailer.
Це дозволяє запускати дашборд окремо від бота (наприклад, post-mortem).
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from scalper.common import time as _time
from scalper.dashboard.config import DashboardConfig
from scalper.dashboard.tailer import JournalTailer

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class DashboardServer:
    """Тримає FastAPI app + фоновий JournalTailer. Запускається через uvicorn."""

    def __init__(self, config: DashboardConfig, tailer: JournalTailer | None = None) -> None:
        self._config = config
        self._tailer = tailer if tailer is not None else JournalTailer(
            journal_dir=config.journal_dir,
            poll_interval_ms=config.poll_interval_ms,
        )
        self._connected_clients: int = 0

    @property
    def tailer(self) -> JournalTailer:
        return self._tailer

    def build_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
            await self._tailer.start()
            logger.info("Dashboard tailer started; watching %s", self._config.journal_dir)
            try:
                yield
            finally:
                await self._tailer.stop()
                logger.info("Dashboard tailer stopped")

        app = FastAPI(title="Scalper Dashboard", lifespan=lifespan)

        # Статика та головна
        if STATIC_DIR.exists():
            app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/api/status")
        async def status() -> JSONResponse:
            return JSONResponse({
                "journal_dir": str(self._config.journal_dir),
                "poll_interval_ms": self._config.poll_interval_ms,
                "connected_clients": self._connected_clients,
                "tailer_subscribers": self._tailer.subscriber_count,
                "server_time_ms": _time.clock(),
            })

        @app.websocket("/ws/events")
        async def ws_events(ws: WebSocket) -> None:
            await ws.accept()
            self._connected_clients += 1
            logger.info("WS client connected (total=%d)", self._connected_clients)

            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)

            async def on_event(event: dict[str, Any]) -> None:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Клієнт повільний — дропаємо, логіку drop рахує окремо дашборд,
                    # бо в журналі drop-ів не буде.
                    logger.warning("WS queue full, dropping event for slow client")

            unsubscribe = self._tailer.subscribe(on_event)

            try:
                # 1) Backfill — віддаємо останні N подій одним frame-ом.
                backfill = self._tailer.read_recent(limit=self._config.backfill_lines)
                await ws.send_text(json.dumps({"type": "backfill", "events": backfill}))

                # 2) Live — паралельно стрімимо нові + слухаємо ping/close від клієнта.
                recv_task = asyncio.create_task(self._drain_recv(ws), name="ws.recv")
                send_task = asyncio.create_task(self._drain_send(ws, queue), name="ws.send")
                done, pending = await asyncio.wait(
                    {recv_task, send_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    exc = t.exception()
                    if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                        logger.warning("WS task ended with %s", exc)
            finally:
                unsubscribe()
                self._connected_clients -= 1
                logger.info("WS client disconnected (total=%d)", self._connected_clients)

        return app

    @staticmethod
    async def _drain_recv(ws: WebSocket) -> None:
        """Читаємо пінги/pong від клієнта — коли він закриває з'єднання, буде WebSocketDisconnect."""
        while True:
            await ws.receive_text()

    @staticmethod
    async def _drain_send(ws: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
        while True:
            event = await queue.get()
            await ws.send_text(json.dumps({"type": "event", "event": event}))


def create_app(config: DashboardConfig) -> FastAPI:
    """Фабрика для uvicorn (--factory)."""
    server = DashboardServer(config)
    return server.build_app()


__all__ = ["DashboardServer", "create_app", "STATIC_DIR"]
