# Logging & Monitoring — Слідкування за ботом

## Навіщо?

Бот працює **24/7 без нагляду**. Якщо щось зламається — ти маєш дізнатись про це **ВЖЕ**, не через 12 годин коли відкриєш компʼютер.

Три рівні моніторингу:
1. **Логи** — детальний запис усього що відбулось (для дебагу)
2. **Метрики** — агреговані числа (скільки угод, P&L, latency)
3. **Алерти** — пуш-повідомлення в Telegram при важливих подіях

---

## 1. Logging (структуровані логи)

### Принципи:
- **Structured JSON** — не простий текст, щоб легко парсити
- **Рівні**: DEBUG / INFO / WARNING / ERROR / CRITICAL
- **Rotation** — автоматично архівувати старі логи (щоб диск не забивало)
- **Окремі файли** по компонентам

### Налаштування `logging` в Python:

```python
# logger.py
import logging
import json
from logging.handlers import RotatingFileHandler
from pathlib import Path

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log = {
            'timestamp': self.formatTime(record, '%Y-%m-%dT%H:%M:%S.%fZ'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        # Додаткові поля з extra
        if hasattr(record, 'symbol'): log['symbol'] = record.symbol
        if hasattr(record, 'trade_id'): log['trade_id'] = record.trade_id
        if hasattr(record, 'signal_type'): log['signal_type'] = record.signal_type
        if record.exc_info:
            log['exception'] = self.formatException(record.exc_info)
        return json.dumps(log, ensure_ascii=False)

def setup_logger(name: str, log_file: str, level=logging.INFO):
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Файловий handler з ротацією
    Path('logs').mkdir(exist_ok=True)
    fh = RotatingFileHandler(
        f'logs/{log_file}',
        maxBytes=100 * 1024 * 1024,    # 100 MB
        backupCount=10,                  # зберігати 10 старих
        encoding='utf-8'
    )
    fh.setFormatter(JSONFormatter())
    logger.addHandler(fh)

    # Console handler (для розробки)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    logger.addHandler(ch)

    return logger

# Використання:
signal_log = setup_logger('signals', 'signals.log')
trade_log = setup_logger('trades', 'trades.log')
error_log = setup_logger('errors', 'errors.log', level=logging.WARNING)

# Логування з контекстом:
signal_log.info('Signal detected', extra={
    'signal_type': 'bid_imbalance',
    'symbol': 'BTCUSDT',
    'price': 50000.0,
    'score': 0.75
})
```

### Структура логів:

```
logs/
├── signals.log           ← всі виявлені сигнали
├── signals.log.1         ← архів (після rotation)
├── trades.log            ← відкриті/закриті угоди
├── errors.log            ← помилки і винятки
├── websocket.log         ← події WebSocket (reconnect, gap)
├── data.log              ← статистика обробки даних
└── system.log            ← heartbeat, startup, shutdown
```

### Приклад JSON-лога:
```json
{
  "timestamp": "2026-04-20T15:34:22.123Z",
  "level": "INFO",
  "logger": "trades",
  "message": "Trade opened",
  "trade_id": "T_20260420_15342201",
  "symbol": "BTCUSDT",
  "side": "BUY",
  "entry_price": 50123.5,
  "quantity": 0.1,
  "stop_loss": 50050.0,
  "take_profit": 50280.0,
  "risk_usd": 7.35,
  "confluence_score": 0.72,
  "signals_triggered": ["stacked_imbalance", "absorption", "at_poi"]
}
```

---

## 2. Trade Journal (журнал угод)

Окремо від логів — **JSONL файл** (один JSON на рядок) з кожною угодою.

### Зберігаємо ВСЕ:
```python
import json
from datetime import datetime

class TradeJournal:
    def __init__(self, path: str = 'journal/trades.jsonl'):
        self.path = Path(path)
        self.path.parent.mkdir(exist_ok=True)

    def record_open(self, trade):
        entry = {
            'event': 'open',
            'timestamp': datetime.utcnow().isoformat(),
            'trade_id': trade['id'],
            'symbol': trade['symbol'],
            'side': trade['side'],
            'entry_price': trade['entry_price'],
            'quantity': trade['quantity'],
            'stop_loss': trade['stop_loss'],
            'take_profit': trade['take_profit'],
            'risk_usd': trade['risk'],
            'signals': trade['signals_triggered'],
            'confluence_score': trade['confluence'],
            'market_state': {
                'price': trade['current_price'],
                'volatility_1h': trade['atr_1h'],
                'cvd': trade['cvd'],
                'htf_trend': trade['htf_trend']
            }
        }
        self._append(entry)

    def record_close(self, trade, exit_reason: str):
        pnl = self._calc_pnl(trade)
        entry = {
            'event': 'close',
            'timestamp': datetime.utcnow().isoformat(),
            'trade_id': trade['id'],
            'exit_price': trade['exit_price'],
            'exit_reason': exit_reason,     # 'stop_loss' / 'take_profit' / 'manual' / 'kill_switch'
            'pnl_usd': pnl,
            'pnl_pct': pnl / trade['risk'],   # в R-множниках (R:R)
            'duration_sec': trade['duration'],
            'mae_pct': trade['mae'],          # max adverse excursion
            'mfe_pct': trade['mfe'],          # max favorable excursion
            'fees_usd': trade['fees']
        }
        self._append(entry)

    def _append(self, entry: dict):
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
```

### Аналіз журналу:
```python
import pandas as pd

def load_journal(path='journal/trades.jsonl') -> pd.DataFrame:
    records = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            records.append(json.loads(line))
    return pd.DataFrame(records)

# Денна статистика:
df = load_journal()
closes = df[df['event'] == 'close']
daily = closes.set_index(pd.to_datetime(closes['timestamp'])).resample('D')['pnl_usd'].agg(['sum', 'count', 'mean'])
```

---

## 3. Telegram Alerts (повідомлення в реальному часі)

### Налаштування:

1. Створи бота через [@BotFather](https://t.me/BotFather) — отримаєш `BOT_TOKEN`
2. Створи приватний канал, додай бота як admin
3. Дізнайся `CHAT_ID` каналу:
```bash
curl https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
```

### Реалізація:
```python
import aiohttp
import asyncio
from enum import Enum

class AlertLevel(Enum):
    INFO = '🟢'
    WARNING = '🟡'
    ERROR = '🔴'
    CRITICAL = '🚨'

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = None

    async def send(self, text: str, level: AlertLevel = AlertLevel.INFO):
        if not self.session:
            self.session = aiohttp.ClientSession()

        message = f"{level.value} {text}"
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            await self.session.post(url, json={
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }, timeout=5)
        except Exception as e:
            # Не даємо Telegram помилкам зупинити бота
            error_log.error(f"Telegram send failed: {e}")

    async def trade_opened(self, trade):
        await self.send(
            f"<b>📥 Trade OPENED</b>\n"
            f"Symbol: <code>{trade['symbol']}</code>\n"
            f"Side: {trade['side']}\n"
            f"Entry: {trade['entry_price']}\n"
            f"Stop: {trade['stop_loss']}\n"
            f"TP: {trade['take_profit']}\n"
            f"Risk: ${trade['risk']:.2f}\n"
            f"Score: {trade['confluence']:.2f}\n"
            f"Signals: {', '.join(trade['signals_triggered'])}",
            AlertLevel.INFO
        )

    async def trade_closed(self, trade, pnl: float, reason: str):
        level = AlertLevel.INFO if pnl > 0 else AlertLevel.WARNING
        emoji = '✅' if pnl > 0 else '❌'
        await self.send(
            f"<b>{emoji} Trade CLOSED ({reason})</b>\n"
            f"P&L: <b>${pnl:+.2f}</b> ({pnl / trade['risk']:+.2f}R)\n"
            f"Duration: {trade['duration_sec']}s",
            level
        )

    async def daily_summary(self, stats):
        await self.send(
            f"<b>📊 Daily Summary</b>\n"
            f"Trades: {stats['count']}\n"
            f"Win rate: {stats['win_rate']:.1%}\n"
            f"P&L: ${stats['total_pnl']:+.2f}\n"
            f"Balance: ${stats['balance']:.2f}",
            AlertLevel.INFO
        )
```

### Коли надсилати алерти?

**ЗАВЖДИ (Critical):**
- 🚨 Kill switch triggered (бот зупинив усе)
- 🚨 Circuit breaker hit (досягнуто денного ліміту збитку)
- 🚨 WebSocket disconnected >30 sec
- 🚨 Exception unhandled (крітичний баг)
- 🚨 Order rejected by exchange
- 🚨 Балан нижче мінімуму

**Важливе (Warning):**
- 🟡 Stop-loss hit великою сумою
- 🟡 Rate limit близько до ліміту (>80%)
- 🟡 Низька частота даних (підозра на проблеми)

**Інформаційне (INFO):**
- 🟢 Trade opened
- 🟢 Trade closed
- 🟢 Daily summary (щоденний звіт)
- 🟢 Bot started/stopped
- 🟢 Config reloaded

### ⚠️ Правило: НЕ спам!
- Максимум 1 алерт на сигнал
- Дебаунс: якщо та сама подія повторюється — тільки перший алерт

---

## 4. Health Check / Heartbeat

Бот повинен **сам повідомляти що живий**.

```python
import time
import asyncio

class HeartbeatMonitor:
    def __init__(self, notifier):
        self.last_trade_signal = time.time()
        self.last_ws_message = time.time()
        self.notifier = notifier
        self.alerted = False

    def touch_ws(self):
        self.last_ws_message = time.time()

    async def monitor_loop(self):
        while True:
            now = time.time()

            # WebSocket мовчить >30 сек — алерт
            if now - self.last_ws_message > 30:
                if not self.alerted:
                    await self.notifier.send(
                        f"⚠️ WebSocket silent for {int(now - self.last_ws_message)}s",
                        AlertLevel.CRITICAL
                    )
                    self.alerted = True
            else:
                self.alerted = False

            # Щогодинний heartbeat "я живий"
            await asyncio.sleep(60)
```

---

## 5. Metrics / Dashboard

### Prometheus + Grafana (для просунутих)
- Бот експонує метрики на `localhost:9090/metrics`
- Grafana їх малює на dashboard
- Можна бачити real-time: trades/min, latency, PnL curve

### Простий варіант: CSV → Pandas → matplotlib
```python
import pandas as pd
import matplotlib.pyplot as plt

def plot_equity_curve(journal_path='journal/trades.jsonl'):
    df = load_journal(journal_path)
    closes = df[df['event'] == 'close'].copy()
    closes['timestamp'] = pd.to_datetime(closes['timestamp'])
    closes['cumulative_pnl'] = closes['pnl_usd'].cumsum()

    plt.figure(figsize=(12, 6))
    plt.plot(closes['timestamp'], closes['cumulative_pnl'])
    plt.title('Equity Curve')
    plt.xlabel('Time')
    plt.ylabel('Cumulative P&L ($)')
    plt.grid(True)
    plt.savefig('reports/equity_curve.png')
```

### Метрики які варто відстежувати:

**Live метрики (real-time):**
- Balance, equity, open positions count
- P&L today / this week / this month
- Trades today / this week
- Current drawdown
- Signal rate (signals per hour)
- WebSocket latency, messages/sec

**Денні метрики (звіт):**
- Win rate
- Average R (середнє R:R)
- Profit factor
- Sharpe ratio (rolling 30 днів)
- Max drawdown (rolling 30 днів)
- Per-signal-type performance (який сигнал найприбутковіший?)

---

## 6. Error Handling

### Принципи:
1. **НІКОЛИ не ковтай помилки** (`except: pass` — заборонено)
2. **Log + Alert + Recover** — кожен catch робить всі три
3. **Критичні помилки → Kill Switch**

```python
import traceback

async def safe_loop(coro_func, name: str, notifier):
    """Wrapper що перезапускає корутину при помилці (з exp backoff)."""
    retry_delay = 1
    while True:
        try:
            await coro_func()
            retry_delay = 1    # reset delay on success
        except asyncio.CancelledError:
            raise
        except Exception as e:
            error_log.error(f"[{name}] crashed: {e}\n{traceback.format_exc()}")
            await notifier.send(
                f"🚨 Task <b>{name}</b> crashed: <code>{e}</code>",
                AlertLevel.CRITICAL
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)    # max 60 сек
```

### Критичні помилки (→ Kill Switch):
- Order rejected 3 рази поспіль
- WebSocket не відновлюється >5 хв
- Balance mismatch (наш внутрішній облік не збігається з біржею)
- Invalid price (NaN, 0, негативне)

---

## 7. Audit Trail (для регуляторних вимог)

Кожна дія бота повинна мати запис **хто / коли / що / чому**:

```json
{
  "timestamp": "2026-04-20T15:34:22.123Z",
  "action": "order_placed",
  "actor": "bot",
  "reason": "signal_triggered:stacked_imbalance",
  "payload": {
    "symbol": "BTCUSDT",
    "side": "BUY",
    "type": "LIMIT",
    "price": 50000,
    "quantity": 0.1
  },
  "exchange_response": {
    "order_id": "12345",
    "status": "NEW"
  },
  "prior_state": {
    "balance": 1000.0,
    "position_count": 0
  }
}
```

Зберігати мінімум **6 місяців**.

---

## Чеклист налаштування моніторингу

- [ ] Structured JSON логи з rotation (100MB × 10)
- [ ] Окремі файли: signals, trades, errors, system
- [ ] Trade journal у JSONL форматі
- [ ] Telegram бот налаштований, тестовий алерт прийшов
- [ ] Heartbeat моніторить WebSocket (30 сек threshold)
- [ ] Exception wrapper на всіх async tasks
- [ ] Daily summary щодня в Telegram
- [ ] Startup / shutdown алерти
- [ ] Kill switch → критичний алерт
- [ ] Equity curve малюється щоденно

---

## Що далі?

Наступний документ: [12-glossary.md](./12-glossary.md) — словник термінів для швидкого довідника.
