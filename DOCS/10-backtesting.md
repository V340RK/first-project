# Backtesting — Тестування стратегії на історії

## Навіщо бектест?

Перед тим як пустити бота з реальними грошима, потрібно перевірити:
1. **Прибутковість стратегії** — чи заробляє вона взагалі?
2. **Drawdown** — який максимальний збиток (просадка)?
3. **Win rate** — % прибуткових угод
4. **R:R** — середнє співвідношення ризик/прибуток
5. **Статистична значущість** — скільки угод було? (мінімум 100, краще 500+)

**⚠️ Правило**: стратегія яка не виживає на історії — НЕ виживе на реальних грошах.

---

## Три рівні тестування

### 1. Paper Trading (віртуальні гроші на live-ринку)
- Бот працює в реальному часі, але НЕ відправляє ордери на біржу
- Просто логуємо "тут би ми відкрили угоду"
- **Плюс**: реальні ринкові умови
- **Мінус**: повільно (один день = 24 години очікування)

### 2. Historical Backtest (event-driven)
- Завантажуємо історичні дані (tick-by-tick або свічки)
- Програємо їх бота як у прискореному відео
- **Плюс**: швидко (місяць історії за кілька хвилин)
- **Мінус**: треба правильно змоделювати ринок

### 3. Walk-Forward Testing
- Розбиваємо історію на шматки: train → test → train → test
- Оптимізуємо параметри на train, перевіряємо на test
- **Плюс**: захист від overfitting (підгонки)
- **Мінус**: складніше в реалізації

---

## Дані для бектесту

### Джерела історії:

| Джерело | Що дає | Формат | Ціна |
|---|---|---|---|
| **Binance API** | Kline + aggTrade | JSON | Безкоштовно |
| **Binance Data** ([data.binance.vision](https://data.binance.vision)) | Повна історія по днях | CSV | Безкоштовно |
| **Tardis.dev** | Full order book + trades | CSV/JSON | $59+/міс |
| **CryptoDataDownload** | OHLCV | CSV | Безкоштовно |

### Рекомендація:
- Для початку: **Binance Data** (безкоштовно, aggTrade + kline)
- Пізніше якщо треба повний DOM: **Tardis.dev**

### Приклад завантаження aggTrade з Binance Data:
```python
# URL: https://data.binance.vision/data/futures/um/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2024-12-01.zip
import requests
import zipfile
import pandas as pd

def download_agg_trades(symbol: str, date: str) -> pd.DataFrame:
    url = f"https://data.binance.vision/data/futures/um/daily/aggTrades/{symbol}/{symbol}-aggTrades-{date}.zip"
    resp = requests.get(url)
    with open(f"{symbol}-{date}.zip", 'wb') as f:
        f.write(resp.content)

    with zipfile.ZipFile(f"{symbol}-{date}.zip") as z:
        with z.open(z.namelist()[0]) as f:
            df = pd.read_csv(f, names=[
                'agg_id', 'price', 'quantity', 'first_id', 'last_id',
                'timestamp', 'is_buyer_maker'
            ])
    return df

# Завантажити останні 30 днів:
from datetime import datetime, timedelta
dates = [(datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(1, 31)]
for date in dates:
    download_agg_trades('BTCUSDT', date)
```

---

## Архітектура Backtest Engine

```
┌─────────────────────────────────────────────┐
│      HistoricalDataLoader                   │
│   (читає CSV/JSON з диску)                  │
└──────────────┬──────────────────────────────┘
               │ yield events
               ▼
┌─────────────────────────────────────────────┐
│         EventLoop                           │
│   for event in sorted_events:               │
│       process(event)                        │
└──────────────┬──────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────┐
│        BotSimulator                         │
│   (той самий код що і в live!)              │
│   • DataEngine.process_trade()              │
│   • SignalEngine.check_signals()            │
│   • RiskManager.check_allowed()             │
└──────────────┬──────────────────────────────┘
               │ orders
               ▼
┌─────────────────────────────────────────────┐
│       SimulatedBroker                       │
│   (імітує виконання ордерів)                │
│   • fill з slippage                         │
│   • comission 0.04% per side                │
│   • stop-loss triggering                    │
└──────────────┬──────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────┐
│       TradeRecorder                         │
│   (зберігає кожну угоду в journal)         │
└─────────────────────────────────────────────┘
```

### Ключова ідея:
**Код бота ТОЙ САМИЙ** що в live-режимі. Різниця тільки в джерелі даних і в "брокері" (симулятор замість реальної біржі).

---

## Реалізація: SimulatedBroker

```python
class SimulatedBroker:
    def __init__(self, initial_balance: float, commission: float = 0.0004):
        self.balance = initial_balance
        self.commission = commission        # 0.04% per side (maker на Binance Futures)
        self.position = None
        self.trades = []

    def place_order(self, symbol, side, order_type, quantity, price=None, stop_price=None):
        """
        Імітує виконання ордера. У реальності треба враховувати:
        - MARKET ордер виконується за best_ask (BUY) або best_bid (SELL) з slippage
        - LIMIT ордер чекає поки ціна торкнеться (не завжди виконується)
        - STOP триггериться коли ціна перетинає stop_price
        """
        current_price = self.get_current_price(symbol)

        if order_type == 'MARKET':
            # Slippage: додаємо 0.02% до ціни (імітуємо проскальзування)
            slippage = 0.0002
            fill_price = current_price * (1 + slippage) if side == 'BUY' else current_price * (1 - slippage)
            self._execute_fill(symbol, side, quantity, fill_price)

        elif order_type == 'LIMIT':
            # Додаємо до "черги" — виконається коли ціна торкнеться
            self.pending_orders.append({
                'symbol': symbol, 'side': side, 'quantity': quantity, 'price': price
            })

        elif order_type == 'STOP_MARKET':
            # Спрацює коли mark_price досягне stop_price
            self.pending_stops.append({
                'symbol': symbol, 'side': side, 'quantity': quantity, 'stop_price': stop_price
            })

    def _execute_fill(self, symbol, side, quantity, fill_price):
        fee = fill_price * quantity * self.commission
        self.balance -= fee

        if self.position is None:
            # Відкриваємо нову позицію
            self.position = {
                'symbol': symbol, 'side': side, 'quantity': quantity,
                'entry_price': fill_price, 'entry_time': self.current_time
            }
        else:
            # Закриваємо позицію
            pnl = self._calculate_pnl(self.position, fill_price)
            self.balance += pnl - fee
            self.trades.append({
                'entry_time': self.position['entry_time'],
                'exit_time': self.current_time,
                'side': self.position['side'],
                'entry_price': self.position['entry_price'],
                'exit_price': fill_price,
                'quantity': self.position['quantity'],
                'pnl': pnl - 2 * fee,     # комісія двічі (вхід + вихід)
                'balance_after': self.balance
            })
            self.position = None

    def on_price_update(self, symbol, high, low, close):
        """Викликається на кожній новій свічці — перевіряє спрацювання стопів."""
        # Перевірка STOP ордерів
        for stop in list(self.pending_stops):
            if stop['side'] == 'SELL' and low <= stop['stop_price']:
                self._execute_fill(symbol, 'SELL', stop['quantity'], stop['stop_price'])
                self.pending_stops.remove(stop)
            elif stop['side'] == 'BUY' and high >= stop['stop_price']:
                self._execute_fill(symbol, 'BUY', stop['quantity'], stop['stop_price'])
                self.pending_stops.remove(stop)
```

---

## Транзакційні витрати

**Найбільша помилка нубів**: не враховувати комісію і slippage.

### Binance Futures:
- **Maker fee**: 0.02% (той хто ставить LIMIT ордер)
- **Taker fee**: 0.04% (той хто бере MARKET ордер)
- **Slippage**: 0.01-0.05% для ліквідних пар, більше для алтів
- **Funding**: 0.01% кожні 8 годин (якщо позиція відкрита)

### Приклад розрахунку:
```
Угода: BTCUSDT, LONG, 0.1 BTC at $50,000
Ціль: +0.3% ($150 profit)

Комісії:
- Вхід (maker): 50000 × 0.1 × 0.0002 = $1.00
- Вихід (taker): 50150 × 0.1 × 0.0004 = $2.01
- Slippage:     50000 × 0.1 × 0.0002 = $1.00

Чистий прибуток: $150 - $4.01 = $145.99 (-2.7% від gross)
```

**Висновок**: на коротких таргетах (<0.3%) комісії "з'їдають" значну частину прибутку. Треба або збільшувати ціль, або зменшувати частоту.

---

## Метрики для оцінки стратегії

```python
import numpy as np
import pandas as pd

def analyze_backtest(trades: list[dict], initial_balance: float):
    """
    trades = [{'pnl': ..., 'entry_time': ..., 'exit_time': ...}, ...]
    """
    df = pd.DataFrame(trades)
    if df.empty:
        return None

    # Базові метрики
    total_trades = len(df)
    wins = df[df['pnl'] > 0]
    losses = df[df['pnl'] < 0]
    win_rate = len(wins) / total_trades
    avg_win = wins['pnl'].mean() if len(wins) > 0 else 0
    avg_loss = losses['pnl'].mean() if len(losses) > 0 else 0

    total_pnl = df['pnl'].sum()
    total_return = total_pnl / initial_balance

    # Profit Factor = сума виграшів / сума програшів
    profit_factor = abs(wins['pnl'].sum() / losses['pnl'].sum()) if len(losses) > 0 else float('inf')

    # Expectancy (очікуваний виграш з однієї угоди)
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    # Max Drawdown
    df['cumulative'] = df['pnl'].cumsum() + initial_balance
    df['peak'] = df['cumulative'].cummax()
    df['drawdown'] = (df['cumulative'] - df['peak']) / df['peak']
    max_drawdown = df['drawdown'].min()

    # Sharpe Ratio (щоденні поверни)
    daily = df.set_index('exit_time').resample('D')['pnl'].sum()
    if daily.std() > 0:
        sharpe = (daily.mean() / daily.std()) * np.sqrt(365)
    else:
        sharpe = 0

    # Sortino (тільки негативна волатильність)
    downside = daily[daily < 0]
    if len(downside) > 0 and downside.std() > 0:
        sortino = (daily.mean() / downside.std()) * np.sqrt(365)
    else:
        sortino = 0

    return {
        'total_trades': total_trades,
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'expectancy': expectancy,
        'total_return_pct': total_return * 100,
        'max_drawdown_pct': max_drawdown * 100,
        'sharpe_ratio': sharpe,
        'sortino_ratio': sortino
    }
```

### Інтерпретація метрик:

| Метрика | Погано | OK | Добре | Відмінно |
|---|---|---|---|---|
| **Win rate** | <40% | 40-50% | 50-60% | >60% |
| **Profit Factor** | <1.2 | 1.2-1.5 | 1.5-2.0 | >2.0 |
| **Max DD** | >30% | 20-30% | 10-20% | <10% |
| **Sharpe** | <0.5 | 0.5-1.0 | 1.0-2.0 | >2.0 |
| **Sortino** | <1.0 | 1.0-1.5 | 1.5-3.0 | >3.0 |

**Критичне правило**: стратегія з Win rate 70% але Profit Factor 1.1 — ГІРША ніж 45% Win rate з Profit Factor 2.0.

---

## MAE / MFE аналіз

**MAE** (Maximum Adverse Excursion) — максимальна ціна проти позиції до моменту її закриття.
**MFE** (Maximum Favorable Excursion) — максимальна ціна за позицію.

### Навіщо?
- Якщо MFE >> TP у багатьох угодах → ми занадто рано виходимо
- Якщо MAE ≈ SL у багатьох прибуткових угодах → стопи занадто близько (багато "торкань")

### Реалізація:
```python
def calculate_mae_mfe(position, candles_during_trade):
    entry = position['entry_price']
    side = position['side']

    if side == 'BUY':
        max_adverse = min(c['low'] for c in candles_during_trade)
        max_favorable = max(c['high'] for c in candles_during_trade)
        mae_pct = (max_adverse - entry) / entry * 100   # від'ємне
        mfe_pct = (max_favorable - entry) / entry * 100 # додатне
    else:  # SELL
        max_adverse = max(c['high'] for c in candles_during_trade)
        max_favorable = min(c['low'] for c in candles_during_trade)
        mae_pct = (entry - max_adverse) / entry * 100   # від'ємне
        mfe_pct = (entry - max_favorable) / entry * 100 # додатне

    return mae_pct, mfe_pct
```

---

## Walk-Forward Optimization

**Проблема**: якщо оптимізуємо параметри на всій історії — отримуємо **overfitting** (стратегія ідеально підходить до минулого, але не працює в майбутньому).

**Рішення**: Walk-Forward:
```
Дані:  [===== 2024 =====][== 2025 ==]

Ітерація 1:
  Train: Jan-Mar 2024 → знайти найкращі параметри
  Test:  Apr 2024 → перевірити на НЕБАЧЕНИХ даних

Ітерація 2:
  Train: Feb-Apr 2024 → знайти параметри
  Test:  May 2024

... і так далі (зсуваємо вікно)

Результат: метрики тільки з Test-періодів, НЕ з Train.
```

### Простий Grid Search:
```python
from itertools import product

# Діапазони параметрів для оптимізації
param_grid = {
    'imbalance_ratio': [1.5, 2.0, 2.5, 3.0],
    'delta_threshold': [1.5, 2.0, 2.5],
    'min_confluence_score': [0.5, 0.6, 0.7, 0.8]
}

results = []
for combo in product(*param_grid.values()):
    params = dict(zip(param_grid.keys(), combo))
    metrics = run_backtest(params, data_train)
    if metrics['total_trades'] >= 30:  # мінімум угод для статзначущості
        results.append({**params, **metrics})

# Вибрати параметри з найбільшим Sharpe або найвищою Expectancy
best = max(results, key=lambda r: r['sharpe_ratio'])
```

---

## Overfitting — головний ворог

### Ознаки що ти переоптимізувався:
1. **Backtest результати неправдоподібно добрі** (Sharpe > 4, Win rate > 75%)
2. **Занадто багато параметрів** (>10 окремих тюнінгованих значень)
3. **Чутливість до параметрів**: змінив 2.0 на 2.1 і все розвалилось
4. **Різниця train vs test >30%**

### Як зменшити:
- Обмежити кількість параметрів (<7)
- Out-of-sample тест (ніколи не торкайся тестових даних під час розробки)
- Monte Carlo simulation (переставляємо угоди у випадковому порядку — якщо метрики сильно змінюються → overfit)

---

## Monte Carlo симуляція

```python
import random

def monte_carlo(trades: list, initial_balance: float, n_simulations: int = 1000):
    """Перевіряє розподіл можливих equity curves."""
    results = []
    for _ in range(n_simulations):
        shuffled = random.sample(trades, len(trades))
        balance = initial_balance
        min_balance = balance
        for trade in shuffled:
            balance += trade['pnl']
            min_balance = min(min_balance, balance)
        max_dd = (initial_balance - min_balance) / initial_balance
        results.append({'final_balance': balance, 'max_dd': max_dd})

    # Показуємо ймовірність великого просідання
    dds = [r['max_dd'] for r in results]
    return {
        'median_dd': np.median(dds),
        'percentile_95_dd': np.percentile(dds, 95),  # з 95% ймовірністю DD не перевищить
        'worst_case_dd': max(dds)
    }
```

Якщо **95-й percentile DD > твого ризик-ліміту** — стратегія небезпечна.

---

## Чеклист перед live-deployment

- [ ] Backtest на ≥6 місяців даних
- [ ] Мінімум 100 угод у вибірці
- [ ] Win rate + Profit Factor відповідають мінімумам
- [ ] Max DD < 20%
- [ ] Walk-Forward test показав стабільність параметрів
- [ ] Monte Carlo 95-percentile DD < твого ліміту
- [ ] Транзакційні витрати враховані реалістично
- [ ] Paper trading 2+ тижні показав схожі результати з бектестом
- [ ] Перевірено різні ринкові режими (trend, range, high vol, low vol)

---

## Що далі?

Наступний документ: [11-logging-monitoring.md](./11-logging-monitoring.md) — як слідкувати за ботом коли він працює наживо.
