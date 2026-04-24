"""12 Replay / Simulator — historical backtest без зміни pipeline-коду.

ReplayGateway підмінює MarketDataGateway, SimulatedExecutionEngine — Execution.
Див. DOCS/architecture/12-replay-simulator.md.
"""

from scalper.replay.gateway import ReplayGateway
from scalper.replay.simulator import (
    FillPolicy,
    SimulatedExecutionEngine,
    SimulatorConfig,
    SlippageModel,
)

__all__ = [
    "FillPolicy", "ReplayGateway", "SimulatedExecutionEngine",
    "SimulatorConfig", "SlippageModel",
]
