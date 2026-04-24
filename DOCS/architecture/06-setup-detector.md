---
name: 06-setup-detector
description: Архітектура модуля Setup Detector — stateless перевірка фіч на 4-5 скальпінг-сетапів, кожен повертає SetupCandidate із trigger/confirmation/invalidation
type: project
---

# 06. SetupDetector — виявлення сетапів

## Відповідальність

Бере `Features` від FeatureEngine і перевіряє, чи **якийсь з відомих сетапів** проявляється зараз. Повертає **список кандидатів** (може бути порожній). Кожен кандидат — це конкретна торгова ідея з умовами trigger/confirmation/invalidation, entry/stop hint і setup-type тегом.

Модуль **повністю stateless** — кожен виклик незалежний. Набір сетапів декларативний: кожен сетап — окремий клас/функція з чітким контрактом `check(features) → SetupCandidate | None`.

### Сетапи першої черги (MVP):

1. **ABSORPTION_REVERSAL_LONG/SHORT** — великий sell-потік поглинається лімітом на bid → розворот вгору (і дзеркально)
2. **IMBALANCE_CONTINUATION_LONG/SHORT** — 3+ stacked imbalance в бік тренду + поточний бар підтверджує
3. **SPOOF_FAIL_FADE_LONG/SHORT** — великий лімітник на ask провисів і зник без виконання → fade (fade = ставити проти)
4. **MICRO_PULLBACK_AFTER_AGGRESSION_LONG/SHORT** — сильний імпульс → короткий відкот зі слабким контр-потоком → продовження
5. (опційно) **SFP_LIQ_SWEEP_LONG/SHORT** — sweep низька/верху попереднього бару з миттєвим рикошетом

### Що робить:
- Приймає `Features`, віддає `list[SetupCandidate]` (0..N)
- Кожен детектор — окрема функція, ізольована від інших
- Генерує entry_price, stop_price hint (remporary — фінальні рівні вирішить DecisionEngine з урахуванням slippage/spread)
- Формує список `invalidation_conditions` — умови під якими кандидат стає недійсним ще до входу
- Тегує setup_type (enum), щоб DecisionEngine і RiskEngine могли диференціювати поведінку

### Що НЕ робить:
- НЕ рахує position size (RiskEngine)
- НЕ вирішує "чи торгувати" (DecisionEngine)
- НЕ дивиться на свій попередній стан (stateless)
- НЕ звертається до біржі
- НЕ фільтрує за регімом (це DecisionEngine)

---

## Входи / виходи

### Публічний API:

```python
class SetupDetector:
    def __init__(self, config: SetupConfig):
        self.config = config
        self._detectors: list[Callable[[Features], SetupCandidate | None]] = [
            self._check_absorption_reversal_long,
            self._check_absorption_reversal_short,
            self._check_imbalance_continuation_long,
            self._check_imbalance_continuation_short,
            self._check_spoof_fail_fade_long,
            self._check_spoof_fail_fade_short,
            self._check_micro_pullback_long,
            self._check_micro_pullback_short,
        ]

    def detect(self, features: Features) -> list[SetupCandidate]:
        return [c for det in self._detectors if (c := det(features))]
```

### Типи:

```python
class SetupType(Enum):
    ABSORPTION_REVERSAL_LONG = "absorption_reversal_long"
    ABSORPTION_REVERSAL_SHORT = "absorption_reversal_short"
    IMBALANCE_CONTINUATION_LONG = "imbalance_continuation_long"
    IMBALANCE_CONTINUATION_SHORT = "imbalance_continuation_short"
    SPOOF_FAIL_FADE_LONG = "spoof_fail_fade_long"
    SPOOF_FAIL_FADE_SHORT = "spoof_fail_fade_short"
    MICRO_PULLBACK_LONG = "micro_pullback_long"
    MICRO_PULLBACK_SHORT = "micro_pullback_short"

@dataclass(frozen=True)
class SetupCandidate:
    setup_type: SetupType
    direction: Literal['LONG', 'SHORT']
    timestamp_ms: int
    symbol: str

    # Торгові параметри (hint — DecisionEngine може скоригувати)
    entry_price_hint: float
    stop_price_hint: float                    # визначає R
    stop_distance_ticks: int

    # Фактори, з яких зібрано сетап (для DecisionEngine score)
    trigger_factors: dict[str, float]         # ім'я → значення (для дебагу / score weights)
    confirmation_factors: dict[str, float]

    # Якщо щось із цього стане True — кандидат дискваліфіковано ще до входу
    invalidation_conditions: list[InvalidationCondition]

    # Прив'язка до HTF (DecisionEngine бачить, чи ми в POI)
    in_htf_poi: bool
    htf_poi_type: str | None

    # Діагностика — які фічі використовувались (згорнутий знімок)
    features_hash: str                        # sha of Features для ідемпотентності

@dataclass(frozen=True)
class InvalidationCondition:
    description: str
    kind: Literal['PRICE_BEYOND', 'OPPOSITE_DELTA', 'TIME_EXPIRED', 'BOOK_TURNS']
    price_level: float | None = None
    delta_threshold: float | None = None
    expires_at_ms: int | None = None
```

---

## Залежності

**Читає:**
- `Features` (аргумент методу)
- `SetupConfig` — пороги кожного сетапу

**Не пише нікуди** — pure function. Сам **не** викликає `Journal.log(...)`. Якщо потрібно
журналити факт появи кандидата (event `SETUP_CANDIDATE_GENERATED`) — це робить Orchestrator
на основі значення, яке повернув `detect()`. Це зберігає модуль детермінованим і легко-тестованим.

**Читають його:**
- Orchestrator — викликає `detect(features)` на кожен тік і логує результати в Journal
- DecisionEngine — основний споживач (отримує кандидатів від Orchestrator, не напряму)

**НЕ залежить від:**
- OrderBookEngine, TapeAnalyzer, FeatureEngine (їх викликають ВИЩЕ за нього — сюди приходить готовий `Features`)
- Регіму, RiskEngine, Execution

---

## Стан

**Нема.** Pure stateless. Кожен виклик `detect(features)` незалежний.

Причини:
- Легко unit-тестити (заданий Features → очікуваний список кандидатів)
- Можна паралелити по символах
- Перезапуск нічого не ламає — "знання про минулі сетапи" тримає Journal, не цей модуль

---

## Ключові алгоритми

### 1. ABSORPTION_REVERSAL_LONG

**Ідея**: ринок активно продає, але лімітний покупець на best bid не пускає ціну нижче.

```python
def _check_absorption_reversal_long(self, f: Features) -> SetupCandidate | None:
    c = self.config.absorption

    # Триггер
    if f.absorption_score < c.min_score: return None
    if f.absorption_side != 'BID':       return None
    if f.delta_500ms > -c.min_sell_pressure: return None  # має бути серйозний продаж
    if f.spread_ticks > c.max_spread_ticks: return None

    # Конфірм (хоча б один)
    confirmations = {}
    if f.stacked_imbalance_long:
        confirmations['stacked_imbalance'] = 1.0
    if f.delta_2s > c.confirm_recovery_delta:
        confirmations['delta_recovery'] = f.delta_2s / c.confirm_recovery_delta
    if f.weighted_imbalance > c.confirm_book_pressure:
        confirmations['book_pressure_bid'] = f.weighted_imbalance
    if not confirmations:
        return None

    # Entry / stop hint
    best_bid = f.snapshot.book.bids[0].price
    tick = c.tick_size
    entry = best_bid + tick                          # зайти вище best bid (taker або ioc limit)
    stop = best_bid - c.stop_buffer_ticks * tick     # під best bid + buffer
    stop_distance = round((entry - stop) / tick)

    return SetupCandidate(
        setup_type=SetupType.ABSORPTION_REVERSAL_LONG,
        direction='LONG',
        timestamp_ms=f.snapshot.timestamp_ms,
        symbol=f.snapshot.symbol,
        entry_price_hint=entry,
        stop_price_hint=stop,
        stop_distance_ticks=stop_distance,
        trigger_factors={
            'absorption_score': f.absorption_score,
            'sell_pressure_delta': abs(f.delta_500ms),
        },
        confirmation_factors=confirmations,
        invalidation_conditions=[
            InvalidationCondition("best_bid breaks down", 'PRICE_BEYOND', price_level=stop),
            InvalidationCondition("opposite sell burst >2s",
                                  'OPPOSITE_DELTA', delta_threshold=-c.invalidation_counter_delta),
            InvalidationCondition("expires after 8 sec",
                                  'TIME_EXPIRED',
                                  expires_at_ms=f.snapshot.timestamp_ms + 8000),
        ],
        in_htf_poi=f.in_htf_poi and f.htf_poi_side == 'SUPPORT',
        htf_poi_type=f.htf_poi_type,
        features_hash=_hash_features(f),
    )
```

### 2. IMBALANCE_CONTINUATION_LONG

**Ідея**: в останньому барі (або двох) кілька stacked ask-imbalance → покупці ламають рівні → продовження вгору на micro-pullback чи ретест.

```python
def _check_imbalance_continuation_long(self, f: Features) -> SetupCandidate | None:
    c = self.config.imbalance_cont

    if not f.stacked_imbalance_long: return None
    if f.delta_2s < c.min_buy_pressure: return None
    if f.bid_ask_imbalance_5 < c.min_book_imbalance: return None

    # Ми хочемо вступати на micro pullback, не на вершині імпульсу
    pb = f.micro_pullback
    if pb is None or pb.direction != 'LONG_PULLBACK':
        return None
    if pb.depth_ticks < c.pullback_min_depth:
        return None

    impulse_high = _estimate_recent_impulse_high(f)
    tick = c.tick_size
    entry = f.snapshot.last_price + tick
    stop = pb_low := (impulse_high - pb.depth_ticks * tick) - c.stop_buffer_ticks * tick
    stop_distance = round((entry - stop) / tick)

    return SetupCandidate(
        setup_type=SetupType.IMBALANCE_CONTINUATION_LONG,
        direction='LONG',
        timestamp_ms=f.snapshot.timestamp_ms,
        symbol=f.snapshot.symbol,
        entry_price_hint=entry,
        stop_price_hint=stop,
        stop_distance_ticks=stop_distance,
        trigger_factors={
            'stacked_imbalance_long': 1.0,
            'buy_pressure_delta': f.delta_2s,
            'book_imbalance': f.bid_ask_imbalance_5,
            'pullback_depth_ticks': float(pb.depth_ticks),
        },
        confirmation_factors={
            'weak_counter_delta': -pb.delta_during_pullback,
        },
        invalidation_conditions=[
            InvalidationCondition("pullback breaks below impulse start",
                                  'PRICE_BEYOND', price_level=stop),
            InvalidationCondition("opposing absorption appears on ask",
                                  'OPPOSITE_DELTA', delta_threshold=-c.opposing_delta),
            InvalidationCondition("5s time limit for entry window",
                                  'TIME_EXPIRED',
                                  expires_at_ms=f.snapshot.timestamp_ms + 5000),
        ],
        in_htf_poi=f.in_htf_poi and f.htf_poi_side == 'SUPPORT',
        htf_poi_type=f.htf_poi_type,
        features_hash=_hash_features(f),
    )
```

### 3. SPOOF_FAIL_FADE_SHORT

**Ідея**: на ask з'являється великий лімітник, висить 1-2 сек, зникає БЕЗ виконання → "обман", реальний інтерес інший. Fade = ставити проти видимого тиску.

```python
def _check_spoof_fail_fade_short(self, f: Features) -> SetupCandidate | None:
    c = self.config.spoof

    if f.spoof_score < c.min_score:  return None
    if f.spoof_side != 'BID':        return None   # spoof НА bid → фейкова підтримка → short fade
    # (Увага: spoof_side означає СТОРОНУ де був спуфер; тут BID-spoofer говорив "я тримаю" — але відмінив, значить підтримки нема → shorts)

    # Підтвердження: ринок одразу починає давити вниз
    if f.delta_500ms > -c.confirm_sell_pressure:
        return None

    tick = c.tick_size
    best_ask = f.snapshot.book.asks[0].price
    entry = best_ask - tick
    stop = best_ask + c.stop_buffer_ticks * tick
    stop_distance = round((stop - entry) / tick)

    return SetupCandidate(
        setup_type=SetupType.SPOOF_FAIL_FADE_SHORT,
        direction='SHORT',
        timestamp_ms=f.snapshot.timestamp_ms,
        symbol=f.snapshot.symbol,
        entry_price_hint=entry,
        stop_price_hint=stop,
        stop_distance_ticks=stop_distance,
        trigger_factors={
            'spoof_score': f.spoof_score,
            'sell_follow_through': abs(f.delta_500ms),
        },
        confirmation_factors={
            'book_pressure_ask': max(0.0, -f.weighted_imbalance),
        },
        invalidation_conditions=[
            InvalidationCondition("best ask breaks up", 'PRICE_BEYOND', price_level=stop),
            InvalidationCondition("counter buy burst",
                                  'OPPOSITE_DELTA', delta_threshold=c.invalidation_counter_delta),
            InvalidationCondition("5s expiry",
                                  'TIME_EXPIRED',
                                  expires_at_ms=f.snapshot.timestamp_ms + 5000),
        ],
        in_htf_poi=f.in_htf_poi and f.htf_poi_side == 'RESISTANCE',
        htf_poi_type=f.htf_poi_type,
        features_hash=_hash_features(f),
    )
```

### 4. MICRO_PULLBACK_AFTER_AGGRESSION_LONG

**Ідея**: після агресивного buy-burst ціна робить коротку паузу/пуллбек з МАЛИМ контр-об'ємом — значить продавці слабкі, очікуємо продовження.

```python
def _check_micro_pullback_long(self, f: Features) -> SetupCandidate | None:
    c = self.config.micro_pullback
    pb = f.micro_pullback
    if pb is None or pb.direction != 'LONG_PULLBACK': return None
    if not f.aggressive_buy_burst and f.burst_size_usd is None:
        # Перевіряємо, чи ДО pullback'у був burst (FeatureEngine це заклав у PullbackState)
        return None
    if pb.depth_ticks < c.min_depth: return None
    if pb.delta_during_pullback < -c.max_counter_delta: return None  # занадто сильний контр-потік
    ...  # entry/stop як у IMBALANCE_CONTINUATION_LONG, але з іншими порогами

    return SetupCandidate(setup_type=SetupType.MICRO_PULLBACK_LONG, ...)
```

### 5. Загальний скелет тесту доречності

Усі детектори слідують одній структурі:

```
1. Hard guards → None (spread занадто великий, flag "CVD unreliable", тощо)
2. Trigger check → None якщо основний сигнал відсутній
3. Confirmation check → None якщо немає підтверджень
4. Обчислити entry/stop hints
5. Зібрати invalidation conditions
6. Повернути SetupCandidate
```

### 6. HTF POI як МОДИФІКАТОР, не гейт

`in_htf_poi` не є обовʼязковим для всіх сетапів. Це **бонус** для DecisionEngine — сетапи у HTF POI на правильній стороні отримують вищий score. SetupDetector просто прокидає цей прапор далі.

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Одночасно спрацьовують `ABSORPTION_REVERSAL_LONG` і `IMBALANCE_CONTINUATION_SHORT` | Повертаємо ОБИДВА кандидати — суперечність розрулить DecisionEngine (вони рідко обидва проходять поріг score) |
| `Features.cvd_reliable == False` | Сетапи що сильно залежать від CVD (наприклад imbalance continuation) дискваліфікуються; ті що дивляться лише на 500ms delta — працюють |
| Книга порожня на одній стороні | Hard guard → None для всіх сетапів (небезпечно торгувати) |
| Spread ≥ stop_distance | Hard guard → None (у stop заложено менше відстані ніж шум) |
| Первинний запуск, `Features` з warmup-нулями | Жоден детектор не спрацює (усі trigger-пороги вимагають ненульових значень) |

---

## Конфіги

```yaml
setup_detector:
  tick_size: 0.1                              # per-symbol override

  absorption:
    min_score: 0.6
    min_sell_pressure: 30000                  # USD за 500ms
    max_spread_ticks: 2
    confirm_recovery_delta: 20000
    confirm_book_pressure: 0.35
    stop_buffer_ticks: 2
    invalidation_counter_delta: 50000

  imbalance_cont:
    min_buy_pressure: 40000
    min_book_imbalance: 0.3
    pullback_min_depth: 3
    stop_buffer_ticks: 2
    opposing_delta: 60000

  spoof:
    min_score: 0.5
    confirm_sell_pressure: 25000
    stop_buffer_ticks: 2
    invalidation_counter_delta: 40000

  micro_pullback:
    min_depth: 2
    max_counter_delta: 15000
    stop_buffer_ticks: 2
```

---

## Як тестувати

### Unit (основний спосіб — "input → output")

Для кожного сетапу — набір тестів:
- **Positive**: зібрати `Features` з ідеальними умовами → очікуємо кандидата з правильним `setup_type` і напрямком
- **Negative guard trigger**: послабити одну тригерну умову → `None`
- **Negative guard confirmation**: прибрати всі конфірми → `None`
- **Invalidation structure**: у результаті має бути ≥2 InvalidationCondition включно з `TIME_EXPIRED` і `PRICE_BEYOND`

### Property-based (hypothesis)
- Для будь-якого `Features`: кожен `SetupCandidate.stop_distance_ticks > 0`
- `entry_price_hint` завжди або на стороні snapshot.last_price (LONG: entry ≥ last_price; SHORT: entry ≤ last_price)
- `setup_type.name` завжди закінчується на `_LONG` або `_SHORT`, і відповідає `direction`

### Integration
- Replay історії → підрахувати кількість кандидатів кожного типу за день
- Ручний sanity-check: 5 випадкових кандидатів з кожного типу на графіку → перевірити "це ж дійсно виглядає як absorption/imbalance/spoof?"

### Regression
- Snapshot-тест: для 20 зафіксованих `Features` JSON-ів — очікуваний `list[SetupCandidate]`. При рефакторі — перевіряємо що зміни навмисні.
