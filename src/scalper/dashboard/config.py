"""Конфіг дашборда."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class DashboardConfig(BaseModel):
    journal_dir: Path
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    poll_interval_ms: int = Field(default=150, ge=20, le=5000)
    backfill_lines: int = Field(default=200, ge=0, le=5000)
