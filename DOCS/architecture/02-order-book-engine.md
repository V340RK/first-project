---
name: 02-order-book-engine
description: Архітектура модуля Order Book Engine — підтримка L2-стакану з diff-merge, footprint бару, кластерні утиліти (imbalance/stacked/absorption helpers)
type: project
---

# 02. OrderBookEngine — стакан і footprint

## Відповідальність

**"Книга" і "footprint бару"** — два об'єкти, які кожен інший модуль дивиться як на джерело правди.

OrderBookEngine приймає `RawDepthDiff` від Gateway, тримає актуальний L2-стакан (через snapshot+diff merge з sequence-перевіркою). Паралельно приймає `RawAggTrade` від TapeAnalyzer (через спільну шину) і будує footprint поточного бару.

Експортує:
- `OrderBookState` — топ-N рівнів bid/ask
- `FootprintBar` — обсяги bid/ask по кожному ціновому рівню всередині бару
- Помічники для Feature Engine: `detect_imbalances`, `detect_stacked`, `classify_poc_location`

### Що робить:
- Reinit OrderBook на старті: REST snapshot + replay буферизованих diff-ів
- Apply incremental diff із перевіркою `U == last_update_id + 1`
- Виявляє sequence gap → форсує reinit
- Будує `FootprintBar` для кожного активного таймфрейму
- Закриває бар на межі хвилини (або 5m/15m), створює новий
- Скидає метрики бару (`PoC`, `delta`) при закритті
- Зберігає **обмежену історію** закритих footprint'ів (deque maxlen)

### Що НЕ робить:
- НЕ обробляє стрічку (це TapeAnalyzer; OrderBookEngine лише отримує trade-події для оновлення footprint)
- НЕ рахує CVD, delta-вікна 500ms/2s — це TapeAnalyzer
- НЕ оцінює absorption/spoof score — це FeatureEngine (тут лише низькорівневі helpers)
- НЕ приймає рішень

---

## Входи / виходи

### Публічний API:

```python
class OrderBookEngine:
    def __init__(self, config: OBConfig, gateway: MarketDataGateway):
        self._gateway = gateway
        self._books: dict[str, _BookState] = {}
        self._footprints: dict[str, dict[str, FootprintBar]] = {}  # symbol → tf → bar
        ...

    # === Lifecycle ===
    async def start(self, symbols: list[str], timeframes: list[str]) -> None
    async def stop(self) -> None

    # === Query (для FeatureEngine та SetupDetector) ===
    def get_book(self, symbol: str) -> OrderBookState
    def get_current_footprint(self, symbol: str, tf: str) -> FootprintBar
    def get_recent_footprints(self, symbol: str, tf: str, n: int) -> list[FootprintBar]

    # === Передплата (опційно — для slow-loop модулів типу ZoneRegistry) ===
    def on_bar_close(self, callback: Callable[[FootprintBar], Awaitable[None]]) -> None

    # === Низькорівневі утиліти (stateless static-методи для FeatureEngine) ===
    @staticmethod
    def detect_imbalances(fp: FootprintBar, ratio: float) -> list[Imbalance]
    @staticmethod
    def detect_stacked(imbalances: list[Imbalance], min_count: int) -> list[StackedImbalance]
    @staticmethod
    def classify_poc_location(fp: FootprintBar, bar: BarOHLC) -> PocLocation
```

### Типи:

```python
@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float

@dataclass(frozen=True)
class OrderBookState:
    symbol: str
    timestamp_ms: int
    bids: list[OrderBookLevel]   # відсортовані desc
    asks: list[OrderBookLevel]   # відсортовані asc
    last_update_id: int

@dataclass
class _BookState:
    """Internal mutable state — НЕ експонується назовні."""
    bids: SortedDict[float, float]   # price desc → size
    asks: SortedDict[float, float]   # price asc → size
    last_update_id: int
    initialized: bool

@dataclass
class FootprintBar:
    symbol: str
    timeframe: str                   # '1m', '5m', '15m'
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    levels: dict[float, _LevelVolume]  # tick-rounded price → (bid_vol, ask_vol)
    poc_price: float | None
    delta: float                     # Σ(ask_vol − bid_vol) — додатна = покупець
    is_closed: bool

@dataclass
class _LevelVolume:
    bid_vol: float                   # обсяг ринкових продажів на цьому рівні
    ask_vol: float                   # обсяг ринкових купівель

@dataclass(frozen=True)
class Imbalance:
    price_level: float
    side: Literal['BID', 'ASK']      # хто домінує діагонально
    ratio: float
    volume: float

@dataclass(frozen=True)
class StackedImbalance:
    start_price: float
    end_price: float
    side: Literal['BID', 'ASK']
    level_count: int
    total_volume: float

class PocLocation(Enum):
    TOP = "top"                      # PoC у верхній третині бару
    BOTTOM = "bottom"
    CENTER = "center"
    UPPER_WICK = "upper_wick"
    LOWER_WICK = "lower_wick"
```

---

## Залежності

**Читає:**
- `MarketDataGateway` — підписується на `on_depth_diff` і `on_agg_trade`
- `OBConfig` — tick_size override, levels_to_keep, footprint timeframes
- `ExchangeInfo` (через Gateway) — для tick_size per symbol

**Пише:**
- Власний внутрішній стан `_BookState` і `_footprints`
- Виплескує подію `bar_close` своїм підписникам (наприклад, ZoneRegistry в slow loop)

**Читають його (downstream):**
- TapeAnalyzer — НЕ читає, він пише сюди trade-події (через спільний event-fan-out у Gateway)
- FeatureEngine — `get_book()`, `get_current_footprint()`, статичні `detect_*`
- SetupDetector — переважно через FeatureEngine; інколи безпосередньо `get_recent_footprints()`

**НЕ залежить від:**
- FeatureEngine, SetupDetector, Decision, Risk, Execution

---

## Стан

```python
@dataclass
class _EngineState:
    books: dict[str, _BookState]                          # symbol → mutable book
    footprints: dict[str, dict[str, FootprintBar]]        # symbol → tf → active bar
    closed_history: dict[str, dict[str, deque[FootprintBar]]]  # symbol → tf → ring (maxlen ≈ 200)
    diff_buffer: dict[str, list[RawDepthDiff]]            # для warmup-replay
    initialized: dict[str, bool]
```

Стан тримається тільки в пам'яті. Рестарт = повний reinit OrderBook через REST snapshot + порожні footprint'и (історія втрачається — це прийнятно).

---

## Ключові алгоритми

### 1. OrderBook init (snapshot + diff replay)

Це найскладніша частина. Binance віддає **diff-стрім** без початкового snapshot. Алгоритм з документації:

```python
async def _init_book(self, symbol: str):
    # 1. Почати буферизувати diff-події
    self._diff_buffer[symbol] = []
    self._gateway.on_depth_diff(self._on_diff_buffering)

    # 2. Дочекатись хоча б одного diff'у
    await self._wait_for_first_diff(symbol, timeout=5)

    # 3. Запросити REST snapshot
    snap = await self._gateway.fetch_depth_snapshot(symbol, limit=1000)
    book = _BookState(
        bids=SortedDict({p: q for p, q in snap.bids if q > 0}),
        asks=SortedDict({p: q for p, q in snap.asks if q > 0}),
        last_update_id=snap.last_update_id,
        initialized=False,
    )

    # 4. Програти буферизовані diff-и з умовами:
    #    - skip ті де u <= snap.last_update_id (вже в snapshot)
    #    - перший прийнятий: U <= snap.last_update_id+1 <= u
    #    - далі строго U == prev_u + 1
    valid_start_found = False
    for diff in self._diff_buffer[symbol]:
        if diff.final_update_id <= snap.last_update_id:
            continue
        if not valid_start_found:
            if diff.first_update_id <= snap.last_update_id + 1 <= diff.final_update_id:
                valid_start_found = True
            else:
                logger.warning("Snapshot too old or too new — retrying init")
                return await self._init_book(symbol)
        else:
            if diff.first_update_id != book.last_update_id + 1:
                logger.error("Sequence gap during warmup — retrying")
                return await self._init_book(symbol)
        self._apply_diff(book, diff)

    # 5. Переключитись з buffering на live
    self._books[symbol] = book
    book.initialized = True
    self._gateway.on_depth_diff(self._on_diff_live)
    self._diff_buffer.pop(symbol, None)
```

### 2. Apply diff (live)

```python
def _on_diff_live(self, diff: RawDepthDiff):
    book = self._books.get(diff.symbol)
    if not book or not book.initialized:
        return
    if diff.first_update_id != book.last_update_id + 1:
        logger.error(f"Sequence gap on {diff.symbol}: expected {book.last_update_id+1}, got {diff.first_update_id}")
        asyncio.create_task(self._reinit_book(diff.symbol))
        return
    self._apply_diff(book, diff)

def _apply_diff(self, book: _BookState, diff: RawDepthDiff):
    for price, qty in diff.bids:
        if qty == 0:
            book.bids.pop(price, None)
        else:
            book.bids[price] = qty
    for price, qty in diff.asks:
        if qty == 0:
            book.asks.pop(price, None)
        else:
            book.asks[price] = qty
    book.last_update_id = diff.final_update_id
```

### 3. Trade → footprint update

```python
def _on_trade(self, trade: RawAggTrade):
    for tf in self._timeframes:
        bar = self._footprints[trade.symbol][tf]
        if trade.timestamp_ms >= bar.close_time_ms:
            self._close_bar(trade.symbol, tf)
            bar = self._footprints[trade.symbol][tf]

        # OHLC update
        if bar.open == 0:
            bar.open = trade.price
        bar.high = max(bar.high, trade.price)
        bar.low = trade.price if bar.low == 0 else min(bar.low, trade.price)
        bar.close = trade.price

        # Footprint level
        price_level = self._round_to_tick(trade.price, trade.symbol)
        lvl = bar.levels.setdefault(price_level, _LevelVolume(0, 0))
        if trade.is_buyer_maker:        # ринок ПРОДАВ — bid отримав хіт
            lvl.bid_vol += trade.quantity
            bar.delta -= trade.quantity
        else:                            # ринок КУПИВ — ask отримав хіт
            lvl.ask_vol += trade.quantity
            bar.delta += trade.quantity

        # PoC update — інкрементально
        total = lvl.bid_vol + lvl.ask_vol
        if bar.poc_price is None:
            bar.poc_price = price_level
        else:
            current_poc_total = (bar.levels[bar.poc_price].bid_vol +
                                  bar.levels[bar.poc_price].ask_vol)
            if total > current_poc_total:
                bar.poc_price = price_level
```

### 4. Bar close lifecycle

```python
def _close_bar(self, symbol: str, tf: str):
    bar = self._footprints[symbol][tf]
    bar.is_closed = True

    # Зберегти в історії
    self._closed_history[symbol][tf].append(bar)

    # Послати підписникам
    for cb in self._bar_close_callbacks:
        asyncio.create_task(cb(bar))

    # Створити новий
    next_open = bar.close_time_ms
    next_close = next_open + self._tf_to_ms(tf)
    self._footprints[symbol][tf] = FootprintBar(
        symbol=symbol, timeframe=tf,
        open_time_ms=next_open, close_time_ms=next_close,
        open=0, high=0, low=0, close=0,
        levels={}, poc_price=None, delta=0,
        is_closed=False,
    )
```

### 5. Cluster utilities (статичні, для FeatureEngine)

#### Imbalance — діагональне порівняння

```python
@staticmethod
def detect_imbalances(fp: FootprintBar, ratio: float = 2.5) -> list[Imbalance]:
    """Діагональний imbalance: ask на верхньому рівні vs bid на нижньому."""
    levels = sorted(fp.levels.items())
    out: list[Imbalance] = []
    for i in range(1, len(levels)):
        p_hi, v_hi = levels[i]
        p_lo, v_lo = levels[i-1]
        # Bull: ask(hi) >> bid(lo)
        if v_lo.bid_vol > 0 and v_hi.ask_vol / v_lo.bid_vol >= ratio:
            out.append(Imbalance(p_hi, 'ASK', v_hi.ask_vol / max(v_lo.bid_vol, 1e-9), v_hi.ask_vol))
        # Bear: bid(lo) >> ask(hi)
        if v_hi.ask_vol > 0 and v_lo.bid_vol / v_hi.ask_vol >= ratio:
            out.append(Imbalance(p_lo, 'BID', v_lo.bid_vol / max(v_hi.ask_vol, 1e-9), v_lo.bid_vol))
    return out
```

#### Stacked imbalance

```python
@staticmethod
def detect_stacked(imbs: list[Imbalance], min_count: int = 3) -> list[StackedImbalance]:
    out, current = [], []
    for imb in sorted(imbs, key=lambda x: x.price_level):
        if not current or current[-1].side != imb.side:
            if len(current) >= min_count:
                out.append(_build_stacked(current))
            current = [imb]
        else:
            current.append(imb)
    if len(current) >= min_count:
        out.append(_build_stacked(current))
    return out
```

#### PoC location

```python
@staticmethod
def classify_poc_location(fp: FootprintBar, bar: BarOHLC) -> PocLocation:
    if fp.poc_price is None:
        return PocLocation.CENTER
    body_lo, body_hi = min(bar.open, bar.close), max(bar.open, bar.close)
    if fp.poc_price > body_hi:
        return PocLocation.UPPER_WICK
    if fp.poc_price < body_lo:
        return PocLocation.LOWER_WICK
    rng = bar.high - bar.low
    if rng <= 0:
        return PocLocation.CENTER
    pos = (fp.poc_price - bar.low) / rng
    if pos > 0.66:
        return PocLocation.TOP
    if pos < 0.33:
        return PocLocation.BOTTOM
    return PocLocation.CENTER
```

> **Absorption** не детектується тут (бо вимагає поєднання з tape-метриками типу delta_500ms). Цим займається `FeatureEngine._score_absorption()`. OrderBookEngine надає лише будівельні блоки.

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Перший diff прийшов до snapshot | Буферизувати, не панікувати |
| Snapshot старіший за всі diff-и (`u_first > snap.lastUpdateId+1`) | Retry init (свіжий snapshot) |
| Sequence gap у live | Лог ERROR, async reinit, ця операція може зайняти 1-2 сек — на цей час `book.initialized=False` → FeatureEngine має фолбек |
| `qty=0` у diff | Видалити рівень |
| Trade з ціною поза tick_grid (рідко на агрегованих) | Округлити до найближчого tick |
| Бар "застряг" — немає trade'ів | На зміну хвилини форс-закриваємо порожній бар (для синхронності TF) — `delta=0`, `poc=None` |
| Дуже великий aggTrade (whale) | Не фільтруємо, footprint оновлюється як завжди |
| Memory зростає | maxlen на `closed_history` (200 барів × tf), `levels` всередині бару очищається при close |

---

## Конфіги

```yaml
order_book_engine:
  levels_to_keep: 20                # топ-N з кожної сторони експонується через get_book()
  full_book_max_levels: 1000        # снапшот REST

  timeframes: ['1m', '5m', '15m']
  closed_history_size: 200          # барів на TF на symbol

  cluster:
    imbalance_ratio: 2.5
    stacked_min_count: 3

  reinit:
    snapshot_limit: 1000
    max_attempts: 3
    backoff_ms: 500
```

---

## Як тестувати

### Unit
- `_apply_diff()` з відомою послідовністю → перевірка фінального стану книги
- `_init_book()` — імітувати snapshot + diff-стрім, перевірити правильний skip/replay
- `_close_bar()` на переході хвилини — новий бар має правильний `open_time_ms`
- `detect_imbalances()` на синтетичному footprint
- `classify_poc_location()` — згенерувати бар з PoC у різних позиціях, перевірити enum

### Property-based (hypothesis)
- Σ(level.bid_vol + level.ask_vol) == sum(trade.quantity) для всіх trade у бар
- bar.delta == sum(+q if buyer_taker else -q for trade in bar)
- `book.bids` завжди відсортований desc, `book.asks` — asc

### Integration (testnet)
- Reinit на старті → перевірити `last_update_id` зростає, без gap'ів за 5 хв
- Force gap (відключити мережу на 2 сек) → ловимо reinit, без втрати консистентності

### Stress
- 5000 trades/sec × 60 сек → memory не росте більш ніж на 20%, p99 latency `_on_trade` < 200µs
