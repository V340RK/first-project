---
name: 08-risk-engine
description: Архітектура модуля Risk Engine — "head"-модуль із kill switch, position sizing через R-buffer formula, денні/місячні ліміти в R, loss-streak, anti-martingale
type: project
---

# 08. RiskEngine ★ — голова системи

## Відповідальність

**Останній рубіж перед біржею.** Бере `TradePlan` proposal від DecisionEngine, накладає **розмір позиції** і перевіряє десятки risk-гейтів. Якщо хоч один не проходить — план **відхиляється**. Якщо проходить — план позначається `risk_gate_passed=True` і передається у ExecutionEngine.

Це також місце де живе **kill switch**. RiskEngine може самостійно зупинити торгівлю (примусити MarketRegime → DISABLED) за своїми тригерами: денна втрата перевищила ліміт, API-ban, надмірна волатильність, clock drift, аномальна статистика.

### Що робить:
- Рахує **size** за формулою `Qty = R_usd / (stop_distance_price + buffer)` (див. [12-playbook.md §8.4](../12-scalping-playbook.md))
- Перевіряє **per-trade R**: не більше ніж `max_risk_per_trade_usd` (абсолют) і `max_risk_per_trade_pct` (% від equity)
- Перевіряє **денний ліміт**: кумулятивна втрата в R не перевищує `daily_loss_limit_R` (-3R / -5R залежно від календарного дня)
- **Місячний ліміт**: `-10R..-12R` → автоматичний DISABLED до наступного місяця
- **Loss streak**: після 3 збитків поспіль — кулдаун 30 хв; після 5 — DISABLED на день
- **Денна кількість трейдів**: max 10/день, з яких ≤3 initiative (контртренд)
- **Anti-martingale**: НЕ збільшувати розмір після збитку (розмір тільки від equity, не від "відіграти")
- **Concurrent positions**: max N (зазвичай 1, в майбутньому можна розширити до 2-3)
- **Kill switch triggers**: API ban, clock drift >1s, WS silence >60s, margin call, ручний stop
- Тримає **loss streak counter** для DecisionEngine
- Тримає **daily/monthly R accumulator** (persist!)

### Що НЕ робить:
- НЕ приймає рішення "чи торгувати за сетапом" (це DecisionEngine)
- НЕ виставляє ордери (Execution)
- НЕ модифікує стоп / TP після входу (PositionManager)

---

## Входи / виходи

### Публічний API:

```python
class RiskEngine:
    def __init__(self, config: RiskConfig,
                 regime: MarketRegime,
                 store: RiskStore,                 # persist daily/monthly accum
                 clock: Callable[[], int] = default_clock):
        ...

    # === Основний flow ===
    def evaluate(self, plan: TradePlan, equity_usd: float) -> RiskDecision

    # === Callback'и від PositionManager ===
    def on_position_closed(self, outcome: TradeOutcome) -> None

    # === Глобальний стан ===
    def is_kill_switch_on(self) -> bool
    def get_loss_streak(self) -> int
    def get_daily_r(self) -> float
    def get_monthly_r(self) -> float

    # === Ручні операції ===
    def kill(self, reason: str) -> None
    def unkill(self) -> None                      # тільки ручно, не автоматично
    def reset_daily(self) -> None                 # о 00:00 UTC
```

### Типи:

```python
@dataclass(frozen=True)
class RiskDecision:
    accepted_plan: TradePlan | None               # з заповненим position_size, risk_usd
    rejection_reason: str | None
    snapshot: RiskSnapshot                        # стан на момент рішення (для журналу)

@dataclass(frozen=True)
class RiskSnapshot:
    timestamp_ms: int
    equity_usd: float
    daily_r: float
    monthly_r: float
    loss_streak: int
    trades_today: int
    initiative_trades_today: int                  # контртрендові
    open_positions: int
    kill_switch: bool
    kill_reason: str | None

@dataclass(frozen=True)
class TradeOutcome:
    """Передається PositionManager'ом при закритті угоди."""
    plan: TradePlan
    trade_id: str                                 # ← корелює всі події угоди в Journal
    symbol: str                                   # ← дублюємо з plan для зручних query/group by
    setup_type: SetupType                         # ← дублюємо з plan; ExpectancyTracker ключує по (setup_type, symbol)
    closed_at_ms: int
    realized_r: float                             # +0.85R, -1.0R, і т.д.
    realized_usd: float
    max_favorable_r: float                        # MFE
    max_adverse_r: float                          # MAE
    was_stopped: bool                             # True = стоп; False = сигнал / time / manual
    fees_usd: float
```

---

## Залежності

**Читає:**
- `MarketRegime` — не для дозволу (це DecisionEngine), а для kill-switch тригерів (clock drift, ws silence ідуть через NotificationService/HealthMonitor → RiskEngine їх підписує)
- `RiskStore` — persist денної/місячної статистики (SQLite / JSONL)
- `RiskConfig`
- `HealthMonitor` (підписка) — отримує події про API ban, WS silence, clock drift

**Пише:**
- `RiskStore` — оновлення кумулятивних показників після кожної `on_position_closed`
- `MarketRegime.force_disabled()` / `clear_disabled()` — коли вмикає kill switch
- `NotificationService` — CRITICAL alert при тригерінгу kill
- `JournalLogger` — кожен `RiskDecision` (і accepted, і rejected)

**Читають його:**
- DecisionEngine — `is_kill_switch_on()`, `get_loss_streak()`
- ExecutionEngine — тільки через переданий `accepted_plan`
- PositionManager — викликає `on_position_closed`

---

## Стан

```python
@dataclass
class _RiskState:
    equity_last_known_usd: float                  # оновлюється з ACCOUNT_UPDATE user stream

    daily:
        date_utc: str                             # '2026-04-21'
        r_accumulated: float                      # від'ємна чи додатна
        trades_count: int
        initiative_trades_count: int

    monthly:
        year_month: str                           # '2026-04'
        r_accumulated: float

    loss_streak: int                              # скидається при win
    last_loss_ms: int | None

    open_positions_count: int                     # інкрементується при accepted, декрементується при on_position_closed

    kill_switch: bool
    kill_reason: str | None
    kill_until_ms: int | None                     # для тимчасових killів (loss streak → 30 хв)
```

**Persistable частина** (SQLite): `daily.*`, `monthly.*`, `loss_streak`, `last_loss_ms`, `kill_switch`, `kill_reason`, `kill_until_ms`. Після рестарту бота має точно знати скільки було програно сьогодні.

---

## Ключові алгоритми

### 1. Position sizing (buffer formula)

Формула з [playbook §8.4](../12-scalping-playbook.md):

```
Qty = R_usd / (stop_distance_price + buffer_price)

де:
  R_usd         = ризик на угоду в USD (абсолют або % від equity)
  stop_distance_price = |entry - stop| у price units
  buffer_price  = додатковий запас на проковзування (наприклад, 1-2 тіки)
```

```python
def _compute_size(self, plan: TradePlan, equity: float) -> tuple[float, float]:
    """Повертає (position_size, risk_usd_actual)."""
    c = self.config
    tick = c.tick_size
    buffer_price = c.slippage_buffer_ticks * tick

    # Базовий R
    r_abs = c.risk_per_trade_usd_abs
    r_pct = equity * c.risk_per_trade_pct
    r_usd = min(r_abs, r_pct)                     # беремо менший з двох

    stop_distance_price = abs(plan.entry_price - plan.stop_price)
    effective_distance = stop_distance_price + buffer_price

    qty = r_usd / effective_distance

    # Округлення до lot size біржі (буде з exchangeInfo.stepSize)
    qty = self._round_to_step(qty, c.step_size)

    # Реальний ризик після округлення
    real_risk = qty * effective_distance
    return qty, real_risk
```

### 2. Head-метод `evaluate()`

```python
def evaluate(self, plan: TradePlan, equity_usd: float) -> RiskDecision:
    snapshot = self._snapshot(equity_usd)

    if snap_reason := self._snapshot_blockers(snapshot):
        return RiskDecision(None, snap_reason, snapshot)

    # Обчислити size
    qty, risk_usd = self._compute_size(plan, equity_usd)

    if qty <= 0:
        return RiskDecision(None, "qty_rounded_to_zero", snapshot)

    # Перевірка min/max size (exchange requirements)
    if qty < self.config.min_qty:
        return RiskDecision(None, f"qty_below_min ({qty} < {self.config.min_qty})", snapshot)
    if qty > self.config.max_qty:
        qty = self.config.max_qty                 # clamp, не reject

    # Risk per trade guard
    if risk_usd > self.config.risk_per_trade_usd_abs * 1.1:
        return RiskDecision(None, f"risk_overshoot (~{risk_usd:.2f}$)", snapshot)

    # Projected drawdown: якщо за цю угоду втратимо 1R — чи вилетимо за денний ліміт?
    projected_daily = snapshot.daily_r - 1.0
    if projected_daily < -self.config.daily_loss_limit_R:
        return RiskDecision(None,
            f"would_breach_daily_limit ({projected_daily:.2f}R < -{self.config.daily_loss_limit_R}R)",
            snapshot)

    # Контртрендовий квотер
    is_initiative = self._is_initiative(plan)
    if is_initiative and snapshot.initiative_trades_today >= self.config.max_initiative_trades_per_day:
        return RiskDecision(None, "initiative_quota_exhausted", snapshot)

    # Concurrent positions
    if snapshot.open_positions >= self.config.max_concurrent_positions:
        return RiskDecision(None, "max_concurrent_positions", snapshot)

    # Пройшло
    accepted = replace(plan,
        position_size=qty,
        risk_usd=risk_usd,
        risk_gate_passed=True,
    )

    # Оптимістично інкрементуємо open_positions — декремент у on_position_closed
    self._state.open_positions_count += 1
    self._state.daily.trades_count += 1
    if is_initiative:
        self._state.daily.initiative_trades_count += 1
    self._persist()

    return RiskDecision(accepted, None, snapshot)
```

### 3. Snapshot-blockers (hard rejection)

```python
def _snapshot_blockers(self, snap: RiskSnapshot) -> str | None:
    if snap.kill_switch:
        return f"kill_switch:{self._state.kill_reason}"
    if snap.daily_r <= -self.config.daily_loss_limit_R:
        return f"daily_limit_reached ({snap.daily_r:.2f}R)"
    if snap.monthly_r <= -self.config.monthly_loss_limit_R:
        return f"monthly_limit_reached ({snap.monthly_r:.2f}R)"
    if snap.trades_today >= self.config.max_trades_per_day:
        return "trade_count_cap"
    if snap.loss_streak >= self.config.loss_streak_hard_limit:
        return f"loss_streak_hard ({snap.loss_streak})"
    return None
```

### 4. `on_position_closed` — оновлення стану

```python
def on_position_closed(self, outcome: TradeOutcome) -> None:
    self._state.open_positions_count = max(0, self._state.open_positions_count - 1)
    self._state.daily.r_accumulated += outcome.realized_r
    self._state.monthly.r_accumulated += outcome.realized_r

    if outcome.realized_r < 0:
        self._state.loss_streak += 1
        self._state.last_loss_ms = outcome.closed_at_ms
        self._check_loss_streak_triggers()
    else:
        self._state.loss_streak = 0

    # Денний ліміт перевиконано — автоматичний kill
    if self._state.daily.r_accumulated <= -self.config.daily_loss_limit_R:
        self.kill(f"daily_loss_limit ({self._state.daily.r_accumulated:.2f}R)")

    # Місячний ліміт — довший kill
    if self._state.monthly.r_accumulated <= -self.config.monthly_loss_limit_R:
        self.kill(f"monthly_loss_limit ({self._state.monthly.r_accumulated:.2f}R)")
        self._state.kill_until_ms = self._end_of_month_ms()

    self._persist()
    self._journal_outcome(outcome)

def _check_loss_streak_triggers(self):
    streak = self._state.loss_streak
    if streak == self.config.loss_streak_cooldown_trigger:         # напр., 3
        cooldown_end = self.clock() + self.config.loss_streak_cooldown_ms
        self.kill(f"loss_streak={streak}_cooldown_30min")
        self._state.kill_until_ms = cooldown_end
    elif streak >= self.config.loss_streak_hard_limit:             # напр., 5
        self.kill(f"loss_streak={streak}_day_off")
        self._state.kill_until_ms = self._end_of_day_ms()
```

### 5. Kill switch

```python
def kill(self, reason: str) -> None:
    if self._state.kill_switch:
        logger.info(f"kill already on ({self._state.kill_reason}), new reason: {reason}")
        return
    self._state.kill_switch = True
    self._state.kill_reason = reason
    self.regime.force_disabled(self._symbols, reason)     # для всіх torgoviх символів
    self._persist()
    asyncio.create_task(self.notifier.send(
        f"🛑 KILL SWITCH ENGAGED\nReason: {reason}", level=AlertLevel.CRITICAL))

def unkill(self) -> None:
    """ТІЛЬКИ ручний виклик. Автоматично не вмикається."""
    self._state.kill_switch = False
    self._state.kill_reason = None
    self._state.kill_until_ms = None
    self.regime.clear_disabled(self._symbols)
    self._persist()
    logger.info("kill switch cleared manually")

def _tick_kill_expiry(self):
    """Викликається slow-loop'ом. Тимчасові killі (кулдаун loss streak) самі знімаються."""
    if (self._state.kill_switch and self._state.kill_until_ms
            and self.clock() > self._state.kill_until_ms
            and self._state.kill_reason.startswith("loss_streak") is False):
        # АВТОМАТИЧНО ЗНІМАТИ тільки тимчасові кулдауни loss streak
        # Денні/місячні ліміти і ручний kill НЕ автоскидаються
        self.unkill()
```

> ⚠️ **Дизайн-рішення**: автоматично знімаємо kill ТІЛЬКИ для тимчасового loss-streak cooldown (30 хв). Денний/місячний ліміти і manual kill — знімаються оператором або rollover'ом (`reset_daily` о 00:00 UTC; `reset_monthly` 1-го числа).

### 6. Initiative vs continuation classification

```python
def _is_initiative(self, plan: TradePlan) -> bool:
    """Контртрендовий (initiative) = йдемо проти регіму."""
    if plan.regime == Regime.TRENDING_UP and plan.direction == 'SHORT': return True
    if plan.regime == Regime.TRENDING_DOWN and plan.direction == 'LONG': return True
    return False
```

### 7. Reset daily / monthly

```python
async def _rollover_loop(self):
    while not self._shutdown:
        await asyncio.sleep(60)
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.strftime('%Y-%m-%d')
        if today_str != self._state.daily.date_utc:
            self.reset_daily()
        month_str = now_utc.strftime('%Y-%m')
        if month_str != self._state.monthly.year_month:
            self.reset_monthly()

def reset_daily(self):
    old = self._state.daily
    self._state.daily = _DailyAccum(
        date_utc=datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        r_accumulated=0.0, trades_count=0, initiative_trades_count=0)
    logger.info(f"daily_rollover: prev={old.r_accumulated:.2f}R, {old.trades_count} trades")
    self._persist()
    # УВАГА: kill switch що спричинений daily_loss_limit НЕ скидається автоматично.
    # Оператор має перевірити стан і unkill вручну — це захист від автоматичного
    # "відіграваня" після поганого дня.
```

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Перший запуск — persist-store порожній | Ініціалізувати `daily.r=0`, `monthly.r=0`, `loss_streak=0`, `kill=False` |
| Рестарт серед торгового дня | Прочитати persist-store — `loss_streak=2` зберіглось, продовжуємо з того ж місця |
| TradePlan з position_size != None (помилка API) | Ігнорувати переданий size, перерахувати |
| `stop_distance_price ≤ 0` (помилка у сетапі) | Reject з `invalid_stop_distance` |
| equity невідомий (немає ACCOUNT_UPDATE від Gateway) | Використати last-known; якщо його немає — reject з `equity_unknown` |
| Time stop спрацював у невинному трейді (R=0) | `on_position_closed` з `realized_r=0` не ламає loss streak |
| on_position_closed викликано двічі на той самий план (ідемпотентність) | Identify by `plan.candidate.features_hash` + `closed_at_ms`; dedupe |
| Автоматичний kill через daily limit, потім о 00:00 UTC | daily.r=0, але `kill_switch=True` ЗАЛИШАЄТЬСЯ. Оператор має unkill вручну. |
| Margin call від біржі | HealthMonitor → `kill("margin_call")` негайно; всі позиції НЕ закриваємо авто (це може бути помилка) — алерт оператору |

---

## Конфіги

```yaml
risk_engine:
  # === Per-trade ===
  risk_per_trade_usd_abs: 10.0              # абсолютний ризик в USD за одну угоду
  risk_per_trade_pct: 0.003                 # або 0.3% від equity (беремо менше з двох)
  slippage_buffer_ticks: 1                  # додається до stop_distance у формулі sizing

  # === Size constraints (FALLBACK-и) ===
  # ⚠ Source of truth — ExchangeInfo.SymbolFilters з Gateway (див. 01-market-data-gateway.md).
  # Ці значення використовуються ЛИШЕ якщо symbol_filters ще не завантажились
  # (cold start до першого fetch_exchange_info або в unit-тестах).
  # У проді RiskEngine читає `gateway.get_symbol_filters(symbol)` і бере звідти
  # tick_size / step_size / min_qty / max_qty / min_notional.
  fallback_min_qty: 0.001
  fallback_max_qty: 10.0
  fallback_step_size: 0.001
  fallback_tick_size: 0.1
  fallback_min_notional: 5.0

  # === Денні / місячні ===
  daily_loss_limit_R: 3                     # -3R → kill на день (перегляд о 00:00 UTC, але kill не знімається авто)
  monthly_loss_limit_R: 10
  max_trades_per_day: 10
  max_initiative_trades_per_day: 3          # контртрендові (initiative)
  max_concurrent_positions: 1

  # === Loss streak ===
  loss_streak_cooldown_trigger: 3           # після 3 L поспіль — 30 хв кулдаун
  loss_streak_cooldown_ms: 1800000          # 30 хв
  loss_streak_hard_limit: 5                 # після 5 L — kill до кінця дня

  # === Persist ===
  store_path: data/risk_state.sqlite
```

---

## Як тестувати

### Unit
- `_compute_size`: entry=100, stop=99.5, R=10$, buffer=1 тік (0.1) → expected qty = 10 / (0.5 + 0.1) = 16.67, округлено до 16.666 (step=0.001)
- `_snapshot_blockers`: для кожного гейта (kill, daily limit, monthly, trade count, streak) — будуємо state, перевіряємо rejection reason
- `on_position_closed` з realized_r=-1 × 3 → streak=3, kill активовано, `kill_until_ms` виставлено
- `on_position_closed` з realized_r=+0.5 після streak=2 → streak скинутий до 0
- `reset_daily` — daily.r=0, kill залишається якщо був через daily limit
- Ідемпотентність `kill()` — викликали двічі → `notifier.send` викликано один раз

### Property-based
- Для будь-якого accepted plan: `risk_usd <= risk_per_trade_usd_abs * 1.1`
- Після accepted: `open_positions_count` збільшився на 1
- Після `on_position_closed`: `open_positions_count` зменшився (clamp ≥ 0)

### Integration
- Симулювати серію угод: +0.5R, -1R, -1R, -1R → перевірити що 4-й trade зареджектиться з `loss_streak_cooldown`
- Рестарт бота після 2 програних угод → loss_streak=2 читається з persist
- Rollover на 00:00 UTC — daily лічильники = 0, але `kill_switch` що був через daily limit залишається

### Regression
- Fixture "typical trading day" — 8 угод з відомими outcome → expected final state (daily_r, streak, kill) = baseline
