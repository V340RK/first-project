# Architecture Overview — Загальна карта модулів

## Мета цього документа

Це **вхідна точка** для всієї архітектурної документації. Тут показано:
- які модулі є в боті
- як вони з'єднані
- в який бік іде потік даних
- які правила взаємодії (що кому дозволено викликати)

Для деталей по кожному модулю — дивись окремий файл у цій папці.

---

## Принципи архітектури

### 1. Pipeline — однонаправлений потік

Дані течуть зліва направо, ніколи назад:

```
Market Data → Order Book + Tape → Features → Regime → Setup → Decision → Risk → Execution → Position
                                                                                        ↓
                                                          ↓                         Journal (всім)
                                                        Replay (окрема петля для тестування)
```

Кожен наступний модуль читає те що видав попередній, і нічого не викликає назад. Це робить кожен крок тестованим окремо.

### 2. Stateless фічі, stateful state

- **Stateless** (чисті функції): Feature Engine, Setup Detector, Decision Engine — приймають snapshot, повертають значення
- **Stateful** (з пам'яттю): Order Book Engine, Tape Analyzer, Position Manager, State Machine, Risk Engine

### 3. Магії немає — лише формалізація

Кожна фіча має формулу. Якщо її не можна записати — її не існує. Це усуває "бот побачив поглинання, бо так здавалося".

### 4. Fail-safe за замовчуванням

Будь-який збій → бот переходить у `DISABLED`. Краще пропустити сигнал ніж відкрити з кривими даними.

### 5. Live і Replay ділять той самий код

Replay — це не окрема симуляція, а **той самий pipeline** з підмінним джерелом даних. Якщо backtest показує одне а live інше — це баг, а не "ринок змінився".

---

## Карта модулів (12 + 1 інфра)

```
┌────────────────────────────────────────────────────────────────┐
│                    EXCHANGE (Binance Futures)                   │
└──────────────────────┬──────────────────────────▲───────────────┘
                       │ raw streams              │ orders
                       ▼                          │
┌────────────────────────────────────────────────────────────────┐
│  1  MARKET DATA GATEWAY                                         │
│     reconnect, heartbeat, sequence check, normalization         │
└────────┬──────────────────┬──────────────────┬─────────────────┘
         ▼                  ▼                  │
┌────────────────┐  ┌────────────────┐         │
│ 2 ORDER BOOK   │  │ 3 TAPE / FLOW  │         │
│   ENGINE       │  │   ANALYZER     │         │
│ DOM, spread,   │  │ delta windows, │         │
│ microprice,    │  │ aggressive     │         │
│ imbalance N    │  │ bursts, CVD    │         │
└────────┬───────┘  └────────┬───────┘         │
         │                   │                 │
         └─────────┬─────────┘                 │
                   ▼                           │
┌─────────────────────────────────────────┐    │
│  4  FEATURE ENGINE                       │    │
│  bid/ask imbalance, absorption,          │    │
│  spoof-like, delta spike, micro pullback │    │
│  + Zone Context (HTF POI, VAH/VAL/POC)   │    │
└──────────┬───────────────────────────────┘    │
           │                                    │
           ▼                                    │
┌─────────────────────────────────────────┐    │
│  5  MARKET REGIME ENGINE                 │    │
│  TRENDING_UP / DOWN / CHOPPY /           │    │
│  HIGH_VOL / LOW_LIQ / NEWS / NORMAL      │    │
└──────────┬───────────────────────────────┘    │
           ▼                                    │
┌─────────────────────────────────────────┐    │
│  6  SETUP DETECTOR                       │    │
│  ABSORPTION_REVERSAL_LONG/SHORT          │    │
│  IMBALANCE_CONTINUATION_LONG/SHORT       │    │
│  SPOOF_FAIL_FADE                         │    │
│  MICRO_PULLBACK_AFTER_AGGRESSION         │    │
└──────────┬───────────────────────────────┘    │
           ▼                                    │
┌─────────────────────────────────────────┐    │
│  7  DECISION ENGINE                      │    │
│  score-based (з + та − вагами)           │    │
│  threshold + regime compatibility        │    │
└──────────┬───────────────────────────────┘    │
           ▼                                    │
┌─────────────────────────────────────────┐    │
│  8  RISK ENGINE  ★ головний              │    │
│  per-trade, daily, consecutive,          │    │
│  spread/latency/slippage gates,          │    │
│  KILL SWITCH                             │    │
└──────────┬───────────────────────────────┘    │
           ▼                                    │
┌─────────────────────────────────────────┐    │
│  9  EXECUTION ENGINE                     │ ───┘
│  market/limit/post-only/reduce-only      │
│  partial fills, cancel/replace,          │
│  emergency flatten                       │
└──────────┬───────────────────────────────┘
           ▼
┌─────────────────────────────────────────┐
│  10  POSITION MANAGER                    │
│  initial stop, TP-ladder, scale-out,     │
│  break-even, trailing,                   │
│  exit on signal failure (per-setup),     │
│  time stop (per-setup, configurable)     │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  11  JOURNAL / DECISION LOGGER           │  ← пише ВСЕ з усіх модулів
│  full decision rationale + outcomes      │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  12  REPLAY / BACKTEST / SIMULATOR       │  ← підмінює модуль 1 у тестах
│  market replay, latency sim, slippage    │
│  sim, fees, live-vs-replay diff          │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  13  EXPECTANCY TRACKER (інфра)          │
│  rolling E per-setup, auto-deactivation  │
└─────────────────────────────────────────┘
```

---

## Список модулів з посиланнями

| # | Модуль | Тип | Файл |
|---|---|---|---|
| 1 | Market Data Gateway | Stateful infra | [01-market-data-gateway.md](./01-market-data-gateway.md) |
| 2 | Order Book Engine | Stateful | [02-order-book-engine.md](./02-order-book-engine.md) |
| 3 | Tape / Flow Analyzer | Stateful | [03-tape-flow-analyzer.md](./03-tape-flow-analyzer.md) |
| 4 | Feature Engine | Stateless | [04-feature-engine.md](./04-feature-engine.md) |
| 5 | Market Regime Engine | Stateful | [05-market-regime.md](./05-market-regime.md) |
| 6 | Setup Detector | Stateless | [06-setup-detector.md](./06-setup-detector.md) |
| 7 | Decision Engine | Stateless | [07-decision-engine.md](./07-decision-engine.md) |
| 8 | Risk Engine ★ | Stateful | [08-risk-engine.md](./08-risk-engine.md) |
| 9 | Execution Engine | Stateful | [09-execution-engine.md](./09-execution-engine.md) |
| 10 | Position Manager | Stateful | [10-position-manager.md](./10-position-manager.md) |
| 11 | Journal / Logger | Infra | [11-journal-logger.md](./11-journal-logger.md) |
| 12 | Replay / Simulator | Infra | [12-replay-simulator.md](./12-replay-simulator.md) |
| 13 | Expectancy Tracker | Infra | [13-expectancy-tracker.md](./13-expectancy-tracker.md) |
| 14 | Orchestrator (крос-cutting) | Runtime glue | [14-orchestrator.md](./14-orchestrator.md) |
| 15 | NotificationService (крос-cutting) | Infra | [15-notifications.md](./15-notifications.md) |

---

## Правила залежностей

### Що кому ДОЗВОЛЕНО викликати

| Модуль | Може читати з | НЕ може викликати |
|---|---|---|
| **1 Gateway** | Exchange | будь-що вище в pipeline |
| **2 Order Book** | Gateway events | Tape, Feature, нічого далі |
| **3 Tape** | Gateway events | Order Book, Feature, нічого далі |
| **4 Feature Engine** | Order Book, Tape (read-only snapshot) | Regime, нічого далі |
| **5 Regime** | Feature output, історія цін | Setup, Decision |
| **6 Setup Detector** | Feature output, Regime | Decision, Risk |
| **7 Decision** | Setup output, Regime, Feature scores | Risk, Execution |
| **8 Risk** | Decision output, поточні позиції, daily P&L | Execution напряму |
| **9 Execution** | Risk output (TradePlan), Exchange | Setup/Decision знає тільки `OrderRequest` |
| **10 Position** | Execution events, Tape, Feature | Setup/Decision напряму |
| **11 Journal** | усі модулі (read-only events) | нічого не змінює |
| **12 Replay** | історичні дані | в live підміняє Gateway |
| **13 Expectancy** | Journal events | може деактивувати сетапи через config |

### Чому так

- **Execution Engine не знає що таке "absorption setup"** — він приймає `OrderRequest{symbol, side, type, qty, price, stop, tp}`. Інакше при додаванні нового сетапу треба правити Execution.
- **Decision не викликає Risk напряму** — Decision видає `TradeIntent`, Risk його приймає чи відхиляє. Так Risk залишається єдиним джерелом правди про "можна/не можна".
- **Position Manager не знає про Setup Detector** — він супроводжує позицію за описом invalidation conditions які прийшли разом із позицією.

---

## Дві петлі в боті

### Hot loop (тікова частота)
На кожен tick від WebSocket:
1. Gateway парсить
2. Order Book Engine оновлює DOM
3. Tape Analyzer оновлює rolling windows (delta_500ms, delta_2s)
4. Feature Engine рахує фічі
5. Setup Detector перевіряє патерни
6. Якщо патерн → Decision → Risk → (можливо) Execution

Навантаження: **10-500 викликів/сек** на BTCUSDT. Цільова latency від tick до Decision: **<20 мс**.

### Slow loop (періодична)
- Regime Engine recalc — раз на 5-10 сек
- Expectancy Tracker rebuild — раз на закриту угоду
- Health/Heartbeat — раз на 10 сек
- Daily summary — 00:00 UTC

---

## Структура події в pipeline

Кожен модуль додає свій шар у `MarketSnapshot` → `Features` → `Decision`:

```python
# Шар 1-3 — сирі дані
@dataclass
class MarketSnapshot:
    timestamp_ms: int
    symbol: str
    book: OrderBookState              # від Order Book Engine
    tape: TapeWindowsState            # від Tape Analyzer
    last_price: float
    spread_ticks: int

# Шар 4 — формалізовані фічі
@dataclass
class Features:
    snapshot: MarketSnapshot
    bid_ask_imbalance_5: float
    bid_ask_imbalance_10: float
    delta_500ms: float
    delta_2s: float
    cvd: float
    aggressive_buy_burst: bool
    aggressive_sell_burst: bool
    absorption_score: float           # 0..1
    spoof_score: float
    micro_pullback: PullbackState | None
    in_htf_poi: bool                  # Zone Context
    htf_poi_type: str | None          # 'vah' / 'val' / 'poc' / 'fvg' / 'ob'

# Шар 5
class Regime(Enum):
    NORMAL_BALANCED = "normal_balanced"
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    CHOPPY = "choppy"
    HIGH_VOL = "high_vol"             # підвищена волатильність (ATR spike)
    LOW_LIQ = "low_liq"               # широкий spread / тонкий стакан
    NEWS_RISK = "news_risk"           # перед/під час релізів
    DISABLED = "disabled"             # kill switch / manual pause

# Шар 6
@dataclass(frozen=True)
class SetupCandidate:
    setup_type: SetupType             # enum: ABSORPTION_REVERSAL, STACKED_IMBALANCE, ...
    direction: Literal['LONG', 'SHORT']
    symbol: str
    timestamp_ms: int
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    stop_distance_ticks: int
    invalidation_conditions: list[InvalidationCondition]   # primary-exit тригери
    features_snapshot: Features                            # зріз для audit/replay

# Шар 7-8 — DecisionEngine формує TradePlan; RiskEngine його дозаповнює
@dataclass(frozen=True)
class TradePlan:
    candidate: SetupCandidate
    setup_type: SetupType
    direction: Literal['LONG', 'SHORT']
    symbol: str
    timestamp_ms: int

    entry_price: float
    stop_price: float
    tp1_price: float                  # 50% розміру
    tp2_price: float                  # 25%
    tp3_price: float                  # 25% з трейлом
    stop_distance_ticks: int

    score: float                      # сума зважених факторів (може бути > 1)
    score_threshold: float            # поріг, з яким порівнювали
    regime: Regime
    expectancy_multiplier: float      # з ExpectancyTracker

    invalidation_conditions: list[InvalidationCondition]   # primary-exit для PositionManager
    time_stop_ms: int | None                               # backup exit, per-setup

    # Заповнює RiskEngine:
    position_size: float | None = None
    risk_usd: float | None = None
    risk_gate_passed: bool = False

@dataclass(frozen=True)
class DecisionResult:
    accepted: TradePlan | None        # переможець (або None якщо всіх відмовили)
    rejected: list[RejectedCandidate] # з причинами — для post-mortem/журналу

# Шар 9-10
@dataclass
class OrderRequest: ...               # те що йде в Execution
@dataclass
class OpenPosition: ...               # те що моніторить Position Manager
```

Кожен шар **доповнює** попередній, не змінює. Це дає можливість зберегти повний контекст рішення в журналі.

---

## Крос-cutting сервіси

Два сервіси, які використовуються з більшості модулів, але самі не належать до 13-шарового pipeline:

- **Orchestrator** — runtime-граф, що склеює модулі й веде hot/slow loop. Деталі — [14-orchestrator.md](./14-orchestrator.md).
- **NotificationService** — єдина точка вихідних алертів (stdout/Telegram/email). Деталі — [15-notifications.md](./15-notifications.md).

---

## Anti-patterns яких уникаємо

- ❌ Setup Detector сам викликає `place_order()` — мусить пройти через Decision → Risk → Execution
- ❌ Hard-coded цифри (`if delta > 5000`) — все через config
- ❌ Магічні фічі без формули
- ❌ Position Manager читає Setup Detector щоб "перевірити чи сетап ще валідний" — invalidation conditions передаються разом із позицією
- ❌ Risk Engine знає про конкретні сетапи — він знає тільки про `TradePlan` і ризик-параметри
- ❌ Backtest має свою копію логіки сигналів — Replay підміняє Gateway, решта pipeline та сама

---

## Що далі

Етап 2 — детальні файли по кожному з 13 модулів за шаблоном:

1. **Відповідальність** — що робить, що НЕ робить
2. **Входи / виходи** — публічний API
3. **Залежності** — кого читає, куди пише
4. **Стан** — що тримає в пам'яті
5. **Ключові алгоритми** — pseudo-code
6. **Edge cases** — поведінка при збоях
7. **Конфіги** — з settings.yaml
8. **Як тестувати** — unit/integration/manual
