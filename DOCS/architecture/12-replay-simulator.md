---
name: 12-replay-simulator
description: Архітектура модуля Replay/Simulator — підміна MarketDataGateway історичними даними та симуляція ExecutionEngine; решта коду (FeatureEngine, SetupDetector, Decision, Risk, PositionManager) — ідентична продакшн
type: project
---

# 12. Replay / Simulator — бектест і dry-run

## Відповідальність

**Лабораторія.** Дозволяє програти минулі дні через ТУ САМУ стратегію, що в проді. Головний принцип: **жоден з модулів 04-08, 10-11, 13 не знає, що він у симуляції**. Підмінюється тільки два шари:

- `MarketDataGateway` → `ReplayGateway` (читає з файлів замість WS)
- `ExecutionEngine` → `SimulatedExecutionEngine` (імітує філи замість REST)

Це дає **гарантію**: якщо симуляція показує +15R на зафіксованій серії днів, а потім ми змінили вагу у DecisionEngine — перезапустимо симуляцію і побачимо різницю. Жодних "але в проді там інший код".

### Що робить:
- `ReplayGateway` — читає raw-файли (`data/raw/2026-04-21/{symbol}.jsonl.gz`) і емітує івенти у реальному масштабі часу або прискорено (`speed=10x`, `speed=∞`)
- Підтримує кілька форматів raw: наш власний JSONL-dump (write-through з Gateway), Binance історичні archives, кастомні fixture-файли
- Синхронізує тикання кількох символів по `event_time_ms`
- `SimulatedExecutionEngine` — імітує заповнення ордерів на основі tape-даних (aggTrade):
  - MARKET: філ миттєво по next tick + проковзування (`slippage_model`)
  - LIMIT: чекає поки ціна торкнеться (`touch`) або перетне (`cross`) — конфігурується
  - STOP_MARKET: тригериться коли ціна перетнула `stop_price`
  - Комісія per-fill (0.04% taker / 0.02% maker Binance default)
- Підраховує PnL на льоту, пише у Journal як реальний
- Дає **deterministic** результат: той самий вхід + той самий seed → той самий трейд-ліст

### Що НЕ робить:
- НЕ запускає справжні REST/WS-запити (повна ізоляція)
- НЕ модифікує модулі 04-08, 10, 13 (їх код тотожний продакшн)
- НЕ тестує network/timeout edge cases (це робить chaos-test у Integration)
- НЕ моделює маркет-мейкер/queue position ідеально (це лаба, не HFT)

---

## Входи / виходи

### Публічний API:

```python
class ReplayRunner:
    def __init__(self, config: ReplayConfig):
        self.cfg = config
        self.gateway = ReplayGateway(config.gateway)
        self.exec = SimulatedExecutionEngine(config.simulator, self.gateway)
        # Решта — звичайні:
        self.feature_engine = FeatureEngine(config.features)
        self.setup_detector = SetupDetector(config.setups)
        self.regime = MarketRegime(config.regime)
        self.risk = RiskEngine(config.risk, self.regime, InMemoryRiskStore())
        self.decision = DecisionEngine(config.decision, self.regime, self.risk, ...)
        self.position_mgr = PositionManager(config.position, self.exec, self.risk, ...)
        self.journal = JournalLogger(config.journal)
        self.expectancy = ExpectancyTracker(config.expectancy, self.journal)

    async def run(self) -> ReplayResult: ...
    async def stop(self) -> None: ...

class ReplayGateway:
    """Drop-in replacement для MarketDataGateway."""
    async def start(self, symbols: list[str]) -> None
    async def stop(self) -> None
    def on_trade(self, cb) -> None
    def on_depth_diff(self, cb) -> None
    def on_book_ticker(self, cb) -> None
    def on_kline(self, cb) -> None
    def on_user_event(self, cb) -> None                   # для філ-апдейтів з SimExec

class SimulatedExecutionEngine:
    """Drop-in replacement для ExecutionEngine."""
    # Ідентичний API: place_order, cancel_order, place_bracket, on_fill, ...
```

### Типи:

```python
@dataclass(frozen=True)
class ReplayConfig:
    data_dir: str                                        # 'data/raw'
    date_from: str                                       # '2026-04-01'
    date_to: str                                         # '2026-04-21'
    symbols: list[str]
    speed: float                                          # 1.0 = real time; 0 = as-fast-as-possible
    rng_seed: int                                         # для детермінізму
    gateway: ReplayGatewayConfig
    simulator: SimulatorConfig
    # ... передаємо в dataclass'и відповідних модулів:
    features: FeatureEngineConfig
    setups: SetupsConfig
    regime: RegimeConfig
    risk: RiskConfig
    decision: DecisionConfig
    position: PositionConfig
    journal: JournalConfig
    expectancy: ExpectancyConfig

@dataclass(frozen=True)
class SimulatorConfig:
    limit_fill_policy: Literal['touch', 'cross']         # коли LIMIT вважається філом
    taker_fee_rate: float = 0.0004                        # 0.04%
    maker_fee_rate: float = 0.0002
    slippage_model: Literal['zero', 'fixed_ticks', 'spread_based'] = 'spread_based'
    slippage_fixed_ticks: int = 1
    partial_fills: bool = False                          # MVP: повний філ одразу
    latency_ms: int = 50                                  # симуляція latency між request і fill

@dataclass(frozen=True)
class ReplayResult:
    date_from: str
    date_to: str
    trades: list[TradeOutcome]
    total_r: float
    total_usd: float
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float
    max_drawdown_r: float
    max_consecutive_losses: int
    per_setup_stats: dict[str, SetupStats]
    per_regime_stats: dict[str, RegimeStats]
    journal_path: str                                     # де лежить JSONL цього runу
```

---

## Залежності

**Читає:**
- `data/raw/YYYY-MM-DD/{symbol}.jsonl.gz` — власний dump Gateway (writethrough під час live)
- Альтернативно: Binance aggTrades/bookTicker archives (конвертер окремо)
- ExchangeInfo snapshot на `date_from` (бо filter'и змінюються з часом)

**Пише:**
- `journal/replay_YYYYMMDD_HHMMSS.jsonl` — окремий namespace (префікс `replay_`)
- `ReplayResult` — JSON-звіт в `data/replay_results/`

**НЕ читає/пише:**
- Жодних біржових endpoint'ів
- Жодних user stream'ів

**Читають його:** оператор (CLI / Jupyter notebook).

---

## Стан

```python
@dataclass
class _ReplayState:
    current_sim_time_ms: int                              # єдиний годинник симуляції
    symbol_file_iters: dict[str, Iterator[RawEvent]]      # по одному на символ
    event_queue: list[tuple[int, str, RawEvent]]          # heap за event_time_ms
    pending_orders: dict[str, _SimOrder]                  # client_order_id → state
    symbol_book: dict[str, _LastKnownBook]                # для фактичної ціни філу

@dataclass
class _SimOrder:
    request: OrderRequest
    client_order_id: str
    submitted_ms: int
    trigger_ms: int | None                                # = submitted + latency_ms
    status: Literal['PENDING', 'WAITING_PRICE', 'FILLED', 'CANCELED']
    filled_qty: float = 0.0
    filled_price: float | None = None
```

---

## Ключові алгоритми

### 1. Merge-sort потоків кількох символів

Кожен файл містить події одного символу, упорядковані по `event_time_ms`. Треба віддавати їх **глобально** відсортованими.

```python
async def _event_loop(self):
    heap: list[tuple[int, str, RawEvent]] = []

    # Ініціалізація: по одному event з кожного файлу
    for sym, it in self._state.symbol_file_iters.items():
        first = next(it, None)
        if first:
            heapq.heappush(heap, (first.event_time_ms, sym, first))

    while heap and not self._shutdown:
        t_ms, sym, ev = heapq.heappop(heap)
        self._state.current_sim_time_ms = t_ms

        # Дати симулятору можливість обробити pending ордери ДО події
        await self._sim_exec.on_clock_tick(t_ms)

        # Диспетч події у відповідний callback
        await self._dispatch(ev, sym)

        # Підняти наступну подію з того самого файлу
        nxt = next(self._state.symbol_file_iters[sym], None)
        if nxt:
            heapq.heappush(heap, (nxt.event_time_ms, sym, nxt))

        # Швидкість відтворення
        if self.cfg.speed > 0:
            await self._throttle(t_ms)
```

### 2. Throttle (real-time / прискорений)

```python
async def _throttle(self, sim_time_ms: int):
    """speed=1.0 → рівно як в житті; speed=10.0 → x10 швидше; speed=0 → as-fast-as-possible."""
    if self._wall_start_ms is None:
        self._wall_start_ms = time_ms()
        self._sim_start_ms = sim_time_ms
        return

    elapsed_sim = sim_time_ms - self._sim_start_ms
    target_wall = self._wall_start_ms + elapsed_sim / self.cfg.speed
    delta = target_wall - time_ms()
    if delta > 0:
        await asyncio.sleep(delta / 1000)
```

### 3. Simulated order filling

```python
async def place_order(self, req: OrderRequest) -> OrderResult:
    coid = req.client_order_id or self._gen_coid(...)
    sim_order = _SimOrder(
        request=req, client_order_id=coid,
        submitted_ms=self.clock_ms(),
        trigger_ms=self.clock_ms() + self.cfg.latency_ms,
        status='PENDING',
    )
    self._state.pending_orders[coid] = sim_order

    return OrderResult(
        success=True, client_order_id=coid, exchange_order_id=self._next_id(),
        status='NEW', filled_qty=0, avg_fill_price=None,
        error_code=None, error_msg=None,
        request_sent_ms=sim_order.submitted_ms,
        response_received_ms=sim_order.submitted_ms,
    )

async def on_clock_tick(self, sim_time_ms: int):
    """Викликається ReplayRunner перед кожною подією. Перевіряє чи можна філити."""
    for coid, order in list(self._state.pending_orders.items()):
        if order.status != 'PENDING' and order.status != 'WAITING_PRICE':
            continue
        if sim_time_ms < (order.trigger_ms or 0):
            continue

        book = self._state.symbol_book.get(order.request.symbol)
        if not book:
            continue

        fill = self._try_fill(order, book, sim_time_ms)
        if fill:
            await self._emit_fill(fill)
            order.status = 'FILLED'
            self._state.pending_orders.pop(coid, None)

def _try_fill(self, o: _SimOrder, book: _LastKnownBook, t_ms: int) -> _FillData | None:
    req = o.request
    side = req.side

    if req.type == OrderType.MARKET:
        price = book.ask if side == OrderSide.BUY else book.bid
        slip = self._slippage(book, side)
        fill_price = price + slip if side == OrderSide.BUY else price - slip
        return _FillData(req.symbol, o.client_order_id, req.qty, fill_price, is_maker=False, ms=t_ms)

    if req.type == OrderType.LIMIT:
        if side == OrderSide.BUY:
            touched = book.ask <= req.price if self.cfg.limit_fill_policy == 'touch' \
                                             else book.ask < req.price
        else:
            touched = book.bid >= req.price if self.cfg.limit_fill_policy == 'touch' \
                                             else book.bid > req.price
        if touched:
            return _FillData(req.symbol, o.client_order_id, req.qty, req.price,
                             is_maker=True, ms=t_ms)
        return None

    if req.type == OrderType.STOP_MARKET:
        # Тригер: ціна перетнула stop_price у невигідний для нас бік
        triggered = (book.last_trade_price <= req.stop_price if side == OrderSide.BUY else
                     book.last_trade_price >= req.stop_price)
        if triggered:
            # Після тригера — market-філ з проковзуванням
            price = book.ask if side == OrderSide.BUY else book.bid
            slip = self._slippage(book, side)
            fill_price = price + slip if side == OrderSide.BUY else price - slip
            return _FillData(req.symbol, o.client_order_id, req.qty, fill_price,
                             is_maker=False, ms=t_ms)
        return None

    return None
```

### 4. Slippage

```python
def _slippage(self, book: _LastKnownBook, side: OrderSide) -> float:
    m = self.cfg.slippage_model
    if m == 'zero':
        return 0.0
    if m == 'fixed_ticks':
        return self.cfg.slippage_fixed_ticks * book.tick_size
    if m == 'spread_based':
        # Половина спреду на нашу сторону
        spread = book.ask - book.bid
        return spread / 2
    return 0.0
```

### 5. User-stream emulation (для PositionManager)

PositionManager підписаний на `on_fill`. SimulatedExecutionEngine генерує той самий `FillEvent` зсередини `_emit_fill`:

```python
async def _emit_fill(self, data: _FillData):
    req = self._state.pending_orders[data.coid].request
    commission = data.qty * data.price * (
        self.cfg.maker_fee_rate if data.is_maker else self.cfg.taker_fee_rate)

    fill = FillEvent(
        symbol=data.symbol,
        client_order_id=data.coid,
        exchange_order_id=self._oid_by_coid[data.coid],
        side=req.side,
        qty=data.qty,
        price=data.price,
        is_maker=data.is_maker,
        commission_usd=commission,
        filled_cumulative=data.qty,
        order_status='FILLED',
        timestamp_ms=data.ms,
        realized_pnl_usd=self._compute_realized_pnl(req, data),
    )
    for cb in self._fill_callbacks:
        await cb(fill)
```

### 6. Зведена статистика

```python
def _compile_result(self) -> ReplayResult:
    trades = list(self.journal.iter_closed_trades_all())
    total_r = sum(t.realized_r for t in trades)
    wins = [t for t in trades if t.realized_r > 0]
    losses = [t for t in trades if t.realized_r < 0]

    return ReplayResult(
        date_from=self.cfg.date_from,
        date_to=self.cfg.date_to,
        trades=trades,
        total_r=total_r,
        win_rate=len(wins) / max(1, len(trades)),
        avg_win_r=mean(t.realized_r for t in wins) if wins else 0,
        avg_loss_r=mean(t.realized_r for t in losses) if losses else 0,
        expectancy_r=total_r / max(1, len(trades)),
        max_drawdown_r=self._compute_mdd(trades),
        max_consecutive_losses=self._compute_max_streak(trades),
        per_setup_stats=self._group_by(trades, lambda t: t.setup_type),
        per_regime_stats=self._group_by(trades, lambda t: t.regime),
        journal_path=str(self.journal.path_root),
    )
```

### 7. Write-through Gateway → raw-file (для live-dump майбутніх реплеїв)

Не частина цього модуля, але **корелює**: у проді `MarketDataGateway` пише **усі** raw-події в файл (окремий background consumer). Через це завтра можна відтворити будь-який торговий день точно.

```
data/raw/2026-04-21/BTCUSDT.jsonl.gz
data/raw/2026-04-21/ETHUSDT.jsonl.gz
```

Формат рядка: `{"t":"trade"|"depth"|"book"|"kline","ms":..., "payload":{...}}`.

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Немає файлу для символу на дату | `FileNotFoundError` → fatal, зупинити run (не "тихо пропустити") |
| Події в файлі не відсортовані | Detector: перевіряємо монотонність `event_time_ms`; при порушенні → fatal і звіт |
| Файл обривається посередині рядка (gzip tail corruption) | Skip останнього рядка з warning (у журнал) |
| STOP_MARKET між двома тиками (ціна "перестрибнула") | Тригер на першому тику де перетнуто; slippage +1 тік як штраф |
| Ордер залишився PENDING на кінці дня | На stop() — переводимо в CANCELED, пишемо в journal |
| Ордер LIMIT на ціні що буде досягнута тільки завтра | Лишається активним; на rotation дня — автоцанцел |
| Повний fill у той самий tick коли SL поставлено | Обидві події у черзі; порядок = порядок виставлення з PositionManager (ентрі → SL) |
| speed=0 на великому діапазоні | Throttle відключається, run тільки CPU-bound; progress log раз на N подій |
| Різні символи мають різні тривалості файлів | Merge heap сам розбирається — коли один файл закінчується, інший триває |
| rng_seed конфіг: різні запуски → різний результат | БУГ. Детермінізм порушено. Додати assert на deterministic test — одні й ті самі входи, два запуски, ідентичний журнал |

---

## Конфіги

```yaml
replay:
  data_dir: data/raw
  date_from: 2026-04-01
  date_to: 2026-04-21
  symbols: [BTCUSDT, ETHUSDT]
  speed: 0                          # 0 = max
  rng_seed: 42

  gateway:
    validate_monotonic: true
    skip_corrupt_lines: true

  simulator:
    limit_fill_policy: touch
    taker_fee_rate: 0.0004
    maker_fee_rate: 0.0002
    slippage_model: spread_based
    slippage_fixed_ticks: 1
    partial_fills: false
    latency_ms: 50

  # Решта конфігів (features/setups/regime/risk/decision/position)
  # — імпортуються з того самого YAML що в проді, без змін.
```

---

## Як тестувати

### Unit
- `_try_fill` MARKET BUY: book.ask=100, slip=0.1 → fill_price=100.1
- `_try_fill` LIMIT BUY price=99, book.ask=99.5 → немає філу; book.ask=99 (touch policy) → філ
- `_try_fill` STOP_MARKET SELL stop=95, last_trade=94.9 → тригер; last=95.1 → немає
- Slippage spread_based: ask=100, bid=99.8 → slip=0.1
- Merge-sort: 2 файли з перемішаними часами → output монотонний
- Throttle `speed=0`: послідовні await'и, wall-time не росте суттєво
- Fee calc: qty=1, price=100, taker=0.04% → commission=0.04

### Integration
- Сценарій: 1 годинний файл з реальними даними, 1 сетап у цьому годину → ReplayResult містить 1 TradeOutcome
- Детермінізм: run двічі з однаковим seed → `trades` ідентичні байт-у-байт у journal
- STOP_MARKET тригериться коли тік падає через рівень; перевіряємо що filled_price має slippage
- LIMIT ордер не філиться, якщо ціна не досягла → закривається як CANCELED на stop()

### Regression
- Fixture "golden day 2026-04-15" → зафіксована `ReplayResult` baseline. Будь-яка зміна стратегії → diff показує чи метрики покращились/погіршились

### Performance
- Replay 1 день × 2 символа з speed=0 → < 60 сек (профайл, якщо повільніше)

### Parity
- Дзеркальний тест: запустити реальний `ExecutionEngine` на testnet і `SimulatedExecutionEngine` на тому ж потоці тиків → перевірити що обидва видають сумірні філ-ціни (спред-based slippage близько до реальності)
