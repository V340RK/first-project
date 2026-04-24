"""Тести JournalTailer: поллинг нових рядків, backfill, ротація, обірваний хвіст."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scalper.dashboard.tailer import JournalTailer


def _ts(date_str: str, hour: int = 12) -> int:
    dt = datetime.fromisoformat(date_str).replace(hour=hour, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _write_line(path: Path, obj: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


@pytest.mark.asyncio
async def test_tailer_delivers_new_lines(tmp_path: Path) -> None:
    clock_ms = _ts("2026-04-21")
    date_str = "2026-04-21"
    path = tmp_path / f"{date_str}.jsonl"
    path.write_text("", encoding="utf-8")  # файл вже існує → tailer почне з кінця

    tailer = JournalTailer(tmp_path, poll_interval_ms=30, clock_fn=lambda: clock_ms)
    received: list[dict] = []

    async def on_event(ev: dict) -> None:
        received.append(ev)

    tailer.subscribe(on_event)
    await tailer.start()
    try:
        # Невелика пауза, щоб tailer встиг зробити перший tick і зафіксувати позицію.
        await asyncio.sleep(0.1)
        _write_line(path, {"seq": 1, "kind": "heartbeat"})
        _write_line(path, {"seq": 2, "kind": "warning"})
        await asyncio.sleep(0.2)
    finally:
        await tailer.stop()

    assert [e["seq"] for e in received] == [1, 2]


@pytest.mark.asyncio
async def test_tailer_ignores_partial_line_until_newline(tmp_path: Path) -> None:
    clock_ms = _ts("2026-04-21")
    path = tmp_path / "2026-04-21.jsonl"
    path.write_text("", encoding="utf-8")

    tailer = JournalTailer(tmp_path, poll_interval_ms=30, clock_fn=lambda: clock_ms)
    received: list[dict] = []
    tailer.subscribe(lambda ev: _append_async(received, ev))
    await tailer.start()
    try:
        await asyncio.sleep(0.1)
        # Пишемо обірваний фрагмент без `\n`
        with path.open("a", encoding="utf-8") as f:
            f.write('{"seq": 1, "kind": "heart')
            f.flush()
        await asyncio.sleep(0.1)
        assert received == [], "фрагмент без `\\n` не має давати події"

        # Дописуємо хвіст + ще одну подію
        with path.open("a", encoding="utf-8") as f:
            f.write('beat"}\n{"seq": 2, "kind": "warning"}\n')
        await asyncio.sleep(0.15)
    finally:
        await tailer.stop()

    assert [e["seq"] for e in received] == [1, 2]


async def _append_async(lst: list[dict], ev: dict) -> None:
    lst.append(ev)


@pytest.mark.asyncio
async def test_tailer_rotates_on_date_change(tmp_path: Path) -> None:
    current = _ts("2026-04-21", hour=23)

    def clock_fn() -> int:
        return current

    (tmp_path / "2026-04-21.jsonl").write_text("", encoding="utf-8")

    tailer = JournalTailer(tmp_path, poll_interval_ms=30, clock_fn=clock_fn)
    received: list[dict] = []
    tailer.subscribe(lambda ev: _append_async(received, ev))
    await tailer.start()
    try:
        await asyncio.sleep(0.1)
        _write_line(tmp_path / "2026-04-21.jsonl", {"seq": 1, "day": 1})
        await asyncio.sleep(0.1)

        # Годинник — на нову добу; tailer при наступному tick-у має переключитись.
        current = _ts("2026-04-22", hour=0)
        _write_line(tmp_path / "2026-04-22.jsonl", {"seq": 2, "day": 2})
        await asyncio.sleep(0.2)
    finally:
        await tailer.stop()

    seqs = [e["seq"] for e in received]
    assert 1 in seqs and 2 in seqs


def test_read_recent_returns_last_lines(tmp_path: Path) -> None:
    clock_ms = _ts("2026-04-21")
    path = tmp_path / "2026-04-21.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for i in range(1, 11):
            f.write(json.dumps({"seq": i}) + "\n")

    tailer = JournalTailer(tmp_path, clock_fn=lambda: clock_ms)
    recent = tailer.read_recent(limit=5)
    assert [e["seq"] for e in recent] == [6, 7, 8, 9, 10]


def test_read_recent_skips_corrupt(tmp_path: Path) -> None:
    clock_ms = _ts("2026-04-21")
    path = tmp_path / "2026-04-21.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"seq": 1}) + "\n")
        f.write("{not json}\n")
        f.write(json.dumps({"seq": 2}) + "\n")

    tailer = JournalTailer(tmp_path, clock_fn=lambda: clock_ms)
    recent = tailer.read_recent(limit=10)
    assert [e["seq"] for e in recent] == [1, 2]


def test_read_recent_empty_when_no_file(tmp_path: Path) -> None:
    clock_ms = _ts("2026-04-21")
    tailer = JournalTailer(tmp_path, clock_fn=lambda: clock_ms)
    assert tailer.read_recent() == []
