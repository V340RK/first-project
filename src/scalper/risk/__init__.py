"""08 Risk Engine — sizing, ліміти, kill switch, loss streak.

Див. DOCS/architecture/08-risk-engine.md.
"""

from scalper.risk.config import RiskConfig
from scalper.risk.engine import RiskDecision, RiskEngine, RiskSnapshot, TradeOutcome

__all__ = ["RiskConfig", "RiskDecision", "RiskEngine", "RiskSnapshot", "TradeOutcome"]
