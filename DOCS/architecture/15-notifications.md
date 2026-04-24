---
name: 15-notifications
description: Архітектура NotificationService — єдина точка вихідних алертів (Telegram/email/stdout); fire-and-forget, rate-limit, не блокує hot loop
type: project
---

# 15. NotificationService — сповіщення

## Відповідальність

**Єдина точка виходу для алертів.** Жоден бізнес-модуль не шле повідомлення напряму в Telegram/email/stdout — усе через цей сервіс. Це дає:

- єдину політику rate-limit (щоб при каскаді kill-switch подій не DDoS-нути бот сам себе);
- централізований фільтр по `AlertLevel` (наприклад, у production INFO → stdout, WARNING+ → Telegram);
- можливість тестувати решту коду з заглушкою, яка не шле нічого.

### Що робить (за специфікацією):

- `send(text, level)` — fire-and-forget, повертається миттєво, реальна доставка асинхронна.
- Rate-limit по каналу (`rate_limit_per_min` у конфізі).
- Async queue всередині — `send()` не блокує hot loop.
- Помилка доставки → лог + `WARNING`-подія у Journal (через [14-orchestrator.md](./14-orchestrator.md) або прямо з notifier).

### Що НЕ робить:

- Не приймає рішень — тільки шле.
- Не формулює тексти — це відповідальність викликаючого модуля.
- Не гарантує доставку — Telegram/мережа можуть впасти, сервіс drop-ить з лічильником.

---

## Входи / виходи

### Публічний API (специфікація — зараз у коді скелет):

```python
from scalper.common.enums import AlertLevel

class NotificationService:
    def __init__(self, config: NotificationConfig) -> None: ...

    # === Lifecycle ===
    async def start(self) -> None     # підняти queue + background worker
    async def stop(self) -> None      # drain + flush

    # === API для всіх модулів ===
    async def send(self, text: str, level: AlertLevel) -> None
```

### Рівні сповіщень ([common/enums.py:46](../../src/scalper/common/enums.py#L46)):

```python
class AlertLevel(str, Enum):
    INFO = "info"             # позиція відкрита, TP1 зафіксовано
    WARNING = "warning"       # reconnect, retry, degrade (без блокування торгівлі)
    ERROR = "error"           # reject order, часткова поломка (без kill)
    CRITICAL = "critical"     # kill switch, інваріант порушено, треба ручне втручання
```

---

## Залежності

**Читає:**
- `NotificationConfig` — `channels: [stdout, telegram]`, `telegram.chat_id`, `telegram.rate_limit_per_min`.
- Secrets (Telegram bot token) — **окремо з `configs/secrets.yaml`**, не з `settings.yaml`.

**Хто викликає:**

| Модуль | Коли |
|---|---|
| RiskEngine | kill switch engaged/cleared, daily loss hit |
| ExecutionEngine | reject order, retry exhausted, post-only rejected N разів |
| PositionManager | STOP_MOVED до BE, TP3 hit, invalidation triggered |
| MarketRegime | перехід у DISABLED (manual pause) |
| ExpectancyTracker | setup×symbol SUSPENDED / RESUMED |
| Orchestrator | STARTUP / SHUTDOWN / reconnect storm |

**Не викликає інших модулів.** Це листок у графі залежностей.

---

## Стан

```python
@dataclass
class _NotifierState:
    queue: asyncio.Queue[_Msg]           # pending повідомлення
    worker_task: asyncio.Task | None
    last_send_per_channel: dict[str, int]     # ms timestamp останнього send
    dropped_count: dict[AlertLevel, int]      # метрики
    shutdown: asyncio.Event
```

Персистентність: **немає**. Черга in-memory, при crash'і втрачаємо pending. Це свідоме рішення — алерти цінні саме свіжими, stale-алерти через 10 хв шкідливі.

---

## Ключові алгоритми

### 1. `send` — non-blocking put

```python
async def send(self, text: str, level: AlertLevel) -> None:
    msg = _Msg(ts=clock(), text=text, level=level)
    try:
        self._queue.put_nowait(msg)
    except asyncio.QueueFull:
        self._dropped_count[level] += 1
        logger.error(f"notifier queue full, dropped: level={level.value}")
```

### 2. Worker loop

```python
async def _worker(self):
    while not self._shutdown.is_set():
        msg = await self._queue.get()
        for ch in self._config.channels:
            if not self._rate_limit_ok(ch):
                continue
            try:
                await self._send_to_channel(ch, msg)
                self._last_send_per_channel[ch] = clock()
            except Exception as e:
                logger.warning(f"notifier channel {ch} failed: {e}")
                # журналюємо як WARNING — для аудиту
                # (через callback до Journal, або прямий лог)
```

### 3. Rate-limit (sliding window per channel)

```python
def _rate_limit_ok(self, channel: str) -> bool:
    limit_per_min = self._config.rate_limit[channel]
    window_start = clock() - 60_000
    sent_in_window = sum(1 for ts in self._sent_history[channel] if ts > window_start)
    return sent_in_window < limit_per_min
```

Історія зберігається як `collections.deque` з maxlen = limit_per_min.

### 4. Canali

- **stdout** — завжди працює (fallback; тримаємо як sanity-output коли Telegram лежить).
- **telegram** — через Bot API (`aiohttp`-запит до `https://api.telegram.org/bot<TOKEN>/sendMessage`).
- **email** — опціонально, low-priority (через SMTP; по замовчуванню вимкнено).

---

## Edge cases

| Ситуація | Поведінка |
|---|---|
| Telegram API 429 (rate limit) | exponential backoff (1s → 5s → 30s), drop якщо >3 поспіль |
| Token невалідний | один лог ERROR при старті, далі канал вимикається (щоб не спамити логи) |
| Queue переповнена | `put_nowait` кидає QueueFull → `dropped_count++`, message губиться |
| Shutdown під час активної відправки | `stop()` drain'ить queue з таймаутом 5 с; далі forcefully cancel |
| Секрети в логу | **Ніколи не логуємо text** повідомлення (може містити PII/account-info); логуємо тільки `level` і counter |
| CRITICAL spam (каскад 20 kill-ів за секунду) | rate-limit дозволить 1–2 першими, решта в dropped; це ОК — перший вже пояснює суть |

---

## Конфіги

З [configs/settings.example.yaml:122](../../configs/settings.example.yaml#L122):

```yaml
notifications:
  channels: [stdout, telegram]
  telegram:
    chat_id: 0
    rate_limit_per_min: 20
```

Token (`telegram_token`) — **у `configs/secrets.yaml`**, який у `.gitignore`.

---

## Як тестувати

### Unit
- `send()` non-blocking навіть при 10000 викликах підряд.
- Queue overflow: заповнити N+1 → dropped_count == 1.
- Rate-limit: 25 send за 1 хв при limit=20 → 5 пропущено (fake clock).
- Channel failure: stdout завжди працює, telegram raise → stdout все одно отримує.
- Секрет не потрапляє в лог: `caplog.text` не містить токен.

### Integration (manual або з real Telegram test-бота)
- Запустити, надіслати по 1 INFO/WARNING/ERROR/CRITICAL → отримати в тестовому чаті.
- Durng `stop()`: queue з 10 pending → всі drain'уються протягом 5 с.

---

## Поточний статус імплементації

**Сервіс — скелет.** Файл [src/scalper/notifications/service.py](../../src/scalper/notifications/service.py) містить клас з сигнатурами, але всі методи піднімають `NotImplementedError`.

Для тестів Orchestrator підставляється fake-об'єкт. Entry point (крок 3) має:
1. Або реалізувати повноцінний сервіс за специфікацією вище.
2. Або тимчасово підставити мінімальну імплементацію: `stdout-only`, без rate-limit, sync `print` (ОК для MVP у replay/paper-режимі, не ОК для live).

Рекомендація: MVP — stdout-only (30 рядків коду), повна версія з Telegram — в окремому PR.
