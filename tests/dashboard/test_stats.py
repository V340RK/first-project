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
async def test_counts_trades_and_pnl_per_symbol(tmp_path: Path) -> None:
    """Дві пари торгуються паралельно — статистика розділена."""
    from datetime import date
    jfile = tmp_path / f"{date.today().isoformat()}.jsonl"
    events = [
        # BTC сесія
        {"seq": 1, "timestamp_ms": 1000, "kind": "startup",
         "payload": {"symbols": ["BTCUSDT"]}},
        {"seq": 2, "timestamp_ms": 2000, "kind": "position_opened",
         "symbol": "BTCUSDT", "payload": {}},
        {"seq": 3, "timestamp_ms": 3000, "kind": "position_closed",
         "symbol": "BTCUSDT", "payload": {}},
        {"seq": 4, "timestamp_ms": 3001, "kind": "trade_outcome",
         "symbol": "BTCUSDT", "payload": {"realized_r": 1.2, "realized_usd": 1.20}},
        # ETH сесія
        {"seq": 5, "timestamp_ms": 4000, "kind": "startup",
         "payload": {"symbols": ["ETHUSDT"]}},
        {"seq": 6, "timestamp_ms": 5000, "kind": "position_opened",
         "symbol": "ETHUSDT", "payload": {}},
        {"seq": 7, "timestamp_ms": 6000, "kind": "position_closed",
         "symbol": "ETHUSDT", "payload": {}},
        {"seq": 8, "timestamp_ms": 6001, "kind": "trade_outcome",
         "symbol": "ETHUSDT", "payload": {"realized_r": -0.5, "realized_usd": -0.50}},
    ]
    _write_journal(jfile, events)

    tailer = JournalTailer(journal_dir=tmp_path, poll_interval_ms=50)
    await tailer.start()
    try:
        stats = SessionStats(tailer)
        await stats.start()
        all_snaps = stats.snapshot_all()
        assert set(all_snaps.keys()) == {"BTCUSDT", "ETHUSDT"}

        btc = all_snaps["BTCUSDT"]
        assert btc.session_started_ms == 1000
        assert btc.trades_closed == 1
        assert btc.realized_r == pytest.approx(1.2)
        assert btc.realized_usd == pytest.approx(1.20)

        eth = all_snaps["ETHUSDT"]
        assert eth.session_started_ms == 4000
        assert eth.trades_closed == 1
        assert eth.realized_r == pytest.approx(-0.5)
        await stats.stop()
    finally:
        await tailer.stop()


@pytest.mark.asyncio
async def test_reset_clears_counters_for_specific_symbol(tmp_path: Path) -> None:
    tailer = JournalTailer(journal_dir=tmp_path, poll_interval_ms=50)
    await tailer.start()
    try:
        stats = SessionStats(tailer)
        s = stats._get_or_create("BTCUSDT")   # type: ignore[attr-defined]
        s.trades_closed = 10
        s.realized_r = 3.5
        # ETH untouched
        e = stats._get_or_create("ETHUSDT")   # type: ignore[attr-defined]
        e.trades_closed = 7

        stats.reset("BTCUSDT")
        snap = stats.snapshot("BTCUSDT")
        assert snap is not None
        assert snap.trades_closed == 0
        # ETH не зачеплений
        assert stats.snapshot("ETHUSDT").trades_closed == 7
    finally:
        await tailer.stop()


@pytest.mark.asyncio
async def test_startup_event_resets_session_for_that_symbol(tmp_path: Path) -> None:
    """Повторний startup того ж символу — нова сесія, лічильники обнулені."""
    from datetime import date
    jfile = tmp_path / f"{date.today().isoformat()}.jsonl"
    _write_journal(jfile, [
        {"seq": 1, "timestamp_ms": 1000, "kind": "startup",
         "payload": {"symbols": ["BTCUSDT"]}},
        {"seq": 2, "timestamp_ms": 2000, "kind": "trade_outcome",
         "symbol": "BTCUSDT", "payload": {"realized_r": 2.0}},
        # Повторний старт BTC = нова сесія для BTC
        {"seq": 3, "timestamp_ms": 10_000, "kind": "startup",
         "payload": {"symbols": ["BTCUSDT"]}},
        {"seq": 4, "timestamp_ms": 11_000, "kind": "trade_outcome",
         "symbol": "BTCUSDT", "payload": {"realized_r": 0.5}},
    ])
    tailer = JournalTailer(journal_dir=tmp_path, poll_interval_ms=50)
    await tailer.start()
    try:
        stats = SessionStats(tailer)
        await stats.start()
        snap = stats.snapshot("BTCUSDT")
        assert snap.session_started_ms == 10_000
        assert snap.realized_r == pytest.approx(0.5)
        await stats.stop()
    finally:
        await tailer.stop()


@pytest.mark.asyncio
async def test_unknown_symbol_returns_none(tmp_path: Path) -> None:
    tailer = JournalTailer(journal_dir=tmp_path, poll_interval_ms=50)
    await tailer.start()
    try:
        stats = SessionStats(tailer)
        await stats.start()
        assert stats.snapshot("UNKNOWN") is None
        await stats.stop()
    finally:
        await tailer.stop()
