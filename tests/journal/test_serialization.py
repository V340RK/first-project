"""Round-trip JournalEvent ↔ dict ↔ JSONL."""

from __future__ import annotations

from scalper.journal.serialization import (
    event_from_dict,
    event_from_jsonl,
    event_to_dict,
    event_to_jsonl,
)
from scalper.journal.types import EventKind, JournalEvent


def _sample_event() -> JournalEvent:
    return JournalEvent(
        seq=42,
        timestamp_ms=1700000000123,
        kind=EventKind.DECISION_ACCEPTED,
        trade_id="t-abc",
        symbol="BTCUSDT",
        payload={"score": 1.47, "direction": "LONG", "factors": ["absorption", "imbalance"]},
        schema_version=1,
    )


def test_round_trip_dict() -> None:
    ev = _sample_event()
    restored = event_from_dict(event_to_dict(ev))
    assert restored == ev


def test_round_trip_jsonl() -> None:
    ev = _sample_event()
    line = event_to_jsonl(ev)
    assert line.endswith("\n")
    restored = event_from_jsonl(line)
    assert restored == ev


def test_dict_uses_string_kind() -> None:
    """EventKind серіалізується як рядок (для читабельності файлу)."""
    d = event_to_dict(_sample_event())
    assert d["kind"] == "decision_accepted"


def test_unicode_in_payload_survives() -> None:
    ev = JournalEvent(
        seq=1, timestamp_ms=1, kind=EventKind.WARNING, trade_id=None, symbol=None,
        payload={"msg": "WS reconnect — таймаут пінгу"},
    )
    restored = event_from_jsonl(event_to_jsonl(ev))
    assert restored.payload["msg"] == "WS reconnect — таймаут пінгу"
