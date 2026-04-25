"""Enum-и, які використовуються >1 модулем.

Один центральний модуль enum-ів гарантує, що, наприклад, `Regime.HIGH_VOL`
має однаковий рядковий literal в MarketRegime, DecisionEngine, Journal і Replay.
"""

from __future__ import annotations

from enum import Enum


class Direction(str, Enum):
    """Напрям угоди. Значення — рядки для зручного JSON-серіалайзу в Journal."""

    LONG = "LONG"
    SHORT = "SHORT"


class Regime(str, Enum):
    """Режим ринку (див. DOCS/architecture/05-market-regime.md)."""

    NORMAL_BALANCED = "normal_balanced"
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    CHOPPY = "choppy"
    HIGH_VOL = "high_vol"          # підвищена волатильність (ATR spike)
    LOW_LIQ = "low_liq"            # широкий spread / тонкий стакан
    NEWS_RISK = "news_risk"        # перед/під час релізів
    DISABLED = "disabled"          # kill switch / manual pause


class SetupType(str, Enum):
    """Типи сетапів, які вміє детектити SetupDetector.

    Перелік розширюємо в міру додавання нових сетапів у [DOCS/architecture/06-setup-detector.md].
    Значення — стабільні snake_case-рядки для Journal/ExpectancyTracker.
    """

    ABSORPTION_REVERSAL = "absorption_reversal"
    STACKED_IMBALANCE = "stacked_imbalance"
    DELTA_SPIKE_REJECTION = "delta_spike_rejection"
    MICRO_PULLBACK_CONTINUATION = "micro_pullback_continuation"
    LIQUIDITY_GRAB = "liquidity_grab"
    MOMENTUM_BREAKOUT = "momentum_breakout"


class AlertLevel(str, Enum):
    """Рівень сповіщення NotificationService."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
