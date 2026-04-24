---
name: 05-market-regime
description: Архітектура модуля Market Regime — класифікація стану ринку (8 регімів); працює в slow loop, впливає на DecisionEngine як фільтр і модифікатор порогів
type: project
---

# 05. MarketRegime — класифікація стану ринку

## Відповідальність

Визначає, в якому **"настрої"** ринок зараз: нормальний збалансований, трендовий, chop, high-vol тощо. Це slow-loop модуль — працює не на кожному тіку, а на **закриття бару** (1m/5m) + раз на 30s для новин/spread.

Результат — `RegimeState` — читається DecisionEngine як:
1. **Фільтр**: деякі сетапи дискваліфікуються в певних регімах (наприклад, MICRO_PULLBACK не брати в CHOPPY)
2. **Модифікатор порогів**: score-threshold у DecisionEngine залежить від регіму (у HIGH_VOL піднімаємо планку)
3. **Гейт**: у NEWS_RISK і DISABLED — нові входи заборонені

### Що робить:
- Обчислює ATR, spread, range-expansion, OI-ratio, CVD-slope на закритих барах 1m/5m/15m
- Класифікує ринок у один з 8 регімів (+ впевненість 0..1)
- Тримає **хистерезис** — регім не флапає щосекунди (перемикається тільки якщо нова класифікація стабільна 3+ бари)
- Зчитує **календар новин** (опційно) — виставляє NEWS_RISK за X хв до і Y хв після high-impact події
- Публікує оновлений `RegimeState` підписникам (DecisionEngine)

### Що НЕ робить:
- НЕ приймає торгових рішень (лише "настроює" DecisionEngine)
- НЕ дивиться в hot loop (tape/book в моменті) — працює на готових барах
- НЕ зберігає історію сама — читає з OrderBookEngine `get_recent_footprints()`

---

## Входи / виходи

### Публічний API:

```python
class MarketRegime:
    def __init__(self, config: RegimeConfig,
                 ob_engine: OrderBookEngine,
                 tape: TapeAnalyzer,
                 news_calendar: NewsCalendar | None = None):
        ...

    # === Lifecycle ===
    async def start(self) -> None            # запускає slow loop
    async def stop(self) -> None

    # === Query ===
    def get_regime(self, symbol: str) -> RegimeState
    def is_trading_allowed(self, symbol: str) -> bool   # shortcut: regime != DISABLED, NEWS_RISK

    # === Передплата ===
    def on_regime_change(self, callback: Callable[[RegimeChange], Awaitable[None]]) -> None
```

### Типи:

```python
class Regime(Enum):
    NORMAL_BALANCED = "normal_balanced"   # середні ATR, двосторонній потік
    TRENDING_UP = "trending_up"           # range-expansion, CVD росте, OI росте
    TRENDING_DOWN = "trending_down"
    CHOPPY = "choppy"                     # range flat, частий flip delta, багато wick-ів
    HIGH_VOL = "high_vol"                 # ATR >> avg, великі бари, великий spread
    LOW_LIQ = "low_liq"                   # тонка книга, великий spread, мало trade'ів
    NEWS_RISK = "news_risk"               # близько до high-impact події в календарі
    DISABLED = "disabled"                 # аварійний стоп (ручний або RiskEngine kill switch)

@dataclass(frozen=True)
class RegimeState:
    symbol: str
    regime: Regime
    confidence: float                     # 0..1 — наскільки чітко підтримується кластером метрик
    computed_at_ms: int

    # Метрики на основі яких класифіковано (для дебагу і журналу):
    atr_1m: float
    atr_5m: float
    atr_ratio_1m_vs_avg: float            # 1.0 = норма
    spread_ticks_avg: float
    range_expansion: float                # last_bar_range / avg_range_20
    cvd_slope_5m: float                   # нормалізований ∂CVD/∂t
    oi_delta_1h_pct: float | None         # % зміна OI за годину (якщо є)
    bar_direction_run: int                # +5 = 5 зелених поспіль; -3 = 3 червоних
    next_news_minutes: int | None         # хв до найближчої high-impact події

    disabled_reason: str | None           # причина DISABLED (якщо встановлено)

@dataclass(frozen=True)
class RegimeChange:
    symbol: str
    from_regime: Regime
    to_regime: Regime
    timestamp_ms: int
```

---

## Залежності

**Читає:**
- `OrderBookEngine.get_recent_footprints(symbol, tf, n)` — 20 останніх барів 1m і 5m
- `TapeAnalyzer.get_cvd(symbol)` — для slope
- `OrderBookEngine.get_book(symbol)` — для avg spread (семплується раз на 30s у slow loop)
- `NewsCalendar.next_high_impact(symbol)` — опційно, може бути None
- `RegimeConfig` — пороги, ваги, hysteresis counters

**Пише:**
- Власний кеш `RegimeState` per symbol
- Події `RegimeChange` підписникам (DecisionEngine, NotificationService)
- JournalLogger — кожна зміна регіму (для post-mortem)

**Читають його:**
- DecisionEngine — фільтр і модифікатор
- RiskEngine — може примусово встановити `DISABLED` (kill switch)
- NotificationService — алерт на зміну в HIGH_VOL, NEWS_RISK, DISABLED

**НЕ залежить від:**
- FeatureEngine (це hot-loop; regime повільний)
- SetupDetector, Execution

---

## Стан

```python
@dataclass
class _RegimeCache:
    current: RegimeState
    pending_candidate: Regime | None      # кандидат на перехід, ще не підтверджений
    pending_count: int                    # скільки барів поспіль ми бачимо кандидата
    history: deque[RegimeState]           # останні 100 станів (для аналізу)
```

Стан у пам'яті. Рестарт = перерахунок на першому закритому барі після старту (до цього — дефолт `NORMAL_BALANCED` з `confidence=0`).

---

## Ключові алгоритми

### 1. Основний цикл (slow loop)

```python
async def _regime_loop(self, symbol: str):
    while not self._shutdown:
        try:
            new_state = self._compute_regime(symbol)
            self._apply_hysteresis(symbol, new_state)
        except Exception as e:
            logger.exception(f"regime compute failed: {e}")
        await asyncio.sleep(self.config.compute_interval_sec)  # 30s
```

Додатково підписуємось на `ob_engine.on_bar_close(tf='1m')` — тоді `_compute_regime` викликається також на кожне закриття 1m-бару (частіше ніж 30s у активний час).

### 2. Обчислення метрик

```python
def _compute_regime(self, symbol: str) -> RegimeState:
    bars_1m = self._ob.get_recent_footprints(symbol, '1m', 20)
    bars_5m = self._ob.get_recent_footprints(symbol, '5m', 10)

    atr_1m = self._atr([b.high - b.low for b in bars_1m], period=14)
    atr_5m = self._atr([b.high - b.low for b in bars_5m], period=7)
    avg_atr_1m = self._historical_avg_atr(symbol, '1m')  # з тривалого буфера або конфігу
    atr_ratio = atr_1m / avg_atr_1m if avg_atr_1m else 1.0

    book = self._ob.get_book(symbol)
    spread_ticks = (book.asks[0].price - book.bids[0].price) / self._tick_size(symbol) \
                   if book.asks and book.bids else 999

    last_range = bars_1m[-1].high - bars_1m[-1].low
    avg_range = mean((b.high - b.low) for b in bars_1m[:-1]) or 1e-9
    range_expansion = last_range / avg_range

    cvd_slope = self._compute_cvd_slope(symbol, bars_5m)  # лінійна регресія
    direction_run = self._direction_run(bars_1m)          # +N зелених або -N червоних

    news_min = self._news.next_high_impact(symbol) if self._news else None

    regime, confidence, reason = self._classify(
        atr_ratio=atr_ratio,
        spread_ticks=spread_ticks,
        range_expansion=range_expansion,
        cvd_slope=cvd_slope,
        direction_run=direction_run,
        next_news_min=news_min,
    )

    return RegimeState(
        symbol=symbol, regime=regime, confidence=confidence,
        computed_at_ms=self._now_ms(),
        atr_1m=atr_1m, atr_5m=atr_5m, atr_ratio_1m_vs_avg=atr_ratio,
        spread_ticks_avg=spread_ticks, range_expansion=range_expansion,
        cvd_slope_5m=cvd_slope, oi_delta_1h_pct=None,
        bar_direction_run=direction_run,
        next_news_minutes=news_min,
        disabled_reason=reason if regime == Regime.DISABLED else None,
    )
```

### 3. Класифікація (правила у порядку пріоритету)

```python
def _classify(self, *, atr_ratio, spread_ticks, range_expansion,
              cvd_slope, direction_run, next_news_min) -> tuple[Regime, float, str | None]:
    c = self.config

    # 1. DISABLED — викликається зовні (RiskEngine). Тут — тільки якщо набрались критичні умови
    if self._manual_disabled.get(symbol):
        return Regime.DISABLED, 1.0, self._manual_disabled_reason

    # 2. NEWS_RISK
    if next_news_min is not None and -c.news_after_min <= next_news_min <= c.news_before_min:
        return Regime.NEWS_RISK, 1.0, None

    # 3. LOW_LIQ — перед HIGH_VOL, бо тонка книга часто супроводжує expensive spread
    if spread_ticks > c.low_liq_spread_ticks:
        return Regime.LOW_LIQ, _conf_linear(spread_ticks, c.low_liq_spread_ticks, c.low_liq_spread_ticks * 2), None

    # 4. HIGH_VOL
    if atr_ratio > c.high_vol_atr_ratio:
        return Regime.HIGH_VOL, _conf_linear(atr_ratio, c.high_vol_atr_ratio, c.high_vol_atr_ratio * 1.5), None

    # 5. TRENDING — потрібно ТРИ умови разом
    trending_up = (
        direction_run >= c.trend_min_run and
        cvd_slope > c.trend_cvd_slope and
        range_expansion > c.trend_range_expansion
    )
    trending_down = (
        direction_run <= -c.trend_min_run and
        cvd_slope < -c.trend_cvd_slope and
        range_expansion > c.trend_range_expansion
    )
    if trending_up:
        return Regime.TRENDING_UP, min(1.0, (abs(direction_run) - c.trend_min_run + 1) / 3), None
    if trending_down:
        return Regime.TRENDING_DOWN, min(1.0, (abs(direction_run) - c.trend_min_run + 1) / 3), None

    # 6. CHOPPY — ранжуємо "флап": delta змінює знак часто, range малий
    if self._is_choppy(cvd_slope, range_expansion, direction_run):
        return Regime.CHOPPY, 0.7, None

    # 7. Default
    return Regime.NORMAL_BALANCED, 0.6, None
```

### 4. Hysteresis (антифлап)

Регім не повинен перемикатися кожен 30s. Тільки якщо нова класифікація **тримається N разів поспіль**, ми перемикаємось. Виняток: `NEWS_RISK`, `DISABLED`, `HIGH_VOL` — миттєво, бо це захисні стани.

```python
def _apply_hysteresis(self, symbol: str, new_state: RegimeState):
    cache = self._caches[symbol]
    current_regime = cache.current.regime
    new_regime = new_state.regime

    INSTANT_REGIMES = {Regime.NEWS_RISK, Regime.DISABLED, Regime.HIGH_VOL}

    if new_regime == current_regime:
        cache.pending_candidate = None
        cache.pending_count = 0
        cache.current = new_state  # оновлюємо метрики
        return

    if new_regime in INSTANT_REGIMES or current_regime in INSTANT_REGIMES:
        self._commit_change(symbol, cache.current.regime, new_regime, new_state)
        return

    # Потрібне підтвердження
    if cache.pending_candidate == new_regime:
        cache.pending_count += 1
    else:
        cache.pending_candidate = new_regime
        cache.pending_count = 1

    if cache.pending_count >= self.config.hysteresis_bars:
        self._commit_change(symbol, current_regime, new_regime, new_state)

def _commit_change(self, symbol, from_regime, to_regime, state):
    cache = self._caches[symbol]
    cache.current = state
    cache.history.append(state)
    cache.pending_candidate = None
    cache.pending_count = 0
    change = RegimeChange(symbol, from_regime, to_regime, state.computed_at_ms)
    for cb in self._change_callbacks:
        asyncio.create_task(cb(change))
    logger.info(f"regime_change {symbol}: {from_regime} → {to_regime} (conf={state.confidence:.2f})")
```

### 5. Kill switch (DISABLED)

RiskEngine і оператор можуть ВИМУШЕНО виставити DISABLED:

```python
def force_disabled(self, symbol: str, reason: str):
    self._manual_disabled[symbol] = True
    self._manual_disabled_reason = reason
    cache = self._caches.get(symbol)
    if cache and cache.current.regime != Regime.DISABLED:
        new_state = replace(cache.current, regime=Regime.DISABLED,
                            confidence=1.0, disabled_reason=reason)
        self._commit_change(symbol, cache.current.regime, Regime.DISABLED, new_state)

def clear_disabled(self, symbol: str):
    self._manual_disabled[symbol] = False
    # наступний _compute_regime перерахує і може перевести в інший стан
```

---

## Взаємодія з DecisionEngine

Табличка впливу регіму (DecisionEngine читає її з config, не hardcode):

| Setup \ Regime | NORMAL | TRENDING↑ | TRENDING↓ | CHOPPY | HIGH_VOL | LOW_LIQ | NEWS_RISK | DISABLED |
|---|---|---|---|---|---|---|---|---|
| ABSORPTION_REVERSAL | ✓ | threshold +0.1 (contr-trend) | threshold +0.1 | ✓ | threshold +0.2 | ✗ | ✗ | ✗ |
| IMBALANCE_CONTINUATION_LONG | ✓ | ✓ bonus | ✗ | threshold +0.2 | threshold +0.1 | ✗ | ✗ | ✗ |
| IMBALANCE_CONTINUATION_SHORT | ✓ | ✗ | ✓ bonus | threshold +0.2 | threshold +0.1 | ✗ | ✗ | ✗ |
| SPOOF_FAIL_FADE | ✓ | ✓ | ✓ | ✓ | threshold +0.2 | ✗ | ✗ | ✗ |
| MICRO_PULLBACK_AFTER_AGGRESSION | ✓ | ✓ bonus | ✓ bonus | ✗ | threshold +0.2 | ✗ | ✗ | ✗ |

"✗" = сетап заборонено в цьому регімі.
"bonus" = додатковий +0.1 до score (попутний вітер).

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Перші <20 барів 1m після старту | `regime=NORMAL_BALANCED`, `confidence=0` — DecisionEngine має розуміти що це warmup |
| avg_atr_1m невідомий (недостатньо історії) | Взяти дефолт з конфігу (per-symbol calibration) |
| NewsCalendar недоступний (API лежить) | Просто не виставляти NEWS_RISK — працюємо без цього фільтра + WARNING у лог |
| CVD unreliable (tape gap) | Пропускати CVD-slope у класифікації, але не падати — `trending_*` стає менш ймовірним |
| Різкий перехід HIGH_VOL ↔ NORMAL кілька разів за хвилину | Антифлап тут не спрацьовує (HIGH_VOL instant), але є guard: не змінюватись менш ніж за 5s (debounce) |
| DISABLED був встановлений, потім clear — нова класифікація одразу | Так, clear просто скидає manual flag, наступний compute переведе у відповідний стан |

---

## Конфіги

```yaml
market_regime:
  compute_interval_sec: 30          # slow loop; plus on bar_close 1m

  hysteresis_bars: 3                # скільки разів поспіль треба побачити новий регім
  debounce_min_seconds: 5

  high_vol:
    atr_ratio_threshold: 1.8        # atr_1m / avg_atr_1m

  low_liq:
    spread_ticks: 4                 # >4 тіків спред = LOW_LIQ

  trending:
    min_run: 4                      # 4+ бари поспіль у напрямку
    cvd_slope: 0.5                  # нормалізований поріг
    range_expansion: 1.3            # > 1.3× avg range

  choppy:
    cvd_flip_count: 4               # флипів за 20 барів
    max_range_expansion: 0.8

  news:
    enabled: true
    before_minutes: 5               # NEWS_RISK за 5 хв до події
    after_minutes: 10               # і 10 хв після
    sources: ['forexfactory_high_impact']  # поки заглушка

  atr:
    calibration_lookback_bars: 2000 # скільки барів на історичний avg_atr
```

---

## Як тестувати

### Unit (ключовий — стабільність класифікації)
- `_classify()` з фіксованими метриками для кожного з 8 регімів → перевірка правильної гілки
- Hysteresis: new=TRENDING_UP, hysteresis_bars=3 → перемикання тільки після 3 викликів; якщо кандидат змінився на NORMAL до 3-го — counter скидається
- INSTANT: HIGH_VOL спрацьовує з першого разу
- `force_disabled` → стан переходить одразу; `clear_disabled` + compute → повернення в обчислений регім

### Integration
- Replay 1 години історії з відомими трендовими, бічними і волатильними ділянками → ручний sanity-check класифікацій
- Порівняти з людською експертною розміткою (оператор дивиться на графік і каже "це тренд", ми звіряємось)

### Calibration (не unit-тест, а процес)
- Історичні `avg_atr_1m` для BTCUSDT, ETHUSDT — ПЕРЕД запуском на live треба зібрати ≥30 днів історії і зберегти в `regime/calibration.yaml`. Без цього `atr_ratio` безглуздий.
