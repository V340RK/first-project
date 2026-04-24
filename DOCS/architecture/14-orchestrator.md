---
name: 14-orchestrator
description: Архітектура Orchestrator — runtime-граф, що склеює 13 pipeline-модулів і веде hot/slow loop; єдина точка, що знає про всіх
type: project
---

# 14. Orchestrator — runtime-граф pipeline

## Відповідальність

Orchestrator — **єдиний компонент, що знає про всіх інших**. Його робота:
- тримати посилання на всі 13 модулів + Journal + NotificationService
- маршрутизувати події між ними (WS event → on_tick → pipeline)
- вести **hot loop** (на кожен тік) і **slow loop** (періодичні reclass/heartbeat)
- логувати ключові точки pipeline у Journal (setup/decision/risk/position події)
- обробляти lifecycle: startup, graceful shutdown, kill switch

### Що робить:

- `start(symbols)` — піднімає в правильному порядку: Journal → Notifier → Book → Tape → Gateway → Regime; початкова reclassify для кожного символу.
- `on_tick(symbol, event_time_ms)` — hot path: будує `MarketSnapshot` з Book+Tape, передає його FeatureEngine, далі `_run_pipeline`.
- `_run_pipeline` — Position.on_features → якщо позиція вже є, вихід; інакше Detector → Decision → Risk → Position.open; кожен крок логується у Journal.
- `on_slow_tick()` — раз на ~5–10 с перекласифіковує регім для всіх символів + пише `HEARTBEAT` з метриками (daily_r, monthly_r, open positions).
- `_on_user_event` — делегує event з Gateway user-stream (fills, update'и позицій) до `ExecutionEngine.handle_user_event`.
- `_on_regime_change` — логує `REGIME_CHANGED` у Journal.
- `stop()` — force-close відкритих позицій, `cancel_all` на всіх символах, зупиняє Gateway/Regime/Book/Tape/Notifier/Journal у зворотному порядку.

### Що НЕ робить:

- Не приймає торгових рішень (делегує DecisionEngine).
- Не обраховує фічі (делегує FeatureEngine).
- Не зберігає бізнес-стан — тільки runtime-рефи + `_seq` для journal events + список `_symbols`.
- Не викликає Exchange напряму — тільки через Gateway/Execution.

---

## Входи / виходи

### Публічний API (реальний, з коду):

```python
from collections.abc import Callable

ClockFn = Callable[[], int]       # UTC мілісекунди
EquityFn = Callable[[], float]    # рахунок у USDT

class Orchestrator:
    def __init__(
        self,
        config: object,
        gateway: MarketDataGateway,
        features: FeatureEngine,
        regime: MarketRegime,
        detector: SetupDetector,
        decision: DecisionEngine,
        risk: RiskEngine,
        execution: ExecutionEngine,
        position: PositionManager,
        expectancy: ExpectancyTracker,
        journal: JournalLogger,
        notifier: NotificationService,
        *,
        book: OrderBookEngine | None = None,
        tape: TapeFlowAnalyzer | None = None,
        clock_fn: ClockFn | None = None,     # default: common.time.clock()
        equity_fn: EquityFn | None = None,   # default: lambda: 1000.0 (TEST-заглушка!)
    ) -> None: ...

    # === Lifecycle ===
    async def start(self, symbols: list[str]) -> None
    async def stop(self) -> None

    # === Hot loop ===
    async def on_tick(self, symbol: str, event_time_ms: int) -> None

    # === Slow loop ===
    async def on_slow_tick(self) -> None
```

### Події у Journal, які пише Orchestrator:

| EventKind | Коли | Payload |
|---|---|---|
| `STARTUP` | у `start()` | `{"symbols": [...]}` |
| `SHUTDOWN` | у `stop()` | `{}` |
| `REGIME_CHANGED` | callback з MarketRegime | `{"old": ..., "new": ...}` |
| `SETUP_CANDIDATE_GENERATED` | кожен кандидат з Detector | `{"setup_type", "direction", "entry"}` |
| `DECISION_REJECTED` | кожен rejected у Decision | `{"setup_type", "reason", "score", "threshold"}` |
| `DECISION_ACCEPTED` | переможець Decision | `{"setup_type", "score", "regime"}` |
| `RISK_REJECTED` | Risk не пропустив | `{"reason", "setup_type"}` |
| `RISK_ACCEPTED` | Risk пропустив | `{"qty", "risk_usd"}` |
| `POSITION_OPENED` | Position.open повернув True | `{"setup_type", "direction", "entry", "stop"}` |
| `HEARTBEAT` | кожен slow tick | `{"daily_r", "monthly_r", "open_positions"}` |

Усі інші події (FILL, ORDER_REQUESTED, POSITION_CLOSED, TRADE_OUTCOME тощо) пишуть самі відповідні модулі — Orchestrator їх НЕ ретранслює.

---

## Залежності

**Читає (тримає посилання):**
- Усі 13 pipeline-модулів + JournalLogger + NotificationService.
- `clock_fn`, `equity_fn` — інжектовані функції (щоб тести могли підміняти).

**Підписується на колбеки (у `_wire_callbacks`):**
- `gateway.on_user_event(cb)` → `self._on_user_event` (fills/position updates з Binance user-stream).
- `regime.on_regime_change(cb)` → `self._on_regime_change` (audit у Journal).

**Викликають його (ззовні):**
- Entry point (`scalper/__main__.py` — ще не створено): `start(symbols)` / `stop()`.
- Market-data loop (**ще не існує**): `await orch.on_tick(symbol, ts)` на кожному depth/trade event з Gateway.
- Scheduler (**ще не існує**): `await orch.on_slow_tick()` раз на 1–5 с.

**Не викликає:** модулі напряму між собою (пропускає через себе); Exchange.

---

## Стан

Orchestrator сам майже не зберігає стану — тільки runtime-контекст:

```python
self._symbols: list[str]          # з останнього start()
self._running: bool               # False до start(), True після; False після stop()
self._seq: int                    # монотонний лічильник для JournalEvent.seq
```

Весь бізнес-стан лежить у власних модулях (book, tape, position, risk, expectancy). Orchestrator — **stateless по відношенню до ринку**.

---

## Ключові алгоритми

### 1. Wiring (у `__init__`)

```python
def _wire_callbacks(self) -> None:
    self._gateway.on_user_event(self._on_user_event)
    self._regime.on_regime_change(self._on_regime_change)
```

Market-data callbacks (depth/trade) — **не підключені тут**. Очікується, що зовнішній loop дістає їх з Gateway (або підписується через окремий метод) і викликає `on_tick` сам. Це зроблено, бо саме loop вирішує, **як** розпарсити tick у виклик orchestrator-а (напр., debounce, throttle).

### 2. Startup порядок

```python
async def start(self, symbols: list[str]) -> None:
    self._symbols = list(symbols)
    self._journal.start()        # ⚠ CURRENT: sync-style call, а start у Journal async → треба await
    self._notifier.start()       # ⚠ те саме
    if self._book  is not None: self._book.start(symbols)
    if self._tape  is not None: self._tape.start(symbols)
    await self._gateway.start(symbols)
    self._regime.start(symbols)
    for sym in symbols:
        self._regime.reclassify(sym)
    self._running = True
    self._log(EventKind.STARTUP, payload={"symbols": symbols})
```

**Відомий баг** (див. нижче в Edge cases): `self._journal.start()` і `self._notifier.start()` — у коді без `await`, а методи у `JournalLogger`/`NotificationService` — `async`. Тести зелені, бо використовуються fake-об'єкти з sync `start`. При реальному запуску тригериться `RuntimeWarning: coroutine was never awaited`, і writer-task Journal не запускається. Виправлення — зробити `start` тут `async` і awaitнути.

### 3. Hot loop — один тік

```python
async def on_tick(self, symbol: str, event_time_ms: int) -> None:
    if not self._running: return

    snapshot = self._build_snapshot(symbol, event_time_ms)
    if snapshot is None: return

    features = self._features.compute(snapshot)
    await self._run_pipeline(symbol, features)
```

`_build_snapshot` бере поточний стан book+tape і збирає `MarketSnapshot`. Якщо book/tape порожні — повертає None (тік ігнорується). `spread_ticks` рахується з fallback `tick=0.1` (див. Edge cases — треба брати з ExchangeInfo).

### 4. `_run_pipeline` — повний шлях

```python
async def _run_pipeline(self, symbol, features):
    self._position.on_features(features)           # оновлюємо позицію
    if self._position.has_open_position(symbol): return
    if not self._regime.is_trading_allowed(symbol): return

    candidates = self._detector.detect(features)
    for cand in candidates: self._log(SETUP_CANDIDATE_GENERATED, ...)
    if not candidates: return

    decision_result = self._decision.decide(candidates)
    for rej in decision_result.rejected: self._log(DECISION_REJECTED, ...)
    if decision_result.accepted is None: return

    plan = decision_result.accepted
    self._log(DECISION_ACCEPTED, ...)

    risk_decision = self._risk.evaluate(plan, self._equity())
    if risk_decision.plan is None:
        self._log(RISK_REJECTED, ...); return

    sized_plan = risk_decision.plan
    self._log(RISK_ACCEPTED, ...)

    opened = await self._position.open(sized_plan)
    if opened:
        self._risk.on_position_opened(sized_plan)
        self._log(POSITION_OPENED, ...)
```

**Порядок інваріантів:**
1. Позиція відкрита → новий setup ігнорується (ми в дуже консервативному режимі: `max_concurrent_positions: 1` у default конфізі).
2. Регім заборонив торгівлю → ігнор.
3. Detector нічого не знайшов → ігнор.
4. Decision всіх відхилив → лог кожного rejected з причиною.
5. Risk відхилив → лог причини (spread/latency/kill-switch).
6. Position.open() повернула False (напр., Execution kicknuv) → **не** логуємо POSITION_OPENED.

### 5. Slow loop

```python
async def on_slow_tick(self) -> None:
    if not self._running: return
    for sym in self._symbols:
        self._regime.reclassify(sym)
    self._log(EventKind.HEARTBEAT, payload={
        "daily_r": self._risk.get_daily_r(),
        "monthly_r": self._risk.get_monthly_r(),
        "open_positions": len(self._position.all_open()),
    })
```

Викликається зовнішнім scheduler-ом (**ще не створено**). Інтервал рекомендований: 1–5 с. Reclassify дешеве — читає атр/спред з вже зібраних вікон.

### 6. Graceful shutdown

```python
async def stop(self) -> None:
    self._running = False
    for pos in list(self._position.all_open()):
        self._position.force_close(pos.plan.symbol, "shutdown")
    for sym in self._symbols:
        try: await self._execution.cancel_all(sym)
        except Exception as e: logger.warning("cancel_all failed for %s: %s", sym, e)
    self._regime.stop()
    if self._tape is not None: self._tape.stop()
    if self._book is not None: self._book.stop()
    await self._gateway.stop()
    self._log(EventKind.SHUTDOWN, payload={})
    self._notifier.stop()        # ⚠ async без await — той самий баг що в start
    self._journal.stop()         # ⚠
```

**Порядок shutdown — інверсія startup:** позиції → exchange cancel → регім → ринкові стрімерs → gateway → journal/notifier. Причина — якщо спочатку вбити Journal, останні `POSITION_CLOSED`/`TRADE_OUTCOME` не потраплять у audit.

### 7. Журналування (`_log` helper)

```python
def _log(self, kind, *, symbol=None, trade_id=None, payload):
    self._seq += 1
    event = JournalEvent(
        seq=self._seq,
        timestamp_ms=self._clock(),
        kind=kind,
        trade_id=trade_id,
        symbol=symbol,
        payload=payload,
    )
    self._journal.log(event)
```

`seq` Orchestrator'а **НЕ збігається** з `seq` усередині Journal — Journal має власний seq, що переприсвоюється в writer-loop. Сенс `_seq` Orchestrator'а — лише щоб не кидати нульові значення туди, де `JournalEvent` вимагає int.

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| `on_tick` до `start()` | `self._running=False` → ранній return, тік ігнорується. |
| Book/Tape порожні (старт, resync) | `_build_snapshot` повертає None → тік скіпнуто. |
| ExchangeInfo невідомий → tick size | **Зараз hardcoded `0.1`** в [orchestrator.py:227](../../src/scalper/orchestrator/orchestrator.py#L227). На production треба брати з `gateway.get_symbol_info(symbol).tick_size`. |
| `equity_fn` не передано | default = `lambda: 1000.0` — **це тестова заглушка, не для live**. Entry point зобов'язаний передати реальний callback на баланс з REST. |
| `cancel_all` впав при shutdown | `logger.warning`, цикл продовжує для решти символів (щоб один проблемний символ не блокував вихід з інших). |
| Регім перемкнувся на DISABLED в середині `_run_pipeline` | `is_trading_allowed` перевіряється *перед* Detector; якщо перемкнулося між перевіркою і Risk — Risk сам відхилить (має kill-switch гейт). |
| Виняток у Detector/Decision/Risk | Наразі НЕ обгорнуто try/except — падіння tick'а підніметься до event loop. Розгляд: додати глобальний wrapper + алерт CRITICAL. |
| Journal/Notifier не запустилися (async без await) | Тести мочать — проходять. Live: події пишуться в queue, writer-task спить → втрата audit trail. **Має бути виправлено в entry point.** |
| Паралельні тіки по одному символу | Наразі Orchestrator не має lock'а. Якщо loop викликає `on_tick(sym)` двічі overlap — Detector/Position можуть побачити один snapshot двічі. Рекомендація: entry point гарантує serial виклик per-symbol (sequential consumer). |
| Два символи паралельно | Дозволено. Вони ділять лише Risk (daily_r) і Position (max_concurrent). Race на daily_r теоретично можливий, але practically ігнорабельний (snapshot-порядок). |

---

## Конфіги

Orchestrator **не має власного** розділу в yaml. Він просто збирає вже-сконфігуровані модулі і приймає `config: object` (непрозорий) для передачі далі. Конфіги беруться з:

- `mode`, `symbols` — з root конфіга
- `orchestrator.slow_tick_interval_ms: 1000` — ще не імплементовано, очікується в entry point, який створить `asyncio.create_task(slow_loop(orch, interval))`

---

## Як тестувати

### Unit — `tests/orchestrator/test_wiring.py` (вже існує)

Використовує повний набір **fake**-модулів (FakeGateway, FakeFeatures, FakeRegime, FakeDetector, FakeDecision, FakeRisk, FakeExecution, FakePosition, FakeJournal, FakeNotifier). Перевіряє:

- `start(symbols)` викликає `start` на всіх модулях і пише STARTUP.
- `on_user_event` з Gateway передається в `execution.handle_user_event`.
- `_on_regime_change` пише `REGIME_CHANGED`.
- Pipeline при flat-позиції: Detector → Decision → Risk → Position; кожен крок генерує відповідний EventKind.
- Pipeline коли позиція вже відкрита: Detector не викликається.
- Pipeline коли Regime заборонив: Detector не викликається.
- Decision rejected → log `DECISION_REJECTED`, далі нічого.
- Risk rejected → log `RISK_REJECTED`, `position.open` не викликано.
- `stop()` при відкритій позиції викликає `force_close` + `cancel_all` + пише SHUTDOWN.

### Integration (ще не написано)

- Повний pipeline з реальним (але in-memory) Gateway-mock + ReplayGateway → прогнати 1 день журналу → порівняти з baseline JSONL.
- Performance: 500 tick/s → на одному символі `on_tick` не перевищує 20 ms p99.

### Manual

- `python -m scalper --config configs/settings.yaml` — запуск у live mode (ще не імплементовано).
- `python -m scalper --mode replay --data data/recordings/...` — бек-тест.

---

## Що треба зробити в entry point (TODO для кроку 3)

1. **Виправити async lifecycle** у `start`/`stop`: awaitнути `journal.start/stop`, `notifier.start/stop`, зробити ці методи в Orchestrator теж повністю async.
2. **Підключити market-data callbacks** — після `gateway.start(symbols)` дістати стрім depth/trade і перетворювати кожен event на виклик `await orch.on_tick(symbol, event_time_ms)`. Важливо: serializuvati per-symbol, щоб не було race.
3. **Запустити slow-loop як asyncio.Task:**
   ```python
   async def slow_loop(orch, interval_s=1.0):
       while True:
           await orch.on_slow_tick()
           await asyncio.sleep(interval_s)
   ```
4. **Передати реальний `equity_fn`** — callback що повертає поточний USDT-баланс з REST (з кешем на N секунд).
5. **Передати реальний `tick_size`** у `_build_snapshot` — через Gateway.get_symbol_info (або вбудувати в Orchestrator доступ до exchange_info).
6. **Signal handler** — SIGINT/SIGTERM → `await orch.stop()` → exit.
