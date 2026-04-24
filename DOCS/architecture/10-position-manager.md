---
name: 10-position-manager
description: Архітектура модуля Position Manager — state machine відкритої позиції, TP-ladder 50/25/25, break-even, trailing, exit on signal failure як первинний вихід, configurable time stop як резервний
type: project
---

# 10. PositionManager — керування відкритою позицією

## Відповідальність

**State machine для життєвого циклу однієї позиції** — від "план прийнято" до "позиція закрита + repport RiskEngine". Це єдиний модуль що має право **закривати позиції** (шлях до Execution).

Керує:
- Виставленням entry-ордера, SL і TP-ladder (50/25/25)
- Переведенням стопу у break-even після TP1
- Трейлінгом стопу після TP2
- **Primary exit — на зрив сигналу**: активно моніторить `invalidation_conditions` із TradePlan; якщо спрацьовують до TP1 → вихід маркетом
- **Time stop як резервний механізм** (configurable per-setup; deflt None для багатьох сетапів) — угода може відпрацьовувати довше, якщо сигнал жив
- Dump ручки для PartialFill, CancelFill, Reject обробки
- Формування `TradeOutcome` і передача в `RiskEngine.on_position_closed`

> **Важливо**: зрив сигналу = первинний тригер виходу, не time stop. Сценарій для LONG: протилежний absorption на ask, opposite delta burst, книга різко розвертає push. Конкретні умови зафіксовані у `TradePlan.invalidation_conditions` ще на етапі SetupDetector → DecisionEngine.

### Що робить:
- Тримає state machine для кожної активної позиції
- Підписується на `FillEvent` від Execution → оновлює стан
- Підписується на `Features` (hot-loop) → перевіряє `invalidation_conditions`
- Викликає `Execution.place_order` для SL, TP, scale-out, emergency-close
- При повному закритті (TP3 або stop hit або invalidation) → викликає `RiskEngine.on_position_closed(outcome)`
- Журналює кожен перехід стану

### Що НЕ робить:
- НЕ приймає рішення "чи відкривати нову" (Decision)
- НЕ рахує size / R (Risk)
- НЕ парсить ринок (Features/Setup)
- НЕ обробляє низькорівневі помилки біржі (Execution)

---

## Входи / виходи

### Публічний API:

```python
class PositionManager:
    def __init__(self, config: PositionConfig,
                 execution: ExecutionEngine,
                 risk: RiskEngine,
                 feature_engine: FeatureEngine,
                 clock: Callable[[], int] = default_clock):
        execution.on_fill(self._on_fill)
        execution.on_order_update(self._on_order_update)

    # === Головний вхід ===
    async def open(self, plan: TradePlan) -> bool

    # === Query ===
    def has_open_position(self, symbol: str) -> bool
    def get_position(self, symbol: str) -> OpenPosition | None
    def get_all_positions(self) -> list[OpenPosition]

    # === Tick (викликається кожен Features від pipeline) ===
    async def on_features(self, features: Features) -> None

    # === Ручні ===
    async def close_position(self, symbol: str, reason: str) -> bool
```

### Типи:

```python
class PositionState(Enum):
    ENTRY_PENDING = "entry_pending"        # ордер виставлено, чекаємо fill
    FILLED_NO_PROTECTION = "filled_no_protection"  # entry filled, ще не встановили SL
    ACTIVE = "active"                       # SL і TP виставлені, торгуємо
    TP1_HIT = "tp1_hit"                     # part filled → BE активовано
    TP2_HIT = "tp2_hit"                     # part filled → trailing активовано
    CLOSING = "closing"                     # приступили до emergency close
    CLOSED = "closed"                       # повністю закрита — чекаємо cleanup

@dataclass
class OpenPosition:
    plan: TradePlan
    state: PositionState
    opened_at_ms: int
    entry_coid: str
    sl_coid: str | None
    tp_coids: list[str]                     # [tp1, tp2, tp3]

    filled_qty: float                       # з entry
    remaining_qty: float                    # скільки ще відкрито
    avg_entry_price: float

    realized_pnl_usd: float                 # сумарний з filled TP / SL
    realized_r: float                       # сумарний у R

    current_stop_price: float               # поточний SL (може рухатись — BE, trail)
    max_favorable_price: float
    max_adverse_price: float
    max_favorable_r: float                  # MFE
    max_adverse_r: float                    # MAE

    last_feature_check_ms: int
    invalidation_triggered: InvalidationCondition | None

    time_stop_deadline_ms: int | None       # None = без time stop
```

---

## Залежності

**Читає:**
- `FeatureEngine` — hot-loop через `on_features(features)` передається кожен знімок
- `ExecutionEngine` — підписка на `FillEvent`, `OrderUpdate`
- `PositionConfig` — BE triggers, trail offsets, partial sizes
- `clock()`

**Пише:**
- `ExecutionEngine.place_order` / `cancel_order`
- `RiskEngine.on_position_closed(outcome)` при повному закритті
- `JournalLogger` — кожен перехід стану + MAE/MFE snapshot на закритті
- `NotificationService` — входи, виходи, аномалії

**Читають його:**
- DecisionEngine — `has_open_position(symbol)` (щоб не брати дубль)
- UI / статусний ендпоінт — `get_all_positions()`

---

## Стан

```python
@dataclass
class _PositionMgrState:
    positions: dict[str, OpenPosition]      # symbol → position (max 1 per symbol у MVP)
    pending_opens: dict[str, TradePlan]     # plan queued перед fill entry
    closed_log: deque[TradeOutcome]         # ring для UI, maxlen=100
```

Persistence НЕ потрібна: при рестарті бота
1. Читаємо `GET /fapi/v2/positionRisk` і `GET /fapi/v1/openOrders`
2. Якщо є відкрита позиція — реконструюємо `OpenPosition` з мінімальним станом (`ACTIVE`)
3. Якщо є висячі SL/TP без позиції — скасовуємо їх

---

## Ключові алгоритми

### 1. `open(plan)` — entry flow

```python
async def open(self, plan: TradePlan) -> bool:
    if self.has_open_position(plan.symbol):
        logger.warning(f"open rejected: already open on {plan.symbol}")
        return False

    # 1. Entry-ордер (IOC LIMIT для контролю slippage, або MARKET якщо конфіг)
    entry_req = self._build_entry_request(plan)
    entry_result = await self.execution.place_order(entry_req)

    if not entry_result.success or entry_result.status == 'REJECTED':
        logger.error(f"entry rejected: {entry_result.error_msg}")
        return False

    # 2. Записуємо позицію у стані ENTRY_PENDING — FillEvent її "активує"
    pos = OpenPosition(
        plan=plan,
        state=PositionState.ENTRY_PENDING,
        opened_at_ms=self.clock(),
        entry_coid=entry_result.client_order_id,
        sl_coid=None,
        tp_coids=[],
        filled_qty=0, remaining_qty=plan.position_size,
        avg_entry_price=0,
        realized_pnl_usd=0, realized_r=0,
        current_stop_price=plan.stop_price,
        max_favorable_price=plan.entry_price,
        max_adverse_price=plan.entry_price,
        max_favorable_r=0, max_adverse_r=0,
        last_feature_check_ms=self.clock(),
        invalidation_triggered=None,
        time_stop_deadline_ms=(self.clock() + plan.time_stop_ms) if plan.time_stop_ms else None,
    )
    self._state.positions[plan.symbol] = pos
    return True
```

### 2. Обробка entry fill → виставити SL і TP

```python
async def _on_fill(self, fill: FillEvent):
    pos = self._find_position_by_coid(fill.client_order_id)
    if not pos:
        return

    # === Entry fill ===
    if fill.client_order_id == pos.entry_coid:
        # Оновити avg_entry
        total_notional = pos.avg_entry_price * pos.filled_qty + fill.price * fill.qty
        pos.filled_qty += fill.qty
        pos.avg_entry_price = total_notional / pos.filled_qty
        pos.remaining_qty = pos.plan.position_size - pos.filled_qty

        if fill.order_status == 'FILLED':
            # Entry повністю виконаний — ставимо SL і TPs
            pos.state = PositionState.FILLED_NO_PROTECTION
            await self._place_protection(pos)
            pos.state = PositionState.ACTIVE
        else:
            # Partial — нічого поки не робимо, чекаємо
            pass

    # === SL fill ===
    elif fill.client_order_id == pos.sl_coid:
        pos.realized_pnl_usd += fill.realized_pnl_usd
        r = (pos.current_stop_price - pos.avg_entry_price) / \
             (pos.plan.entry_price - pos.plan.stop_price) * \
             (1 if pos.plan.direction == 'LONG' else -1)
        # Коли SL спрацьовує — це може бути BE (0R), trail (+0.5R+), або початковий (-1R)
        # Розрахунок максимально простий: (current_stop - entry) / (entry - initial_stop)
        pos.realized_r += r * (fill.qty / pos.plan.position_size)
        pos.remaining_qty -= fill.qty
        if pos.remaining_qty <= 0:
            await self._close_position_fully(pos, was_stopped=True, reason="sl_hit")

    # === TP fill ===
    elif fill.client_order_id in pos.tp_coids:
        idx = pos.tp_coids.index(fill.client_order_id)   # 0=tp1, 1=tp2, 2=tp3
        pos.realized_pnl_usd += fill.realized_pnl_usd
        pos.realized_r += (idx + 1) * (fill.qty / pos.plan.position_size)
        pos.remaining_qty -= fill.qty

        if idx == 0:                                      # TP1 → BE
            pos.state = PositionState.TP1_HIT
            await self._move_stop_to_breakeven(pos)
        elif idx == 1:                                    # TP2 → trailing
            pos.state = PositionState.TP2_HIT
            await self._switch_to_trailing(pos)
        elif idx == 2 or pos.remaining_qty <= 0:
            await self._close_position_fully(pos, was_stopped=False, reason="tp3_hit")
```

### 3. Protection orders (після entry fill)

```python
async def _place_protection(self, pos: OpenPosition):
    plan = pos.plan
    side_close = OrderSide.SELL if plan.direction == 'LONG' else OrderSide.BUY

    # SL — STOP_MARKET reduce_only, за весь обсяг
    sl_req = OrderRequest(
        symbol=plan.symbol, side=side_close, type=OrderType.STOP_MARKET,
        qty=pos.filled_qty, stop_price=plan.stop_price,
        reduce_only=True, close_position=False,
    )
    sl_result = await self.execution.place_order(sl_req)
    pos.sl_coid = sl_result.client_order_id

    # TP ladder 50/25/25
    sizes = [
        pos.filled_qty * self.config.tp_split[0],
        pos.filled_qty * self.config.tp_split[1],
        pos.filled_qty * self.config.tp_split[2],
    ]
    prices = [plan.tp1_price, plan.tp2_price, plan.tp3_price]
    for i, (sz, pr) in enumerate(zip(sizes, prices)):
        tp_req = OrderRequest(
            symbol=plan.symbol, side=side_close,
            type=OrderType.TAKE_PROFIT_MARKET,
            qty=sz, stop_price=pr, reduce_only=True,
        )
        tp_result = await self.execution.place_order(tp_req)
        pos.tp_coids.append(tp_result.client_order_id)
```

### 4. Break-even після TP1

```python
async def _move_stop_to_breakeven(self, pos: OpenPosition):
    # Cancel стан SL
    await self.execution.cancel_order(pos.plan.symbol, pos.sl_coid)

    # Новий SL на entry price + buffer (щоб не вилетіти на шумі)
    buffer = self.config.breakeven_buffer_ticks * self.config.tick_size
    new_stop = pos.avg_entry_price + (buffer if pos.plan.direction == 'LONG' else -buffer)
    pos.current_stop_price = new_stop

    side_close = OrderSide.SELL if pos.plan.direction == 'LONG' else OrderSide.BUY
    new_sl = OrderRequest(
        symbol=pos.plan.symbol, side=side_close, type=OrderType.STOP_MARKET,
        qty=pos.remaining_qty, stop_price=new_stop, reduce_only=True,
    )
    result = await self.execution.place_order(new_sl)
    pos.sl_coid = result.client_order_id

    logger.info(f"{pos.plan.symbol}: moved SL to BE at {new_stop}")
```

### 5. Trailing stop після TP2

```python
async def _switch_to_trailing(self, pos: OpenPosition):
    # Скасовуємо TP3 (замінюємо на trailing SL)
    # Trailing: SL рухається у бік профіту, не назад
    # Реалізація — на кожному `on_features` перераховуємо бажаний SL
    pos._trailing_active = True
    # TP3 лишаємо як є (якщо ціна ривково пробіжить 3R — зафіксуємо залишок)

async def _update_trailing(self, pos: OpenPosition, last_price: float):
    if not getattr(pos, '_trailing_active', False):
        return
    trail_ticks = self.config.trail_distance_ticks
    tick = self.config.tick_size
    if pos.plan.direction == 'LONG':
        desired_stop = last_price - trail_ticks * tick
        if desired_stop > pos.current_stop_price:
            await self._move_stop(pos, desired_stop)
    else:
        desired_stop = last_price + trail_ticks * tick
        if desired_stop < pos.current_stop_price:
            await self._move_stop(pos, desired_stop)
```

### 6. `on_features` — моніторинг invalidation + trailing + time stop

```python
async def on_features(self, features: Features) -> None:
    pos = self._state.positions.get(features.snapshot.symbol)
    if not pos or pos.state in (PositionState.CLOSED, PositionState.ENTRY_PENDING):
        return

    last_price = features.snapshot.last_price

    # MFE/MAE tracking
    self._update_extremes(pos, last_price)

    # Exit on signal failure (ПЕРВИННИЙ)
    ic = self._check_invalidations(pos, features)
    if ic:
        pos.invalidation_triggered = ic
        await self._close_position_fully(pos, was_stopped=False,
                                          reason=f"signal_invalidated:{ic.kind}:{ic.description}")
        return

    # Time stop (РЕЗЕРВНИЙ)
    if pos.time_stop_deadline_ms and self.clock() > pos.time_stop_deadline_ms:
        await self._close_position_fully(pos, was_stopped=False, reason="time_stop")
        return

    # Trailing update
    if pos.state == PositionState.TP2_HIT:
        await self._update_trailing(pos, last_price)

def _check_invalidations(self, pos: OpenPosition, f: Features) -> InvalidationCondition | None:
    # Не активуємо invalidation після TP1 (ми вже у BE — нехай ринок сам вирішує)
    if pos.state in (PositionState.TP1_HIT, PositionState.TP2_HIT):
        return None

    for ic in pos.plan.invalidation_conditions:
        if ic.kind == 'PRICE_BEYOND':
            if pos.plan.direction == 'LONG' and f.snapshot.last_price <= ic.price_level:
                return ic
            if pos.plan.direction == 'SHORT' and f.snapshot.last_price >= ic.price_level:
                return ic

        elif ic.kind == 'OPPOSITE_DELTA':
            # У LONG — тривога якщо delta різко негативна
            if pos.plan.direction == 'LONG' and f.delta_2s < ic.delta_threshold:
                return ic
            if pos.plan.direction == 'SHORT' and f.delta_2s > abs(ic.delta_threshold):
                return ic

        elif ic.kind == 'BOOK_TURNS':
            # напр.: для LONG — ворожа книга (weighted_imbalance різко перекинулася на ASK)
            if pos.plan.direction == 'LONG' and f.weighted_imbalance < -0.4:
                return ic
            if pos.plan.direction == 'SHORT' and f.weighted_imbalance > 0.4:
                return ic

        elif ic.kind == 'TIME_EXPIRED':
            # Ця умова — ПЕРЕВІРКА до входу (SetupDetector); після входу
            # використовуємо окремий `time_stop_deadline_ms`. Тут просто skip.
            pass

    return None
```

### 7. Повне закриття — маркетом

```python
async def _close_position_fully(self, pos: OpenPosition, *,
                                 was_stopped: bool, reason: str) -> None:
    if pos.state == PositionState.CLOSING:
        return
    pos.state = PositionState.CLOSING

    # 1. Скасувати всі висячі ордери (SL і TP'и що лишились)
    await self.execution.cancel_all(pos.plan.symbol)

    # 2. Якщо залишок > 0 — маркет-ордер close_position
    if pos.remaining_qty > 0 and not was_stopped:
        side = OrderSide.SELL if pos.plan.direction == 'LONG' else OrderSide.BUY
        close_req = OrderRequest(
            symbol=pos.plan.symbol, side=side, type=OrderType.MARKET,
            qty=pos.remaining_qty, reduce_only=True,
        )
        await self.execution.place_order(close_req)
        # Fill дійде через user stream; в on_fill це буде додано до realized_pnl

    # 3. Формуємо outcome і повідомляємо RiskEngine (коли всі філи отримано)
    # → виставляємо дедлайн (3s), якщо не всі філи дійшли — все одно closing
    asyncio.create_task(self._finalize_after_delay(pos, reason, was_stopped))

async def _finalize_after_delay(self, pos: OpenPosition, reason: str, was_stopped: bool):
    await asyncio.sleep(3)
    outcome = TradeOutcome(
        plan=pos.plan,
        closed_at_ms=self.clock(),
        realized_r=pos.realized_r,
        realized_usd=pos.realized_pnl_usd,
        max_favorable_r=pos.max_favorable_r,
        max_adverse_r=pos.max_adverse_r,
        was_stopped=was_stopped,
        fees_usd=self._sum_fees(pos),
    )
    self.risk.on_position_closed(outcome)
    self._state.closed_log.append(outcome)
    self._state.positions.pop(pos.plan.symbol, None)
    pos.state = PositionState.CLOSED

    logger.info(f"closed {pos.plan.symbol}: {outcome.realized_r:+.2f}R ({reason})")
    await self.notifier.send(
        f"📕 Closed {pos.plan.symbol} {outcome.realized_r:+.2f}R — {reason}",
        AlertLevel.INFO)
```

---

## State machine diagram

```
        open(plan)              FillEvent(entry)
  IDLE ───────────► ENTRY_PENDING ──────────► FILLED_NO_PROTECTION
                                                       │
                                                       ▼
                                                    ACTIVE
                                                    │   │   │
                             FillEvent(tp1) ────────┘   │   │
                                   │                    │   │
                                   ▼                    │   │
                                TP1_HIT                 │   │
                                (SL moved to BE)        │   │
                                   │                    │   │
                             FillEvent(tp2) ────────────┘   │
                                   │                        │
                                   ▼                        │
                                TP2_HIT                     │
                                (trailing on)               │
                                   │                        │
    FillEvent(sl/tp3) | invalidation | time_stop ───────────┘
                                   ▼
                                CLOSING ──(fills settle)──► CLOSED
                                   │
                                   └──► RiskEngine.on_position_closed(outcome)
```

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Entry IOC partial filled, решта expired | `filled_qty < plan.position_size`. Виставляємо SL/TP на filled_qty only; решту в Journal як "unfilled" |
| SL placement failed (API error) | CRITICAL alert, ЯКЩО позиція >0 — миттєвий emergency close по MARKET |
| TP1 і SL спрацювали "одночасно" (у межах 50ms) | Перший FillEvent рухає state; другий дивиться на поточний state; стандартний код оброблює обидва |
| Invalidation тригернувся, але до закриття на ринку прилетів SL fill | Обидва виконуються, `_close_position_fully(already_in_closing)` — no-op; outcome рахується коректно |
| Рестарт бота з відкритою позицією | Recover з `positionRisk` + `openOrders` → стан ACTIVE, plan не відновиш, але SL/TP керуються фоновим "passive" режимом (без invalidation checks, бо немає Features-контексту) |
| Features приходять дуже часто (> 100Hz) | `on_features` має p99 < 50µs — lookup позиції, простий check. Не робимо I/O у hot loop |
| Time stop після TP1 | Після TP1 ми вже у BE, time stop можна ІГНОРУВАТИ (див. config) — нехай ринок вирішує |

---

## Конфіги

```yaml
position_manager:
  tp_split: [0.5, 0.25, 0.25]                # TP1 50%, TP2 25%, TP3 25%
  tick_size: 0.1

  breakeven:
    activate_on: tp1                          # або tp2
    buffer_ticks: 1                           # BE = entry + 1 тік у бік профіту

  trailing:
    activate_on: tp2
    distance_ticks: 5                         # trail stop на 5 тіків нижче/вище high/low
    update_min_price_move_ticks: 1            # не оновлюємо SL на кожному тіку

  invalidation:
    enabled: true
    disable_after_state: TP1_HIT              # після TP1 не слухаємо invalidation

  time_stop:
    enabled: true                             # включений — але per-setup null у плані = без time stop
    disable_after_state: TP1_HIT              # після BE time stop не спрацьовує

  entry:
    type: IOC_LIMIT                           # або MARKET
    ioc_offset_ticks: 1                       # LIMIT на best + 1 тік (LONG) для швидкого філа

  emergency:
    finalize_delay_sec: 3                     # чекаємо філи після cancel_all
    market_close_fallback: true
```

---

## Як тестувати

### Unit
- `_move_stop_to_breakeven`: entry=100, buffer=1 тік (0.1), LONG → new_stop=100.1
- `_check_invalidations` для кожного kind — заготовка Features що тригерить → повертає `InvalidationCondition`
- Після TP1_HIT → invalidation не тригериться (disable_after_state)
- `_update_trailing`: LONG, last=105, trail=5 тіків, current_stop=100 → new_stop=104.9 > 100 → move
- Trailing не рухає стоп назад (last=103 після 105 → stop лишається на 104.9)

### Integration (testnet)
- Сценарій happy path: open → entry fill → SL+TPs → TP1 fill → BE SL → TP2 → trailing → TP3 → closed, R = +2.25 (0.5*1 + 0.25*2 + 0.25*3)
- Сценарій SL: open → entry → SL triggered → closed, R = -1
- Сценарій invalidation до TP1: open → entry → (симулюємо Features з hostile book) → market close, R між -1 і 0

### Property
- `realized_r` на закритті = зважена сума з outcomes кожної фази; ніколи не має |realized_r| > 5 (sanity)
- Після `_close_position_fully`, `positions[symbol]` видалено

### Regression
- "Golden" trades — 5 записаних сесій з фіксованими FillEvent'ами + Features → очікуваний TradeOutcome baseline
