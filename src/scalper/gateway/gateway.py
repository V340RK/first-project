"""MarketDataGateway — оркестрація WebSocket + REST до Binance USDT-M Futures.

Алгоритми та поведінка деталізовані у DOCS/architecture/01-market-data-gateway.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from scalper.common.enums import AlertLevel
from scalper.common.time import clock
from scalper.gateway.config import GatewayConfig
from scalper.gateway.parsers import (
    parse_agg_trade,
    parse_book_ticker,
    parse_depth_diff,
    parse_depth_snapshot,
    parse_exchange_info,
    parse_kline,
    parse_kline_rest,
    parse_user_event,
)
from scalper.gateway.transport import _RestTransport
from scalper.gateway.types import (
    DepthSnapshot,
    ExchangeInfo,
    RawAggTrade,
    RawBookTicker,
    RawDepthDiff,
    RawKline,
    RawUserEvent,
    SymbolFilters,
)
from scalper.notifications.service import NotificationService

logger = logging.getLogger(__name__)

AggTradeCallback = Callable[[RawAggTrade], Awaitable[None]]
DepthDiffCallback = Callable[[RawDepthDiff], Awaitable[None]]
KlineCallback = Callable[[RawKline], Awaitable[None]]
BookTickerCallback = Callable[[RawBookTicker], Awaitable[None]]
UserEventCallback = Callable[[RawUserEvent], Awaitable[None]]


class MarketDataGateway:
    """Тонкий шар над біржею для market-data + user stream.

    Один екземпляр на процес. Тримає всі WS-зʼєднання, маршрутизує події по callback-ам,
    кешує ExchangeInfo, синхронізує час з біржею.
    """

    def __init__(
        self,
        config: GatewayConfig,
        notifier: NotificationService,
        transport: _RestTransport | None = None,
    ) -> None:
        self._config = config
        self._notifier = notifier
        self._transport = transport or _RestTransport(config)

        # Callback-и: один споживач на тип події. Якщо нема — подія тихо скіпається.
        self._cb_agg_trade: AggTradeCallback | None = None
        self._cb_depth_diff: DepthDiffCallback | None = None
        self._cb_kline: KlineCallback | None = None
        self._cb_book_ticker: BookTickerCallback | None = None
        self._cb_user_event: UserEventCallback | None = None

        # Стан runtime — НЕ персистимо (рестарт = свіжий стан).
        self._symbols: list[str] = []
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._last_message_ms: dict[str, int] = {}      # stream_name → ts
        self._listen_key: str | None = None
        self._exchange_info: ExchangeInfo | None = None

    # === Lifecycle ===

    async def start(self, symbols: list[str]) -> None:
        """Підняти REST-сесію, fetch ExchangeInfo, відкрити WS і запустити фонові цикли."""
        if self._tasks:
            raise RuntimeError("Gateway вже запущений")
        self._symbols = [s.upper() for s in symbols]
        self._shutdown.clear()
        await self._transport.start()

        # Перший exchangeInfo — щоб RiskEngine/Execution мали SymbolFilters одразу.
        await self._refresh_exchange_info()
        # Початковий time sync (важливо ДО першого приватного запиту).
        await self._sync_time_once()

        # Запускаємо фонові цикли. Послідовність важлива: time_sync до user_stream.
        self._tasks = [
            asyncio.create_task(self._market_ws_loop(), name="gateway.market_ws"),
            asyncio.create_task(self._sync_time_loop(), name="gateway.time_sync"),
            asyncio.create_task(self._silence_watchdog(), name="gateway.silence_watchdog"),
        ]
        if self._config.api_key is not None:
            self._tasks.append(asyncio.create_task(self._user_stream_loop(), name="gateway.user_stream"))

        await self._notifier.send(
            f"Gateway started: symbols={self._symbols}, testnet={self._config.testnet}",
            AlertLevel.INFO,
        )

    async def stop(self) -> None:
        self._shutdown.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        await self._transport.close()

    # === Підписки ===

    def on_agg_trade(self, callback: AggTradeCallback) -> None:
        self._cb_agg_trade = callback

    def on_depth_diff(self, callback: DepthDiffCallback) -> None:
        self._cb_depth_diff = callback

    def on_kline_close(self, callback: KlineCallback) -> None:
        self._cb_kline = callback

    def on_book_ticker(self, callback: BookTickerCallback) -> None:
        self._cb_book_ticker = callback

    def on_user_event(self, callback: UserEventCallback) -> None:
        self._cb_user_event = callback

    # === REST для warmup / reinit ===

    async def fetch_depth_snapshot(self, symbol: str, limit: int = 1000) -> DepthSnapshot:
        # Weight для /depth: 5/10/20/50 залежно від limit (limit=1000 → 20).
        weight = 20 if limit >= 1000 else (10 if limit >= 500 else 5)
        data = await self._transport.public_get(
            "/fapi/v1/depth",
            params={"symbol": symbol.upper(), "limit": limit},
            weight=weight,
        )
        return parse_depth_snapshot(symbol.upper(), data, received_at_ms=clock())

    async def fetch_klines(self, symbol: str, interval: str, limit: int) -> list[RawKline]:
        data = await self._transport.public_get(
            "/fapi/v1/klines",
            params={"symbol": symbol.upper(), "interval": interval, "limit": limit},
            weight=1 if limit <= 100 else (2 if limit <= 500 else 5),
        )
        return [parse_kline_rest(symbol.upper(), interval, row) for row in data]

    async def fetch_exchange_info(self) -> ExchangeInfo:
        """Forced refresh + повернення. Для звичайного читання — `get_symbol_filters()`."""
        return await self._refresh_exchange_info()

    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        if self._exchange_info is None:
            raise RuntimeError(
                "ExchangeInfo не завантажено — викликай fetch_exchange_info() або start() спочатку"
            )
        try:
            return self._exchange_info.symbols[symbol.upper()]
        except KeyError:
            raise RuntimeError(f"Невідомий символ: {symbol!r} (не в exchangeInfo)") from None

    # === Інфраструктура ===

    def get_server_time_offset_ms(self) -> int:
        return self._transport.get_time_offset_ms()

    def get_rate_limit_weight(self) -> int:
        return self._transport.get_used_weight()

    async def ping(self) -> int:
        start_ms = clock()
        await self._transport.public_get("/fapi/v1/ping", weight=1)
        return clock() - start_ms

    # === Internal: market WS ===

    def _build_market_streams(self) -> list[str]:
        """Combined stream URL для всіх символів і потрібних каналів."""
        streams: list[str] = []
        for s in self._symbols:
            sym = s.lower()
            streams.append(f"{sym}@aggTrade")
            streams.append(f"{sym}@depth@100ms")
            streams.append(f"{sym}@kline_1m")
            streams.append(f"{sym}@bookTicker")
        return streams

    async def _market_ws_loop(self) -> None:
        """Combined WS з auto-reconnect + exponential backoff."""
        streams = self._build_market_streams()
        url = f"{self._config.ws_url}/stream?streams={'/'.join(streams)}"
        delay = self._config.websocket.reconnect_delay_min
        max_delay = self._config.websocket.reconnect_delay_max

        while not self._shutdown.is_set():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=self._config.websocket.ping_interval,
                    close_timeout=5,
                ) as ws:
                    delay = self._config.websocket.reconnect_delay_min  # reset після успіху
                    async for message in ws:
                        if isinstance(message, bytes):
                            message = message.decode("utf-8", errors="replace")
                        try:
                            envelope = json.loads(message)
                        except json.JSONDecodeError:
                            logger.warning("WS non-JSON message: %r", message[:200])
                            continue
                        stream = envelope.get("stream", "")
                        data = envelope.get("data", {})
                        self._last_message_ms[stream] = clock()
                        await self._dispatch_market(stream, data)
            except (ConnectionClosed, WebSocketException, OSError) as e:
                logger.warning("Market WS disconnected: %s", e)
                await self._notifier.send(
                    f"Market WS reconnecting (delay={delay}s): {e}", AlertLevel.WARNING
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

    async def _dispatch_market(self, stream: str, data: dict[str, object]) -> None:
        """Маршрутизація однієї події в callback. Тип каналу визначаємо по підрядку."""
        try:
            if "@aggTrade" in stream:
                if self._cb_agg_trade:
                    await self._cb_agg_trade(parse_agg_trade(data))
            elif "@depth" in stream:
                if self._cb_depth_diff:
                    await self._cb_depth_diff(parse_depth_diff(data))
            elif "@kline" in stream:
                kline = parse_kline(data)
                if kline.is_closed and self._cb_kline:
                    await self._cb_kline(kline)
            elif "@bookTicker" in stream:
                if self._cb_book_ticker:
                    await self._cb_book_ticker(parse_book_ticker(data))
            else:
                logger.debug("Unknown stream: %s", stream)
        except Exception:
            # Якщо callback кинув — логуємо, але WS-loop НЕ падає (інакше пропустимо всі майбутні).
            logger.exception("Callback failed for stream %s", stream)

    # === Internal: time sync ===

    async def _sync_time_once(self) -> None:
        try:
            data = await self._transport.public_get("/fapi/v1/time", weight=1)
            offset = int(data["serverTime"]) - clock()
            self._transport.set_time_offset_ms(offset)
            if abs(offset) > self._config.time_sync.drift_alert_ms:
                logger.warning("Clock drift detected: %dms", offset)
                await self._notifier.send(
                    f"Clock drift {offset}ms — перевір системний час", AlertLevel.WARNING
                )
        except Exception:
            logger.exception("time_sync failed")

    async def _sync_time_loop(self) -> None:
        interval = self._config.time_sync.interval_sec
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                await self._sync_time_once()

    # === Internal: silence watchdog ===

    async def _silence_watchdog(self) -> None:
        threshold_ms = self._config.websocket.silence_alert_threshold * 1000
        check_interval_s = 5
        # Зберігаємо ts останнього алерту по стріму, щоб не спамити повторно.
        alerted_at: dict[str, int] = {}
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=check_interval_s)
                return
            except asyncio.TimeoutError:
                pass
            now = clock()
            for stream, last_ms in list(self._last_message_ms.items()):
                age = now - last_ms
                last_alert = alerted_at.get(stream, 0)
                # Алертуємо не частіше ніж раз на silence_alert_threshold.
                if age > threshold_ms and (now - last_alert) > threshold_ms:
                    await self._notifier.send(
                        f"WS silence on {stream}: {age}ms", AlertLevel.ERROR
                    )
                    alerted_at[stream] = now

    # === Internal: ExchangeInfo refresh ===

    async def _refresh_exchange_info(self) -> ExchangeInfo:
        data = await self._transport.public_get("/fapi/v1/exchangeInfo", weight=1)
        info = parse_exchange_info(data, fetched_at_ms=clock())
        self._exchange_info = info
        return info

    # === Internal: user stream + listenKey renewal ===

    async def _user_stream_loop(self) -> None:
        """User Data Stream: виставляємо listenKey, тримаємо WS, періодично pong-ом продовжуємо."""
        delay = self._config.websocket.reconnect_delay_min
        max_delay = self._config.websocket.reconnect_delay_max
        while not self._shutdown.is_set():
            try:
                listen_key = await self._create_listen_key()
                self._listen_key = listen_key
                url = f"{self._config.ws_url}/ws/{listen_key}"
                async with websockets.connect(
                    url,
                    ping_interval=self._config.websocket.ping_interval,
                    close_timeout=5,
                ) as ws:
                    delay = self._config.websocket.reconnect_delay_min
                    renewal = asyncio.create_task(self._renew_listen_key_loop())
                    try:
                        async for message in ws:
                            if isinstance(message, bytes):
                                message = message.decode("utf-8", errors="replace")
                            try:
                                event = parse_user_event(json.loads(message))
                            except json.JSONDecodeError:
                                logger.warning("User WS non-JSON: %r", message[:200])
                                continue
                            self._last_message_ms["user"] = clock()
                            if event.event_type == "listenKeyExpired":
                                logger.warning("listenKey expired — re-creating")
                                break
                            if self._cb_user_event:
                                try:
                                    await self._cb_user_event(event)
                                except Exception:
                                    logger.exception("user_event callback failed")
                    finally:
                        renewal.cancel()
                        try:
                            await renewal
                        except asyncio.CancelledError:
                            pass
            except (ConnectionClosed, WebSocketException, OSError) as e:
                logger.warning("User WS disconnected: %s", e)
                await self._notifier.send(
                    f"User WS reconnecting (delay={delay}s): {e}", AlertLevel.WARNING
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)

    async def _create_listen_key(self) -> str:
        data = await self._transport.private_post("/fapi/v1/listenKey", weight=1)
        return str(data["listenKey"])

    async def _renew_listen_key_loop(self) -> None:
        interval = self._config.user_stream.listen_key_renewal_min * 60
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._transport.private_put("/fapi/v1/listenKey", weight=1)
            except Exception:
                logger.exception("listenKey renewal failed")
