---
name: 03-tape-flow-analyzer
description: Архітектура модуля Tape/Flow Analyzer — буфер угод, rolling-вікна 500ms/2s/10s, CVD, price_path, tape-gap detection
type: project
---

# 03. TapeAnalyzer — стрічка, дельта, CVD

## Відповідальність

**Все що пов'язано зі стрічкою угод (time & sales).** Бере `RawAggTrade` від MarketDataGateway, тримає буфер останніх N угод і **rolling-вікна** (500ms / 2s / 10s) із попередньо обчисленими агрегатами. Підтримує **кумулятивний CVD** і **price_path** для детекції імпульсів та pullback-ів у FeatureEngine.

Це "очі на потоці" — модуль, через який видно ХТО зараз агресивний (покупці чи продавці), і чи ця агресивність наростає.

### Що робить:
- Тримає `deque` останніх N trade'ів (для відлагодження + інспекції)
- Підтримує rolling-вікна з агрегатами: buy_vol_usd, sell_vol_usd, delta, count
- Веде кумулятивний CVD за сесію
- Зберігає price_path (timestamp, price) для аналізу імпульсів/pullback
- Виявляє tape gap (розрив `agg_id`) → виставляє флаг "unreliable" на CVD на 5 хв
- Експонує `TapeWindowsState` для FeatureEngine

### Що НЕ робить:
- НЕ класифікує "burst" / "absorption" / "spoof" — це FeatureEngine
- НЕ читає стакан, не торкається OrderBook
- НЕ зберігає на диск (всі буфери в пам'яті)
- НЕ приймає рішень

---

## Входи / виходи

### Публічний API:

```python
class TapeAnalyzer:
    def __init__(self, config: TapeConfig, gateway: MarketDataGateway):
        self._gateway = gateway
        self._states: dict[str, _SymbolTapeState] = {}

    # === Lifecycle ===
    async def start(self, symbols: list[str]) -> None
    async def stop(self) -> None

    # === Query (для FeatureEngine, OrderBookEngine) ===
    def get_windows(self, symbol: str) -> TapeWindowsState
    def get_recent_trades(self, symbol: str, n: int) -> list[RawAggTrade]
    def get_cvd(self, symbol: str) -> float
    def is_cvd_reliable(self, symbol: str) -> bool

    # === Передплата (опційно — OrderBookEngine підписується на raw trade
    #     для оновлення footprint) ===
    def on_trade(self, callback: Callable[[RawAggTrade], None]) -> None
```

### Вихідний тип — `TapeWindowsState`:

```python
@dataclass(frozen=True)
class TapeWindow:
    duration_ms: int               # 500, 2000, 10000
    trade_count: int
    buy_volume_qty: float          # base asset (BTC)
    sell_volume_qty: float
    buy_volume_usd: float          # quote asset (USDT)
    sell_volume_usd: float
    delta_qty: float               # buy_qty - sell_qty
    delta_usd: float
    last_trade_price: float
    first_trade_ms: int
    last_trade_ms: int

@dataclass(frozen=True)
class TapeWindowsState:
    symbol: str
    timestamp_ms: int

    window_500ms: TapeWindow
    window_2s: TapeWindow
    window_10s: TapeWindow

    cvd: float                     # кумулятивна за сесію (USD)
    cvd_reliable: bool             # False, якщо нещодавно був tape gap

    # Зручні shortcuts для FeatureEngine:
    delta_500ms: float
    delta_2s: float
    delta_10s: float

    # Для micro_pullback / impulse detection:
    price_path: list[tuple[int, float]]   # останні N точок (ts_ms, price), maxlen ≈ 200
```

---

## Залежності

**Читає:**
- `MarketDataGateway` — підписується на `on_agg_trade`, `on_book_ticker` (опційно)
- `TapeConfig` — розміри вікон, maxlen буферів, поріг tape-gap

**Пише (callback):**
- Власний внутрішній `_SymbolTapeState`
- Емітує raw trade-події підписникам через `on_trade()` — ОБОВ'ЯЗКОВО для OrderBookEngine, який оновлює footprint

> Архітектурне рішення: trade-події фан-ауться через TapeAnalyzer (а не напряму від Gateway), щоб порядок гарантовано був "tape state updated → OB footprint updated → downstream бачить узгоджений знімок".

**Читають його (downstream):**
- FeatureEngine — `get_windows()` (основний споживач)
- OrderBookEngine — підписаний на `on_trade` для footprint
- (опційно) Journal — `get_recent_trades()` при події (наприклад, при entry — записати останні 50 угод як контекст)

**НЕ залежить від:**
- OrderBookEngine, FeatureEngine, SetupDetector, Decision, Risk, Execution

---

## Стан

```python
@dataclass
class _SymbolTapeState:
    symbol: str
    trades: deque[RawAggTrade]                # maxlen=10000
    rolling_500ms: _RollingWindow
    rolling_2s: _RollingWindow
    rolling_10s: _RollingWindow
    cvd: float                                 # кумулятивна USD
    cvd_unreliable_until_ms: int               # 0 якщо ок
    last_agg_id: int                           # для виявлення gap
    price_path: deque[tuple[int, float]]       # maxlen=200

@dataclass
class _RollingWindow:
    """Двостороння черга (timestamp_ms, contribution) з автоматичним викиданням
    застарілих елементів при доступі."""
    duration_ms: int
    entries: deque[_TradeContribution]
    sum_buy_qty: float                         # підтримуємо інкрементально
    sum_sell_qty: float
    sum_buy_usd: float
    sum_sell_usd: float

@dataclass
class _TradeContribution:
    ts_ms: int
    price: float
    qty: float
    is_buyer_maker: bool
```

Стан виключно в пам'яті. Рестарт = свіжий CVD з нуля (це обмеження сприймаємо — для скальпінгу важлива короткострокова дельта, а не абсолютний рівень CVD).

---

## Ключові алгоритми

### 1. Обробка trade (hot path)

```python
def _on_trade(self, trade: RawAggTrade):
    state = self._states[trade.symbol]

    # 1. Tape gap detection
    if state.last_agg_id and trade.agg_id != state.last_agg_id + 1:
        gap = trade.agg_id - state.last_agg_id - 1
        logger.warning(f"tape_gap symbol={trade.symbol} missing={gap}")
        state.cvd_unreliable_until_ms = trade.timestamp_ms + 5 * 60 * 1000
    state.last_agg_id = trade.agg_id

    # 2. Buffer
    state.trades.append(trade)

    # 3. Вікна
    contrib = _TradeContribution(trade.timestamp_ms, trade.price,
                                  trade.quantity, trade.is_buyer_maker)
    for w in (state.rolling_500ms, state.rolling_2s, state.rolling_10s):
        self._window_add(w, contrib)
        self._window_evict(w, now_ms=trade.timestamp_ms)

    # 4. CVD (USD)
    qty_usd = trade.price * trade.quantity
    state.cvd += -qty_usd if trade.is_buyer_maker else qty_usd

    # 5. price_path (для FeatureEngine — pullback/impulse detection)
    state.price_path.append((trade.timestamp_ms, trade.price))

    # 6. Фан-аут підписникам (OrderBookEngine оновить footprint)
    for cb in self._trade_callbacks:
        cb(trade)
```

### 2. Rolling window — додавання і евікція

```python
def _window_add(self, w: _RollingWindow, c: _TradeContribution):
    w.entries.append(c)
    qty_usd = c.price * c.qty
    if c.is_buyer_maker:    # ринок продав
        w.sum_sell_qty += c.qty
        w.sum_sell_usd += qty_usd
    else:                    # ринок купив
        w.sum_buy_qty += c.qty
        w.sum_buy_usd += qty_usd

def _window_evict(self, w: _RollingWindow, now_ms: int):
    cutoff = now_ms - w.duration_ms
    while w.entries and w.entries[0].ts_ms < cutoff:
        e = w.entries.popleft()
        qty_usd = e.price * e.qty
        if e.is_buyer_maker:
            w.sum_sell_qty -= e.qty
            w.sum_sell_usd -= qty_usd
        else:
            w.sum_buy_qty -= e.qty
            w.sum_buy_usd -= qty_usd
```

> Інкрементальна сума уникає ре-обходу вікна на кожному запиті. O(1) amortized.

### 3. Експорт `TapeWindowsState`

```python
def get_windows(self, symbol: str) -> TapeWindowsState:
    s = self._states[symbol]
    now_ms = self._now_ms()
    # Force-evict перед read — якщо trade'ів давно не було
    for w in (s.rolling_500ms, s.rolling_2s, s.rolling_10s):
        self._window_evict(w, now_ms)

    last_price = s.trades[-1].price if s.trades else 0.0
    cvd_ok = now_ms > s.cvd_unreliable_until_ms

    return TapeWindowsState(
        symbol=symbol,
        timestamp_ms=now_ms,
        window_500ms=self._snapshot_window(s.rolling_500ms, last_price),
        window_2s=self._snapshot_window(s.rolling_2s, last_price),
        window_10s=self._snapshot_window(s.rolling_10s, last_price),
        cvd=s.cvd,
        cvd_reliable=cvd_ok,
        delta_500ms=s.rolling_500ms.sum_buy_usd - s.rolling_500ms.sum_sell_usd,
        delta_2s=s.rolling_2s.sum_buy_usd - s.rolling_2s.sum_sell_usd,
        delta_10s=s.rolling_10s.sum_buy_usd - s.rolling_10s.sum_sell_usd,
        price_path=list(s.price_path),
    )
```

### 4. Tape gap → unreliable CVD

При втраті trade'ів дельта вже не точна. Замість того щоб блокувати весь pipeline — виставляємо флаг на 5 хв. FeatureEngine читає `cvd_reliable`; SetupDetector може дискваліфікувати сетапи що сильно залежать від CVD (наприклад, CVD-divergence).

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Перший trade після старту — `last_agg_id=0` | Не вважати це gap'ом, просто запам'ятати |
| `agg_id` рестартнувся (рідко при maintenance) | Лог WARN, скинути `last_agg_id` без unreliable-флагу |
| Дуже довге мовчання (>10s) | Вікна спорожніють → `delta=0`, `last_trade_price` залишиться старим (FeatureEngine має це бачити) |
| Аномальний trade з ціною, що сильно відрізняється від `last_price` (>5%) | НЕ фільтрувати, але додати лог-маркер `WARN price_jump` (буде корисно у Journal) |
| price_path переповнився (200 точок) | deque автоматично викине найстаріший |
| Виклик `get_windows()` до жодного trade | Повернути `TapeWindowsState` з порожніми вікнами, `last_trade_price=0`, `cvd=0` |
| Concurrent доступ до `_SymbolTapeState` | Pipeline single-threaded asyncio → блокування не потрібне; якщо колись будуть thread workers — додати `Lock` per symbol |

---

## Конфіги

```yaml
tape_analyzer:
  trade_buffer_maxlen: 10000        # на symbol
  price_path_maxlen: 200

  windows:
    short_ms: 500
    medium_ms: 2000
    long_ms: 10000

  tape_gap:
    unreliable_window_min: 5        # хв після виявлення gap
    log_level: WARNING

  cvd:
    reset_on_session: false         # для крипти сесій нема — не скидаємо
```

---

## Як тестувати

### Unit
- `_window_add` + `_window_evict` — серія угод, перевірка `sum_*` після евікції
- `_on_trade` із штучним розривом `agg_id` → `cvd_unreliable_until_ms` виставлене
- CVD: серія {buy 100, sell 50, buy 30} → CVD == +80 (USD еквівалент)
- `get_windows()` коли вікно спорожніло → delta=0, `trade_count=0`

### Property-based (hypothesis)
- Для будь-якої послідовності trade'ів: `window.delta_qty == sum(buy.qty) - sum(sell.qty)` усередині вікна
- Інваріант інкрементальних сум: після N add+evict операцій, `sum_*` дорівнює sum-over-entries

### Integration (testnet)
- Підписатись на BTCUSDT 5 хв → перевірити що CVD рухається, кількість trade'ів > 0
- Симулювати tape gap (не можна на реальній біржі — використати recorded stream із вирізаним блоком)

### Performance
- `_on_trade` має займати **<50µs** на середньому ноутбуці
- Бенч: 100k trade'ів через `_on_trade` → p99 < 100µs, memory зростає лінійно тільки до maxlen

---

## Відкриті питання / TODO

- [ ] Додати окреме `book_ticker` вікно для відстеження "останньої best-bid/ask" — потрібно для частини сетапів (наприклад, "ціна зайшла за best bid, але не пробила")
- [ ] Чи варто винести `BookEvolutionTracker` (для absorption/spoof у FeatureEngine) сюди як `_RollingBookEventsWindow`? Це б зробило FeatureEngine 100% stateless. Поки залишаю в FeatureEngine (там простіше тримати pre/post snapshot).
- [ ] CVD Δ-since-VAH/VAL/POC (для зон-relative дельти) — корисно при reaction-trade у HTF POI
