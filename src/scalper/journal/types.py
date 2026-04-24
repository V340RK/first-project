"""EventKind enum + JournalEvent + TradeRecord.

Узгоджений з DOCS/architecture/11-journal-logger.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EventKind(str, Enum):
    # === Decision layer ===
    SETUP_CANDIDATE_GENERATED = "setup_candidate"     # SetupDetector знайшов (до decision)
    DECISION_ACCEPTED = "decision_accepted"
    DECISION_REJECTED = "decision_rejected"
    RISK_ACCEPTED = "risk_accepted"
    RISK_REJECTED = "risk_rejected"

    # === Execution layer ===
    ORDER_REQUESTED = "order_requested"
    ORDER_RESPONSE = "order_response"
    ORDER_UPDATE = "order_update"
    FILL = "fill"
    ORDER_CANCELLED = "order_cancelled"

    # === Position layer ===
    POSITION_OPENED = "position_opened"
    STOP_MOVED = "stop_moved"
    INVALIDATION_TRIGGERED = "invalidation_triggered"
    TIME_STOP_TRIGGERED = "time_stop_triggered"
    POSITION_CLOSED = "position_closed"
    TRADE_OUTCOME = "trade_outcome"

    # === Regime / risk ===
    REGIME_CHANGED = "regime_changed"
    KILL_SWITCH_ENGAGED = "kill_switch_engaged"
    KILL_SWITCH_CLEARED = "kill_switch_cleared"
    DAILY_ROLLOVER = "daily_rollover"

    # === Expectancy ===
    EXPECTANCY_SUSPENDED = "expectancy_suspended"
    EXPECTANCY_RESUMED = "expectancy_resumed"

    # === Infra ===
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    ERROR = "error"
    WARNING = "warning"
    HEARTBEAT = "heartbeat"


@dataclass(frozen=True)
class JournalEvent:
    seq: int                                # заповнює writer
    timestamp_ms: int
    kind: EventKind
    trade_id: str | None
    symbol: str | None
    payload: dict[str, object]
    schema_version: int = 1


@dataclass
class TradeRecord:
    """Агрегат усіх подій однієї угоди — будується on-demand при query."""

    trade_id: str
    symbol: str
    opened_at_ms: int | None
    closed_at_ms: int | None
    setup_type: str | None
    direction: str | None
    realized_r: float | None
    events: list[JournalEvent]
