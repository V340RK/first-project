"""TapeConfig — параметри стрічкового аналізатора."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TapeWindowsConfig(BaseModel):
    short_ms: int = Field(default=500, ge=50)
    medium_ms: int = Field(default=2000, ge=200)
    long_ms: int = Field(default=10_000, ge=1000)


class TapeGapConfig(BaseModel):
    unreliable_window_min: int = Field(default=5, ge=1)


class TapeConfig(BaseModel):
    trade_buffer_maxlen: int = Field(default=10_000, ge=100)
    price_path_maxlen: int = Field(default=200, ge=20)
    windows: TapeWindowsConfig = Field(default_factory=TapeWindowsConfig)
    tape_gap: TapeGapConfig = Field(default_factory=TapeGapConfig)
