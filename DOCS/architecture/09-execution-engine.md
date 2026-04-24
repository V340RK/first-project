---
name: 09-execution-engine
description: Архітектура модуля Execution Engine — виконання OrderRequest на біржі з ідемпотентністю через client_order_id, обробка фільтрів і помилок, без знання сетапів
type: project
---

# 09. ExecutionEngine — виконання ордерів

## Відповідальність

**"Рука", що натискає кнопку купити/продати.** Знає тільки про `OrderRequest` і біржовий API. Не знає що таке "сетап", "R", "setup_type" — тільки `symbol`, `side`, `qty`, `price`, `type`, `client_order_id`.

Приймає команди від PositionManager (і в одиничному випадку — від RiskEngine через PositionManager). Виставляє ордери через `_RestTransport`, відстежує їх life cycle через User Data Stream, повертає результати.

### Що робить:
- Перекладає `OrderRequest` у Binance REST-виклик
- Генерує **ідемпотентний** `newClientOrderId` — можна retry'ити без ризику дубля
- Округлює `qty` і `price` до біржових фільтрів (`stepSize`, `tickSize`, `minNotional`)
- Виставляє та скасовує ордери (`placeOrder`, `cancelOrder`, `cancelAllOrders`)
- Модифікує ордери через cancel+replace (Binance futures не має `modifyOrder` для звичайних)
- Одночасно виставляє **OCO-подібний** набір (entry + SL + TP-ladder) через batch-ордер
- Обробляє код помилок:
  - `-2010 insufficient balance` → fatal, алерт RiskEngine
  - `-2011 unknown order` → можливо вже виконано, не помилка
  - `-4003 qty too small` → конфіг-помилка, логувати і не retry'ити
  - `-1021 timestamp out of recvWindow` → force time sync, retry 1 раз
  - 429 / 418 → delay, потім retry
- Віддає **`FillEvent`** назад у PositionManager коли філ відбувся (через підписку на user stream)
- Встановлює `leverage` і `marginType` при старті per-symbol

### Що НЕ робить:
- НЕ знає про сетапи, R, TradePlan, Features
- НЕ приймає рішень "коли закривати" (PositionManager)
- НЕ рахує розмір позиції (RiskEngine)
- НЕ зберігає торгову історію (Journal)

---

## Входи / виходи

### Публічний API:

```python
class ExecutionEngine:
    def __init__(self, config: ExecConfig,
                 transport: _RestTransport,
                 gateway: MarketDataGateway):
        self.transport = transport
        self.gateway = gateway
        gateway.on_user_event(self._on_user_event)    # ORDER_TRADE_UPDATE

    # === Life cycle ===
    async def start(self, symbols: list[str]) -> None
    async def stop(self) -> None

    # === Одиночні ордери ===
    async def place_order(self, req: OrderRequest) -> OrderResult
    async def cancel_order(self, symbol: str, client_order_id: str) -> OrderResult
    async def cancel_all(self, symbol: str) -> list[OrderResult]

    # === Batch (entry + SL + TP ladder одним викликом — коли біржа підтримує) ===
    async def place_bracket(self, bracket: BracketRequest) -> BracketResult

    # === Leverage / margin (викликається при warmup) ===
    async def set_leverage(self, symbol: str, leverage: int) -> None
    async def set_margin_type(self, symbol: str, mode: Literal['ISOLATED', 'CROSSED']) -> None

    # === Підписки ===
    def on_fill(self, cb: Callable[[FillEvent], Awaitable[None]]) -> None
    def on_order_update(self, cb: Callable[[OrderUpdate], Awaitable[None]]) -> None
```

### Типи:

```python
class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    # reduceOnly позначка через окремий bool

class TimeInForce(Enum):
    GTC = "GTC"                 # good-till-cancel
    IOC = "IOC"                 # immediate-or-cancel (для entry)
    FOK = "FOK"                 # fill-or-kill
    GTX = "GTX"                 # post-only

@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    type: OrderType
    qty: float
    price: float | None = None                   # для LIMIT
    stop_price: float | None = None              # для STOP_MARKET / TP_MARKET
    time_in_force: TimeInForce | None = None
    reduce_only: bool = False
    close_position: bool = False                 # для "закрити всю позицію" типу STOP_MARKET
    client_order_id: str | None = None           # якщо None — згенеруємо

@dataclass(frozen=True)
class OrderResult:
    success: bool
    client_order_id: str
    exchange_order_id: int | None
    status: Literal['NEW', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'NOT_FOUND']
    filled_qty: float
    avg_fill_price: float | None
    error_code: int | None
    error_msg: str | None
    request_sent_ms: int
    response_received_ms: int

@dataclass(frozen=True)
class BracketRequest:
    """Entry + стоп-лосс + 3 take-profit'и одночасно."""
    entry: OrderRequest
    stop_loss: OrderRequest                       # reduce_only=True, close_position або конкретний qty
    take_profits: list[OrderRequest]              # [tp1, tp2, tp3]
    symbol: str                                   # дублікат для зручного логу
    bracket_id: str                               # зовнішній ID (зазвичай == TradePlan.features_hash)

@dataclass(frozen=True)
class BracketResult:
    bracket_id: str
    entry_result: OrderResult
    stop_result: OrderResult | None
    tp_results: list[OrderResult]
    all_success: bool

@dataclass(frozen=True)
class FillEvent:
    symbol: str
    client_order_id: str
    exchange_order_id: int
    side: OrderSide
    qty: float                                    # цей філ
    price: float
    is_maker: bool
    commission_usd: float
    filled_cumulative: float                      # з початку ордера
    order_status: str                             # 'PARTIALLY_FILLED' / 'FILLED'
    timestamp_ms: int
    realized_pnl_usd: float                       # для reduce_only філа

@dataclass(frozen=True)
class OrderUpdate:
    """Будь-яка зміна статусу (CANCELED, REJECTED, EXPIRED)."""
    symbol: str
    client_order_id: str
    exchange_order_id: int
    old_status: str
    new_status: str
    timestamp_ms: int
```

---

## Залежності

**Читає:**
- `_RestTransport` (спільний з MarketDataGateway)
- `MarketDataGateway.on_user_event` — підписка для філ-апдейтів
- `ExchangeInfo` — фільтри `stepSize`, `tickSize`, `minNotional`, `minQty`
- `ExecConfig`

**Пише:**
- Виставляє ордери на біржу
- Підписникам: `FillEvent`, `OrderUpdate`
- `JournalLogger` — кожен request/response (audit trail для відлагодження)
- `NotificationService` — критичні помилки (`-2010`, margin call)

**Читають його:**
- PositionManager — основний споживач
- RiskEngine — опосередковано (через kill switch PositionManager припиняє нові ордери)

**НЕ залежить від:**
- DecisionEngine, SetupDetector, FeatureEngine (вища логіка)

---

## Стан

```python
@dataclass
class _ExecState:
    active_orders: dict[str, _LocalOrderState]   # client_order_id → state
    brackets: dict[str, BracketResult]           # bracket_id → current state
    symbol_meta: dict[str, _SymbolMeta]          # tickSize, stepSize, minNotional, leverage

@dataclass
class _LocalOrderState:
    request: OrderRequest
    exchange_order_id: int | None
    status: str
    filled_qty: float
    avg_price: float
    created_ms: int
    updated_ms: int
    bracket_id: str | None
```

Стан дуже коротко-живучий — усе очищається після `status in {FILLED, CANCELED, REJECTED, EXPIRED}`. Persistence не потрібна: при рестарті запитуємо `GET /openOrders` і відновлюємо активні.

---

## Ключові алгоритми

### 1. Ідемпотентність через `client_order_id`

```python
def _gen_coid(self, hint: str) -> str:
    """Форма: <bracket_id>-<suffix>-<rand4> — не більше 36 символів (Binance limit).
    hint — 'entry' / 'sl' / 'tp1' / ..., щоб людина могла парсити в логу."""
    rand = secrets.token_hex(2)
    coid = f"{hint[:12]}-{self._short_id(8)}-{rand}"
    return coid[:36]
```

При retry'ї після timeout REST виклику — використовуємо ТОЙ САМИЙ `client_order_id`. Binance поверне існуючий ордер замість створити дубль.

### 2. Round до фільтрів

```python
def _round_price(self, symbol: str, price: float) -> float:
    meta = self._state.symbol_meta[symbol]
    return round(price / meta.tick_size) * meta.tick_size

def _round_qty(self, symbol: str, qty: float) -> float:
    meta = self._state.symbol_meta[symbol]
    return math.floor(qty / meta.step_size) * meta.step_size

def _check_notional(self, symbol: str, qty: float, price: float) -> bool:
    meta = self._state.symbol_meta[symbol]
    notional = qty * price
    return notional >= meta.min_notional
```

### 3. `place_order` з retry

```python
async def place_order(self, req: OrderRequest) -> OrderResult:
    coid = req.client_order_id or self._gen_coid(self._hint_for(req))
    symbol = req.symbol
    qty = self._round_qty(symbol, req.qty)
    price = self._round_price(symbol, req.price) if req.price else None

    if qty <= 0:
        return OrderResult(False, coid, None, 'REJECTED', 0, None,
                           -4003, "qty_below_step", now_ms(), now_ms())
    if price and not self._check_notional(symbol, qty, price):
        return OrderResult(False, coid, None, 'REJECTED', 0, None,
                           -4164, "notional_below_min", now_ms(), now_ms())

    params = self._build_params(req, qty, price, coid)
    sent_ms = now_ms()

    for attempt in range(self.config.max_retries):
        try:
            data = await self.transport.private_post('/fapi/v1/order', params=params, weight=1)
            resp_ms = now_ms()
            self._track_active(coid, req, data, sent_ms)
            return _parse_order_result(data, coid, sent_ms, resp_ms)

        except ExchangeError as e:
            if e.code == -1021:                          # timestamp
                await self.transport.force_time_sync()
                continue
            if e.code in (-2010, -2019):                 # margin / balance
                await self.notifier.send(f"Insufficient: {e.msg}", AlertLevel.CRITICAL)
                return OrderResult(False, coid, None, 'REJECTED', 0, None, e.code, e.msg, sent_ms, now_ms())
            if e.code == -4003:                          # qty too small — конфіг-баг
                logger.error(f"qty filter failed despite rounding: {e.msg}")
                return OrderResult(False, coid, None, 'REJECTED', 0, None, e.code, e.msg, sent_ms, now_ms())
            if e.code == -1001 or e.code == -2014:       # disconnected / signature
                await asyncio.sleep(self.config.retry_delay_sec)
                continue
            raise

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"place_order network error (attempt {attempt}): {e}")
            await asyncio.sleep(self.config.retry_delay_sec * (attempt + 1))

    # Вичерпали retry. Можливо ордер створився але ми не отримали відповідь.
    # Перевіряємо через GET /openOrder по coid
    check = await self._check_order_by_coid(symbol, coid)
    return check or OrderResult(False, coid, None, 'REJECTED', 0, None, -1, "max_retries", sent_ms, now_ms())
```

### 4. `place_bracket` — атомарне розміщення

Binance підтримує batch-order (`/fapi/v1/batchOrders`, до 5 ордерів). Ми можемо надіслати entry + SL + 3 TP одним викликом і отримати atomicity best-effort.

```python
async def place_bracket(self, br: BracketRequest) -> BracketResult:
    orders = [self._build_params(br.entry, ..., self._gen_coid(f"e-{br.bracket_id}"))]
    orders.append(self._build_params(br.stop_loss, ..., self._gen_coid(f"sl-{br.bracket_id}")))
    for i, tp in enumerate(br.take_profits, 1):
        orders.append(self._build_params(tp, ..., self._gen_coid(f"tp{i}-{br.bracket_id}")))

    data = await self.transport.private_post('/fapi/v1/batchOrders',
                                              params={'batchOrders': json.dumps(orders)}, weight=5)

    # Якщо якийсь з частин failed — rollback: скасувати все що створилось
    results = [_parse_order_result(item, ...) for item in data]
    if not all(r.success for r in results):
        await self._rollback_bracket(results)
        return BracketResult(br.bracket_id, results[0], None, [], all_success=False)

    br_result = BracketResult(br.bracket_id, results[0], results[1], results[2:], all_success=True)
    self._state.brackets[br.bracket_id] = br_result
    return br_result

async def _rollback_bracket(self, results: list[OrderResult]):
    to_cancel = [r for r in results if r.success]
    for r in to_cancel:
        with suppress(Exception):
            await self.cancel_order(symbol, r.client_order_id)
```

> ⚠️ Batch не гарантує атомарність у повному сенсі. Entry може фільнути маркетом ДО того як SL буде прийнято. Реалістично:
> - **Entry** виставляємо як IOC LIMIT або MARKET
> - **SL** виставляємо ПІСЛЯ підтвердження entry fill (у PositionManager), щоб уникнути ситуації "SL поставлено, entry не виконано → висячий reduce-only SL"
> - Таким чином `place_bracket` реально виставляє тільки entry, а SL/TP — PositionManager через окремі `place_order` у відповідь на `FillEvent`

### 5. Обробка user stream → FillEvent

```python
async def _on_user_event(self, ev: RawUserEvent):
    if ev.event_type != 'ORDER_TRADE_UPDATE':
        if ev.event_type == 'ACCOUNT_UPDATE':
            self._on_account_update(ev.payload)
        elif ev.event_type == 'MARGIN_CALL':
            await self.notifier.send("MARGIN CALL", AlertLevel.CRITICAL)
        return

    order = ev.payload['o']
    coid = order['c']
    status = order['X']

    local = self._state.active_orders.get(coid)
    if not local:
        logger.debug(f"unknown coid {coid} — skip")
        return

    old_status = local.status
    local.status = status
    local.filled_qty = float(order.get('z', 0))
    local.updated_ms = ev.timestamp_ms

    # Філ (частковий або повний)
    if order.get('x') == 'TRADE':
        fill = FillEvent(
            symbol=order['s'],
            client_order_id=coid,
            exchange_order_id=order['i'],
            side=OrderSide(order['S']),
            qty=float(order['l']),
            price=float(order['L']),
            is_maker=order['m'],
            commission_usd=float(order.get('n', 0)),
            filled_cumulative=local.filled_qty,
            order_status=status,
            timestamp_ms=ev.timestamp_ms,
            realized_pnl_usd=float(order.get('rp', 0)),
        )
        for cb in self._fill_callbacks:
            asyncio.create_task(cb(fill))

    # Зміна статусу (для дебагу і UI)
    if status != old_status:
        update = OrderUpdate(order['s'], coid, order['i'], old_status, status, ev.timestamp_ms)
        for cb in self._order_update_callbacks:
            asyncio.create_task(cb(update))

    # Cleanup якщо термінальний статус
    if status in ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'):
        self._state.active_orders.pop(coid, None)
```

### 6. Warmup при старті

```python
async def start(self, symbols: list[str]) -> None:
    info = await self.gateway.fetch_exchange_info()
    for s in symbols:
        meta = _SymbolMeta.from_info(info, s)
        self._state.symbol_meta[s] = meta

        # Leverage / margin (тільки якщо відрізняється від поточного)
        try:
            await self.set_leverage(s, self.config.leverage[s])
            await self.set_margin_type(s, self.config.margin_type)
        except ExchangeError as e:
            if e.code == -4046:  # no need to change margin type
                pass
            else:
                raise

    # Recover: прочитати активні ордери з біржі і синхронізувати локальний стан
    for s in symbols:
        open_orders = await self.transport.private_get('/fapi/v1/openOrders',
                                                        params={'symbol': s}, weight=1)
        for o in open_orders:
            self._state.active_orders[o['clientOrderId']] = _LocalOrderState(
                request=None,  # тепер невідомий, але статус трекаємо
                exchange_order_id=o['orderId'],
                status=o['status'],
                filled_qty=float(o.get('executedQty', 0)),
                avg_price=float(o.get('avgPrice', 0)),
                created_ms=o['updateTime'],
                updated_ms=o['updateTime'],
                bracket_id=None,
            )
```

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Network timeout на place_order | Retry з тим же `client_order_id` → дубля не буде |
| Відповідь 200 OK але ордер насправді відхилений (`status=REJECTED` у payload) | Парсимо payload, повертаємо OrderResult з success=False |
| Відповідь отримана, але локальний `_on_user_event` прийшов ПЕРШИМ (race) | Перевіряємо `active_orders[coid]` — якщо вже є, оновлюємо; не створюємо заново |
| `-2011 unknown order` при cancel | Повертаємо `status='NOT_FOUND'` — PositionManager вирішує (напр., вже закрито) |
| Partial fill, потім ORDER_CANCELED | `FillEvent` + `OrderUpdate` — PositionManager знає що позиція часткова і стопи треба перевиставити |
| Ордер висить довше maxAge | Auto-cancel через `_cleanup_loop` (slow loop раз на 60s) |
| exchangeInfo змінилося під час роботи (біржа оновила фільтри) | Warmup раз на годину оновлює `symbol_meta` |
| User stream розрив під час філа | Після reconnect — викликаємо `GET /userTrades` за останні 5 хв для reconcile втрачених філ-івентів |

---

## Конфіги

```yaml
execution_engine:
  leverage:
    BTCUSDT: 10
    ETHUSDT: 10
  margin_type: ISOLATED

  retry:
    max_retries: 3
    retry_delay_sec: 0.5

  order_lifetime:
    entry_ioc_limit_ttl_ms: 2000            # IOC не висить, але на всяк
    limit_order_max_age_sec: 60             # якщо висить довше — скасовуємо

  reconcile:
    orphan_check_interval_sec: 60
    on_reconnect_trades_lookback_min: 5

  batch:
    bracket_use_batch_api: false            # поки false — SL/TP ставимо після fill
```

---

## Як тестувати

### Unit
- `_round_qty`: qty=16.6789, step=0.001 → 16.678 (floor)
- `_round_price`: price=100.123, tick=0.1 → 100.1
- `_check_notional`: qty=0.001, price=5 → 5$ < min_notional=10 → False
- `_gen_coid`: максимум 36 символів, стабільний формат
- `_parse_order_result` на фіксованих Binance payload'ах (із documentation examples)
- Ретрай: мок transport що кидає `-1021` раз, потім success → 2 спроби, на 2-й успіх
- `-2010` → негайний fail без retry, `send()` викликано

### Integration (testnet)
- `place_order(LIMIT IOC)` на testnet → `status='EXPIRED'` або `FILLED`, перевірка що `FillEvent` приходить через user stream
- `place_order` + `cancel_order` → статус CANCELED
- Rollback: `place_bracket` з одним навмисно невалідним TP — перевірити що всі створені ордери скасовуються
- Reconnect user stream — ввімкнути force disconnect, перевірити що втрачений філ відновлюється через REST reconcile

### Property
- Для будь-якого OrderRequest: `client_order_id` у результаті == `req.client_order_id` якщо переданий, інакше 1..36 символів
- `place_order` після `start(warmup)` не кидає на будь-який валідний qty/price (з точністю до rounding)

### Chaos
- Навмисний kill WiFi на 10 сек під час `place_order` → перевіряємо що після reconnect стан синхронізується без дублів
