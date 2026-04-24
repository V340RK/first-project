"""Тести JournalLogger: відновлення seq, drain, ротація, query, queue overflow."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scalper.journal.config import JournalConfig
from scalper.journal.logger import JournalLogger
from scalper.journal.types import EventKind, JournalEvent


# === Helpers ===

def _ts(date_str: str, hour: int = 12) -> int:
    """Convert 'YYYY-MM-DD' + hour → epoch ms (UTC)."""
    dt = datetime.fromisoformat(date_str).replace(hour=hour, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _make_event(kind: EventKind = EventKind.HEARTBEAT, **kwargs: object) -> JournalEvent:
    base = {
        "seq": 0, "timestamp_ms": 0, "kind": kind, "trade_id": None,
        "symbol": None, "payload": {},
    }
    base.update(kwargs)  # type: ignore[arg-type]
    return JournalEvent(**base)  # type: ignore[arg-type]


def _make_logger(tmp_path: Path, clock_ms: int, **overrides: object) -> JournalLogger:
    cfg_kwargs: dict[str, object] = {
        "journal_dir": tmp_path,
        "batch_max": 100,
        "flush_interval_ms": 50,
        "fsync_interval_ms": 100,
        "queue_size": 100,
        "gzip_old_files": False,
    }
    cfg_kwargs.update(overrides)
    config = JournalConfig(**cfg_kwargs)  # type: ignore[arg-type]
    return JournalLogger(config, clock_fn=lambda: clock_ms)


# === _read_last_seq ===

def test_read_last_seq_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    assert JournalLogger._read_last_seq(p) == 0


def test_read_last_seq_normal_file(tmp_path: Path) -> None:
    p = tmp_path / "x.jsonl"
    lines = "\n".join(json.dumps({"seq": i}) for i in range(1, 6)) + "\n"
    p.write_text(lines, encoding="utf-8")
    assert JournalLogger._read_last_seq(p) == 5


def test_read_last_seq_truncated_last_line(tmp_path: Path) -> None:
    """Файл закінчується недописаним рядком → беремо передостанній валідний."""
    p = tmp_path / "x.jsonl"
    good = json.dumps({"seq": 7}) + "\n"
    bad = '{"seq": 8, "kind"'  # обірваний JSON, без \n
    p.write_text(good + bad, encoding="utf-8")
    assert JournalLogger._read_last_seq(p) == 7


# === Round-trip: write events → file → re-open → continue seq ===

@pytest.mark.asyncio
async def test_writes_events_in_order(tmp_path: Path) -> None:
    clock = _ts("2026-04-21")
    logger = _make_logger(tmp_path, clock)
    await logger.start()
    for i in range(10):
        logger.log(_make_event(payload={"i": i}))
    await logger.stop()

    path = tmp_path / "2026-04-21.jsonl"
    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # STARTUP + 10 heartbeats + SHUTDOWN = 12
    assert len(lines) == 12
    seqs = [ln["seq"] for ln in lines]
    assert seqs == list(range(1, 13)), f"seq має бути монотонним 1..12, отримали {seqs}"

    # Перший — STARTUP, останній — SHUTDOWN
    assert lines[0]["kind"] == "startup"
    assert lines[-1]["kind"] == "shutdown"


@pytest.mark.asyncio
async def test_seq_continues_after_restart(tmp_path: Path) -> None:
    clock = _ts("2026-04-21")
    log1 = _make_logger(tmp_path, clock)
    await log1.start()
    log1.log(_make_event())
    log1.log(_make_event())
    await log1.stop()

    log2 = _make_logger(tmp_path, clock)
    await log2.start()
    log2.log(_make_event())
    await log2.stop()

    path = tmp_path / "2026-04-21.jsonl"
    seqs = [json.loads(ln)["seq"] for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # 1..4 з першого запуску (STARTUP, 2 heartbeat, SHUTDOWN), потім 5.. з другого
    assert seqs == sorted(seqs)
    assert seqs[0] == 1
    assert seqs[-1] == len(seqs)


# === Query API ===

@pytest.mark.asyncio
async def test_iter_events_filters_by_kind(tmp_path: Path) -> None:
    clock = _ts("2026-04-21")
    logger = _make_logger(tmp_path, clock)
    await logger.start()
    logger.log(_make_event(kind=EventKind.HEARTBEAT))
    logger.log(_make_event(kind=EventKind.WARNING, payload={"msg": "test"}))
    logger.log(_make_event(kind=EventKind.HEARTBEAT))
    await logger.stop()

    only_warnings = list(logger.iter_events("2026-04-21", kinds={EventKind.WARNING}))
    assert len(only_warnings) == 1
    assert only_warnings[0].payload == {"msg": "test"}


@pytest.mark.asyncio
async def test_iter_events_skips_corrupt_lines(tmp_path: Path) -> None:
    clock = _ts("2026-04-21")
    logger = _make_logger(tmp_path, clock)
    await logger.start()
    logger.log(_make_event(kind=EventKind.HEARTBEAT))
    await logger.stop()

    # Вручну дописуємо побитий рядок між валідними.
    path = tmp_path / "2026-04-21.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write("{not a valid json}\n")
        f.write(json.dumps({
            "seq": 99, "timestamp_ms": 1, "kind": "heartbeat",
            "trade_id": None, "symbol": None, "payload": {},
        }) + "\n")

    events = list(logger.iter_events("2026-04-21"))
    # Побитий рядок пропущено, валідні залишились
    assert any(e.seq == 99 for e in events)


@pytest.mark.asyncio
async def test_get_trade_aggregates_events(tmp_path: Path) -> None:
    clock = _ts("2026-04-21")
    logger = _make_logger(tmp_path, clock)
    await logger.start()
    trade_id = "t-xyz"
    logger.log(_make_event(
        kind=EventKind.DECISION_ACCEPTED, trade_id=trade_id, symbol="BTCUSDT",
        payload={"setup_type": "absorption_reversal", "direction": "LONG"},
    ))
    logger.log(_make_event(
        kind=EventKind.POSITION_OPENED, trade_id=trade_id, symbol="BTCUSDT",
        timestamp_ms=clock + 1000,
    ))
    logger.log(_make_event(
        kind=EventKind.POSITION_CLOSED, trade_id=trade_id, symbol="BTCUSDT",
        timestamp_ms=clock + 60_000,
    ))
    logger.log(_make_event(
        kind=EventKind.TRADE_OUTCOME, trade_id=trade_id, symbol="BTCUSDT",
        payload={"realized_r": 1.85, "setup_type": "absorption_reversal"},
    ))
    # Подія, яка НЕ належить угоді
    logger.log(_make_event(kind=EventKind.HEARTBEAT))
    await logger.stop()

    record = logger.get_trade(trade_id)
    assert record is not None
    assert record.symbol == "BTCUSDT"
    assert record.setup_type == "absorption_reversal"
    assert record.direction == "LONG"
    assert record.realized_r == 1.85
    assert record.opened_at_ms == clock + 1000
    assert record.closed_at_ms == clock + 60_000
    assert len(record.events) == 4


@pytest.mark.asyncio
async def test_get_trade_missing_returns_none(tmp_path: Path) -> None:
    clock = _ts("2026-04-21")
    logger = _make_logger(tmp_path, clock)
    await logger.start()
    await logger.stop()
    assert logger.get_trade("never-existed") is None


# === Rotation ===

@pytest.mark.asyncio
async def test_rotation_on_date_change(tmp_path: Path) -> None:
    """Підмінити clock-функцію, щоб після старту перейшла дата → новий файл."""
    current_ms = _ts("2026-04-21", hour=23)

    def clock_fn() -> int:
        return current_ms

    config = JournalConfig(
        journal_dir=tmp_path, batch_max=100, flush_interval_ms=20,
        queue_size=100, gzip_old_files=False,
    )
    logger = JournalLogger(config, clock_fn=clock_fn)
    await logger.start()
    logger.log(_make_event(payload={"day": 1}))

    # Зачекаємо щоб writer обробив батч (один цикл — flush_interval_ms)
    import asyncio
    await asyncio.sleep(0.1)

    # Перенесемо годинник на наступну добу
    current_ms = _ts("2026-04-22", hour=0)
    logger.log(_make_event(payload={"day": 2}))

    await asyncio.sleep(0.15)
    await logger.stop()

    files = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert "2026-04-21.jsonl" in files
    assert "2026-04-22.jsonl" in files

    day1 = (tmp_path / "2026-04-21.jsonl").read_text(encoding="utf-8")
    day2 = (tmp_path / "2026-04-22.jsonl").read_text(encoding="utf-8")
    assert '"day":1' in day1
    assert '"day":2' in day2
    # У новому дні має бути DAILY_ROLLOVER маркер
    assert "daily_rollover" in day2


# === Queue overflow ===

@pytest.mark.asyncio
async def test_queue_overflow_drops_events_without_crash(tmp_path: Path) -> None:
    clock = _ts("2026-04-21")
    # Маленька черга + великий flood + НЕ запускаємо writer (щоб черга не дренилась)
    config = JournalConfig(
        journal_dir=tmp_path, batch_max=1000, flush_interval_ms=50,
        queue_size=10, gzip_old_files=False,
    )
    logger = JournalLogger(config, clock_fn=lambda: clock)
    # Не викликаємо start() — writer не працює, черга швидко переповниться.
    for _ in range(100):
        logger.log(_make_event())
    assert logger.dropped_count >= 80  # 100 - queue_size = ~90 drop-ів
