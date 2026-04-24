# Exchange API — Підключення до біржі

## Чому Binance/ByBit?

- **Binance** — найбільша біржа по об'ємах. Глибокий стакан, найменший спред, найкраща ліквідність.
- **ByBit** — альтернатива. Схожий API, іноді менші комісії для mаker ордерів.

Обираємо **Binance Futures** як основну (для скальпінгу ф'ючерси кращі — менші комісії ніж на споті).

---

## Ключові концепції

### 1. REST vs WebSocket

| Тип | Коли використовується |
|---|---|
| **REST API** | Одноразові запити: баланс, відправка ордера, історія угод |
| **WebSocket** | Потокові дані: ринкові ціни, стакан, свічки (real-time) |

Для скальпінга **WebSocket критичний** — затримка REST (100-300 мс) занадто велика.

### 2. Public vs Private endpoints

| Тип | Потребує ключа | Що можна |
|---|---|---|
| **Public** | Ні | Читати ринкові дані |
| **Private** | Так | Рахунок, ордери, баланс |

---

## Реєстрація і API ключі

### Крок 1: Створити акаунт на Binance
1. Зайти на [binance.com](https://www.binance.com)
2. Пройти KYC верифікацію (паспорт/ID)
3. Поповнити рахунок (мінімум $50 для тестів)

### Крок 2: Створити API ключ
1. Account → API Management → Create API
2. Вибрати **System generated**
3. Назва: `scalping-bot-prod`
4. Permissions:
   - ✅ Enable Reading
   - ✅ Enable Futures
   - ❌ Enable Withdrawals (НЕ вмикати!)
5. IP restriction: вказати IP сервера де буде бот

### Крок 3: Зберегти ключі
```
API Key:    abcd1234...     ← публічний, ідентифікує акаунт
Secret Key: xyz789...        ← приватний, НІКОЛИ не показувати
```

**⚠️ Secret Key показується ОДИН раз. Скопіюй одразу.**

### Крок 4: Тестнет (ОБОВ'ЯЗКОВО для розробки)
Для тестування без реальних грошей:
- [testnet.binance.vision](https://testnet.binance.vision) — спот тестнет
- [testnet.binancefuture.com](https://testnet.binancefuture.com) — ф'ючерс тестнет

Окремі API ключі, фейковий баланс $10,000. Ідентичний API до реальної біржі.

---

## WebSocket Streams

### Base URL:
- **Production**: `wss://fstream.binance.com/ws/`
- **Testnet**: `wss://stream.binancefuture.com/ws/`

### Основні streams для бота:

#### 1. Aggregated Trades (`aggTrade`)
Кожна виконана угода на ринку.
```
URL: wss://fstream.binance.com/ws/btcusdt@aggTrade

Приклад повідомлення:
{
  "e": "aggTrade",
  "E": 1749999999999,       // event time
  "s": "BTCUSDT",
  "a": 5933014,              // agg trade ID
  "p": "50000.00",           // price
  "q": "0.100",              // quantity
  "f": 100,                  // first trade ID
  "l": 105,                  // last trade ID
  "T": 1749999999990,        // trade time
  "m": false                 // isBuyerMaker: false = taker buy (агресивна купівля)
}
```

**Критично**: поле `m` показує напрямок агресії:
- `m = false` → покупець агресивний (market buy) → **додаємо до ask volume**
- `m = true` → продавець агресивний (market sell) → **додаємо до bid volume**

#### 2. Partial Order Book Depth (`depth20@100ms`)
Топ-20 рівнів bid/ask, оновлення кожні 100ms.
```
URL: wss://fstream.binance.com/ws/btcusdt@depth20@100ms

Приклад:
{
  "e": "depthUpdate",
  "E": 1749999999999,
  "s": "BTCUSDT",
  "b": [                     // bids (покупці)
    ["49999.50", "1.234"],
    ["49999.00", "2.500"],
    ...
  ],
  "a": [                     // asks (продавці)
    ["50000.00", "0.800"],
    ["50000.50", "1.500"],
    ...
  ]
}
```

#### 3. Kline/Candlestick (`kline_1m`)
Свічки. Нам потрібні 1m, 5m, 15m.
```
URL: wss://fstream.binance.com/ws/btcusdt@kline_1m

Приклад:
{
  "e": "kline",
  "E": 1749999999999,
  "s": "BTCUSDT",
  "k": {
    "t": 1749999960000,      // candle open time
    "T": 1750000019999,      // candle close time
    "s": "BTCUSDT",
    "i": "1m",
    "o": "50000.00",         // open
    "c": "50050.00",         // close
    "h": "50100.00",         // high
    "l": "49980.00",         // low
    "v": "10.500",           // volume
    "n": 150,                // trades count
    "x": false,              // чи свічка закрита?
    "q": "525000.00",        // quote volume
    "V": "6.300",            // taker buy volume (агресивні покупки)
    "Q": "315150.00"         // taker buy quote volume
  }
}
```

Поля `V` і `Q` дають нам готову delta (taker buys), решта (volume - taker buy) = taker sells.

#### 4. Book Ticker (`bookTicker`)
Найкращі bid/ask у real-time (для точного фіксування moment).
```
{
  "u": 400900217,
  "s": "BTCUSDT",
  "b": "49999.50",           // best bid price
  "B": "10.500",             // best bid quantity
  "a": "50000.00",           // best ask price
  "A": "8.300",              // best ask quantity
  "T": 1749999999999
}
```

### Комбінування streams:
Можна підписатись на кілька streams одним підключенням:
```
wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@depth20@100ms/btcusdt@kline_1m
```

---

## REST API — критичні endpoints

### Public (без ключів):

```
GET /fapi/v1/exchangeInfo      — правила символів (tick size, min qty)
GET /fapi/v1/klines             — історичні свічки
GET /fapi/v1/depth              — snapshot стакану
GET /fapi/v1/aggTrades          — історичні угоди
```

### Private (потребує автентифікації):

```
# Рахунок і баланси
GET /fapi/v2/account            — весь акаунт
GET /fapi/v2/balance            — баланси

# Позиції
GET /fapi/v2/positionRisk       — поточні позиції

# Ордери
POST /fapi/v1/order             — створити ордер
DELETE /fapi/v1/order           — скасувати ордер
DELETE /fapi/v1/allOpenOrders   — скасувати ВСІ ордери (для Kill Switch!)
GET /fapi/v1/openOrders         — відкриті ордери
GET /fapi/v1/allOrders          — історія ордерів

# Leverage / Margin
POST /fapi/v1/leverage          — змінити плече
POST /fapi/v1/marginType        — Isolated / Cross margin
```

---

## Автентифікація private запитів

Binance використовує **HMAC SHA256** підпис.

### Алгоритм:
```
1. Сформувати query string: symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.1&timestamp=1749999999999
2. Підписати його через HMAC_SHA256(query_string, secret_key)
3. Додати підпис до запиту: ...&signature=ABCDEF...
4. Додати заголовок: X-MBX-APIKEY: <api_key>
```

### Python приклад:
```python
import hmac
import hashlib
import time
import requests

API_KEY = "your_api_key"
SECRET_KEY = "your_secret_key"
BASE_URL = "https://fapi.binance.com"

def sign_request(params: dict) -> dict:
    params['timestamp'] = int(time.time() * 1000)
    query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
    signature = hmac.new(
        SECRET_KEY.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    params['signature'] = signature
    return params

def place_market_order(symbol: str, side: str, quantity: float):
    params = {
        'symbol': symbol,
        'side': side,       # 'BUY' or 'SELL'
        'type': 'MARKET',
        'quantity': quantity
    }
    params = sign_request(params)
    headers = {'X-MBX-APIKEY': API_KEY}
    response = requests.post(f"{BASE_URL}/fapi/v1/order", params=params, headers=headers)
    return response.json()
```

**УВАГА**: використовуй бібліотеку `python-binance` або `binance-connector` — вона робить це все за тебе правильно.

---

## Типи ордерів для бота

### Ті що нам потрібні:

| Тип | Для чого |
|---|---|
| `LIMIT` | Вхід в угоду (ми maker, менша комісія) |
| `MARKET` | Аварійні ситуації (якщо треба ВЖЕ закрити) |
| `STOP_MARKET` | Стоп-лос (спрацьовує market-ордером при досягненні ціни) |
| `TAKE_PROFIT_MARKET` | Тейк-профіт |
| `STOP` | Стоп з лімітним виконанням |
| `TRAILING_STOP_MARKET` | Трейлінг-стоп |

### Приклад створення повної угоди Long:
```python
# 1. Вхід
entry = client.futures_create_order(
    symbol='BTCUSDT',
    side='BUY',
    type='LIMIT',
    timeInForce='GTC',        # Good Till Canceled
    quantity=0.1,
    price=50000
)

# 2. Стоп-лос (після виконання входу)
stop = client.futures_create_order(
    symbol='BTCUSDT',
    side='SELL',
    type='STOP_MARKET',
    stopPrice=49500,
    closePosition=True,       # закрити всю позицію
    workingType='MARK_PRICE'  # використовувати mark price
)

# 3. Тейк-профіт
tp = client.futures_create_order(
    symbol='BTCUSDT',
    side='SELL',
    type='TAKE_PROFIT_MARKET',
    stopPrice=51000,
    closePosition=True,
    workingType='MARK_PRICE'
)
```

---

## Rate Limits (ліміти запитів)

Binance обмежує кількість запитів:

| Тип | Ліміт |
|---|---|
| **Request weight** | 2400 per minute |
| **Orders** | 300 per 10 seconds, 1200 per minute |

Кожен endpoint має свою "вагу":
- `GET /depth` → weight 20 (важкий)
- `POST /order` → weight 1 (легкий)
- `GET /account` → weight 5

### Заголовки відповіді:
```
X-MBX-USED-WEIGHT-1M: 125     ← скільки ваги використано за хвилину
X-MBX-ORDER-COUNT-10S: 5      ← скільки ордерів за 10 сек
```

### Правило бота:
```
ЯКЩО used_weight > 80% ліміту
  → ЗМЕНШУЙ частоту запитів або вдайся до WebSocket (яка не лімітується)

ЯКЩО отримав 429 (Too Many Requests) або 418 (IP ban)
  → зупини всі запити на 60 секунд мінімум
  → запиши в лог
```

---

## Обробка помилок

### Коди помилок:

| Код | Що означає | Що робити |
|---|---|---|
| `-1021` | Timestamp поза вікном (>5000ms) | Синхронізувати час з сервером Binance |
| `-2010` | Недостатньо балансу | Перевірити баланс перед ордером |
| `-2011` | Ордер не знайдено | Можливо вже виконано |
| `-4003` | Quantity too small | Перевірити LOT_SIZE |
| `-4016` | PERCENT_PRICE filter | Ціна занадто далеко від поточної |
| `-1003` | Too many requests | Чекати, бан на 2 хв |

### Синхронізація часу:
```python
server_time = client.futures_time()['serverTime']
local_time = int(time.time() * 1000)
offset = server_time - local_time
# Використовувати offset для всіх timestamp у запитах
```

---

## Бібліотеки Python для роботи з API

Рекомендації:

```python
# Офіційна (від Binance)
pip install binance-connector

# Неофіційна але популярна
pip install python-binance

# Async версія (рекомендується для WebSocket)
pip install binance-connector-async
```

Для нашого бота — використовуємо **`python-binance`** + **`websockets`** для custom обробки streams.

---

## Що далі?

Наступний документ: [08-data-processing.md](./08-data-processing.md) — як з цих raw WebSocket потоків отримати footprint, delta, CVD і всі інші сигнали.
