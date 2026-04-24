---
name: 04-feature-engine
description: Архітектура модуля Feature Engine — обчислення фіч (imbalance, delta, absorption, spoof, micro pullback, zone context) з MarketSnapshot
type: project
---

# 04. FeatureEngine — обчислення фіч

## Відповідальність

Перетворює сирий `MarketSnapshot` (книга + стрічка + остання ціна) у структуровану `Features` — об'єкт із усіма числовими/булевими сигналами, які потім читає SetupDetector.

### Що робить:
- Обчислює book imbalance (топ-5, топ-10, зважений)
- Рахує delta у вікнах (500ms, 2s, 10s) і кумулятивну CVD
- Детектує aggressive bursts (різкий ринковий потік на bid/ask)
- Скорить absorption (велике поглинання великих маркет-ордерів пасивним лімітом)
- Скорить spoof-like behavior (поява/зникнення великих лімітів без виконання)
- Детектує micro pullback (відкат після агресії на N тіків)
- Розраховує кластерні фічі (PoC offset, stacked imbalance, finished/unfinished bar)
- Перевіряє zone context (чи snapshot всередині HTF POI: FVG/OB/VAH/VAL/POC)

### Що НЕ робить:
- НЕ зберігає стан між викликами — повністю stateless (всі вікна в TapeAnalyzer)
- НЕ приймає рішень "вхід / не вхід" (це SetupDetector)
- НЕ знає про сетапи, плечі, R, портфель
- НЕ ходить у мережу і не читає файли

---

## Входи / виходи

### Публічний API:

```python
class FeatureEngine:
    def __init__(self, config: FeatureConfig, zones: ZoneRegistry):
        self.config = config
        self.zones = zones  # HTF POI (FVG, OB, VAH/VAL/POC) — оновлюється повільним loop'ом

    def compute(self, snapshot: MarketSnapshot) -> Features:
        ...
```

### Вхід — `MarketSnapshot` (з 00-overview.md):

```python
@dataclass(frozen=True)
class MarketSnapshot:
    timestamp_ms: int
    symbol: str
    book: OrderBookState           # топ-N рівнів bid/ask
    tape: TapeWindowsState         # вже агреговані вікна 500ms / 2s / 10s
    last_price: float
    spread_ticks: int
```

### Вихід — `Features` (з 00-overview.md):

```python
@dataclass(frozen=True)
class Features:
    snapshot: MarketSnapshot

    # === Order book ===
    bid_ask_imbalance_5: float          # (Σbid_5 − Σask_5) / (Σbid_5 + Σask_5)
    bid_ask_imbalance_10: float
    weighted_imbalance: float           # з вагою 1/d від best (d — відстань у тіках)
    book_pressure_side: Literal['BID', 'ASK', 'NEUTRAL']

    # === Tape / flow ===
    delta_500ms: float
    delta_2s: float
    delta_10s: float
    cvd: float                          # кумулятивна за сесію
    aggressive_buy_burst: bool          # ринкові buy > X в 500ms-вікні
    aggressive_sell_burst: bool
    burst_size_usd: float | None        # розмір останнього burst у USD

    # === Behavioral ===
    absorption_score: float             # 0..1, see _score_absorption
    absorption_side: Literal['BID', 'ASK', 'NONE']
    spoof_score: float                  # 0..1
    spoof_side: Literal['BID', 'ASK', 'NONE']

    # === Micro structure ===
    micro_pullback: PullbackState | None

    # === Cluster (footprint) ===
    poc_offset_ticks: int               # +/− від midpoint бару
    poc_location: Literal['HIGH', 'MID', 'LOW']
    stacked_imbalance_long: bool        # >=3 послідовних ask-imbalance
    stacked_imbalance_short: bool
    bar_finished: bool                  # кластер бару закритий?
    bar_delta: float

    # === Zone context (HTF) ===
    in_htf_poi: bool
    htf_poi_type: Literal['FVG', 'OB', 'VAH', 'VAL', 'POC', 'IB_HIGH', 'IB_LOW'] | None
    htf_poi_side: Literal['SUPPORT', 'RESISTANCE'] | None
    distance_to_poi_ticks: int | None
```

### Допоміжні типи:

```python
@dataclass(frozen=True)
class PullbackState:
    direction: Literal['LONG_PULLBACK', 'SHORT_PULLBACK']
    depth_ticks: int                    # на скільки відкотились від екстремуму
    bars_in_pullback: int
    delta_during_pullback: float        # має бути слабкий контр-делта
```

---

## Залежності

**Читає:**
- `MarketSnapshot` (від OrderBookEngine + TapeAnalyzer через MarketDataGateway)
- `ZoneRegistry` (HTF зони — оновлюються раз на N хвилин окремим slow loop'ом)
- `FeatureConfig` (пороги burst, ваги imbalance, тощо)

**Не пише нікуди** — pure function. Повертає `Features` тому, хто викликає (зазвичай — SetupDetector).

**НЕ залежить від:**
- ExchangeClient (не ходить у мережу)
- DecisionEngine, RiskEngine, Execution (нижче по pipeline — anti-pattern)
- Глобального стану

---

## Стан

**Жодного.** Feature Engine — pure function.

Винятки:
- `self.config` — immutable налаштування (читається при старті)
- `self.zones` — `ZoneRegistry` — це stateful об'єкт, але FeatureEngine його тільки читає; апдейтить інший модуль (slow loop)

CVD як "стан сесії" живе в `TapeAnalyzer`, FeatureEngine лише дзеркалить останнє значення з `snapshot.tape.cvd`.

---

## Ключові алгоритми

### 1. Book imbalance (топ-N і зважена)

```python
def _imbalance(self, book: OrderBookState, levels: int) -> float:
    bid_sum = sum(level.size for level in book.bids[:levels])
    ask_sum = sum(level.size for level in book.asks[:levels])
    total = bid_sum + ask_sum
    return (bid_sum - ask_sum) / total if total > 0 else 0.0

def _weighted_imbalance(self, book: OrderBookState) -> float:
    # вага 1/(1 + distance_in_ticks) — чим ближче до best, тим більше ваги
    def side_weighted(levels):
        return sum(lvl.size / (1 + i) for i, lvl in enumerate(levels))
    bid_w = side_weighted(book.bids)
    ask_w = side_weighted(book.asks)
    total = bid_w + ask_w
    return (bid_w - ask_w) / total if total > 0 else 0.0
```

Поріг для `book_pressure_side`: `|imbalance_5| > 0.35`.

### 2. Delta і aggressive burst

`TapeWindowsState` уже містить готові вікна — тут лише класифікуємо:

```python
def _is_burst(self, window: TapeWindow, side: Literal['BUY', 'SELL']) -> tuple[bool, float | None]:
    threshold_usd = self.config.burst_threshold_usd  # напр. 50_000 за 500ms
    flow = window.buy_volume_usd if side == 'BUY' else window.sell_volume_usd
    return (flow >= threshold_usd, flow if flow >= threshold_usd else None)
```

### 3. Absorption score

Поглинання — велика хвиля маркет-продажів б'є по bid, але ціна не йде нижче, бо хтось великий стоїть лімітом і "з'їдає" всі ордери.

Сигнатура:
- велика sell-агресія в останньому вікні (`delta_500ms < −threshold`)
- ціна не пробила best bid більш ніж на 1 тік (`last_price >= best_bid`)
- розмір на best bid не зник або навіть виріс (`book.bids[0].size >= prev_best_bid_size`)

```python
def _score_absorption(self, snap: MarketSnapshot, prev_book_state: OrderBookState | None) -> tuple[float, str]:
    score_bid = 0.0
    if snap.tape.delta_500ms < -self.config.absorption_delta_threshold:
        if snap.last_price >= snap.book.bids[0].price - self.config.tick_size:
            if prev_book_state and snap.book.bids[0].size >= 0.9 * prev_book_state.bids[0].size:
                score_bid = min(1.0, abs(snap.tape.delta_500ms) / self.config.absorption_full_score_delta)
    # симетрично для ask
    ...
    if score_bid > score_ask:
        return score_bid, 'BID'
    elif score_ask > 0:
        return score_ask, 'ASK'
    return 0.0, 'NONE'
```

⚠️ `prev_book_state` — це маленьке відхилення від чистого stateless. Тримаємо в `FeatureEngine` ОДИН snapshot назад, не більше. Альтернатива — TapeAnalyzer пакує "delta_book_top" у свій стан.

### 4. Spoof-like score

Spoof — великий лімітник з'являється, висить кілька секунд, потім зникає БЕЗ виконання (відмінювали).

Детекція:
- з'явився рівень з розміром > `spoof_min_size`
- провисів < `spoof_max_lifetime_ms` (зазвичай 200..2000ms)
- зник не через trade, а через cancel (`size_decrease > recent_traded_at_price`)

Це знов вимагає стану — тримаємо `BookEvolutionTracker` всередині FeatureEngine (або краще — у дочірнього хелпера). Але стан мінімальний: ring buffer останніх N подій книги.

```python
@dataclass
class _BookEventRecord:
    timestamp_ms: int
    side: Literal['BID', 'ASK']
    price: float
    size_delta: float
    matched_trade_volume: float  # скільки з’їлось ринковими

def _score_spoof(self, snap: MarketSnapshot) -> tuple[float, str]:
    # повертає score 0..1 та сторону
    ...
```

### 5. Micro pullback

Після сильного імпульсу (5..10 тіків за <2 сек) — короткий відкат на 30..50% з малим контр-об'ємом. Це тригер "продовження".

```python
def _detect_pullback(self, snap: MarketSnapshot) -> PullbackState | None:
    # дивимось на price_path останніх 20 sec у TapeWindowsState
    impulse = self._find_recent_impulse(snap.tape.price_path, ticks_min=5, window_ms=2000)
    if not impulse:
        return None
    pullback_depth = (impulse.peak - snap.last_price) / self.config.tick_size
    if impulse.direction == 'UP':
        if 2 <= pullback_depth <= impulse.size_ticks * 0.6:
            counter_delta = snap.tape.delta_2s
            if counter_delta > -self.config.weak_counter_delta:  # контр-потік слабкий
                return PullbackState('LONG_PULLBACK', int(pullback_depth), ..., counter_delta)
    # симетрично для DOWN
    return None
```

### 6. Кластерні фічі

`bar_finished`, `poc_offset_ticks`, `stacked_imbalance_*` — береться з останнього бару `Footprint`, який тримає DataEngine (або OrderBookEngine у новій структурі). FeatureEngine просто читає і нормалізує:

```python
def _cluster_features(self, snap: MarketSnapshot, footprint: FootprintBar) -> dict:
    midpoint = (footprint.high + footprint.low) / 2
    poc_offset = round((footprint.poc_price - midpoint) / self.config.tick_size)
    if abs(poc_offset) <= 1:
        location = 'MID'
    elif poc_offset > 0:
        location = 'HIGH'
    else:
        location = 'LOW'
    stacked_long = self._has_stacked(footprint, side='ASK', min_count=3)
    stacked_short = self._has_stacked(footprint, side='BID', min_count=3)
    return {
        'poc_offset_ticks': poc_offset,
        'poc_location': location,
        'stacked_imbalance_long': stacked_long,
        'stacked_imbalance_short': stacked_short,
        'bar_delta': footprint.delta,
        'bar_finished': footprint.is_closed,
    }
```

### 7. Zone context

`ZoneRegistry` — список активних HTF POI з форматом:
```python
@dataclass
class HtfZone:
    type: Literal['FVG', 'OB', 'VAH', 'VAL', 'POC', 'IB_HIGH', 'IB_LOW']
    price_low: float
    price_high: float           # для рівнів-точок high == low
    side: Literal['SUPPORT', 'RESISTANCE']
    timeframe: str              # '15m', '1h', '4h', '1d'
    created_at_ms: int
    touched_count: int
```

```python
def _zone_context(self, snap: MarketSnapshot) -> dict:
    price = snap.last_price
    in_zone = self.zones.find_containing(price, symbol=snap.symbol)
    if in_zone:
        return {
            'in_htf_poi': True,
            'htf_poi_type': in_zone.type,
            'htf_poi_side': in_zone.side,
            'distance_to_poi_ticks': 0,
        }
    nearest = self.zones.find_nearest(price, symbol=snap.symbol, max_distance_ticks=20)
    if nearest:
        dist = round(abs(nearest.price_low - price) / self.config.tick_size)
        return {
            'in_htf_poi': False,
            'htf_poi_type': nearest.type,
            'htf_poi_side': nearest.side,
            'distance_to_poi_ticks': dist,
        }
    return {'in_htf_poi': False, 'htf_poi_type': None, 'htf_poi_side': None, 'distance_to_poi_ticks': None}
```

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Книга порожня (рідко, але буває під час масивного мува) | imbalance = 0, score_* = 0, не падати |
| `tape.windows` ще не наповнились (перші <2s після старту) | повертати `Features` з `aggressive_*_burst=False`, delta=0 — НЕ кидати виняток |
| `prev_book_state is None` (перший виклик) | absorption = 0, не пробувати порівнювати |
| ZoneRegistry порожній (не було часу побудувати HTF POI) | `in_htf_poi=False`, всі POI поля = None |
| Spread > N тіків (поганий ринок) | додати поле `spread_ticks` у Features (вже є в snapshot), SetupDetector сам відфільтрує |
| `footprint.is_closed=False` під час обчислення | проставити `bar_finished=False`, але `poc_offset_ticks` рахувати на поточний неповний бар |
| Час між snapshot'ами < 50ms | no-op, повернути попередній `Features` (опційна оптимізація) |

---

## Конфіги

Секція `feature_engine` у `settings.yaml`:

```yaml
feature_engine:
  tick_size_default: 0.1                # буде override per-symbol з exchangeInfo

  imbalance:
    levels_short: 5
    levels_long: 10
    pressure_threshold: 0.35

  burst:
    threshold_usd_500ms: 50000
    threshold_usd_2s: 150000

  absorption:
    delta_threshold: 30000              # USD за 500ms
    full_score_delta: 100000            # коли score = 1.0
    book_top_size_retention: 0.9        # топ не зменшився більш ніж на 10%

  spoof:
    min_size_usd: 80000
    max_lifetime_ms: 2000
    book_event_buffer: 200              # ring buffer розмір

  micro_pullback:
    impulse_min_ticks: 5
    impulse_window_ms: 2000
    pullback_max_fraction: 0.6
    weak_counter_delta: 20000

  cluster:
    stacked_min_count: 3
    poc_mid_threshold_ticks: 1

  zones:
    nearest_max_distance_ticks: 20
```

---

## Як тестувати

### Unit (pure functions — золото для тестів)
- `_imbalance()` — задаємо синтетичну книгу, перевіряємо точне число
- `_score_absorption()` — будуємо snapshot із потрібним delta + книгою, де топ не зменшився → очікуємо score > 0
- `_score_spoof()` — серія `_BookEventRecord` з появою-зникненням великого лімітника → score > 0
- `_detect_pullback()` — синтетичний `price_path` з імпульсом і відкотом 50% → очікуємо `PullbackState`
- `_cluster_features()` — фейковий `FootprintBar` з PoC у вершині → `poc_location='HIGH'`
- `_zone_context()` — задаємо `ZoneRegistry` з 1 FVG, snapshot всередині → `in_htf_poi=True`

### Property tests (hypothesis)
- Для будь-якого `MarketSnapshot` з валідною книгою: `−1 ≤ imbalance ≤ 1`
- Для будь-якого `Features`: `0 ≤ absorption_score ≤ 1`, `0 ≤ spoof_score ≤ 1`
- Симетрія: якщо інвертувати bid↔ask + buy↔sell у snapshot, то `imbalance` міняє знак, `delta` міняє знак, `absorption_side` змінюється на протилежну

### Integration
- Reads з реального записаного snapshot потоку (10 хв BTCUSDT з тестнету)
- Регресійний дамп `Features` → snapshot-test, що при тих самих вхідних виходи не змінились (якщо рефакторили — навмисне знесли snapshot)

### Performance
- `compute(snapshot)` має займати **<200µs** на середньому ноутбуці
- Бенч: 100k snapshot'ів через `compute` → перевірка p99 latency

---

## Відкриті питання / TODO

- [ ] Чи виносити `BookEvolutionTracker` (для absorption/spoof) у TapeAnalyzer? Тоді FeatureEngine стане 100% stateless.
- [ ] Чи треба окремий `RegimeAwareFeature`-аналог (наприклад поріг absorption різний у TRENDING_UP і CHOPPY)? Поки — ні, регіми застосовуються в DecisionEngine.
- [ ] Microsecond-precision timestamps для spoof детекції — чи переходити на `time.perf_counter_ns()` всередині модуля.
