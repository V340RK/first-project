# Configuration — Конфігурація бота

## Навіщо окремий файл?

Всі параметри бота — пороги, ваги, таймфрейми, ризик — в **одному** місці.
Без цього доводиться шукати магічні числа по всьому коду. З цим — один YAML файл керує всією поведінкою бота.

---

## Структура config/settings.yaml

```yaml
# =============================================================
# SCALPING BOT CONFIGURATION
# =============================================================
# ВАЖЛИВО: копію production settings тримаємо в settings.local.yaml
# і НЕ коммітимо її в git (додати в .gitignore)
# =============================================================

# -------------------------------------------------------------
# Exchange
# -------------------------------------------------------------
exchange:
  name: "binance_futures"
  mode: "testnet"              # "testnet" або "production"
  api_key: "${BINANCE_API_KEY}"       # з env variable
  api_secret: "${BINANCE_API_SECRET}"
  
  base_url_rest:
    testnet: "https://testnet.binancefuture.com"
    production: "https://fapi.binance.com"
  
  base_url_ws:
    testnet: "wss://stream.binancefuture.com/ws"
    production: "wss://fstream.binance.com/ws"

# -------------------------------------------------------------
# Trading pair
# -------------------------------------------------------------
trading:
  symbol: "BTCUSDT"
  tick_size: 0.10              # мінімальний крок ціни
  qty_step: 0.001              # мінімальний крок кількості
  min_notional: 100            # мінімальний розмір позиції в USDT
  leverage: 5                  # плече (1x-20x для BTC)
  margin_type: "ISOLATED"      # "ISOLATED" або "CROSS"

# -------------------------------------------------------------
# Risk Management
# -------------------------------------------------------------
risk:
  # Розмір позиції
  risk_per_trade_pct: 1.0          # % депозиту на одну угоду
  max_position_size_pct: 10.0      # макс % депозиту в одній позиції
  
  # R:R
  min_risk_reward: 2.0             # мінімум 1:2
  
  # Stop Loss
  stop_method: "structure"         # "structure" | "atr" | "fixed"
  stop_buffer_ticks: 2             # додаткові тіки до технічного рівня
  atr_stop_multiplier: 1.5         # для ATR-based стопу
  atr_period: 14
  max_stop_pct: 2.0                # макс % від ціни (захист від дурних сигналів)
  
  # Take Profit
  tp_method: "partial"             # "fixed" | "partial" | "poi_based"
  partial_tp_at_1r: 0.5            # закрити 50% на 1R
  trail_remaining: true            # трейлити залишок
  trail_distance_atr: 1.0          # відстань трейлу в ATR
  
  # Daily limits
  daily_loss_limit_pct: 5.0        # денний стоп
  daily_profit_target_pct: 3.0     # по досягненню — зупиняємось (не жадничаємо)
  max_consecutive_losses: 4        # після 4 програшів — пауза
  
  # Weekly limits
  weekly_loss_limit_pct: 10.0
  
  # Circuit Breaker
  circuit_breaker:
    warning_dd_pct: 5.0            # попередження
    reduced_size_dd_pct: 7.0       # зменшити розмір позиції вдвічі
    pause_dd_pct: 10.0             # пауза на день
    kill_switch_dd_pct: 20.0       # повна зупинка
  
  # Максимум одночасних позицій
  max_concurrent_positions: 1      # для скальпінгу рекомендовано 1

# -------------------------------------------------------------
# Timeframes
# -------------------------------------------------------------
timeframes:
  htf: "1h"                    # High TF — для тренду і головних POI
  mtf: "15m"                   # Middle TF — для структури
  ltf: "1m"                    # Low TF — для тригерів входу
  scalp: "1m"                  # Для скальпінга
  
  # Для multi-timeframe аналізу
  tf_stack:
    - "1m"
    - "5m"
    - "15m"
    - "1h"
    - "4h"

# -------------------------------------------------------------
# Signal Thresholds
# -------------------------------------------------------------
signals:
  
  # Bid/Ask Imbalance
  imbalance:
    threshold: 0.6                 # |imb| > 0.6 вважається значущим
    stacked_min_levels: 3          # мінімум рівнів для stacked
    stacked_threshold: 0.6         # поріг для кожного рівня в stack
  
  # Absorption
  absorption:
    volume_multiplier: 2.0         # об'єм > 2× середнього
    range_multiplier: 0.5          # range < 0.5× середнього
    delta_ratio_min: 0.3           # |delta| / total_vol >= 0.3
    lookback_candles: 20           # для розрахунку "середнього"
  
  # Delta Spike
  delta_spike:
    std_threshold: 2.0             # 2σ
    lookback: 20
    min_spike_size: 50             # абсолютний мінімум (в USDT)
  
  # CVD Divergence
  cvd_divergence:
    lookback: 20                   # свічок для пошуку свінгів
    swing_window: 3                # вікно для виявлення swing
  
  # Spoof Detection
  spoof:
    min_order_size: 50             # BTC (для BTCUSDT), мінімальний розмір
    max_lifetime_sec: 5            # ордер прожив < 5 сек → підозріло
    min_occurrences: 2             # 2+ рази на одному рівні
  
  # Micro Pullback
  pullback:
    min_retrace_pct: 20            # мінімум 20% від розміру імпульсу
    max_retrace_pct: 50            # максимум 50% (інакше це розворот)
    window_candles: 3              # макс свічок на відкат

# -------------------------------------------------------------
# SMC (Smart Money Concepts)
# -------------------------------------------------------------
smc:
  # Swing detection
  swing_window: 5                  # ±N свічок для виявлення swing
  
  # Order Block
  order_block:
    impulse_atr_multiplier: 3.0    # імпульс після OB має бути 3× ATR
    max_age_candles: 50            # OB дійсний до 50 свічок після створення
    entry_zone: [0.3, 0.5]         # входимо в цю зону OB (% від low-high)
  
  # FVG
  fvg:
    min_size_atr_multiplier: 1.5   # імпульс створив FVG має бути >= 1.5 ATR
    max_age_candles: 30            # FVG дійсний до 30 свічок
    mitigation_threshold: 0.5      # FVG "заповнений" на 50% = mitigated
  
  # BOS
  bos:
    confirm_with_close: true       # підтверджуємо закриттям (не wick)
    min_displacement_atr: 1.0      # мінімум 1 ATR пробою
  
  # CHoCH
  choch:
    require_on_lower_tf: true      # CHoCH шукаємо на LTF для підтвердження HTF POI
  
  # SFP
  sfp:
    min_wick_ratio: 0.6            # wick >= 60% від загальної довжини свічки
    lookback_swings: 10
  
  # Inducement
  inducement:
    enabled: true
    search_within_atr: 2.0         # шукати inducement в радіусі 2 ATR від POI

# -------------------------------------------------------------
# Confluence Scoring
# -------------------------------------------------------------
confluence:
  # Мінімальний score для дозволу на вхід
  min_score: 0.65                  # 0.0 - 1.0
  
  # Ваги різних сигналів
  weights:
    htf_trend_aligned: 2
    in_correct_zone: 2             # premium/discount
    at_htf_poi: 2
    liquidity_swept: 1
    ltf_confirmation: 3            # найважливіше
    sc_or_wick: 1
    clear_path_to_tp: 2
    killzone_time: 1
    order_flow_aligned: 2
  
  # Обов'язкові (якщо немає — скасовуємо угоду)
  required:
    - "htf_trend_aligned"
    - "ltf_confirmation"

# -------------------------------------------------------------
# Trading Schedule
# -------------------------------------------------------------
schedule:
  timezone: "Europe/Kyiv"          # UTC+3
  
  # Вікна активної торгівлі (в годинах локального часу)
  active_windows:
    - name: "london_killzone"
      start: "09:00"
      end: "12:00"
      weight: 1.0
    
    - name: "ny_killzone"
      start: "15:00"
      end: "18:00"
      weight: 1.0
    
    - name: "ny_midnight"
      start: "07:00"
      end: "08:00"
      weight: 0.8
  
  # Не торгуємо:
  blackout_windows:
    - name: "asian_session"
      start: "02:00"
      end: "09:00"
    
    - name: "lunch_time"
      start: "14:00"
      end: "15:00"
  
  # Перед новинами (хвилини)
  news_blackout:
    before_minutes: 30
    after_minutes: 30
    min_impact: "high"             # "low" | "medium" | "high"

# -------------------------------------------------------------
# Execution
# -------------------------------------------------------------
execution:
  entry_order_type: "LIMIT"        # "LIMIT" | "MARKET"
  entry_timeout_sec: 30            # скасувати LIMIT якщо не виконався
  
  # Slippage protection
  max_slippage_pct: 0.05           # макс 0.05% проковзування
  
  # Re-entry
  allow_reentry: false             # після стопу — ре-вхід?
  reentry_cooldown_sec: 60         # пауза між спробами
  
  # Partial fills
  min_fill_ratio: 0.8              # прийняти виконання якщо >= 80% кількості

# -------------------------------------------------------------
# Data Processing
# -------------------------------------------------------------
data:
  # Буфери в пам'яті
  trade_buffer_size: 10000
  orderbook_depth: 20              # рівнів в WebSocket підписці
  
  # Історія свічок
  candle_history:
    "1m": 500
    "5m": 200
    "15m": 100
    "1h": 48
    "4h": 30

# -------------------------------------------------------------
# Logging
# -------------------------------------------------------------
logging:
  level: "INFO"                    # DEBUG | INFO | WARNING | ERROR
  
  # Куди писати
  outputs:
    console: true
    file: true
    
  file:
    path: "./logs/bot.log"
    rotation: "daily"
    retention_days: 30
  
  # Окремі логи
  trade_log:
    path: "./logs/trades.jsonl"
    format: "jsonl"                # кожна угода — окремий JSON
  
  signal_log:
    path: "./logs/signals.jsonl"
    log_rejected: true             # писати і ті сигнали які відхилені
  
  error_log:
    path: "./logs/errors.log"

# -------------------------------------------------------------
# Notifications
# -------------------------------------------------------------
notifications:
  telegram:
    enabled: true
    bot_token: "${TELEGRAM_BOT_TOKEN}"
    chat_id: "${TELEGRAM_CHAT_ID}"
    
    # Що нотифікувати
    notify_on:
      - "trade_opened"
      - "trade_closed"
      - "daily_summary"
      - "circuit_breaker_triggered"
      - "kill_switch_activated"
      - "error_critical"
    
    # Не нотифікувати (занадто часто):
    # - "signal_generated"
    # - "order_placed"
  
  email:
    enabled: false

# -------------------------------------------------------------
# Monitoring
# -------------------------------------------------------------
monitoring:
  health_check_interval_sec: 30
  heartbeat_file: "./logs/heartbeat.txt"
  
  # Аварійні умови
  alerts:
    websocket_disconnect_sec: 30   # відключення > 30с → алерт
    no_trades_from_exchange_sec: 60
    high_latency_ms: 500           # якщо обробка > 500ms → алерт

# -------------------------------------------------------------
# Backtesting (для тестового режиму)
# -------------------------------------------------------------
backtest:
  data_dir: "./data/historical"
  start_date: "2024-01-01"
  end_date: "2024-12-31"
  initial_balance: 10000
  commission_pct: 0.04             # Binance Futures taker fee 0.04%
  slippage_ticks: 1                # симулюємо 1 tick slippage

# -------------------------------------------------------------
# Development
# -------------------------------------------------------------
dev:
  dry_run: false                   # якщо true — не відправляємо реальні ордери
  paper_trading: false             # симуляція але в real-time
  debug_signals: false             # детальний лог по кожному сигналу
```

---

## config/settings.local.yaml (приклад)

Файл який **не комітимо в git**. Містить реальні ключі і локальні override.

```yaml
# Production override
exchange:
  mode: "production"

# Реальні ключі (або через env vars)
# Тримати поза репозиторієм!

# Для локального тестування — менший розмір
risk:
  risk_per_trade_pct: 0.25
  daily_loss_limit_pct: 2.0
```

---

## .env файл для секретів

Краще ніж зберігати в yaml:

```bash
# .env (додати в .gitignore!)

BINANCE_API_KEY=abcd1234...
BINANCE_API_SECRET=xyz789...

TELEGRAM_BOT_TOKEN=7123456789:ABC...
TELEGRAM_CHAT_ID=123456789
```

Завантаження в Python:
```python
from dotenv import load_dotenv
import os
load_dotenv()

api_key = os.getenv('BINANCE_API_KEY')
```

---

## Validation — перевірка при старті

Бот при запуску повинен **валідувати** конфіг і видати помилку якщо щось не так:

```python
def validate_config(config):
    errors = []
    
    # Перевірка обов'язкових ключів
    if not config['exchange']['api_key']:
        errors.append("API key not set")
    
    # Ризик
    if config['risk']['risk_per_trade_pct'] > 5:
        errors.append("risk_per_trade_pct > 5% — це занадто ризиковано")
    
    if config['risk']['min_risk_reward'] < 1.5:
        errors.append("min_risk_reward < 1.5 — стратегія математично невигідна")
    
    # Leverage
    if config['trading']['leverage'] > 20:
        errors.append("leverage > 20 — дуже ризиковано")
    
    # Kill switch обов'язковий
    if 'kill_switch_dd_pct' not in config['risk']['circuit_breaker']:
        errors.append("Kill switch не налаштовано!")
    
    if errors:
        for e in errors:
            print(f"❌ {e}")
        raise ValueError("Config validation failed")
    
    print("✅ Config validation passed")
```

---

## Як змінювати конфіг без зупинки бота?

### Hot reload:
```python
import signal

def reload_config(signum, frame):
    global config
    config = load_config('./config/settings.yaml')
    logger.info("Config reloaded")

signal.signal(signal.SIGUSR1, reload_config)
# Відправити: kill -SIGUSR1 <bot_pid>
```

**Увага**: не всі параметри можна змінити на льоту (leverage, API keys) — тільки signal thresholds, risk limits, schedule.

---

## Що далі?

- [10-backtesting.md](./10-backtesting.md) — як використати цей конфіг для тестів на історії
- [11-logging-monitoring.md](./11-logging-monitoring.md) — як моніторити що бот працює правильно
