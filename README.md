# Scalper

Автоматичний скальпер для Binance USDT-M Futures. Order-flow + footprint аналіз,
ризик-контроль, replay-бектест, журнал угод.

## Документація

Уся архітектура — у [DOCS/architecture/](DOCS/architecture/).
Почати з [00-overview.md](DOCS/architecture/00-overview.md).

## Структура коду

```
src/scalper/
├── common/         # shared types (Regime, SetupType, InvalidationCondition, ...)
├── config/         # завантаження settings.yaml
├── gateway/        # 01 Market Data Gateway (WS + REST до Binance)
├── book/           # 02 Order Book Engine
├── tape/           # 03 Tape / Flow Analyzer
├── features/       # 04 Feature Engine
├── regime/         # 05 Market Regime Classifier
├── setups/         # 06 Setup Detector (pure stateless)
├── decision/       # 07 Decision Engine
├── risk/           # 08 Risk Engine
├── execution/      # 09 Execution Engine
├── position/       # 10 Position Manager
├── journal/        # 11 Journal Logger
├── replay/         # 12 Replay / Simulator
├── expectancy/     # 13 Expectancy Tracker
├── notifications/  # NotificationService (Telegram/email)
└── orchestrator/   # склеює все разом
```

## Розробка

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e ".[dev]"

pytest                            # тести
ruff check src/ tests/            # лінтер
mypy src/                         # типи
```
