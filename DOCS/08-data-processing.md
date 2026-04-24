# Data Processing — Обробка ринкових даних

## Навіщо цей документ?

WebSocket біржі дає нам **сирі дані** (кожна угода, оновлення стакану). Але боту для прийняття рішень потрібні **агреговані метрики** — footprint, delta, CVD, imbalance ratio, absorption detection.

Цей документ описує **як з сирих даних рахувати все це**.

---

## Архітектура обробки даних

```
┌─────────────────────────────────────────────────┐
│             WebSocket Streams                    │
│  aggTrade → depth → kline → bookTicker          │
└──────────────────┬──────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────┐
│          Stream Dispatcher                       │
│  Розподіляє повідомлення по обробниках           │
└──────┬───────────┬───────────┬──────────────────┘
       ▼           ▼           ▼
┌──────────┐ ┌──────────┐ ┌──────────┐
│  Trade   │ │OrderBook │ │ Candle   │
│Processor │ │Processor │ │Processor │
└─────┬────┘ └─────┬────┘ └─────┬────┘
      │            │            │
      ▼            ▼            ▼
┌─────────────────────────────────────────────────┐
│               State Store                        │
│  - trades (deque)                                │
│  - orderbook (bids/asks)                         │
│  - candles (1m, 5m, 15m)                         │
│  - footprint (per candle)                        │
│  - delta history                                 │
│  - cvd                                           │
└─────────────────────────────────────────────────┘
```

---

## 1. Обробка Trade Stream

### Що робимо з кожним `aggTrade`:

```python
def process_trade(trade_msg):
    price = float(trade_msg['p'])
    qty   = float(trade_msg['q'])
    is_buyer_maker = trade_msg['m']
    timestamp = trade_msg['T']
    
    # Визначаємо напрямок агресії
    if is_buyer_maker:
        # Продавець був агресивний (market sell)
        side = 'sell'      # aggressive sell
    else:
        # Покупець був агресивний (market buy)
        side = 'buy'       # aggressive buy
    
    # 1. Додати в deque останніх угод
    state.trades.append({
        'price': price,
        'qty': qty,
        'side': side,
        'timestamp': timestamp
    })
    
    # 2. Оновити footprint поточної свічки
    update_footprint(price, qty, side)
    
    # 3. Оновити delta
    update_delta(qty, side)
    
    # 4. Перевірити на spike
    check_delta_spike(qty, side)
```

---

## 2. Footprint — розрахунок

### Що таке footprint:
Для кожної свічки — розбивка об'єму по цінових рівнях.
```
Свічка [00:05-00:06]:
  
  Ціна     | Bid vol | Ask vol | Delta
  ---------|---------|---------|-------
  50,010   |    0    |   150   |  +150
  50,005   |   20    |   300   |  +280
  50,000   |  200    |   250   |   +50
  49,995   |  350    |   100   |  -250
  49,990   |  500    |    50   |  -450
```

### Структура даних:
```python
from collections import defaultdict

footprint = {
    'candle_open_time': 1749999960000,
    'levels': defaultdict(lambda: {'bid_vol': 0, 'ask_vol': 0}),
    'total_bid_vol': 0,
    'total_ask_vol': 0,
    'delta': 0,
    'poc': None,     # Point of Control — рівень з найбільшим об'ємом
}
```

### Оновлення на кожній угоді:
```python
def update_footprint(price, qty, side):
    current_candle = state.current_candle
    
    # Округлюємо ціну до tick_size (для BTCUSDT = $0.10)
    price_level = round_to_tick(price, tick_size=0.10)
    
    if side == 'buy':
        current_candle.footprint['levels'][price_level]['ask_vol'] += qty
        current_candle.footprint['total_ask_vol'] += qty
        current_candle.footprint['delta'] += qty
    else:  # sell
        current_candle.footprint['levels'][price_level]['bid_vol'] += qty
        current_candle.footprint['total_bid_vol'] += qty
        current_candle.footprint['delta'] -= qty
```

### Закриття свічки (коли приходить наступна):
```python
def finalize_footprint(candle):
    levels = candle.footprint['levels']
    
    # POC — Point of Control (рівень з найбільшим сумарним об'ємом)
    poc = max(levels.keys(), key=lambda p: 
              levels[p]['bid_vol'] + levels[p]['ask_vol'])
    candle.footprint['poc'] = poc
    
    # Imbalance per level
    for price, vols in levels.items():
        total = vols['bid_vol'] + vols['ask_vol']
        if total > 0:
            vols['imbalance'] = (vols['ask_vol'] - vols['bid_vol']) / total
        else:
            vols['imbalance'] = 0
    
    # Stacked imbalance detection
    candle.footprint['stacked_up'] = detect_stacked_imbalance(levels, direction='up')
    candle.footprint['stacked_down'] = detect_stacked_imbalance(levels, direction='down')
```

---

## 3. Delta і CVD (Cumulative Volume Delta)

### Delta:
Різниця aggressive buy volume і aggressive sell volume за свічку.
```
delta = taker_buy_volume - taker_sell_volume
```

### CVD:
Накопичувана сума всіх delta з початку сесії.
```python
def update_cvd(new_delta):
    if not state.cvd_history:
        state.cvd_history.append(new_delta)
    else:
        last = state.cvd_history[-1]
        state.cvd_history.append(last + new_delta)
```

### CVD Divergence:
```python
def detect_cvd_divergence(candles, cvd_values, lookback=20):
    # Знаходимо останні swing highs і lows
    price_swings = find_swings([c.close for c in candles[-lookback:]])
    cvd_swings = find_swings(cvd_values[-lookback:])
    
    # Bullish divergence: lower low on price, higher low on CVD
    if price_swings['last_low'] < price_swings['prev_low']:
        if cvd_swings['last_low'] > cvd_swings['prev_low']:
            return 'bullish_divergence'
    
    # Bearish divergence: higher high on price, lower high on CVD
    if price_swings['last_high'] > price_swings['prev_high']:
        if cvd_swings['last_high'] < cvd_swings['prev_high']:
            return 'bearish_divergence'
    
    return None
```

### Delta Spike:
```python
def detect_delta_spike(current_delta, recent_deltas, threshold=2.0):
    if len(recent_deltas) < 20:
        return None
    
    mean = sum(recent_deltas) / len(recent_deltas)
    std = stdev(recent_deltas)
    
    if current_delta > mean + threshold * std:
        return 'spike_up'
    elif current_delta < mean - threshold * std:
        return 'spike_down'
    return None
```

---

## 4. Imbalance Ratio

### Per-level imbalance:
```python
def level_imbalance(level_data):
    bid = level_data['bid_vol']
    ask = level_data['ask_vol']
    total = bid + ask
    
    if total == 0:
        return 0
    
    # Нормалізований від -1 до +1
    return (ask - bid) / total
```

### Stacked Imbalance:
Виявляємо коли 3+ рівні підряд мають дисбаланс в один бік.

```python
def detect_stacked_imbalance(levels, direction='up', threshold=0.6, min_stack=3):
    """
    direction='up'   — шукаємо bullish stack (ask > bid)
    direction='down' — шукаємо bearish stack (bid > ask)
    """
    # Сортуємо ціни
    prices = sorted(levels.keys())
    
    stack_count = 0
    max_stack = 0
    stack_start = None
    
    for price in prices:
        imb = level_imbalance(levels[price])
        
        if direction == 'up' and imb > threshold:
            if stack_count == 0:
                stack_start = price
            stack_count += 1
        elif direction == 'down' and imb < -threshold:
            if stack_count == 0:
                stack_start = price
            stack_count += 1
        else:
            max_stack = max(max_stack, stack_count)
            stack_count = 0
    
    max_stack = max(max_stack, stack_count)
    
    return {
        'detected': max_stack >= min_stack,
        'stack_size': max_stack,
        'start_price': stack_start
    }
```

---

## 5. Order Book Processing

### Maintain локальної копії стакану:

```python
class OrderBook:
    def __init__(self):
        self.bids = {}  # {price: quantity}
        self.asks = {}
        self.last_update_id = 0
    
    def apply_update(self, update):
        """Застосувати оновлення зі stream depthUpdate"""
        for price_str, qty_str in update['b']:  # bids
            price = float(price_str)
            qty = float(qty_str)
            if qty == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty
        
        for price_str, qty_str in update['a']:  # asks
            price = float(price_str)
            qty = float(qty_str)
            if qty == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty
    
    def best_bid(self):
        return max(self.bids.keys()) if self.bids else None
    
    def best_ask(self):
        return min(self.asks.keys()) if self.asks else None
    
    def spread(self):
        bid, ask = self.best_bid(), self.best_ask()
        return ask - bid if bid and ask else None
    
    def top_n_levels(self, n=10):
        sorted_bids = sorted(self.bids.items(), reverse=True)[:n]
        sorted_asks = sorted(self.asks.items())[:n]
        return sorted_bids, sorted_asks
```

### DOM Imbalance (весь стакан):
```python
def dom_imbalance(orderbook, depth=10):
    """Дисбаланс у топ-N рівнях стакану"""
    bids, asks = orderbook.top_n_levels(depth)
    
    total_bid = sum(qty for _, qty in bids)
    total_ask = sum(qty for _, qty in asks)
    total = total_bid + total_ask
    
    if total == 0:
        return 0
    
    return (total_bid - total_ask) / total
    # >0 — більше покупців в стакані (бичачий настрій)
    # <0 — більше продавців
```

---

## 6. Absorption Detection

### Що шукаємо:
Великий об'єм на одному ціновому рівні, але ціна не рухається.

```python
def detect_absorption(candle, orderbook, thresholds):
    footprint = candle.footprint
    
    # 1. Великий об'єм на одному рівні
    max_level_volume = 0
    absorption_price = None
    for price, vols in footprint['levels'].items():
        total = vols['bid_vol'] + vols['ask_vol']
        if total > max_level_volume:
            max_level_volume = total
            absorption_price = price
    
    volume_threshold = thresholds['avg_level_volume'] * 2
    if max_level_volume < volume_threshold:
        return None
    
    # 2. Маленький range свічки попри об'єм
    candle_range = candle.high - candle.low
    if candle_range > thresholds['avg_range'] * 0.5:
        return None
    
    # 3. Сильна delta в одну сторону
    total_vol = footprint['total_bid_vol'] + footprint['total_ask_vol']
    delta_ratio = abs(footprint['delta']) / total_vol if total_vol > 0 else 0
    
    if delta_ratio < 0.3:
        return None
    
    # 4. Визначити напрямок
    direction = 'buy' if footprint['delta'] > 0 else 'sell'
    
    return {
        'detected': True,
        'direction': direction,
        'price': absorption_price,
        'volume': max_level_volume,
        'expected_move': 'up' if direction == 'buy' else 'down'
    }
```

---

## 7. Свічкові операції

### Агрегація свічок з trade stream:
Хоч Binance дає нам kline stream, іноді потрібно рахувати кастомні TF (наприклад 2-хвилинні).

```python
class CandleAggregator:
    def __init__(self, timeframe_seconds):
        self.tf = timeframe_seconds
        self.current = None
    
    def add_trade(self, price, qty, timestamp):
        bucket = (timestamp // (self.tf * 1000)) * (self.tf * 1000)
        
        if self.current is None or self.current['open_time'] != bucket:
            # Нова свічка
            if self.current:
                self._emit_closed(self.current)
            self.current = {
                'open_time': bucket,
                'open': price,
                'high': price,
                'low': price,
                'close': price,
                'volume': qty
            }
        else:
            c = self.current
            c['high'] = max(c['high'], price)
            c['low'] = min(c['low'], price)
            c['close'] = price
            c['volume'] += qty
    
    def _emit_closed(self, candle):
        """Callback коли свічка закрилась"""
        state.candles[f'{self.tf}s'].append(candle)
```

### Swing Highs/Lows:
```python
def find_swings(prices, window=5):
    """
    Знаходить локальні swing high і low.
    window=5 означає: свічка є swing якщо вона найвища/найнижча 
    серед ±5 сусідів.
    """
    swings = []
    for i in range(window, len(prices) - window):
        neighborhood = prices[i-window : i+window+1]
        if prices[i] == max(neighborhood):
            swings.append({'type': 'high', 'index': i, 'price': prices[i]})
        elif prices[i] == min(neighborhood):
            swings.append({'type': 'low', 'index': i, 'price': prices[i]})
    return swings
```

### BOS Detection:
```python
def detect_bos(candles, swings, lookback=20):
    """Виявляє Break of Structure"""
    if len(swings) < 2:
        return None
    
    recent_swings = swings[-lookback:]
    highs = [s for s in recent_swings if s['type'] == 'high']
    lows = [s for s in recent_swings if s['type'] == 'low']
    
    current_price = candles[-1].close
    
    # Bullish BOS: ціна пробила останній swing high
    if highs and current_price > highs[-1]['price']:
        return {'type': 'bullish', 'broken_level': highs[-1]['price']}
    
    # Bearish BOS: ціна пробила останній swing low
    if lows and current_price < lows[-1]['price']:
        return {'type': 'bearish', 'broken_level': lows[-1]['price']}
    
    return None
```

---

## 8. In-memory State Store

### Структура:
```python
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class MarketState:
    # Trade stream
    trades: deque = field(default_factory=lambda: deque(maxlen=10000))
    
    # Order book snapshot
    orderbook: OrderBook = field(default_factory=OrderBook)
    
    # Candles (різні TF)
    candles_1m: deque = field(default_factory=lambda: deque(maxlen=500))
    candles_5m: deque = field(default_factory=lambda: deque(maxlen=200))
    candles_15m: deque = field(default_factory=lambda: deque(maxlen=100))
    candles_1h: deque = field(default_factory=lambda: deque(maxlen=48))
    candles_4h: deque = field(default_factory=lambda: deque(maxlen=30))
    
    # Footprint поточної свічки
    current_footprint: dict = field(default_factory=dict)
    
    # Delta / CVD
    cvd_history: deque = field(default_factory=lambda: deque(maxlen=500))
    recent_deltas: deque = field(default_factory=lambda: deque(maxlen=20))
    
    # Swings
    swings_1m: list = field(default_factory=list)
    swings_5m: list = field(default_factory=list)
    swings_15m: list = field(default_factory=list)
    
    # POIs (активні)
    active_pois: list = field(default_factory=list)
    
    # Тренд по таймфреймах
    trend_15m: Optional[str] = None  # 'bullish' / 'bearish' / 'range'
    trend_1h: Optional[str] = None
    trend_4h: Optional[str] = None
```

### Синхронізація:
Всі обробники пишуть в один `MarketState` об'єкт.
Для thread-safety — використовувати `asyncio.Lock` або `threading.Lock` навколо записів.

---

## 9. Performance вимоги

Скальпінг → критична latency. Цілі:

| Операція | Макс. час |
|---|---|
| Обробка одного `aggTrade` | < 0.5 ms |
| Оновлення orderbook | < 1 ms |
| Повна перевірка всіх сигналів | < 10 ms |
| Розрахунок confidence score | < 5 ms |
| Відправка ордера | < 50 ms (обмежено мережею) |

### Оптимізації:
- Використовувати `deque` замість `list` для FIFO
- Не копіювати великі структури (передавати по reference)
- Кешувати обчислення (середні, std) — перерахувати тільки коли потрібно
- Використовувати `numpy` для масових обчислень (std, mean на великих вікнах)
- `asyncio` для I/O, але CPU-bound обчислення — синхронно

---

## 10. Тестування обробки даних

### Unit тести:
```python
def test_stacked_imbalance_detection():
    levels = {
        50000.0: {'bid_vol': 100, 'ask_vol': 700},  # imb = +0.75
        50001.0: {'bid_vol': 150, 'ask_vol': 850},  # imb = +0.70
        50002.0: {'bid_vol': 80,  'ask_vol': 920},  # imb = +0.84
    }
    result = detect_stacked_imbalance(levels, direction='up')
    assert result['detected'] is True
    assert result['stack_size'] == 3

def test_delta_spike():
    recent = [10, -5, 15, 8, -3, 12, 5, -8, 20, 11,
              7, -2, 15, 9, 6, -4, 18, 10, 14, 8]  # mean≈8, std≈8
    assert detect_delta_spike(35, recent) == 'spike_up'  # > 8 + 2*8 = 24
    assert detect_delta_spike(-20, recent) == 'spike_down'
    assert detect_delta_spike(15, recent) is None
```

### Replay тест:
Завантажити `.csv` з історичними trades і проганяти через обробник — перевіряти що розрахунки збігаються з Binance API history.

---

## Що далі?

- [09-config.md](./09-config.md) — конфігурація всіх порогів (volume thresholds, imbalance thresholds тощо)
- [10-backtesting.md](./10-backtesting.md) — як прогнати бот на історії
