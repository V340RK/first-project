"""SessionStats — агрегація подій у snapshot."""

from __future__ import annotations

from pathlib import Path

import pytest

from scalper.dashboard.stats import SessionStats
from scalper.dashboard.tailer import JournalTailer


def _write_journal(path: Path, events: list[dict]) -> None:
    import json
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


@pytest.mark.asyncio
async def test_counts_trades_and_pnl_from_backfill(tmp_path: Path) -> None:
    from datetime import date
    jfile = tmp_path / f"{date.today().isoformat()}.jsonl"
    events = [
        {"seq": 1, "timestamp_ms": 1000, "kind": "startup",
         "payload": {"symbols": ["BTCUSDT"]}},
        {"seq": 2, "timestamp_ms": 2000, "kind": "position_opened",
         "symbol": "BTCUSDT", "payload": {}},
        {"seq": 3, "timestamp_ms": 3000, "kind": "position_closed",
         "symbol": "BTCUSDT", "payload": {}},
        {"seq": 4, "timestamp_ms": 3001, "kind": "trade_outcome",
         "symbol": "BTCUSDT", "payload": {"realized_r": 1.2, "realized_usd": 1.20}},
        {"seq": 5, "timestamp_ms": 4000, "kind": "position_opened",
         "symbol": "BTCUSDT", "payload": {}},
        {"seq": 6, "timestamp_ms": 5000, "kind": "position_closed",
         "symbol": "BTCUSDT", "payload": {}},
        {"seq": 7, "timestamp_ms": 5001, "kind": "trade_outcome",
         "symbol": "BTCUSDT", "payload": {"realized_r": -0.5, "realized_usd": -0.50}},
    ]
    _write_journal(jfile, events)

    tailer = JournalTailer(journal_dir=tmp_path, poll_interval_ms=50)
    await tailer.start()
    try:
        stats = SessionStats(tailer)
        await stats.start()
        snap = stats.snapshot()
        assert snap.session_started_ms == 1000
        assert snap.trades_closed == 2
        assert snap.open_positions == 0
        assert snap.realized_r == pytest.approx(0.7)
        assert snap.realized_usd == pytest.approx(0.70)
        assert snap.last_event_ms == 5001
        await stats.stop()
    finally:
        await tailer.stop()


@pytest.mark.asyncio
async def test_reset_clears_counters(tmp_path: Path) -> None:
    tailer = JournalTailer(journal_dir=tmp_path, poll_interval_ms=50)
    await tailer.start()
    try:
        stats = SessionStats(tailer)
        stats._trades_closed = 10   # type: ignore[attr-defined]
        stats._realized_r = 3.5     # type: ignore[attr-defined]
        stats.reset()
        snap = stats.snapshot()
        assert snap.trades_closed == 0
        assert snap.realized_r == 0.0
    finally:
        await tailer.stop()


@pytest.mark.asyncio
async def test_startup_event_resets_counters_for_new_session(tmp_path: Path) -> None:
    """Новий startup event = нова сесія → counters обнуляються."""
    from datetime import date
    jfile = tmp_path / f"{date.today().isoformat()}.jsonl"
    _write_journal(jfile, [
        {"seq": 1, "timestamp_ms": 1000, "kind": "startup",
         "payload": {"symbols": ["BTCUSDT"]}},
        {"seq": 2, "timestamp_ms": 2000, "kind": "trade_outcome",
         "payload": {"realized_r": 2.0}},
        # Новий старт — скидає сесію
        {"seq": 3, "timestamp_ms": 10_000, "kind": "startup",
         "payload": {"symbols": ["ETHUSDT"]}},
        {"seq": 4, "timestamp_ms": 11_000, "kind": "trade_outcome",
         "payload": {"realized_r": 0.5}},
    ])
    tailer = JournalTailer(journal_dir=tmp_path, poll_interval_ms=50)
    await tailer.start()
    try:
        stats = SessionStats(tailer)
        await stats.start()
        snap = stats.snapshot()
        # Залишився лише трейд з другої сесії
        assert snap.session_started_ms == 10_000
        assert snap.realized_r == pytest.approx(0.5)
        await stats.stop()
    finally:
        await tailer.stop()
