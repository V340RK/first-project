---
name: 01-market-data-gateway
description: Архітектура модуля Market Data Gateway — WS підписки, REST snapshot/warmup, час синхронізації; видає сирі події в OrderBook та Tape модулі
type: project
---

# 01. MarketDataGateway — інгейст ринкових даних

## Відповідальність

**Тонкий шар над біржею для market-data**. Підписується на WebSocket-стріми, отримує REST snapshots для warmup та реініціалізації OrderBook. Виштовхує сирі типізовані події у нижні модулі (OrderBookEngine, TapeAnalyzer).

> Trading-частина REST API (place_order, cancel_order, set_leverage, get_account) живе у [09-execution-engine.md](09-execution-engine.md) — Gateway її НЕ робить. Спільний транспортний код (HMAC-підпис, rate-limit, time sync) винесено в `_RestTransport` (внутрішній утиліт-клас).

### Що робить:
- Тримає WebSocket з'єднання (combined stream) до публічних потоків
- Auto-reconnect з exponential backoff
- Перевіряє sequence для depth diff (виявляє gap → форсує reinit)
- REST snapshot для OrderBook init (`/depth?limit=1000`)
- REST kline warmup при старті (`/klines`)
- Слідкує за rate-limit заголовками (`X-MBX-USED-WEIGHT-1M`)
- Синхронізує локальний час з `serverTime` раз на хвилину
- Підписується на User Data Stream (для Execution/PositionManager — філи ордерів)

### Що НЕ робить:
- НЕ парсить семантику (це OrderBookEngine/TapeAnalyzer)
- НЕ розраховує imbalance, delta, CVD — це нижче по pipeline
- НЕ виставляє ордери (це Execution Engine)
- НЕ зберігає історію — лише публікує події у моменті

---

## Входи / виходи

### Публічний API:

```python
class MarketDataGateway:
    def __init__(self, config: GatewayConfig, transport: _RestTransport):
        ...

    # === Lifecycle ===
    async def start(self, symbols: list[str]) -> None
    async def stop(self) -> None

    # === Підписки (callback-based) ===
    def on_agg_trade(self, callback: Callable[[RawAggTrade], Awaitable[None]]) -> None
    def on_depth_diff(self, callback: Callable[[RawDepthDiff], Awaitable[None]]) -> None
    def on_kline_close(self, callback: Callable[[RawKline], Awaitable[None]]) -> None
    def on_book_ticker(self, callback: Callable[[RawBookTicker], Awaitable[None]]) -> None
    def on_user_event(self, callback: Callable[[RawUserEvent], Awaitable[None]]) -> None

    # === REST для warmup / reinit (тільки market-data) ===
    async def fetch_depth_snapshot(self, symbol: str, limit: int = 1000) -> DepthSnapshot
    async def fetch_klines(self, symbol: str, interval: str, limit: int) -> list[RawKline]
    async def fetch_exchange_info(self) -> ExchangeInfo

    # === Інфраструктура ===
    def get_server_time_offset_ms(self) -> int
    def get_rate_limit_weight(self) -> int
    async def ping(self) -> int  # latency мс
```

### Сирі payload-типи (нормалізовані Binance структури):

```python
@dataclass(frozen=True)
class RawAggTrade:
    timestamp_ms: int
    symbol: str
    price: float
    quantity: float
    is_buyer_maker: bool          # True → ринок продав; False → ринок купив
    agg_id: int                   # для виявлення gap

@dataclass(frozen=True)
class RawDepthDiff:
    symbol: str
    first_update_id: int          # 'U' з Binance
    final_update_id: int          # 'u'
    bids: list[tuple[float, float]]  # (price, qty)
    asks: list[tuple[float, float]]

@dataclass(frozen=True)
class RawKline:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool

@dataclass(frozen=True)
class RawBookTicker:
    symbol: str
    timestamp_ms: int
    best_bid: float
    best_bid_qty: float
    best_ask: float
    best_ask_qty: float

@dataclass(frozen=True)
class RawUserEvent:
    event_type: Literal['ORDER_TRADE_UPDATE', 'ACCOUNT_UPDATE', 'MARGIN_CALL', 'listenKeyExpired']
    timestamp_ms: int
    payload: dict

@dataclass(frozen=True)
class SymbolFilters:
    """Нормалізовані Binance filters для одного символу.
    Source of truth для тик-прайсу, лот-розміру, мін. нотіонала —
    RiskEngine і ExecutionEngine читають ЗВІДСИ, не з конфігу."""
    tick_size: float                  # PRICE_FILTER.tickSize — крок ціни
    step_size: float                  # LOT_SIZE.stepSize — крок кількості
    min_qty: float                    # LOT_SIZE.minQty
    max_qty: float                    # LOT_SIZE.maxQty
    min_notional: float               # MIN_NOTIONAL.notional — мін. обʼєм угоди в USDT
    price_precision: int              # к-ть знаків після коми для ціни
    qty_precision: int                # к-ть знаків після коми для qty

@dataclass(frozen=True)
class ExchangeInfo:
    """Відповідь GET /fapi/v1/exchangeInfo, нормалізована у per-symbol мапу.
    Gateway кешує на час роботи, оновлює при старті й по потребі (раз на день або
    при помилці -1121 'Invalid symbol')."""
    server_time_ms: int
    fetched_at_ms: int
    symbols: dict[str, SymbolFilters]  # {'BTCUSDT': SymbolFilters(...), ...}
    # Crucial для rate-limit heuristics:
    rate_limits: dict[str, int]        # {'REQUEST_WEIGHT_1M': 2400, 'ORDERS_10S': 300, ...}
```

**Важливо:** `SymbolFilters` — канонічне джерело per-symbol округлення. Конфіги RiskEngine
містять лише `fallback_*` значення (для тестів або при холодному старті до першого fetch).
Жодна бізнес-логіка не має округляти ціну/кількість, не звірившись із `SymbolFilters` цього символу.

---

## Залежності

**Читає:**
- `ConfigManager` → `api_key`, `secret_key`, `base_url`, `ws_url`, `testnet`
- (опційно) `HealthMonitor` для heartbeat метрик

**Пише (через callback'и):**
- OrderBookEngine → `on_depth_diff`
- TapeAnalyzer → `on_agg_trade`, `on_book_ticker`
- KlineCache (опційно) → `on_kline_close`
- Execution / PositionManager → `on_user_event` (філи ордерів)
- NotificationService — критичні події (418, ws silence > 30s, time drift > 1s)
- JournalLogger — audit trail REST викликів

**НЕ залежить від:**
- FeatureEngine, SetupDetector, Decision, Risk, Execution
- Будь-якого торгового стану

---

## Стан

```python
@dataclass
class GatewayState:
    ws_connections: dict[str, WSConnection]  # stream_name → WS
    reconnect_attempts: dict[str, int]
    last_message_ms: dict[str, int]          # для silence detection
    rate_limit_weight: int                   # X-MBX-USED-WEIGHT-1M
    rate_limit_reset_time: float
    server_time_offset_ms: int               # serverTime − localTime
    listen_key: str | None                   # для user stream
    listen_key_renewed_at: float
    ban_until: float | None                  # якщо отримали 418
```

Жодного persistence — рестарт = свіжий стан, переконнект, новий listenKey, повний resync OrderBook.

---

## Ключові алгоритми

### 1. Combined WebSocket stream

Замість N окремих з'єднань — одне:
```
wss://fstream.binance.com/stream?streams=
    btcusdt@aggTrade/btcusdt@depth@100ms/btcusdt@kline_1m/btcusdt@bookTicker
```

Диспетчер за полем `stream` маршрутизує повідомлення відповідним callback-ам:

```python
async def _ws_dispatch_loop(self, streams: list[str]):
    delay = 1
    url = f"{self.ws_url}/stream?streams={'/'.join(streams)}"
    while not self._shutdown:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                self._ws = ws
                delay = 1
                async for msg in ws:
                    parsed = json.loads(msg)
                    stream = parsed.get('stream', '')
                    data = parsed.get('data', {})
                    self.last_message_ms[stream] = self._now_ms()
                    await self._route(stream, data)
        except (ConnectionClosed, WebSocketException) as e:
            logger.warning(f"WS disconnected: {e}")
            await self.notifier.send(f"WS reconnecting (delay={delay}s)", AlertLevel.WARNING)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

async def _route(self, stream: str, data: dict):
    if '@aggTrade' in stream:
        await self._fire(self._agg_trade_cb, _parse_agg_trade(data))
    elif '@depth' in stream:
        await self._fire(self._depth_diff_cb, _parse_depth_diff(data))
    elif '@kline' in stream:
        kline = _parse_kline(data)
        if kline.is_closed:
            await self._fire(self._kline_cb, kline)
    elif '@bookTicker' in stream:
        await self._fire(self._book_ticker_cb, _parse_book_ticker(data))
```

### 2. Depth sequence validation

Gateway лише ПРОКИДАЄ diff — перевірку sequence робить OrderBookEngine. Але Gateway допомагає: показує `first_update_id` / `final_update_id` у `RawDepthDiff` (з полів `U`/`u` Binance).

### 3. REST snapshot для OrderBook reinit

```python
async def fetch_depth_snapshot(self, symbol: str, limit: int = 1000) -> DepthSnapshot:
    data = await self.transport.public_get('/fapi/v1/depth',
                                           params={'symbol': symbol, 'limit': limit},
                                           weight=20)
    return DepthSnapshot(
        last_update_id=data['lastUpdateId'],
        bids=[(float(p), float(q)) for p, q in data['bids']],
        asks=[(float(p), float(q)) for p, q in data['asks']],
    )
```

### 4. Time sync loop (slow loop)

```python
async def _sync_time_loop(self):
    while not self._shutdown:
        try:
            data = await self.transport.public_get('/fapi/v1/time', weight=1)
            offset = data['serverTime'] - self._now_ms()
            self.server_time_offset_ms = offset
            if abs(offset) > 1000:
                logger.warning(f"Clock drift: {offset}ms")
                await self.notifier.send(f"Clock drift {offset}ms", AlertLevel.WARNING)
        except Exception as e:
            logger.error(f"time sync failed: {e}")
        await asyncio.sleep(60)
```

### 5. User data stream + listenKey renewal

```python
async def _user_stream_loop(self):
    while not self._shutdown:
        self.listen_key = await self.transport.private_post('/fapi/v1/listenKey', weight=1)
        url = f"{self.ws_url}/ws/{self.listen_key}"
        async with websockets.connect(url) as ws:
            self._renewal_task = asyncio.create_task(self._renew_listen_key_loop())
            try:
                async for msg in ws:
                    event = _parse_user_event(json.loads(msg))
                    if event.event_type == 'listenKeyExpired':
                        break  # внутрішній цикл while перевідкриє
                    await self._fire(self._user_event_cb, event)
            finally:
                self._renewal_task.cancel()

async def _renew_listen_key_loop(self):
    while True:
        await asyncio.sleep(30 * 60)  # кожні 30 хв
        await self.transport.private_put('/fapi/v1/listenKey', weight=1)
```

### 6. Silence watchdog

```python
async def _silence_watchdog(self):
    threshold_ms = self.config.silence_alert_threshold * 1000
    while not self._shutdown:
        now = self._now_ms()
        for stream, last in self.last_message_ms.items():
            if now - last > threshold_ms:
                await self.notifier.send(
                    f"WS silence on {stream}: {now - last}ms",
                    AlertLevel.ERROR
                )
        await asyncio.sleep(5)
```

### 7. `_RestTransport` (спільний з Execution)

```python
class _RestTransport:
    """Спільна інфраструктура для всіх REST викликів — і market-data (Gateway),
    і trading (Execution). Тримає сесію, підписує приватні запити, трекає rate-limit."""

    async def public_get(self, endpoint: str, params: dict | None = None, weight: int = 1) -> dict:
        await self._check_rate_limit(weight)
        ...

    async def private_get(self, endpoint, params=None, weight=1) -> dict:
        params = self._sign(params or {})
        ...

    async def private_post(self, endpoint, params=None, weight=1) -> dict: ...
    async def private_put(self, endpoint, params=None, weight=1) -> dict: ...
    async def private_delete(self, endpoint, params=None, weight=1) -> dict: ...

    def _sign(self, params: dict) -> dict:
        params = {**params, 'timestamp': self._now_ms() + self._time_offset}
        query = urlencode(params)
        sig = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return {**params, 'signature': sig}

    async def _check_rate_limit(self, incoming_weight: int): ...
    def _track_rate_limit(self, headers: dict): ...
```

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| WebSocket розрив | Exponential backoff 1s → 60s, алерт через NotificationService |
| Silence > 30s на стрімі | ERROR алерт + спроба переконнекту |
| 418 (IP ban) | Пауза всіх REST на час `Retry-After` (мін 60s), CRITICAL алерт |
| 429 (rate limit) | Чекати до скидання вікна `X-MBX-USED-WEIGHT-1M` |
| -1021 (timestamp out of recvWindow) | Форсована time sync + retry один раз |
| Depth diff із U > prev_u + 1 | Gateway просто прокидає; OrderBookEngine ловить gap і викликає `fetch_depth_snapshot` |
| listenKey expired | Перестворити (POST /listenKey) і переконнектитись на user stream |
| WS повертає текстове повідомлення замість JSON | Лог + skip |
| `agg_id` має розрив | Лог `WARN tape_gap`, прокидати далі — TapeAnalyzer виставить флаг "unreliable" на CVD на 5 хв |

---

## Конфіги

```yaml
gateway:
  exchange: binance_futures
  testnet: true
  base_url: https://testnet.binancefuture.com
  ws_url: wss://stream.binancefuture.com
  api_key: ${BINANCE_API_KEY}
  secret_key: ${BINANCE_SECRET}

  websocket:
    ping_interval: 20
    reconnect_delay_min: 1
    reconnect_delay_max: 60
    silence_alert_threshold: 30           # сек

  rest:
    timeout: 10
    max_retries: 3
    retry_delay: 1

  rate_limit:
    weight_threshold: 1920                # 80% від 2400
    block_when_above: true

  time_sync:
    interval_sec: 60
    drift_alert_ms: 1000

  user_stream:
    listen_key_renewal_min: 30            # хв
```

---

## Як тестувати

### Unit
- `_parse_agg_trade()` / `_parse_depth_diff()` / `_parse_kline()` на фікстурах із Binance документації
- `_RestTransport._sign()` — на офіційних test-векторах HMAC
- `_silence_watchdog()` — мокнути `last_message_ms` у минуле, перевірити алерт
- Reconnect — мок WebSocket який кидає `ConnectionClosed` після N повідомлень

### Integration (testnet)
- `start(['BTCUSDT'])` → перевірка що callback'и реально викликаються (≥1 trade за 10 сек)
- Симуляція мережевого розриву → ловимо reconnect, час до відновлення < 5 сек
- `fetch_depth_snapshot('BTCUSDT')` → перевірка що `lastUpdateId > 0`, bids/asks непорожні

### Manual
- tcpdump: трафік тільки на `binance.com` / `binancefuture.com`
- Метрика `rate_limit_weight` ніколи не перевищує 1920

### Health checks
- `ping()` < 500ms, інакше WARNING
- Reconnect counter за годину > 5 → ERROR алерт
