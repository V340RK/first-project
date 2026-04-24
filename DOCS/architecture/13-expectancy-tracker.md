---
name: 13-expectancy-tracker
description: Архітектура модуля Expectancy Tracker — rolling expectancy per-setup з Wilson-довірчими інтервалами, auto-деактивація при значущому негативному E, підтримка DecisionEngine
type: project
---

# 13. ExpectancyTracker — rolling expectancy per setup

## Відповідальність

**Пам'ять прибутковості.** Для кожного `(setup_type, symbol)` тримає rolling-вікно останніх N угод і рахує:
- `win_rate` з Wilson confidence interval (бо 12 угод ≠ статистика)
- `avg_win_R`, `avg_loss_R`
- `expectancy_R = WR × AvgWin − (1 − WR) × |AvgLoss|`
- `rolling_E_R` — усереднене по останніх N, яке споживає DecisionEngine

Додатково — **авто-деактивація**: якщо сетап показав rolling_E_R ≤ поріг на ≥ мін. числі угод — сетап позначається як `suspended`, і SetupDetector/DecisionEngine його припиняють використовувати до ручного revert'у.

### Що робить:
- Підписується на `TRADE_OUTCOME` події з JournalLogger
- Ведe rolling-вікно в пам'яті (deque, maxlen=N)
- Persists агрегати у SQLite (не треба щоразу перечитувати JSONL)
- Експонує `get(setup_type, symbol) -> ExpectancySnapshot` для DecisionEngine (hot path, O(1))
- Фонова задача перераховує agg раз на M хвилин (slow loop)
- Попереджає оператора коли сетап просідає (`NotificationService`)

### Що НЕ робить:
- НЕ вирішує "взяти цей трейд чи ні" (DecisionEngine — ExpectancyTracker лише дає цифру)
- НЕ моделює майбутній P&L (це не прогноз, це **rolling факт**)
- НЕ рахує Sharpe/Sortino (окрема аналітика, поза scope)
- НЕ оптимізує ваги DecisionEngine (reflection-only)

---

## Входи / виходи

### Публічний API:

```python
class ExpectancyTracker:
    def __init__(self, config: ExpectancyConfig,
                 journal: JournalLogger,
                 store: ExpectancyStore,
                 notifier: NotificationService):
        ...

    # === Lifecycle ===
    async def start(self) -> None
    async def stop(self) -> None

    # === Hot path (O(1)) ===
    def get(self, setup_type: SetupType, symbol: str) -> ExpectancySnapshot | None
    def is_suspended(self, setup_type: SetupType, symbol: str) -> bool

    # === Slow path ===
    async def recompute_all(self) -> None
    def get_all_snapshots(self) -> dict[tuple[SetupType, str], ExpectancySnapshot]

    # === Operator ===
    def suspend(self, setup_type: SetupType, symbol: str, reason: str) -> None
    def resume(self, setup_type: SetupType, symbol: str) -> None
```

### Типи:

```python
@dataclass(frozen=True)
class ExpectancySnapshot:
    setup_type: SetupType
    symbol: str
    samples: int                                         # N останніх угод у вікні
    wins: int
    losses: int
    breakevens: int                                      # R≈0 (time-stop, manual)
    win_rate: float                                      # wins / (wins + losses), breakevens не рахуємо у знаменнику
    win_rate_ci_low: float                               # Wilson 95% lower bound
    win_rate_ci_high: float
    avg_win_R: float
    avg_loss_R: float                                    # абсолют (0.8 не -0.8)
    rolling_E_R: float                                   # expectancy
    max_mfe_R: float                                     # best MFE у вікні
    max_mae_R: float                                     # worst MAE у вікні
    last_updated_ms: int
    suspended: bool
    suspended_reason: str | None

@dataclass(frozen=True)
class ExpectancyConfig:
    window_size: int = 50                                # останні N угод
    min_samples_for_signal: int = 20                     # нижче цього — E не використовується для auto-suspend
    min_samples_for_multiplier: int = 10                 # нижче — multiplier=1.0
    auto_suspend_e_threshold_R: float = -0.3             # E ≤ -0.3R
    auto_suspend_min_samples: int = 30                   # і мін. 30 угод
    auto_suspend_ci_upper: float = 0.45                  # ... і CI upper < 0.45 (не просто "не повезло")
    recompute_interval_sec: int = 300                    # раз на 5 хв
    notify_on_drawdown_R: float = -2.0                   # сповіщати коли rolling загального просідання < -2R
```

---

## Залежності

**Читає:**
- `JournalLogger` — підписка на `TRADE_OUTCOME` events
- `ExpectancyStore` (SQLite) — persist вікна
- `ExpectancyConfig`

**Пише:**
- `ExpectancyStore` — update після кожного outcome
- `NotificationService` — INFO коли просідання, WARN коли auto-suspend
- `JournalLogger` — події `EXPECTANCY_SUSPENDED` / `EXPECTANCY_RESUMED`

**Читають його:**
- DecisionEngine — `get(setup, symbol)` у hot-loop для expectancy_multiplier і min_expectancy_R filter
- SetupDetector — опційно, `is_suspended(setup, symbol)` щоб не генерувати кандидатів взагалі
- UI / CLI — `get_all_snapshots()` для дашборду

---

## Стан

```python
@dataclass
class _TrackerState:
    windows: dict[tuple[SetupType, str], deque[_TradeSample]]   # maxlen=window_size
    snapshots: dict[tuple[SetupType, str], ExpectancySnapshot]  # кеш для get()
    suspended: dict[tuple[SetupType, str], str]                 # key → reason

@dataclass(frozen=True)
class _TradeSample:
    trade_id: str
    closed_at_ms: int
    realized_r: float
    mfe_r: float
    mae_r: float
    was_stopped: bool
```

Persist (SQLite):

```sql
CREATE TABLE expectancy_samples (
    setup_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    closed_at_ms INTEGER NOT NULL,
    realized_r REAL NOT NULL,
    mfe_r REAL,
    mae_r REAL,
    was_stopped INTEGER,
    PRIMARY KEY (setup_type, symbol, trade_id)
);
CREATE INDEX idx_expectancy_closed ON expectancy_samples (setup_type, symbol, closed_at_ms DESC);

CREATE TABLE expectancy_suspended (
    setup_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    reason TEXT NOT NULL,
    suspended_at_ms INTEGER NOT NULL,
    PRIMARY KEY (setup_type, symbol)
);
```

---

## Ключові алгоритми

### 1. Приймання outcome

```python
async def _on_trade_outcome(self, ev: JournalEvent):
    if ev.kind != EventKind.TRADE_OUTCOME:
        return
    p = ev.payload
    key = (SetupType(p['setup_type']), ev.symbol)

    sample = _TradeSample(
        trade_id=ev.trade_id,
        closed_at_ms=ev.timestamp_ms,
        realized_r=p['realized_r'],
        mfe_r=p['max_favorable_r'],
        mae_r=p['max_adverse_r'],
        was_stopped=p['was_stopped'],
    )

    dq = self._state.windows.setdefault(key, deque(maxlen=self.config.window_size))
    dq.append(sample)

    self.store.upsert_sample(key, sample)

    # Перерахунок тільки цього ключа (не всіх)
    self._state.snapshots[key] = self._compute_snapshot(key, dq)
    self._check_auto_suspend(key)
```

### 2. `_compute_snapshot`

```python
def _compute_snapshot(self, key, samples: deque[_TradeSample]) -> ExpectancySnapshot:
    setup, symbol = key
    n = len(samples)
    wins = [s for s in samples if s.realized_r > 0.05]              # >0.05R вважаємо win
    losses = [s for s in samples if s.realized_r < -0.05]
    be = n - len(wins) - len(losses)

    # WR рахуємо без BE у знаменнику (BE не виграш і не програш)
    wr_denom = len(wins) + len(losses)
    wr = len(wins) / wr_denom if wr_denom > 0 else 0.0

    avg_win = mean(s.realized_r for s in wins) if wins else 0.0
    avg_loss = abs(mean(s.realized_r for s in losses)) if losses else 0.0

    E = wr * avg_win - (1 - wr) * avg_loss if wr_denom > 0 else 0.0

    lo, hi = wilson_ci(len(wins), wr_denom, z=1.96) if wr_denom > 0 else (0.0, 1.0)

    max_mfe = max((s.mfe_r for s in samples), default=0.0)
    max_mae = min((s.mae_r for s in samples), default=0.0)

    return ExpectancySnapshot(
        setup_type=setup, symbol=symbol, samples=n,
        wins=len(wins), losses=len(losses), breakevens=be,
        win_rate=wr, win_rate_ci_low=lo, win_rate_ci_high=hi,
        avg_win_R=avg_win, avg_loss_R=avg_loss,
        rolling_E_R=E,
        max_mfe_R=max_mfe, max_mae_R=max_mae,
        last_updated_ms=time_ms(),
        suspended=self._state.suspended.get(key) is not None,
        suspended_reason=self._state.suspended.get(key),
    )
```

### 3. Wilson confidence interval

```python
def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% CI для true win-rate. На малих вибірках дає більш консервативні межі ніж нормальне наближення."""
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    n = total
    denom = 1 + z*z/n
    centre = (p + z*z/(2*n)) / denom
    margin = (z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)
```

### 4. Auto-suspend

```python
def _check_auto_suspend(self, key):
    snap = self._state.snapshots[key]
    cfg = self.config

    if snap.suspended:
        return                                           # уже suspend'нутий

    if snap.samples < cfg.auto_suspend_min_samples:
        return                                           # мало даних — не чіпаємо

    bad_e = snap.rolling_E_R <= cfg.auto_suspend_e_threshold_R
    bad_ci = snap.win_rate_ci_high < cfg.auto_suspend_ci_upper

    if bad_e and bad_ci:
        reason = (f"auto: E={snap.rolling_E_R:.2f}R ≤ {cfg.auto_suspend_e_threshold_R}R "
                  f"over {snap.samples}, CI_upper={snap.win_rate_ci_high:.2f} "
                  f"< {cfg.auto_suspend_ci_upper}")
        self.suspend(key[0], key[1], reason)
```

### 5. `suspend` / `resume`

```python
def suspend(self, setup_type, symbol, reason):
    key = (setup_type, symbol)
    self._state.suspended[key] = reason
    self.store.upsert_suspended(key, reason, time_ms())
    self.journal.log(JournalEvent(0, time_ms(),
                                   EventKind.EXPECTANCY_SUSPENDED,
                                   None, symbol,
                                   {'setup_type': setup_type.value, 'reason': reason}))
    asyncio.create_task(self.notifier.send(
        f"⚠️ Setup SUSPENDED: {setup_type.value}/{symbol}\n{reason}",
        level=AlertLevel.WARNING))

def resume(self, setup_type, symbol):
    key = (setup_type, symbol)
    self._state.suspended.pop(key, None)
    self.store.delete_suspended(key)
    self.journal.log(JournalEvent(0, time_ms(),
                                   EventKind.EXPECTANCY_RESUMED,
                                   None, symbol,
                                   {'setup_type': setup_type.value}))
```

> ⚠️ **Дизайн-рішення**: `resume` ТІЛЬКИ ручний. Автоматично не знімається, навіть якщо останні 5 угод були прибутковими — інакше кулдаун на поганий сетап не має сенсу. Оператор дивиться на причину, аналізує, і руками ресюмить якщо вважає що покращилось.

### 6. Recompute loop (startup + періодично)

```python
async def start(self):
    # Lazy-load вікон з персиста — беремо останні N угод кожного ключа
    for key in self.store.list_keys():
        samples = self.store.load_recent(key, self.config.window_size)
        self._state.windows[key] = deque(samples, maxlen=self.config.window_size)
        self._state.snapshots[key] = self._compute_snapshot(key, self._state.windows[key])

    for key, reason in self.store.load_suspended():
        self._state.suspended[key] = reason

    self.journal.subscribe(EventKind.TRADE_OUTCOME, self._on_trade_outcome)
    self._loop_task = asyncio.create_task(self._recompute_loop())

async def _recompute_loop(self):
    while not self._shutdown:
        await asyncio.sleep(self.config.recompute_interval_sec)
        # Recompute усіх snapshot'ів (на випадок якщо config змінився)
        for key, dq in self._state.windows.items():
            self._state.snapshots[key] = self._compute_snapshot(key, dq)
```

### 7. Multiplier / filter для DecisionEngine

DecisionEngine викликає `get()`:

```python
def _e_mult(self, setup, symbol) -> float:
    snap = self.expectancy.get(setup, symbol)
    if snap is None or snap.samples < self.config.min_samples_for_multiplier:
        return 1.0
    mult = 1.0 + snap.rolling_E_R * self.config.expectancy_multiplier_scale
    return max(0.5, min(1.5, mult))
```

DecisionEngine **також** зобов'язаний перевірити `is_suspended` у pre-score filter.

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Перший запуск — JSONL журнал порожній | Пусті вікна; `get()` повертає None; DecisionEngine використовує multiplier=1.0 |
| Сетап був suspend'нутий, потім код рестарту — persist тримає | Після `start()` `suspended` мапа відновлена з SQLite |
| trade_outcome прийшов з неіснуючим `setup_type` (конфіг змінений) | Skip з warning у journal; не падати |
| Два outcome з однаковим `trade_id` (re-send) | UNIQUE constraint у SQLite → ignore з warning |
| Вибух серії виграшів підряд (streak 10) | rolling_E_R злітає, multiplier clamped до 1.5 — не дозволяємо over-bet |
| Breakeven (R=0 через time-stop) | Не рахується ні у wins, ні у losses — впливає тільки на `samples` count |
| Зміна `window_size` у конфігу під час роботи | На наступному recompute вікно перегенерується з персиста з новим N |
| auto_suspend_min_samples = 30, але вікно тримає тільки 50 | Нічого страшного — рахуємо по тим 30+ що є. Старіші за 50 узагалі не в пам'яті |
| Clock skew (closed_at_ms у майбутньому) | Сортування по closed_at_ms: аномалія прорветься у "хвіст"; але на rolling E-R ефект малий |
| Конфіг-baseline зламано (auto_suspend_e_threshold_R = +0.5) | Valid → всі setup'и suspend'нуть. Схема конфіга валідує `< 0` |

---

## Конфіги

```yaml
expectancy:
  window_size: 50
  min_samples_for_signal: 20
  min_samples_for_multiplier: 10

  auto_suspend_e_threshold_R: -0.3
  auto_suspend_min_samples: 30
  auto_suspend_ci_upper: 0.45

  recompute_interval_sec: 300
  notify_on_drawdown_R: -2.0

  store_path: data/expectancy.sqlite
```

---

## Як тестувати

### Unit
- `wilson_ci(10, 20)` → (≈0.30, ≈0.70), ширше ніж нормальне наближення
- `wilson_ci(0, 0)` → (0.0, 1.0)
- `wilson_ci(3, 3)` → (≈0.44, 1.0) — не впевнений 100%
- `_compute_snapshot` на списку [+1, +1, -1, 0] → wr=0.67 (2/3), be=1, avg_win=1.0, avg_loss=1.0, E = 0.67*1 - 0.33*1 = 0.33
- `_compute_snapshot` з breakevens не ламає WR (знаменник = wins+losses, не всі samples)
- `_check_auto_suspend` за умовами (E ≤ -0.3, samples ≥ 30, CI_upper < 0.45) → suspended=True
- `_check_auto_suspend` коли samples=25 < 30 → НЕ suspended навіть при E=-1.0
- `_check_auto_suspend` коли E=-0.5 але CI_upper=0.6 → НЕ suspended (можливо просто не пощастило)
- `resume` після `suspend` → `is_suspended` False, подія у journal

### Integration
- Startup → персист містить 40 samples одного ключа → `get()` повертає snapshot з samples=40
- Послідовно надсилаємо 30 outcome'ів з realized_r=-1 → auto-suspend спрацьовує, WARNING alert надіслано
- Послідовно надсилаємо 10 прибуткових → suspend НЕ знімається автоматично

### Property
- Інваріант: `0 ≤ win_rate ≤ 1`, `win_rate_ci_low ≤ win_rate ≤ win_rate_ci_high`
- Інваріант: `rolling_E_R` не залежить від порядку угод (idempotent щодо shuffle)
- Інваріант: після `window_size+1` додавань — `samples == window_size`

### Regression
- Fixture journal з 500 outcome'ами → snapshot baseline (WR, E, CI) — byte-level порівняння
