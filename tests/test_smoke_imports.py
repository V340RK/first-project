"""Smoke-тест: переконатись, що всі модулі імпортуються без помилок.

Це найдешевший спосіб ловити syntax-errors та circular imports на ранній стадії.
Жодної бізнес-логіки тут не тестуємо.
"""

from __future__ import annotations


def test_common_imports() -> None:
    from scalper.common import (  # noqa: F401
        AlertLevel,
        Direction,
        InvalidationCondition,
        InvalidationKind,
        Regime,
        SetupType,
        TradePlan,
        clock,
        now_ms,
    )


def test_pipeline_module_imports() -> None:
    # Кожен import має вдатися — стаби-методи кидають NotImplementedError, але
    # самі класи мусять бути імпортовані.
    from scalper.book import OrderBookEngine  # noqa: F401
    from scalper.config import AppConfig  # noqa: F401
    from scalper.decision import DecisionEngine  # noqa: F401
    from scalper.execution import ExecutionEngine, OrderRequest  # noqa: F401
    from scalper.expectancy import ExpectancyTracker  # noqa: F401
    from scalper.features import FeatureEngine, Features  # noqa: F401
    from scalper.gateway import ExchangeInfo, MarketDataGateway, SymbolFilters  # noqa: F401
    from scalper.journal import EventKind, JournalLogger  # noqa: F401
    from scalper.notifications import NotificationService  # noqa: F401
    from scalper.orchestrator import Orchestrator  # noqa: F401
    from scalper.position import PositionManager, PositionState  # noqa: F401
    from scalper.regime import MarketRegime, RegimeState  # noqa: F401
    from scalper.replay import ReplayGateway, SimulatedExecutionEngine  # noqa: F401
    from scalper.risk import RiskEngine, TradeOutcome  # noqa: F401
    from scalper.setups import SetupDetector  # noqa: F401
    from scalper.tape import TapeFlowAnalyzer  # noqa: F401


def test_gateway_and_replay_share_api() -> None:
    """ReplayGateway має мати ті ж public-методи, що й MarketDataGateway —
    інакше не можна підмінити одне іншим у Orchestrator-і."""
    from scalper.gateway import MarketDataGateway
    from scalper.replay import ReplayGateway

    public = lambda cls: {n for n in dir(cls) if not n.startswith("_")}
    missing = public(MarketDataGateway) - public(ReplayGateway)
    # Дозволяємо лише ping/get_server_time_offset_ms/get_rate_limit_weight
    # (інфраструктурні, не потрібні в replay).
    allowed_missing = {
        "ping", "get_server_time_offset_ms", "get_rate_limit_weight",
        "set_leverage",   # live-only: replay не шле REST приватних
    }
    assert missing <= allowed_missing, f"ReplayGateway втратив API: {missing - allowed_missing}"
