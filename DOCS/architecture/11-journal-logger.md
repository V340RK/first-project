---
name: 11-journal-logger
description: Архітектура модуля Journal Logger — структурований JSONL audit trail усіх рішень, ордерів, філів, outcome; основа для post-mortem, expectancy, regression snapshot-тестів
type: project
---

# 11. JournalLogger — журнал і audit trail

## Відповідальність

**Пам'ять системи.** Кожне **рішення**, кожен **ордер**, кожен **філ**, кожен **outcome** — пишеться у structured JSONL. Якщо завтра трейд виявиться поганим — по `trade_id` піднімаємо ПОВНИЙ слід: який був регім, які фактори, який score, який SL виставлено, коли рушено на BE, MFE/MAE, причина закриття.

Це також джерело для **ExpectancyTracker** (читає outcome'и), **Replay/Simulator** (reproduce через candidate-stream) і **regression snapshot-тестів** (фіксовані JSON-файли).

### Що робить:
- Пише **append-only** JSONL файли з rotation по днях (`journal/2026-04-21.jsonl`)
- Підписується на події всіх шарів системи — один центральний sink
- Присвоює `trade_id` (= `TradePlan.candidate.features_hash` + accepted timestamp) для зв'язку всіх записів однієї угоди
- Flush на диск у batch'ах (кожні 200ms або 100 записів — не блокує hot-loop)
- Тримає seq-номер в межах доби (`seq: 1, 2, 3, ...`) для відновлення порядку
- **Fsync** кожні N секунд (не кожен запис — занадто повільно)
- Ротує файли опівночі UTC; старі gzip'ить
- Readonly-query API (для UI, post-mortem CLI): `iter_trades(date)`, `get_trade(trade_id)`

### Що НЕ робить:
- НЕ приймає рішень (read-only спостерігач)
- НЕ зберігає ринкові raw-дані (це InfluxDB/Parquet окремо, поза scope)
- НЕ шле алерти (NotificationService окремо)
- НЕ тримає in-memory дамп усіх трейдів за місяць (лічить на льоту по потребі)

---

## Входи / виходи

### Публічний API:

```python
class JournalLogger:
    def __init__(self, config: JournalConfig):
        self.path_root = Path(config.journal_dir)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self._writer_task: asyncio.Task | None = None

    # === Lifecycle ===
    async def start(self) -> None
    async def stop(self) -> None

    # === Sink API (усі шлють через один метод) ===
    def log(self, event: JournalEvent) -> None           # non-blocking put
    async def log_async(self, event: JournalEvent) -> None

    # === Query (read-only) ===
    def iter_events(self, date_utc: str,
                    kinds: set[EventKind] | None = None) -> Iterator[JournalEvent]
    def get_trade(self, trade_id: str) -> TradeRecord | None   # всі події по одному trade
    def iter_closed_trades(self, date_utc: str) -> Iterator[TradeRecord]
```

### Типи:

```python
class EventKind(Enum):
    # === Decision layer ===
    SETUP_CANDIDATE_GENERATED = "setup_candidate"        # SetupDetector знайшов (до decision)
    DECISION_ACCEPTED = "decision_accepted"              # DecisionEngine обрав переможця
    DECISION_REJECTED = "decision_rejected"              # всі rejected (з reason)
    RISK_ACCEPTED = "risk_accepted"                      # RiskEngine пропустив
    RISK_REJECTED = "risk_rejected"                      # RiskEngine заблокував

    # === Execution layer ===
    ORDER_REQUESTED = "order_requested"                  # ExecutionEngine надіслав
    ORDER_RESPONSE = "order_response"                    # REST-відповідь
    ORDER_UPDATE = "order_update"                        # status change з user stream
    FILL = "fill"                                        # частковий/повний філ
    ORDER_CANCELLED = "order_cancelled"

    # === Position layer ===
    POSITION_OPENED = "position_opened"                  # entry filled
    STOP_MOVED = "stop_moved"                            # BE / trailing
    INVALIDATION_TRIGGERED = "invalidation_triggered"    # primary exit
    TIME_STOP_TRIGGERED = "time_stop_triggered"          # backup exit
    POSITION_CLOSED = "position_closed"                  # фіналізовано
    TRADE_OUTCOME = "trade_outcome"                      # агрегат для expectancy

    # === Regime / risk ===
    REGIME_CHANGED = "regime_changed"
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    KILL_SWITCH_CLEARED = "kill_switch_cleared"
    DAILY_ROLLOVER = "daily_rollover"

    # === Expectancy ===
    EXPECTANCY_SUSPENDED = "expectancy_suspended"        # setup×symbol відключено за негативною E
    EXPECTANCY_RESUMED = "expectancy_resumed"            # знову активовано після cooldown

    # === Infra ===
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    ERROR = "error"
    WARNING = "warning"
    HEARTBEAT = "heartbeat"                              # раз на хв

@dataclass(frozen=True)
class JournalEvent:
    seq: int                                             # заповнює writer
    timestamp_ms: int
    kind: EventKind
    trade_id: str | None                                 # якщо подія належить угоді
    symbol: str | None
    payload: dict                                        # вільний JSON-сумісний dict
    schema_version: int = 1

@dataclass
class TradeRecord:
    """Агрегат усіх подій однієї угоди — будується on-demand при query."""
    trade_id: str
    symbol: str
    opened_at_ms: int | None
    closed_at_ms: int | None
    setup_type: str | None
    direction: str | None
    realized_r: float | None
    events: list[JournalEvent]
```

---

## Залежності

**Читає:**
- `JournalConfig` — шлях, rotation settings, flush interval
- (query-mode) — файлова система `journal/*.jsonl`

**Пише:**
- `journal/YYYY-MM-DD.jsonl` — поточний файл
- `journal/YYYY-MM-DD.jsonl.gz` — архівовані
- `journal/index.sqlite` (опційно, для швидкого `get_trade`) — мапа `trade_id → (date, offset_start, offset_end)`

**Читають його:**
- ExpectancyTracker — `iter_closed_trades` раз на N хвилин
- Replay/Simulator — `iter_events(date, kinds={SETUP_CANDIDATE, DECISION_*})` для реплею без біржі
- Post-mortem CLI / UI — довільні запити
- Regression тести — snapshot assertion

**Пишуть у нього:** усі 12 модулів (centralized sink).

---

## Стан

```python
@dataclass
class _WriterState:
    current_file: TextIO                                 # open append
    current_date: str                                    # '2026-04-21'
    current_seq: int                                     # для дня
    unflushed_bytes: int
    last_fsync_ms: int
    buffer: list[JournalEvent]                           # drain queue у batch
```

Стан короткоживучий — `current_seq` скидається на 1 при ротації. Після рестарту читаємо останній рядок поточного файлу, беремо `seq + 1`.

Persistence: самі файли — і є persistence. Окремого snapshot'а стану писати не треба.

---

## Ключові алгоритми

### 1. Append (non-blocking)

```python
def log(self, event: JournalEvent) -> None:
    """Викликається з hot-loop. НЕ блокує."""
    try:
        self._queue.put_nowait(event)
    except asyncio.QueueFull:
        # Критично: якщо чергу переповнено — значить writer повис.
        # Втрачаємо запис, але не валимо торгівлю. Counter для health.
        self._dropped_count += 1
        logger.error(f"journal queue full, event dropped: {event.kind}")
```

### 2. Writer loop (background)

```python
async def _writer_loop(self):
    while not self._shutdown:
        batch: list[JournalEvent] = []
        try:
            # Чекаємо хоча б одну подію
            first = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            batch.append(first)
            # Drain по-максимуму за 1 ітерацію
            while len(batch) < self.config.batch_max and not self._queue.empty():
                batch.append(self._queue.get_nowait())
        except asyncio.TimeoutError:
            # Періодично дивимося на fsync / rotation навіть без подій
            pass

        if batch:
            await self._write_batch(batch)

        await self._maybe_fsync()
        await self._maybe_rotate()

async def _write_batch(self, batch: list[JournalEvent]):
    lines = []
    for ev in batch:
        ev = replace(ev, seq=self._state.current_seq)
        self._state.current_seq += 1
        lines.append(json.dumps(_asjson(ev), separators=(',', ':')) + '\n')
    data = ''.join(lines)
    self._state.current_file.write(data)
    self._state.unflushed_bytes += len(data)

async def _maybe_fsync(self):
    now = time_ms()
    if (now - self._state.last_fsync_ms > self.config.fsync_interval_ms
            or self._state.unflushed_bytes > self.config.fsync_bytes_threshold):
        self._state.current_file.flush()
        os.fsync(self._state.current_file.fileno())
        self._state.last_fsync_ms = now
        self._state.unflushed_bytes = 0
```

### 3. Rotation

```python
async def _maybe_rotate(self):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if today == self._state.current_date:
        return

    # Новий день — закриваємо старий файл
    self._state.current_file.flush()
    os.fsync(self._state.current_file.fileno())
    self._state.current_file.close()

    old_path = self.path_root / f"{self._state.current_date}.jsonl"
    asyncio.create_task(self._gzip_async(old_path))

    # Відкриваємо новий
    self._open_file_for(today)
    self._state.current_seq = 1

async def _gzip_async(self, path: Path):
    """Архівуємо старий файл у окремому thread-pool — не блокуємо writer."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, self._gzip_sync, path)

def _gzip_sync(self, path: Path):
    gz_path = path.with_suffix('.jsonl.gz')
    with open(path, 'rb') as inp, gzip.open(gz_path, 'wb', compresslevel=6) as out:
        shutil.copyfileobj(inp, out, 1024*1024)
    path.unlink()
```

### 4. Recovery при старті

```python
async def start(self):
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    path = self.path_root / f"{today}.jsonl"
    self._state.current_date = today

    if path.exists():
        last_seq = self._read_last_seq(path)
        self._state.current_seq = last_seq + 1
        self._state.current_file = open(path, 'a', buffering=1)  # line-buffered
    else:
        self._state.current_file = open(path, 'w', buffering=1)
        self._state.current_seq = 1

    self._writer_task = asyncio.create_task(self._writer_loop())
    self.log(JournalEvent(0, time_ms(), EventKind.STARTUP, None, None,
                          {'version': __version__, 'pid': os.getpid()}))

def _read_last_seq(self, path: Path) -> int:
    """Швидко: seek з кінця і шукаємо останній '\n'."""
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk = min(size, 4096)
        f.seek(-chunk, os.SEEK_END)
        tail = f.read(chunk).decode('utf-8', errors='ignore')
    last_line = tail.strip().split('\n')[-1]
    if not last_line:
        return 0
    return json.loads(last_line).get('seq', 0)
```

### 5. Query: `get_trade`

```python
def get_trade(self, trade_id: str) -> TradeRecord | None:
    """Шукає в indexdb; якщо немає — скан сьогоднішнього файлу."""
    loc = self._index_lookup(trade_id)
    events: list[JournalEvent] = []

    if loc:
        path, start, end = loc
        with open_maybe_gz(path) as f:
            f.seek(start)
            while f.tell() < end:
                line = f.readline()
                if not line:
                    break
                ev = _parse_event(line)
                if ev.trade_id == trade_id:
                    events.append(ev)
    else:
        # Fallback — лінійний скан сьогоднішнього
        for ev in self.iter_events(self._state.current_date):
            if ev.trade_id == trade_id:
                events.append(ev)

    if not events:
        return None
    return _build_trade_record(trade_id, events)
```

### 6. Index (опційно, для post-mortem performance)

```python
# journal/index.sqlite
# CREATE TABLE trade_index (
#     trade_id TEXT PRIMARY KEY,
#     date_utc TEXT,
#     offset_start INTEGER,
#     offset_end INTEGER,
#     symbol TEXT,
#     setup_type TEXT,
#     opened_ms INTEGER,
#     closed_ms INTEGER,
#     realized_r REAL
# )
```

Index оновлюється в `_writer_loop` після TRADE_OUTCOME. Якщо index пошкоджено — rebuild зі сканом JSONL.

### 7. Спеціальні payloads (приклади)

```python
# DECISION_ACCEPTED
{
    "trade_id": "a1b2c3...",
    "symbol": "BTCUSDT",
    "setup_type": "absorption_reversal_long",
    "direction": "LONG",
    "score": 1.47,
    "score_threshold": 1.0,
    "expectancy_multiplier": 1.15,
    "trigger_factors": {"absorption_score": 0.8, "sell_pressure_delta": 0.6},
    "confirmation_factors": {"stacked_imbalance": 0.5},
    "regime": "trending_up",
    "features_hash": "deadbeef",
    "entry_price_hint": 65432.1,
    "stop_price_hint": 65380.0,
    "tp1_price": 65484.2
}

# FILL
{
    "trade_id": "a1b2c3...",
    "client_order_id": "e-a1b2c3-9f3a",
    "exchange_order_id": 123456789,
    "side": "BUY",
    "qty": 0.015,
    "price": 65432.5,
    "is_maker": false,
    "commission_usd": 0.28,
    "filled_cumulative": 0.015,
    "order_status": "FILLED"
}

# TRADE_OUTCOME
{
    "trade_id": "a1b2c3...",
    "setup_type": "absorption_reversal_long",
    "realized_r": 1.85,
    "realized_usd": 18.5,
    "max_favorable_r": 2.1,
    "max_adverse_r": -0.3,
    "was_stopped": false,
    "exit_reason": "tp3_filled",
    "fees_usd": 0.54,
    "duration_ms": 42000
}
```

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Диск повний | write() кидає OSError → log.error + drop counter++; писати в fallback-log через stderr |
| Черга переповнена (writer відстає) | `put_nowait` → QueueFull → drop counter; health alert коли dropped > 0 |
| Падіння процесу між write і fsync | Втратимо останні <1s подій. Це прийнятно (fsync кожні 250ms) |
| Рестарт у середині дня | `_read_last_seq` → продовжуємо seq з правильного числа |
| Файл `index.sqlite` пошкоджений | Видалити, rebuild зі сканом JSONL (slow path; запустити як background task) |
| Один JSONL-рядок пошкоджений (crash mid-write) | Query: skip з warning; не валити весь iter |
| Годинник стрибнув назад (NTP) | `timestamp_ms` подій може не монотонно зростати. `seq` — так. Сортуємо по `seq`, не по timestamp |
| Rotation спрацював під час активного філа | Batch, що розпочато до півночі, дописується в СТАРИЙ файл; наступний — у новий. Жодна подія не губиться |
| Query на великий діапазон (>1 тиждень) | `iter_events` — lazy iterator, не завантажує все в RAM |

---

## Конфіги

```yaml
journal:
  journal_dir: data/journal
  batch_max: 100                                # подій за один write
  flush_interval_ms: 200                        # примусовий write навіть при малому batch
  fsync_interval_ms: 250                        # між fsync'ами
  fsync_bytes_threshold: 65536                  # або після 64KB, що раніше
  queue_size: 10000                             # asyncio.Queue limit
  gzip_old_files: true
  retention_days: 90                            # видаляти gz старші за 90 днів (slow cleanup)
  index_enabled: true
  index_path: data/journal/index.sqlite
```

---

## Як тестувати

### Unit
- `_read_last_seq` на файлі з N рядками → повертає N
- `_read_last_seq` на порожньому файлі → 0
- `_read_last_seq` на файлі з недописаним останнім рядком (без `\n`) → ігнорує його, повертає попередній seq
- Ротація: змоделювати `_state.current_date = '2026-04-20'`, clock = 21 квітня 00:00:10 → старий файл закрито, новий відкрито, seq=1
- `get_trade(missing_id)` → None
- `get_trade` знаходить усі події з правильним `trade_id` (перевіряємо 1 угоду із 7 events)
- Schema round-trip: `JournalEvent → JSON → JournalEvent` ідентичні
- Queue overflow: завалюємо 20000 events → dropped_count > 0, не падає

### Integration
- Запустити logger, надіслати 1000 event'ів, `stop()` з graceful drain → усі 1000 у файлі
- Симулювати crash: `kill -9` під час write → після рестарту `start` успішний, seq continued
- Rotation live: змусити перехід дати через підміну clock → перевірити два файли, gzip другого

### Property
- Для будь-якої послідовності `log()`: після `stop()` у файлі рівно N рядків, seq монотонно 1..N
- `iter_events` завжди повертає події у зростаючому seq

### Regression (snapshot)
- Fixture "typical day scenario" → generate journal → порівняти з baseline JSONL (normalize timestamps до relative)
