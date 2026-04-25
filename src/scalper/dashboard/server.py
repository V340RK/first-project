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

from dataclasses import asdict

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from scalper.common import time as _time
from scalper.dashboard.account import BinanceAccountService
from scalper.dashboard.book_snapshot import BookSnapshotService
from scalper.dashboard.config import DashboardConfig
from scalper.dashboard.controller import BotRegistry, BotRunParams
from scalper.dashboard.stats import SessionStats
from scalper.dashboard.symbols import BinanceSymbolService
from scalper.dashboard.tailer import JournalTailer

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class StartBotRequest(BaseModel):
    """Запуск окремого бота на ОДНУ пару. Кілька пар = кілька таких запитів.

    Sizing: вкажи ОДИН з двох — `risk_per_trade_usd` (R-based) АБО
    `margin_per_trade_pct` (% від balance як margin для позиції).
    Якщо обидва — пріоритет у margin_per_trade_pct.
    """
    symbol: str = Field(min_length=1)
    leverage: int = Field(ge=1, le=125)
    risk_per_trade_usd: float = Field(default=0, ge=0)
    margin_per_trade_pct: float | None = Field(default=None, ge=0.01, le=100)
    max_book_consumption_pct: float | None = Field(default=None, gt=0, le=100)
    max_expected_slippage_ticks: int | None = Field(default=None, ge=1)
    stop_loss_pct: float | None = Field(default=None, gt=0, le=50)
    mode: str = "live"
    score_threshold_override: float | None = None
    # equity_usd НЕ приймається ззовні — береться з реального balance API


class StopBotRequest(BaseModel):
    symbol: str = Field(min_length=1)


class DashboardServer:
    """FastAPI app + JournalTailer + BotRegistry (per-symbol процеси)."""

    def __init__(
        self,
        config: DashboardConfig,
        tailer: JournalTailer | None = None,
        registry: BotRegistry | None = None,
        symbol_service: BinanceSymbolService | None = None,
        account_service: BinanceAccountService | None = None,
        book_service: BookSnapshotService | None = None,
    ) -> None:
        self._config = config
        self._tailer = tailer if tailer is not None else JournalTailer(
            journal_dir=config.journal_dir,
            poll_interval_ms=config.poll_interval_ms,
        )
        self._registry = registry
        self._stats = SessionStats(self._tailer)
        self._symbol_service = symbol_service
        self._account_service = account_service
        self._book_service = book_service
        self._connected_clients: int = 0

    @property
    def tailer(self) -> JournalTailer:
        return self._tailer

    def build_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
            await self._tailer.start()
            await self._stats.start()
            logger.info("Dashboard tailer started; watching %s", self._config.journal_dir)
            try:
                yield
            finally:
                await self._stats.stop()
                await self._tailer.stop()
                if self._registry is not None:
                    logger.info("Shutting down all managed bot subprocesses")
                    self._registry.stop_all()
                logger.info("Dashboard tailer stopped")

        app = FastAPI(title="Scalper Dashboard", lifespan=lifespan)

        # Статика та головна
        if STATIC_DIR.exists():
            app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/app")
        async def trader_app() -> FileResponse:
            """Trader control panel (відповідає мокапу V340RK)."""
            return FileResponse(STATIC_DIR / "trader.html")

        @app.get("/api/status")
        async def status() -> JSONResponse:
            return JSONResponse({
                "journal_dir": str(self._config.journal_dir),
                "poll_interval_ms": self._config.poll_interval_ms,
                "connected_clients": self._connected_clients,
                "tailer_subscribers": self._tailer.subscriber_count,
                "server_time_ms": _time.clock(),
            })

        @app.get("/api/bot/status")
        async def bot_status() -> JSONResponse:
            """Знімок усіх відомих слотів: bot status + session per symbol."""
            session_snaps = self._stats.snapshot_all()
            bot_statuses = (
                self._registry.all_statuses() if self._registry is not None else {}
            )

            slots: dict[str, dict] = {}
            all_symbols = set(session_snaps.keys()) | set(bot_statuses.keys())
            for sym in all_symbols:
                bot = bot_statuses.get(sym)
                sess = session_snaps.get(sym)
                slots[sym] = {
                    "bot": (
                        asdict(bot) if bot else
                        {"running": False, "pid": None, "started_at_ms": None,
                         "params": None, "exit_code": None}
                    ),
                    "session": asdict(sess) if sess else None,
                }
            return JSONResponse({"slots": slots})

        @app.get("/api/account/balance")
        async def account_balance() -> JSONResponse:
            if self._account_service is None:
                raise HTTPException(503, "account service not configured")
            try:
                bal = await self._account_service.get_balance()
            except Exception as e:
                raise HTTPException(502, f"Binance account API error: {e}") from e
            return JSONResponse({
                "wallet_balance": bal.wallet_balance,
                "available_balance": bal.available_balance,
                "margin_balance": bal.margin_balance,
                "total_unrealized_pnl": bal.total_unrealized_pnl,
                "quote_asset": bal.quote_asset,
                "fetched_at_ms": bal.fetched_at_ms,
            })

        @app.get("/api/orderbook/{symbol}")
        async def orderbook(symbol: str, depth: int = 10) -> JSONResponse:
            if self._book_service is None:
                raise HTTPException(503, "book service not configured")
            try:
                snap = await self._book_service.get(symbol, depth=depth)
            except Exception as e:
                raise HTTPException(502, f"depth fetch failed: {e}") from e
            return JSONResponse({
                "symbol": snap.symbol,
                "bids": [[lvl.price, lvl.size] for lvl in snap.bids],
                "asks": [[lvl.price, lvl.size] for lvl in snap.asks],
                "fetched_at_ms": snap.fetched_at_ms,
                "last_update_id": snap.last_update_id,
            })

        @app.get("/api/symbols")
        async def list_symbols() -> JSONResponse:
            if self._symbol_service is None:
                raise HTTPException(503, "symbol service not configured")
            try:
                syms = await self._symbol_service.list_symbols()
            except Exception as e:
                raise HTTPException(502, f"exchangeInfo unreachable: {e}") from e
            return JSONResponse([
                {"symbol": s.symbol, "base": s.base, "quote": s.quote,
                 "tick_size": s.tick_size, "step_size": s.step_size}
                for s in syms
            ])

        @app.post("/api/bot/start")
        async def bot_start(req: StartBotRequest) -> JSONResponse:
            if self._registry is None:
                raise HTTPException(503, "bot registry not configured")

            sym = req.symbol.upper()
            if self._registry.status(sym).running:
                raise HTTPException(409, f"бот для {sym} вже запущений")

            if self._symbol_service is not None:
                try:
                    valid = await self._symbol_service.is_valid(sym)
                except Exception as e:
                    raise HTTPException(502, f"cannot validate symbol: {e}") from e
                if not valid:
                    raise HTTPException(
                        422, f"невідома пара на Binance: {sym}",
                    )

            # Equity тягнемо з реального balance API (не довіряємо UI input).
            # Якщо account service не доступний — fallback default 100, але це
            # сигнал помилки для оператора.
            if self._account_service is not None:
                try:
                    bal = await self._account_service.get_balance()
                    equity_usd = bal.available_balance
                except Exception as e:
                    raise HTTPException(
                        502, f"не можу отримати баланс акаунту: {e}",
                    ) from e
                if equity_usd <= 0:
                    raise HTTPException(
                        409, f"баланс акаунту = {equity_usd}; пополни перед стартом",
                    )
            else:
                equity_usd = 100.0   # only for tests без account_service

            # На testnet ринок постійно у LOW_LIQ → DecisionEngine блокує всі
            # setup-и за дефолтом. Знімаємо цей блок для testnet, щоб бот міг
            # реально торгувати тестовими грошима. На проді (testnet=False)
            # сувора фільтрація залишається.
            import os as _os
            testnet = _os.environ.get("BINANCE_TESTNET", "true").strip().lower() in ("1", "true", "yes", "on")

            # Auto-low threshold для testnet: scores у тонкому ринку 0.2-0.4,
            # default 1.0 завжди rejected. Користувач може override з UI пізніше.
            score_thr = req.score_threshold_override
            if score_thr is None and testnet:
                score_thr = 0.25

            sizing_mode = "margin_pct" if req.margin_per_trade_pct else "risk_usd"
            if sizing_mode == "risk_usd" and req.risk_per_trade_usd <= 0:
                raise HTTPException(
                    422, "потрібно вказати або risk_per_trade_usd>0, або margin_per_trade_pct>0",
                )

            params = BotRunParams(
                symbol=sym, leverage=req.leverage,
                risk_per_trade_usd=req.risk_per_trade_usd,
                margin_per_trade_pct=req.margin_per_trade_pct,
                sizing_mode=sizing_mode,
                max_book_consumption_pct=req.max_book_consumption_pct,
                max_expected_slippage_ticks=req.max_expected_slippage_ticks,
                stop_loss_pct=req.stop_loss_pct,
                equity_usd=equity_usd, mode=req.mode,
                score_threshold_override=score_thr,
                relaxed_regime=testnet,
            )
            self._stats.reset(sym)
            status = self._registry.start(params)
            return JSONResponse(asdict(status))

        @app.post("/api/bot/stop")
        async def bot_stop(req: StopBotRequest) -> JSONResponse:
            if self._registry is None:
                raise HTTPException(503, "bot registry not configured")
            status = self._registry.stop(req.symbol)
            return JSONResponse(asdict(status))

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


def create_app(
    config: DashboardConfig,
    registry: BotRegistry | None = None,
    symbol_service: BinanceSymbolService | None = None,
    account_service: BinanceAccountService | None = None,
    book_service: BookSnapshotService | None = None,
) -> FastAPI:
    """Фабрика для uvicorn. Якщо registry=None — UI буде read-only."""
    server = DashboardServer(
        config, registry=registry, symbol_service=symbol_service,
        account_service=account_service, book_service=book_service,
    )
    return server.build_app()


__all__ = ["DashboardServer", "create_app", "STATIC_DIR"]
