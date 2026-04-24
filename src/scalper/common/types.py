"""Типи, які проходять крізь pipeline (TradePlan, InvalidationCondition тощо).

Features живе в `scalper.features.types`, сирі gateway-події — у `scalper.gateway.types`,
Journal event-и — у `scalper.journal.types`. Тут тільки те, що ділять ≥2 модулі.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from scalper.common.enums import Direction, Regime, SetupType

if TYPE_CHECKING:
    from scalper.features.types import Features


class InvalidationKind(str, Enum):
    """Типи правил, за якими PositionManager закриває позицію достроково
    (див. DOCS/architecture/10-position-manager.md).
    """

    PRICE_BEYOND_LEVEL = "price_beyond_level"         # ціна перетнула рівень проти нас
    OPPOSITE_ABSORPTION = "opposite_absorption"       # зустрічне поглинання
    DELTA_FLIP = "delta_flip"                         # delta змінила знак на N секундах
    BOOK_IMBALANCE_FLIP = "book_imbalance_flip"       # bid/ask imbalance перевернулось
    VWAP_REJECTION = "vwap_rejection"


@dataclass(frozen=True)
class InvalidationCondition:
    """Одне правило дострокового виходу. Передається з SetupDetector у TradePlan
    і далі в PositionManager разом із позицією."""

    kind: InvalidationKind
    params: dict[str, float | int | str] = field(default_factory=dict)
    """Специфічні параметри (price, window_ms, threshold, ...)."""


@dataclass(frozen=True)
class SetupCandidate:
    """Результат SetupDetector-а. Ще НЕ торговий план —
    DecisionEngine оцінить його і може відкинути.
    """

    setup_type: SetupType
    direction: Direction
    symbol: str
    timestamp_ms: int

    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    stop_distance_ticks: int

    invalidation_conditions: list[InvalidationCondition]
    features_snapshot: "Features"


@dataclass(frozen=True)
class TradePlan:
    """Proposal від DecisionEngine для RiskEngine.

    RiskEngine дозаповнює `position_size` / `risk_usd` / `risk_gate_passed`
    і передає далі в ExecutionEngine. Якщо risk_gate_passed=False — план reject-нутий.
    """

    candidate: SetupCandidate

    # Копія ключових полів із candidate — для зручного доступу без `.candidate.`
    setup_type: SetupType
    direction: Direction
    symbol: str
    timestamp_ms: int

    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    stop_distance_ticks: int

    score: float
    score_threshold: float
    regime: Regime
    expectancy_multiplier: float

    invalidation_conditions: list[InvalidationCondition]
    time_stop_ms: int | None

    # Заповнює RiskEngine:
    position_size: float | None = None
    risk_usd: float | None = None
    risk_gate_passed: bool = False


@dataclass(frozen=True)
class RejectedCandidate:
    """Кандидат, який не пройшов DecisionEngine. Логуємо в Journal для post-mortem."""

    candidate: SetupCandidate
    reason: str
    score: float | None                        # None якщо reject до скорингу (фільтр)
    score_threshold: float | None


@dataclass(frozen=True)
class DecisionResult:
    accepted: TradePlan | None
    rejected: list[RejectedCandidate]
