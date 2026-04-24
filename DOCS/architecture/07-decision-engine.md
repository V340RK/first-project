---
name: 07-decision-engine
description: Архітектура модуля Decision Engine — score-based відбір кандидатів з позитивними і негативними вагами, фільтр за регімом, перевірка кулдауну; передає TradePlan у RiskEngine
type: project
---

# 07. DecisionEngine — рішення "торгувати чи ні"

## Відповідальність

**Серце стратегії.** Бере `list[SetupCandidate]` від SetupDetector, поточний `RegimeState` і `RiskState`, обчислює **score** для кожного кандидата, і за результатом або пропускає кандидат далі (у RiskEngine як `TradePlan`-proposal), або відхиляє.

Це місце де зʼєднуються всі попередні шари в одне рішення. Усі магічні ваги, threshold-и, регіум-модифікатори — тут.

### Що робить:
- Фільтрує кандидатів за регімом (див. таблицю у [05-market-regime.md](05-market-regime.md))
- Рахує **composite score** для кожного кандидата: позитивні ваги (confirmation factors, HTF POI, exp rolling) + **негативні** (adverse regime, low expectancy, нещодавня втрата)
- Порівнює з адаптивним `score_threshold` (підвищується у HIGH_VOL, після loss streak, при низькій expectancy сетапу)
- Перевіряє кулдаун per-setup і per-symbol (не частіше ніж раз на N секунд)
- Блокує дубль-ентрі (якщо вже відкрита позиція по цьому символу)
- Вибирає **одного** переможця якщо кілька кандидатів пройшли (найвищий score)
- Формує proposal `TradePlan` (entry, stop, TP-levels, invalidations) — далі RiskEngine накладе розмір і верхній veto

### Що НЕ робить:
- НЕ рахує розмір позиції (RiskEngine)
- НЕ перевіряє денний/місячний drawdown (RiskEngine)
- НЕ виставляє ордери (Execution)
- НЕ модифікує Features / setups (тільки оцінює)
- НЕ персистує "відхилених" — це journal-ить нижчий шар

---

## Входи / виходи

### Публічний API:

```python
class DecisionEngine:
    def __init__(self, config: DecisionConfig,
                 regime: MarketRegime,
                 risk: RiskEngine,
                 expectancy: ExpectancyTracker,
                 position_mgr: PositionManager,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        ...

    def evaluate(self, candidates: list[SetupCandidate]) -> DecisionResult:
        """Hot-loop метод. Повертає або прийнятий TradePlan, або пояснення відмови."""
```

### Типи:

```python
@dataclass(frozen=True)
class TradePlan:
    """Proposal для RiskEngine. RiskEngine може його відкинути або змінити size."""
    candidate: SetupCandidate
    setup_type: SetupType
    direction: Literal['LONG', 'SHORT']
    symbol: str
    timestamp_ms: int

    entry_price: float
    stop_price: float
    tp1_price: float                    # 50% розміру (див. 12-playbook.md 8.5)
    tp2_price: float                    # 25%
    tp3_price: float                    # 25% з трейлом
    stop_distance_ticks: int

    score: float                        # 0..N (може бути > 1 при сильних конфірмах)
    score_threshold: float              # з яким порівнювали
    regime: Regime
    expectancy_multiplier: float

    invalidation_conditions: list[InvalidationCondition]
    time_stop_ms: int | None            # з конфігу setup'у; None = без time stop

    # Ще НЕ заповнено — це зробить RiskEngine
    position_size: float | None = None
    risk_usd: float | None = None
    risk_gate_passed: bool = False

@dataclass(frozen=True)
class DecisionResult:
    accepted: TradePlan | None
    rejected: list[RejectedCandidate]

@dataclass(frozen=True)
class RejectedCandidate:
    candidate: SetupCandidate
    reason: str
    score: float | None                 # None якщо відмовили до скорингу (фільтр)
    score_threshold: float | None
```

---

## Залежності

**Читає:**
- `MarketRegime.get_regime(symbol)` — поточний регім
- `RiskEngine.is_kill_switch_on()` — глобальний стоп
- `ExpectancyTracker.get(setup_type, symbol)` — rolling expectancy цього сетапу
- `PositionManager.has_open_position(symbol)` — дубль-ентрі
- `DecisionConfig` — ваги, threshold-и, кулдауни
- `clock()` — поточний час (інʼєкція для тестів)

**Пише:**
- Кеш кулдаунів `_last_entry_ms: dict[(symbol, SetupType), int]`
- Виключно НЕ шле ордери. Передає `TradePlan` далі через return — RiskEngine вирішує що далі.

**Читають його:**
- RiskEngine — отримує `TradePlan`, накладає risk-filters і size
- JournalLogger — пише кожну `DecisionResult` (і accepted, і всі rejected) для post-mortem

**НЕ залежить від:**
- SetupDetector, FeatureEngine (отримує готовий вхід)
- Execution

---

## Стан

```python
@dataclass
class _DecisionState:
    last_entry_ms: dict[tuple[str, SetupType], int]   # кулдаун per (symbol, setup_type)
    last_symbol_entry_ms: dict[str, int]              # загальний кулдаун per symbol
    recent_rejections: deque[RejectedCandidate]       # maxlen=500, тільки для дебагу/UI
    loss_streak: int                                  # онов ExpectancyTracker → RiskEngine
```

Кулдауни тримаються в пам'яті. При рестарті скидаються (це плюс — бот не пропустить перший сигнал через stale кулдаун після падіння).

---

## Ключові алгоритми

### 1. Основний flow

```python
def evaluate(self, candidates: list[SetupCandidate]) -> DecisionResult:
    now = self.clock()
    rejected: list[RejectedCandidate] = []

    # 0. Глобальний kill switch
    if self.risk.is_kill_switch_on():
        rejected = [RejectedCandidate(c, "risk_kill_switch", None, None) for c in candidates]
        return DecisionResult(None, rejected)

    scored: list[tuple[float, float, SetupCandidate]] = []  # (score, threshold, candidate)

    for cand in candidates:
        reason = self._pre_score_filter(cand, now)
        if reason:
            rejected.append(RejectedCandidate(cand, reason, None, None))
            continue

        score, threshold, exp_mult = self._score(cand, now)
        if score < threshold:
            rejected.append(RejectedCandidate(cand, f"score_below_threshold",
                                               score=score, score_threshold=threshold))
            continue

        scored.append((score, threshold, cand, exp_mult))

    if not scored:
        return DecisionResult(None, rejected)

    # Вибрати одного переможця
    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top_thresh, top_cand, top_exp = scored[0]

    # Решту — в rejected з поясненням
    for s, t, c, _ in scored[1:]:
        rejected.append(RejectedCandidate(c, "lost_to_higher_score", s, t))

    plan = self._build_trade_plan(top_cand, top_score, top_thresh, top_exp)

    # Зафіксувати кулдаун
    self._state.last_entry_ms[(top_cand.symbol, top_cand.setup_type)] = now
    self._state.last_symbol_entry_ms[top_cand.symbol] = now

    return DecisionResult(accepted=plan, rejected=rejected)
```

### 2. Pre-score фільтри (дешеві, перед скорингом)

```python
def _pre_score_filter(self, cand: SetupCandidate, now: int) -> str | None:
    # Регім-гейт
    regime_state = self.regime.get_regime(cand.symbol)
    if regime_state.regime in (Regime.DISABLED, Regime.NEWS_RISK):
        return f"regime_blocks_trading:{regime_state.regime.value}"
    if not self._regime_allows_setup(regime_state.regime, cand.setup_type):
        return f"setup_blocked_in_regime:{regime_state.regime.value}"

    # Дубль-ентрі
    if self.position_mgr.has_open_position(cand.symbol):
        return "position_already_open"

    # Per-(symbol, setup) кулдаун
    last = self._state.last_entry_ms.get((cand.symbol, cand.setup_type), 0)
    if now - last < self.config.cooldown_per_setup_ms:
        return f"cooldown_per_setup ({now - last}ms < {self.config.cooldown_per_setup_ms})"

    # Per-symbol кулдаун (щоб не ставити одразу протилежні сетапи)
    last_sym = self._state.last_symbol_entry_ms.get(cand.symbol, 0)
    if now - last_sym < self.config.cooldown_per_symbol_ms:
        return f"cooldown_per_symbol"

    # Expectancy: якщо в сетапу дуже поганий rolling E — блокуємо
    exp = self.expectancy.get(cand.setup_type, cand.symbol)
    if exp is not None and exp.rolling_E_R < self.config.min_expectancy_R:
        return f"setup_expectancy_too_low ({exp.rolling_E_R:.3f}R)"

    # Invalidation перевірити — можливо вже протерміновано в SetupDetector→зараз
    if any(self._already_invalidated(ic, cand) for ic in cand.invalidation_conditions):
        return "already_invalidated"

    return None
```

### 3. Scoring

Score — це **зважена сума факторів**. Позитивні і негативні ваги.

```python
def _score(self, cand: SetupCandidate, now: int) -> tuple[float, float, float]:
    w = self.config.weights

    score = 0.0

    # === Позитивні — з кандидата ===
    for factor_name, value in cand.trigger_factors.items():
        weight = w.trigger.get(factor_name, 0.0)
        score += weight * self._clamp01(value)

    for factor_name, value in cand.confirmation_factors.items():
        weight = w.confirmation.get(factor_name, 0.0)
        score += weight * self._clamp01(value)

    # === Контекст ===
    regime_state = self.regime.get_regime(cand.symbol)

    # Бонус за HTF POI
    if cand.in_htf_poi:
        score += w.htf_poi_bonus
        if cand.htf_poi_type in ('FVG', 'OB'):
            score += w.htf_poi_fvg_ob_extra

    # Бонус за попутний регім (TRENDING у напрямку)
    if (regime_state.regime == Regime.TRENDING_UP and cand.direction == 'LONG') or \
       (regime_state.regime == Regime.TRENDING_DOWN and cand.direction == 'SHORT'):
        score += w.regime_tailwind * regime_state.confidence

    # === Негативні ваги ===
    # Контртрендовий сетап у трендовому регімі — штраф
    if (regime_state.regime == Regime.TRENDING_UP and cand.direction == 'SHORT') or \
       (regime_state.regime == Regime.TRENDING_DOWN and cand.direction == 'LONG'):
        score -= w.regime_headwind * regime_state.confidence

    # Spread штраф
    if regime_state.spread_ticks_avg > w.spread_penalty_threshold:
        score -= w.spread_penalty

    # Loss streak штраф (RiskEngine тримає глобальний counter)
    loss_streak = self.risk.get_loss_streak()
    if loss_streak >= 2:
        score -= w.loss_streak_penalty_per_loss * (loss_streak - 1)

    # === Expectancy множник ===
    exp = self.expectancy.get(cand.setup_type, cand.symbol)
    exp_mult = 1.0 + (exp.rolling_E_R if exp else 0.0) * w.expectancy_multiplier_scale
    exp_mult = max(0.5, min(1.5, exp_mult))           # clamp

    final_score = score * exp_mult

    threshold = self._score_threshold(regime_state, loss_streak)

    return final_score, threshold, exp_mult

def _score_threshold(self, regime: RegimeState, loss_streak: int) -> float:
    base = self.config.base_score_threshold
    if regime.regime == Regime.HIGH_VOL:
        base += self.config.threshold_boost_high_vol
    if regime.regime in (Regime.CHOPPY,):
        base += self.config.threshold_boost_choppy
    if loss_streak >= 2:
        base += self.config.threshold_boost_per_loss * (loss_streak - 1)
    return base
```

### 4. Build TradePlan

```python
def _build_trade_plan(self, cand: SetupCandidate, score: float,
                      threshold: float, exp_mult: float) -> TradePlan:
    regime = self.regime.get_regime(cand.symbol)
    tp1, tp2, tp3 = self._compute_tp_ladder(cand)
    time_stop = self._time_stop_for_setup(cand.setup_type)

    return TradePlan(
        candidate=cand,
        setup_type=cand.setup_type,
        direction=cand.direction,
        symbol=cand.symbol,
        timestamp_ms=cand.timestamp_ms,
        entry_price=cand.entry_price_hint,
        stop_price=cand.stop_price_hint,
        tp1_price=tp1,
        tp2_price=tp2,
        tp3_price=tp3,
        stop_distance_ticks=cand.stop_distance_ticks,
        score=score,
        score_threshold=threshold,
        regime=regime.regime,
        expectancy_multiplier=exp_mult,
        invalidation_conditions=cand.invalidation_conditions,
        time_stop_ms=time_stop,
    )

def _compute_tp_ladder(self, cand: SetupCandidate) -> tuple[float, float, float]:
    """50/25/25 — 1R / 2R / trail. Див. 12-playbook.md §8.5"""
    tick = self.config.tick_size
    stop_distance = cand.stop_distance_ticks * tick
    if cand.direction == 'LONG':
        tp1 = cand.entry_price_hint + stop_distance            # 1R
        tp2 = cand.entry_price_hint + 2 * stop_distance        # 2R
        tp3 = cand.entry_price_hint + 3 * stop_distance        # 3R initial trail
    else:
        tp1 = cand.entry_price_hint - stop_distance
        tp2 = cand.entry_price_hint - 2 * stop_distance
        tp3 = cand.entry_price_hint - 3 * stop_distance
    return tp1, tp2, tp3
```

### 5. Table: regime→setup дозволеність

`_regime_allows_setup()` читає таблицю з конфігу. Це та сама матриця що у [05-market-regime.md](05-market-regime.md).

```python
def _regime_allows_setup(self, regime: Regime, setup: SetupType) -> bool:
    return setup.value in self.config.regime_allow_map[regime.value]
```

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Кілька кандидатів з однаковим score | Беремо першого (deterministic) — стабільний порядок у SetupDetector гарантує відтворюваність |
| Кандидат пройшов, але поки будували TradePlan — регім перейшов у DISABLED | RiskEngine повторно перевірить kill_switch — це OK, подвійний захист |
| Expectancy не має даних (новий сетап, <20 трейдів) | exp_mult=1.0, threshold без boost — новий сетап отримує baseline |
| Clock не монотонний (NTP корекція стрибнула назад) | Кулдаун може стати "минулим" — на практиці мала проблема; у тестах використовуємо інʼєктований clock |
| Всі кандидати rejected | `DecisionResult(None, rejected)` — Journal пише все, оператор бачить статистику |
| Candidate без confirmation_factors (можна?) | Score = сума тригерних факторів; threshold має бути калібрований так щоб "голий тригер" не проходив |

---

## Конфіги

```yaml
decision_engine:
  base_score_threshold: 1.0
  threshold_boost_high_vol: 0.2
  threshold_boost_choppy: 0.2
  threshold_boost_per_loss: 0.1

  tick_size: 0.1

  cooldown_per_setup_ms: 30000              # 30s між повторним запуском того ж сетапу
  cooldown_per_symbol_ms: 5000              # 5s між будь-якими сетапами на тому ж символі

  min_expectancy_R: -0.3                    # якщо rolling E гірше — блокуємо setup

  weights:
    trigger:
      absorption_score: 1.0
      sell_pressure_delta: 0.5
      stacked_imbalance_long: 0.8
      buy_pressure_delta: 0.5
      book_imbalance: 0.4
      pullback_depth_ticks: 0.3
      spoof_score: 0.9
    confirmation:
      stacked_imbalance: 0.6
      delta_recovery: 0.5
      book_pressure_bid: 0.5
      weak_counter_delta: 0.4
      sell_follow_through: 0.5
    htf_poi_bonus: 0.5
    htf_poi_fvg_ob_extra: 0.2
    regime_tailwind: 0.3
    regime_headwind: 0.4
    spread_penalty_threshold: 3
    spread_penalty: 0.3
    loss_streak_penalty_per_loss: 0.15
    expectancy_multiplier_scale: 0.5

  regime_allow_map:
    normal_balanced: [absorption_reversal_long, absorption_reversal_short,
                       imbalance_continuation_long, imbalance_continuation_short,
                       spoof_fail_fade_long, spoof_fail_fade_short,
                       micro_pullback_long, micro_pullback_short]
    trending_up:      [absorption_reversal_short,
                       imbalance_continuation_long,
                       spoof_fail_fade_long, spoof_fail_fade_short,
                       micro_pullback_long]
    trending_down:    [absorption_reversal_long,
                       imbalance_continuation_short,
                       spoof_fail_fade_long, spoof_fail_fade_short,
                       micro_pullback_short]
    choppy:           [absorption_reversal_long, absorption_reversal_short,
                       spoof_fail_fade_long, spoof_fail_fade_short]
    high_vol:         [absorption_reversal_long, absorption_reversal_short]
    low_liq:          []
    news_risk:        []
    disabled:         []

  time_stop_ms_by_setup:
    absorption_reversal_long: null             # без time stop — тримаємо поки сигнал живий
    absorption_reversal_short: null
    imbalance_continuation_long: 30000         # 30s — якщо не пішло, вихід
    imbalance_continuation_short: 30000
    spoof_fail_fade_long: 10000
    spoof_fail_fade_short: 10000
    micro_pullback_long: 20000
    micro_pullback_short: 20000
```

---

## Як тестувати

### Unit
- `_pre_score_filter` — для кожної причини відмови: збудувати стан що її тригерить, перевірити рядок `reason`
- `_score` — для кандидата з 1 тригером + 1 конфірмом, відома вага → очікуване точне число
- `_score_threshold` — HIGH_VOL, loss_streak=3 → base + boost*2
- `_build_trade_plan` — TP ladder правильний (1R/2R/3R від стопу)
- Deterministic order для однакових score (тест зі штучно рівними)

### Integration
- Сценарій 1: Один кандидат ABSORPTION_LONG, регім NORMAL_BALANCED, score вище → `accepted=TradePlan`
- Сценарій 2: Той самий, але відкрита позиція по цьому символу → `rejected=position_already_open`
- Сценарій 3: Два кандидати (LONG і SHORT), переможець — з вищим score, другий у rejected з `lost_to_higher_score`
- Сценарій 4: Кандидат, але RiskEngine kill_switch = on → всі rejected з `risk_kill_switch`

### Regression (snapshot)
- Для 10 зафіксованих вхідних комбо `(candidates, regime, risk, expectancy)` → експортована `DecisionResult` має точно збігатися з baseline JSON. Зміни у вагах = усвідомлений bump baseline.

### Property
- Якщо `threshold > max_possible_score`, жоден кандидат не проходить
- Accepted завжди має `score >= threshold`
