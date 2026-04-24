"""JSON-сериалізація JournalEvent ↔ dict ↔ JSONL-рядок.

Окремо від logger.py — щоб тести могли робити round-trip без файлових операцій.
"""

from __future__ import annotations

import json
from typing import Any

from scalper.journal.types import EventKind, JournalEvent


def event_to_dict(event: JournalEvent) -> dict[str, Any]:
    """JournalEvent → JSON-сумісний dict.

    EventKind серіалізується як рядок (`.value`). payload передаємо as-is —
    caller відповідальний, щоб у ньому були тільки JSON-сумісні типи.
    """
    return {
        "seq": event.seq,
        "timestamp_ms": event.timestamp_ms,
        "kind": event.kind.value,
        "trade_id": event.trade_id,
        "symbol": event.symbol,
        "payload": event.payload,
        "schema_version": event.schema_version,
    }


def event_from_dict(data: dict[str, Any]) -> JournalEvent:
    """dict → JournalEvent. Невідомий kind → ValueError."""
    return JournalEvent(
        seq=int(data["seq"]),
        timestamp_ms=int(data["timestamp_ms"]),
        kind=EventKind(data["kind"]),
        trade_id=data.get("trade_id"),
        symbol=data.get("symbol"),
        payload=dict(data.get("payload", {})),
        schema_version=int(data.get("schema_version", 1)),
    )


def event_to_jsonl(event: JournalEvent) -> str:
    """Один JSONL-рядок (з `\\n` в кінці), готовий до запису у файл."""
    return json.dumps(event_to_dict(event), separators=(",", ":"), ensure_ascii=False) + "\n"


def event_from_jsonl(line: str) -> JournalEvent:
    """Один JSONL-рядок → JournalEvent. Перетин рядків / порожні рядки caller відкидає."""
    return event_from_dict(json.loads(line))
