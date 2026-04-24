# Архітектура бота

## Загальна схема

```
┌─────────────────────────────────────────────────────────┐
│                    БІРЖА (Binance/ByBit)                 │
│                                                          │
│  WebSocket Stream:          REST API:                    │
│  - Trade stream             - Відправка ордерів          │
│  - Order book updates       - Скасування ордерів         │
│  - Kline (свічки)           - Баланс акаунту             │
└──────────────┬──────────────────────────┬────────────────┘
               │ (дані надходять)          │ (команди)
               ▼                          ▲
┌─────────────────────────────────────────────────────────┐
│                    ЯДРО БОТА (Python)                    │
│                                                          │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ Data Engine │  │Signal Engine │  │  Risk Manager  │  │
│  │             │  │              │  │                │  │
│  │ - Парсинг   │→ │ - Imbalance  │→ │ - Розмір поз.  │  │
│  │ - Агрегація │  │ - Absorption │  │ - Стоп/Тейк    │  │
│  │ - Footprint │  │ - Delta      │  │ - Circuit Break│  │
│  │ - DOM track │  │ - SMC        │  │ - Kill Switch  │  │
│  └─────────────┘  └──────────────┘  └────────────────┘  │
│                                             │            │
│  ┌──────────────────────────────────────────┘            │
│  ▼                                                       │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │Order Manager│  │ State Machine│  │    Logger      │  │
│  │             │  │              │  │                │  │
│  │ - Вхід      │  │ - IDLE       │  │ - Всі угоди    │  │
│  │ - Модифікац.│  │ - SCANNING   │  │ - Всі сигнали  │  │
│  │ - Вихід     │  │ - IN_TRADE   │  │ - Помилки      │  │
│  │             │  │ - HALTED     │  │ - Алерти       │  │
│  └─────────────┘  └──────────────┘  └────────────────┘  │
└─────────────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│                 МОНІТОРИНГ (опціонально)                  │
│  - Telegram бот (алерти + стан)                         │
│  - Dashboard (веб-інтерфейс зі статистикою)             │
└─────────────────────────────────────────────────────────┘
```

---

## Компоненти детально

### 1. Data Engine (Рушій даних)

Відповідає за отримання і первинну обробку ринкових даних.

**Що отримує з біржі:**
- `aggTrade` stream — кожна виконана угода (ціна, об'єм, напрямок)
- `depth` stream — оновлення стакану ордерів (bid/ask levels)
- `kline` stream — OHLCV свічки (1хв, 5хв, 15хв)

**Що рахує самостійно:**
- Delta per bar (покупки - продажі за свічку)
- Cumulative Delta (CVD)
- Footprint (bid/ask об'єм на кожному ціновому рівні в свічці)
- DOM snapshots (знімки стакану кожні N мілісекунд)

**Структура зберігання (в пам'яті):**
```python
market_data = {
    'trades':    deque(maxlen=10000),  # останні 10К угод
    'orderbook': {'bids': {}, 'asks': {}},  # поточний стакан
    'candles': {
        '1m':  deque(maxlen=500),
        '5m':  deque(maxlen=200),
        '15m': deque(maxlen=100),
    },
    'footprint': {},  # footprint поточної свічки
    'cvd': deque(maxlen=500),  # cumulative delta
}
```

---

### 2. Signal Engine (Рушій сигналів)

Аналізує дані і шукає торгові ситуації.

**Порядок роботи:**
```
1. Визначити тренд (SMC на 15хв/5хв)
   → Якщо тренд не визначений → не торгуємо

2. Шукати точку входу в напрямку тренду
   → Ціна прийшла до OB або FVG?
   
3. Підтвердити через Order Flow
   → Є хоча б 2 сигнали з: Imbalance, Absorption, Delta spike?

4. Якщо умови виконані → генерувати сигнал
   → передати в Risk Manager
```

**Сигнальний об'єкт:**
```python
@dataclass
class Signal:
    direction: str       # 'long' або 'short'
    entry_price: float   # ціна входу
    stop_price: float    # ціна стопу
    target_price: float  # ціна тейку
    confidence: float    # 0.0 - 1.0 (впевненість)
    signals_fired: list  # які сигнали спрацювали
    timestamp: datetime
```

---

### 3. Risk Manager (Ризик-менеджер)

Перевіряє кожен сигнал перед тим як дозволити угоду.

**Чек-лист перед входом:**
```python
def can_enter_trade(signal):
    # 1. Чи не на паузі/зупинці?
    if state == HALTED: return False
    
    # 2. Чи не перевищений денний ліміт?
    if daily_loss >= DAILY_LIMIT: return False
    
    # 3. Чи достатній R:R?
    rr = (signal.target - signal.entry) / (signal.entry - signal.stop)
    if rr < MIN_RR: return False
    
    # 4. Чи розумний розмір стопу?
    stop_pct = abs(signal.entry - signal.stop) / signal.entry
    if stop_pct > MAX_STOP_PCT: return False
    
    # 5. Розрахувати розмір позиції
    position_size = calc_position_size(signal.stop_price)
    
    return True, position_size
```

---

### 4. State Machine (Стан бота)

Бот завжди знаходиться в одному зі станів:

```
    ┌─────────────────────────────────────┐
    │              IDLE                   │
    │  (очікування, немає позицій)        │
    └──────────────┬──────────────────────┘
                   │ сигнал отримано
                   ▼
    ┌─────────────────────────────────────┐
    │            SCANNING                 │
    │  (сигнал є, перевіряємо умови)      │
    └──────┬─────────────────┬────────────┘
           │ умови ок         │ умови не ок
           ▼                  ▼ назад в IDLE
    ┌──────────────┐
    │  IN_TRADE    │
    │  (угода      │
    │  відкрита)   │
    └──────┬───────┘
           │ тейк або стоп спрацював
           ▼
         IDLE
    
    З будь-якого стану:
    Circuit Breaker → HALTED
    HALTED → (тільки ручний перезапуск) → IDLE
```

---

### 5. Order Manager (Менеджер ордерів)

Відправляє команди на біржу.

**Типи ордерів які використовуємо:**
- `LIMIT` — для входу (ставимо ордер по ціні, не платимо maker fee)
- `STOP_MARKET` — для стоп-лосу (виконується за ринком при досягненні ціни)
- `TAKE_PROFIT_MARKET` — для тейк-профіту

**Послідовність відкриття угоди:**
```
1. Відправити LIMIT ордер на вхід
2. Дочекатись виконання (або таймаут 30 сек → скасувати)
3. Одразу після виконання:
   - Відправити STOP_MARKET стоп-лос
   - Відправити TAKE_PROFIT_MARKET тейк
4. Моніторити позицію
```

---

## Файлова структура проекту

```
scalping-bot/
├── DOCS/                    ← Документація (ти зараз тут)
│   ├── README.md
│   ├── 01-tiger-terminal.md
│   ├── 02-signals.md
│   ├── 03-smc.md
│   ├── 04-risk-management.md
│   └── 05-architecture.md
│
├── src/                     ← Вихідний код (будемо писати)
│   ├── data/
│   │   ├── exchange.py      ← WebSocket підключення до біржі
│   │   └── processor.py     ← Обробка даних (footprint, delta, DOM)
│   │
│   ├── signals/
│   │   ├── imbalance.py     ← Bid/Ask Imbalance + Stacked
│   │   ├── absorption.py    ← Absorption detection
│   │   ├── delta.py         ← Delta spike + CVD divergence
│   │   ├── pullback.py      ← Micro pullback
│   │   └── smc.py           ← SMC: FVG, OB, BOS/CHoCH
│   │
│   ├── risk/
│   │   ├── manager.py       ← Risk Manager
│   │   └── circuit_breaker.py ← Circuit Breaker + Kill Switch
│   │
│   ├── execution/
│   │   ├── orders.py        ← Order Manager
│   │   └── state.py         ← State Machine
│   │
│   └── bot.py               ← Головний файл (все з'єднує)
│
├── config/
│   └── settings.yaml        ← Всі параметри (ризик, біржа, тощо)
│
├── logs/                    ← Логи угод
├── tests/                   ← Тести
└── requirements.txt         ← Python залежності
```

---

## Черговість розробки

```
Фаза 1: Дані (підключення до біржі, отримання trade stream)
Фаза 2: Базові сигнали (delta, imbalance)
Фаза 3: Ризик-менеджмент (без нього нічого не запускаємо)
Фаза 4: Виконання ордерів (спочатку в ТЕСТНЕТ)
Фаза 5: SMC сигнали
Фаза 6: Absorption + Spoof detection
Фаза 7: Тестування + оптимізація параметрів
Фаза 8: Обережний запуск на реальних грошах (мінімальний розмір)
```

> **Правило**: Жодного реального грошового торгу поки не пройдені всі фази 1-7 на тестнеті.
