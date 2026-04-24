"""PositionConfig — TP split, breakeven buffer, trailing, time stop, entry."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PositionConfig(BaseModel):
    tp_split: tuple[float, float, float] = (0.5, 0.25, 0.25)

    tick_size: float = Field(default=0.1, gt=0)

    breakeven_buffer_ticks: int = Field(default=1, ge=0)
    disable_invalidation_after_tp1: bool = True
    disable_time_stop_after_tp1: bool = True

    trailing_distance_ticks: int = Field(default=5, gt=0)
    trailing_min_move_ticks: int = Field(default=1, gt=0)

    entry_ioc_offset_ticks: int = Field(default=1, ge=0)
    entry_as_market: bool = False


__all__ = ["PositionConfig"]
