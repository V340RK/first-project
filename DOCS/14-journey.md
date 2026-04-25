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

### Великий debug "5 годин без жодної угоди"

Користувач: «бот пропрацював 5 годин і не зробив жодної угоди». Розкопано **5 окремих багів**, що збиралися каскадом:

| # | Симптом у журналі | Корінь | Фікс |
|---|---|---|---|
| 8 | `risk_accepted=3, position_opened=0` | `__main__.py` ніколи не викликав `execution.register_symbol(filters)` → SimulatedExecutionEngine відмовляв кожен `place_order` як `no_symbol_filters` → `position.open()` повертала `False` | Після `gateway.start()` — для кожного символу вичитуємо `gateway.get_symbol_filters()` і реєструємо в execution. Логуємо `tick/step/min_qty/min_notional` |
| 9 | Помилки невидимі — журнал нічого не показував про реджект | `BotController` запускав subprocess з `stdout=DEVNULL stderr=DEVNULL` → `logger.error("entry order rejected")` йшов у нікуди | `stdout` → `logs/bot_{SYMBOL}.log` файл; stderr → той самий потік. Заголовок `=== START YYYY-MM-DD HH:MM:SS params=... ===` для розмежування сесій |
| 10 | `risk_rejected=53 max_concurrent_positions` після єдиного `risk_accepted` | `RiskEngine.evaluate()` сам інкрементував `open_positions_count` ще ДО успішного `position.open()`. Якщо open() падав — counter залишався переанкрементованим, всі майбутні setups блокувалися | Винесено інкремент у `on_position_opened()`; Orchestrator викликає його лише при `opened=True`. Додано regression-тест `test_evaluate_does_not_increment_until_position_opened` |
| 11 | Після фіксу #8: pipeline взагалі перестав тригеритись (0 setup_candidate) | `MarketDataGateway.on_agg_trade()` тримав `_cb_agg_trade: AggTradeCallback \| None` — ОДИН callback. Мій новий `__main__.py:on_agg_trade(_tick_pending)` (для feeding sim execution) ПЕРЕТЕР Orchestrator-ів `_on_agg_trade_tick` | Усі Gateway callbacks (`agg_trade`, `depth_diff`, `kline`, `book_ticker`, `user_event`) перероблено в `list[Callback]`, кожен `on_xxx()` — append, dispatch у циклі з per-callback try/except |
| 12 | Після всіх фіксів: 20 setup_candidate за 3 хв, але 100% rejected `setup_blocked_in_regime:low_liq` | На testnet ринок постійно у `LOW_LIQ` (мало активності — це факт середовища, не баг). За дефолтом `regime_allow_map[LOW_LIQ] = set()` блокує ВСІ setup-и | Додано `DecisionConfig.relaxed_regime: bool = False`. Якщо True — фільтр `regime_allow_map` пропускається. Dashboard server авто-вмикає при `BINANCE_TESTNET=true` (record у `runtime_{SYMBOL}.yaml: decision.relaxed_regime: true`). На проді (testnet=False) сувора фільтрація залишається |

**Перший живий трейд:** після всіх фіксів — `setup_candidate → decision_accepted (regime: low_liq, relaxed) → risk_accepted (qty=0.123 BTC, risk=$1.99) → position_opened (SHORT @ 77531.6, stop @ 77547.7)` за 90 секунд.

### Друге коло debug «UI показує 0 трейдів попри живі позиції»

Після #8-12 бот таки відкривав позиції (видно у `logs/bot_BTCUSDT.log: closed BTCUSDT: +0.00R`), але `/api/bot/status` повертав `trades_closed=0`. Виявилося ще 3 проблеми:

| # | Симптом | Корінь | Фікс |
|---|---|---|---|
| 13 | UI ніколи не показує закриті трейди, хоча PositionManager їх логує | `PositionManager._finalize()` повідомляє `RiskEngine.on_position_closed` і пише в logger, але **не сповіщає Orchestrator**. Тому в журнал не йдуть події `position_closed` / `trade_outcome`. SessionStats читає журнал → бачить 0 | Додано `PositionManager.on_position_closed(cb)` callback hook. Orchestrator підписується у `_wire_callbacks()` і пише обидві події у журнал з `realized_r`, `realized_usd`, `reason`, `was_stopped` |
| 14 | Score=0.4, threshold=0.4 → reject, хоча `score < threshold` має дати False | Float-precision: `0.39999999999999997 < 0.4 == True`. Сетап «точно по порогу» завжди реджектився | `if score < threshold - 1e-9` |
| 15 | Користувач без UI-control не міг знизити default `base_score_threshold=1.0`, а scores на testnet 0.2-0.4 | Поле `score_threshold_override` приймалось у POST `/api/bot/start`, але UI не мав control для нього → все ігнорувалось → 100% setups rejected | Dashboard server при `BINANCE_TESTNET=true` авто-ставить `score_threshold_override=0.25` якщо клієнт не задав. Записує у `runtime_{SYMBOL}.yaml: decision.base_score_threshold: 0.25` |

**Перший повний цикл:** через **42 секунди** після старту 3-х ботів — `setup_candidate → position_opened → position_closed → trade_outcome` у журналі, UI показав `trades_closed=1` для BTCUSDT.

### Третє коло debug «trades в UI, але порожньо на Binance»

Користувач показав скріншот Binance Futures testnet: `Trade History: You have no trade history`. Виявилося що paper-mode симулює угоди в пам'яті (не шле на біржу) — це by design, але я не зробив це чітким у комунікації. Користувач переключив у LIVE mode → знов 0 угод. Розкопав ще 2 баги:

| # | Симптом | Корінь | Фікс |
|---|---|---|---|
| 16 | `risk_accepted qty=3.333 BTC` (notional ≈ $260k!) → Binance: `"Margin is insufficient"` (-2019) → `position.open()` повертала False, без події в журналі | RiskEngine рахував `qty = risk_usd / stop_distance`. Setup з тонким стопом ($0.5 для BTC) дає величезну позицію, що виходить за `equity * leverage` | `RiskConfig.leverage` + `max_notional_usage = 0.9`. У `_compute_size()`: `max_qty_by_notional = (equity*leverage*usage)/price`. `qty = min(qty_by_risk, max_qty_by_notional)`. `__main__.py` копіює `cfg.leverage` → `risk_cfg.leverage` |
| 17 | Помилки біржі (`-2019`, `-2010`) знаходилися тільки у `logs/bot_{SYMBOL}.log`; журнал нічого не бачив → UI ніяк не сигналізував користувачу | `PositionManager.open()` повертала `False` без сповіщення Orchestrator-а. Тільки `logger.error()` | Доданo callback `PositionManager.on_open_failed(cb)`. Orchestrator підписується і пише `WARNING` подію в журнал з `setup_type/qty/reason`. UI може це показати в майбутньому |

**Підтвердження real-trade:** через **2 секунди** після старту в LIVE mode — позиція 0.116 BTC LONG @ 77596.6 з'явилася у `/fapi/v2/positionRisk` testnet-акаунту. На UI `position_opened` подія, на біржі реальна expozycja з unrealized PnL.

**Залишений блокер #18 (для наступної сесії):** Open Orders на біржі = 0 — SL/TP не виставлені. PositionManager викликає `_place_protection()` тільки коли `result.status == "FILLED"`. Live entry повертається як `NEW` (LIMIT IOC) → бот чекає fill через WebSocket user stream → user stream періодично мовчить (`listenKey expired`, `WS silence on user: 32996ms`) → fill не доставляється → protection не виставляється → позиція висить без SL.

### Четверте коло debug «orphan позиція без SL» (#18 розкритий)

Користувач обрав варіант "всі 3 разом" (trust REST + REST poll + listen key supervisor). По шляху знайшов ще 2 баги. Усього в цьому раунді **5 фіксів**:

| # | Симптом | Корінь | Фікс |
|---|---|---|---|
| 18a | Live LIMIT IOC entry → REST повертає `status=NEW filled_qty>0` (PARTIAL), але PositionManager перевіряв тільки `status=="FILLED"` → protection не ставив, чекав WS fill, який мовчав | `if result.status == "FILLED"` → `if result.filled_qty > 0` (status irrelevant) | [`position/manager.py:152`](src/scalper/position/manager.py) — `entry_processed_via_rest=True` flag для дедупу |
| 18b | Якщо REST повернув filled_qty=0 але реально на біржі fill потім стався (race), позиція висить у PENDING_ENTRY вічно | Ніхто не запитує статус ордера після N сек | `PositionManager.reconcile_pending_entries()` дзвонить `get_open_orders` через 5с; якщо entry_coid немає у відкритих → presume filled. Викликається з Orchestrator.on_slow_tick |
| 18c | WS fill приходить пізно для уже-обробленого через REST entry → `_on_fill` подвоює qty/commission і повторно дзвонить `_place_protection` | Не було дедупу між REST та WS-каналами | У `_on_fill`: `if fill.client_order_id == pos.entry_coid and pos.entry_processed_via_rest: return` |
| 18d | Reconcile використовував `plan.position_size` (0.29 BTC), але реальний fill був 0.005 → `_place_protection` ставив SL з reduce_only qty=0.29 → Binance reject `"max_retries"` → `EMERGENCY close` лупа | Reconcile не знав реальної positionAmt акаунту | Додано `OrderTransport.get_position_risk()` + Binance `/fapi/v2/positionRisk`. Reconcile читає `actual_qty` з біржі, ставить SL/TP саме з нею. Якщо positionAmt=0 → ордер expired без fill, drop locally |
| bonus | LIMIT IOC partial → `status="EXPIRED"` (бо залишок auto-cancelled). `_parse_order_result` каже `success = status not in ("REJECTED", "EXPIRED")` → бот трактує як reject → не зберігає позицію → потім re-place_order накопичує orphan'и на біржі | EXPIRED ≠ rejection якщо filled_qty>0 | `success = (status not in ("REJECTED", "EXPIRED")) or filled > 0` |

**Тести:** +8 нових (`tests/position/test_rest_fill_paths.py`), 308/308 passing. Покривають REST trust, dedup REST↔WS, reconcile-uses-real-positionAmt, drop-on-no-actual-fill.

**Підтвердження безпеки:** на тонкому testnet ордер часто не filled взагалі — reconcile тепер коректно бачить `positionRisk=0` і логує `"entry expired without fill — dropping local position"` без orphan'ів і без emergency_close циклу.

### Альтернативний sizing: «% від балансу як margin»

Користувач: «Хочу налаштовувати у відсотках. На балансі 1000, ставлю 10% — угода відкривається на 100$.»

Це **margin-based sizing** (фіксована частка balance як margin), на відміну від default R-based (qty з допустимого збитку при стопі). Notional = `equity * pct/100 * leverage`. R-ризик плаваючий — залежить від того, як setup виставив stop.

| Поле | Значення |
|---|---|
| `RiskConfig.margin_per_trade_pct: float \| None` | Якщо встановлено → margin sizing активний; інакше — R-based |
| `RiskEngine._compute_size` | Branch на margin-mode: `qty = (equity * pct/100 * leverage) / entry_price` |
| `risk_overshoot` check | Skip-iться у margin-mode (плаваючий R за дизайном) |
| `BotRunParams.sizing_mode` | `'risk_usd'` (default) \| `'margin_pct'` |
| API `POST /api/bot/start` | Приймає `risk_per_trade_usd: float` АБО `margin_per_trade_pct: float`; визначає sizing_mode автоматично |
| UI слот | Dropdown `Ризик USDT` / `% балансу (margin)` + єдине поле value; динамічний hint під полем |

Notional cap (`equity * leverage * 0.9`) залишається активним для обох modes — захист від помилок налаштування.

Тести: +4 у `tests/risk/test_margin_sizing.py` — margin-mode фіксує qty незалежно від stop_distance, R-mode ignoring margin field, notional cap працює і в margin-mode, default behavior збережено.

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
