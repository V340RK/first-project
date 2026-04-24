"""Orchestrator — склеює всі модулі у runtime-граф.

Єдиний компонент, який знає про всіх інших. Будує MarketSnapshot, веде
hot loop (on-tick) і slow loop (regime/expectancy/heartbeat).
Див. DOCS/architecture/00-overview.md, секція 'Orchestrator / Pipeline'.
"""

from scalper.orchestrator.orchestrator import Orchestrator

__all__ = ["Orchestrator"]
