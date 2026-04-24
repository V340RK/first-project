"""10 Position Manager — life-cycle відкритої позиції, TP-ladder, invalidation, time stop.

Див. DOCS/architecture/10-position-manager.md.
"""

from scalper.position.config import PositionConfig
from scalper.position.manager import OpenPosition, PositionManager, PositionState

__all__ = ["OpenPosition", "PositionConfig", "PositionManager", "PositionState"]
