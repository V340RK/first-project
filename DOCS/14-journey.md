---
name: 14-journey
description: Хронологія розробки бота — які модулі збудовано, які баги виловлено, як запускати, що залишилось до prod
type: log
---

# 14. Хронологія розробки та поточний стан

Цей документ — "що де лежить і як запустити", щоб у нову сесію зайти без втрати контексту.

---

## Архітектурна карта (14 модулів + runtime)

| № | Модуль | Код | Тести | Статус |
|---|---|---|---|---|
| 01 | Market Data Gateway | [gateway.py](../src/scalper/gateway/gateway.py) (379 рядків) | [tests/gateway/](../tests/gateway/) | ✅ WS+REST, HMAC, rate-limit |
| 02 | Order Book Engine | [book/engine.py](../src/scalper/book/engine.py) | [tests/book/](../tests/book/) | ✅ + relaxed_sync для testnet |
| 03 | Tape Flow Analyzer | [tape/analyzer.py](../src/scalper/tape/analyzer.py) | [tests/tape/](../tests/tape/) | ✅ |
| 04 | Feature Engine | [features/engine.py](../src/scalper/features/engine.py) | [tests/features/](../tests/features/) | ✅ |
| 05 | Market Regime | [regime/classifier.py](../src/scalper/regime/classifier.py) | [tests/regime/](../tests/regime/) | ✅ |
| 06 | Setup Detector | [setups/](../src/scalper/setups/) | [tests/setups/](../tests/setups/) | ✅ 6 правил (absorption, imbalance, micro-pullback) |
| 07 | Decision Engine | [decision/engine.py](../src/scalper/decision/engine.py) | [tests/decision/](../tests/decision/) | ✅ |
| 08 | Risk Engine | [risk/engine.py](../src/scalper/risk/engine.py) | [tests/risk/](../tests/risk/) | ✅ |
| 09 | Execution Engine | [execution/engine.py](../src/scalper/execution/engine.py) | [tests/execution/](../tests/execution/) | ✅ + BinanceOrderTransport adapter |
| 10 | Position Manager | [position/manager.py](../src/scalper/position/manager.py) | [tests/position/](../tests/position/) | ✅ |
| 11 | Journal Logger | [journal/logger.py](../src/scalper/journal/logger.py) | [tests/journal/](../tests/journal/) | ✅ |
| 12 | Replay / Simulator | [replay/simulator.py](../src/scalper/replay/simulator.py) | [tests/replay/](../tests/replay/) | ✅ SimulatedExecutionEngine; ReplayGateway файл-loader ❌ (не MVP) |
| 13 | Expectancy Tracker | [expectancy/tracker.py](../src/scalper/expectancy/tracker.py) | [tests/expectancy/](../tests/expectancy/) | ✅ Wilson CI, auto-suspend |
| — | NotificationService | [notifications/service.py](../src/scalper/notifications/service.py) | [tests/notifications/](../tests/notifications/) | ✅ Console + опційний Telegram |
| — | Orchestrator | [orchestrator/orchestrator.py](../src/scalper/orchestrator/orchestrator.py) | [tests/orchestrator/](../tests/orchestrator/) | ✅ wiring + integration smoke |
| — | Runtime (AppConfig + CLI) | [config/loader.py](../src/scalper/config/loader.py), [__main__.py](../src/scalper/__main__.py) | [tests/test_config_loader.py](../tests/test_config_loader.py), [tests/test_composition.py](../tests/test_composition.py) | ✅ |
| — | Trader UI Dashboard | [dashboard/](../src/scalper/dashboard/) | [tests/dashboard/](../tests/dashboard/) | ✅ V340RK control panel: per-symbol slots, real Binance balance |

**Тести:** 299 passing (станом на 2026-04-25).

---

## Як запустити

### 1. Секрети

Створи файл `.env` у корені (шаблон — [.env.example](../.env.example)):

```
BINANCE_API_KEY=xxxx
BINANCE_API_SECRET=xxxx
BINANCE_TESTNET=true
```

`.env` **git-ігнорується** правилом `.env.*` з [.gitignore:7](../.gitignore#L7).

### 2. Запуск через UI (рекомендований шлях)

```bash
.venv/Scripts/python.exe -m scalper.dashboard --port 8765
# або подвійний клік на start-dashboard.bat
```

Відкрий у браузері: **http://127.0.0.1:8765/app**.

Тут ти отримаєш V340RK control panel — кожна пара як окрема "slot-картка" зі своїми
налаштуваннями (плече, ризик, режим), кнопками START/STOP та власною статистикою.
Зверху — реальний баланс акаунту з Binance Futures (`/fapi/v2/account`), що
авто-оновлюється кожні 3 секунди. Під кожною парою backend запускає окремий
subprocess `python -m scalper --settings configs/runtime_{SYMBOL}.yaml` — тож
кілька пар торгуються паралельно й незалежно.

### 3. Запуск напряму з CLI (без UI)

Те саме але без UI — корисно для unattended runs / serverless deploys.
Створи `configs/settings.yaml` (папка вже в `.gitignore`):

```yaml
mode: live
symbols: [BTCUSDT]
equity_usd: 100.0
leverage: 5
decision:
  base_score_threshold: 0.5   # на testnet ринок повільний, пороги треба знижувати
risk:
  risk_per_trade_usd_abs: 0.1  # ДРІБНІ суми для першого запуску
  max_trades_per_day: 3
```

Запуск:
```bash
.venv/Scripts/python.exe -m scalper --settings configs/settings.yaml
```

Без `--settings` запускається у paper mode (SimulatedExecutionEngine — без реальних ордерів,
але WS до testnet справжній). Журнал — `journal/YYYY-MM-DD.jsonl`.

---

## Trader UI Dashboard

Архітектурно — окремий процес, FastAPI + uvicorn + vanilla JS (без фреймворків).
Шість компонентів:

| Файл | Роль |
|---|---|
| [`dashboard/server.py`](../src/scalper/dashboard/server.py) | FastAPI app: `/app` (UI), `/api/symbols`, `/api/account/balance`, `/api/bot/{start,stop,status}`, `/ws/events` |
| [`dashboard/controller.py`](../src/scalper/dashboard/controller.py) | `BotRegistry` + `BotController` — менеджер subprocess-ів, по одному на символ |
| [`dashboard/symbols.py`](../src/scalper/dashboard/symbols.py) | `BinanceSymbolService` — кеш `/fapi/v1/exchangeInfo` (TTL 10хв, 528 пар на testnet) |
| [`dashboard/account.py`](../src/scalper/dashboard/account.py) | `BinanceAccountService` — кеш `/fapi/v2/account` (TTL 3с), потребує API ключі |
| [`dashboard/stats.py`](../src/scalper/dashboard/stats.py) | `SessionStats` — per-symbol агрегатор з journal (через `JournalTailer`) |
| [`dashboard/static/trader.{html,css,js}`](../src/scalper/dashboard/static/) | UI з slot-картками, typeahead для додавання пар, balance bar |

**Ключові архітектурні рішення UI:**
1. **Один процес на пару, не один на всі** — кожен слот отримує свій
   `configs/runtime_{SYMBOL}.yaml` і запускається як окремий
   `python -m scalper`. Зупинка одної пари не зачіпає іншу.
2. **Equity не вводиться вручну** — `/api/bot/start` бере `available_balance`
   з реального API і записує в runtime.yaml. Це усуває фейковий ввід.
3. **Symbol validation на backend** — `/api/bot/start` перевіряє пари проти
   exchangeInfo, відмовляє з 422 на невідомі. Frontend typeahead — лише UX.
4. **localStorage persist UX-state, не business** — slot configs зберігаються
   у браузері користувача; runtime status завжди з backend через polling.

---

## Хронологія виловлених багів

### Під час збірки модулів

| Де | Симптом | Причина | Фікс |
|---|---|---|---|
| PositionManager | `remaining_qty` ішла в мінус на TP1 | `remaining_qty = plan.size - filled` → 0 після повного філу → мінус на TP fill | Лічити як "ще відкрита кількість": зростає на entry, спадає на exit |
| ExecutionEngine | test_check_notional | Тест очікував 0.05 × 100 = 5 ≥ 10 | qty 0.2 × 100 = 20 ≥ 10 |
| Orchestrator | `journal.start()` / `book.start()` викидали warning про un-awaited coroutine | Ці методи async, а я кликав sync | `_maybe_await` helper ([orchestrator.py:259](../src/scalper/orchestrator/orchestrator.py#L259)) |

### Під час першого живого запуску

| # | Симптом | Причина | Фікс |
|---|---|---|---|
| 1 | `Orchestrator._on_regime_change() missing 2 positional arguments` | Я написав callback як `(symbol, old, new)`, а класифікатор викликає з одним `RegimeChange` | Змінено на `(change)` ([orchestrator.py:90](../src/scalper/orchestrator/orchestrator.py#L90)) |
| 2 | `BTCUSDT: no diff within warmup timeout` (3 attempts) | Orchestrator стартував book/tape до gateway → WS ще не стрімив коли book чекав перший diff | Gateway стартує ПЕРШИМ ([orchestrator.py:109](../src/scalper/orchestrator/orchestrator.py#L109)) |
| 3 | `snapshot mismatch (U=484029, u=484056, snap=478010)` — різниця ~6000 update IDs | **Testnet повертає застарілий REST depth snapshot** щодо WS stream. Стабільно на 3-8 тис. IDs, тобто протокол Binance не може зійтись | Додав [`OBReinitConfig.relaxed_sync`](../src/scalper/book/config.py): при True книжка приймає snapshot як good-enough і live diffs далі. Авто-ввімкнено для `testnet=true` у [loader.py](../src/scalper/config/loader.py) |
| 4 | Pipeline не тригериться на testnet (aggTrade=0 за 10 секунд) | BTCUSDT на testnet не має активних трейдів | Додав `_fast_tick_loop` 250ms у [__main__.py](../src/scalper/__main__.py) + `on_agg_trade` hook у Orchestrator |

### Перший робочий pipeline run

Після всіх фіксів журнал показав повний ланцюг:

```json
{"seq":1,"kind":"startup",...}
{"seq":2,"kind":"startup","payload":{"symbols":["BTCUSDT"]}}
{"seq":3,"kind":"heartbeat","payload":{"daily_r":0.0,"open_positions":0}}
{"seq":4,"kind":"setup_candidate","symbol":"BTCUSDT","payload":{"setup_type":"micro_pullback_continuation","direction":"SHORT","entry":78174.0}}
{"seq":5,"kind":"decision_rejected","payload":{"reason":"score_below_threshold","score":0.3,"threshold":1.0}}
```

Живі дані → FeatureEngine → SetupDetector → DecisionEngine → Journal. Відхилення слабкого сетапу (score 0.3 < 1.0) — правильна поведінка.

### Під час побудови UI

| # | Симптом / зауваження | Причина | Фікс |
|---|---|---|---|
| 5 | "Картинка, а не продукт" — pair input приймав будь-що, бот не валідував | Текстове поле без обмежень, backend брав symbols як є | `BinanceSymbolService` + `/api/symbols`; backend POST `/api/bot/start` повертає 422 на невідому пару; UI став typeahead |
| 6 | "Має бути окремо під кожну пару" | Один процес, один config на список пар = немає per-pair контролю | Refactor: `BotRegistry` — один процес-bot на символ; `SessionStats` per-symbol; UI slot-cards |
| 7 | "Баланс має підтягуватись з бінансу" | Користувач вводив equity_usd вручну, бот рахував позиції на фейкових цифрах | `BinanceAccountService` (`/fapi/v2/account`) → `/api/account/balance`; equity автоматично береться з real `available_balance` при start |

---

## Що лишилось до prod

### Блокери для першого testnet-трейду
- [ ] Знизити `base_score_threshold` у testnet configs (ринок повільний, сетапи рідко набирають 1.0+) — або через UI додати поле "score override"
- [ ] Протестувати повний цикл з реальним ордером (entry → SL/TP → close → expectancy update)

### Покращення UI (можна зараз)
- [ ] Список **відкритих позицій** окремо (зараз бачимо лише лічильник)
- [ ] Графік ціни + entries/exits (TradingView lightweight-charts)
- [ ] **Score-threshold override per slot** (для testnet/повільних ринків)
- [ ] Кнопка "Force-close all positions" для emergency
- [ ] Notification toasts (browser Notification API) на open/close

### Перед prod (дрібні суми, real money)
- [ ] Chaos testing: WS disconnect recovery, REST timeout retry, listenKey expiration
- [ ] Ручна перевірка `BinanceOrderTransport` проти testnet (сьогодні протестовано unit-тестами з fake transport)
- [ ] `setup.py` / `pyproject.toml` entrypoint console_scripts, щоб запускати як `scalper` замість `python -m scalper`
- [ ] ReplayGateway file-loader (зараз NotImplementedError) для бектестів
- [ ] Metrics endpoint (Prometheus) + Grafana дашборд
- [ ] systemd unit / Docker image для деплою
- [ ] **Auth для dashboard** — зараз `/api/bot/start` будь-хто з мережі може тиснути (binds 127.0.0.1, але reverse-proxy експозиція = ризик)

### Оптимізації, не критичні
- [ ] Кешування `tick_size` per-symbol (зараз fallback 0.1 hardcoded у orchestrator snapshot builder)
- [ ] Historical data writethrough — Gateway дампить WS events у `data/raw/YYYY-MM-DD/*.jsonl.gz`, щоб завтра ReplayGateway міг прокрутити це назад
- [ ] Live `equity_fn` у Orchestrator — щоб RiskEngine бачив поточний баланс під час сесії, а не snapshot на момент старту

---

## Ключові рішення (чому так, а не інак)

1. **Protocol-based `OrderTransport`** — ExecutionEngine не знає про Binance; тестується з fake transport. BinanceOrderTransport — тонкий адаптер.
2. **`_maybe_await` у Orchestrator** — модулі можуть бути sync або async; композиційний root не мусить знати.
3. **`relaxed_sync` як config opt-in** — не ігноруємо protocol correctness у проді, тільки тимчасово на testnet.
4. **`.env` як секрети, `configs/settings.yaml` як структура** — секрети ніколи не в git, структурні параметри можна комітити.
5. **`SecretStr` з pydantic** — ключі не світяться в `repr()` / логах.
6. **Paper mode default** — `mode: paper` → SimulatedExecutionEngine; помилитись і випадково відкрити live-ордер важче.
7. **Один subprocess на пару (UI)** — кожен слот в UI = `python -m scalper` зі своїм `runtime_{SYMBOL}.yaml`. Зупинка/конфіг/статистика ізольовані. Простіше за multi-symbol orchestration; для 20+ пар — refactor у shared resources.
8. **Equity не з UI, з API** — `/api/bot/start` ігнорує client-sent `equity_usd` і бере `available_balance` з `/fapi/v2/account`. Усуває можливість фейкового сайзингу позицій.
9. **JournalTailer як єдина шина для UI stats** — dashboard не дзвонить бот напряму; читає JSONL який бот пише. Це дає read-only режим, post-mortem-аналіз і коректну роботу при крашах бот-процесу (UI бачить історичні події).
