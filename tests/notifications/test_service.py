"""NotificationService — console, rate-limit, min_level, queue, disabled."""

from __future__ import annotations

import asyncio
import logging

import pytest

from scalper.common.enums import AlertLevel
from scalper.notifications import NotificationConfig, NotificationService


@pytest.mark.asyncio
async def test_send_below_min_level_is_dropped(caplog) -> None:   # type: ignore[no-untyped-def]
    caplog.set_level(logging.INFO)
    svc = NotificationService(NotificationConfig(min_level="warning"))
    await svc.start()
    await svc.send("info msg", AlertLevel.INFO)
    await svc.send("warn msg", AlertLevel.WARNING)
    await asyncio.sleep(0.01)
    await svc.stop()
    out = "\n".join(r.message for r in caplog.records)
    assert "warn msg" in out
    assert "info msg" not in out


@pytest.mark.asyncio
async def test_disabled_service_does_nothing(caplog) -> None:   # type: ignore[no-untyped-def]
    caplog.set_level(logging.INFO)
    svc = NotificationService(NotificationConfig(enabled=False))
    await svc.start()
    await svc.send("should not appear", AlertLevel.CRITICAL)
    await svc.stop()
    assert "should not appear" not in "\n".join(r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_console_emits_at_right_level(caplog) -> None:   # type: ignore[no-untyped-def]
    caplog.set_level(logging.INFO)
    svc = NotificationService(NotificationConfig())
    await svc.start()
    await svc.send("hello", AlertLevel.ERROR)
    await asyncio.sleep(0.01)
    await svc.stop()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and "hello" in r.message]
    assert len(errors) == 1


@pytest.mark.asyncio
async def test_rate_limit_drops_excess(caplog) -> None:   # type: ignore[no-untyped-def]
    caplog.set_level(logging.INFO)
    svc = NotificationService(NotificationConfig(rate_limit_per_minute=3))
    await svc.start()
    for i in range(10):
        await svc.send(f"msg{i}", AlertLevel.INFO)
    await asyncio.sleep(0.05)
    await svc.stop()
    delivered = [r.message for r in caplog.records if "[NOTIFY/" in r.message]
    assert len(delivered) == 3


@pytest.mark.asyncio
async def test_send_without_start_is_noop() -> None:
    svc = NotificationService(NotificationConfig())
    # без start() queue = None → ранній return, без виключення
    await svc.send("text", AlertLevel.INFO)
