"""ExecConfig — retry, time-in-force за замовчуванням, leverage/margin."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExecConfig(BaseModel):
    max_retries: int = Field(default=3, ge=0)
    retry_delay_ms: int = Field(default=500, ge=0)

    entry_time_in_force: str = Field(default="IOC")      # для market-like entry
    default_leverage: int = Field(default=10, gt=0)
    margin_type: str = Field(default="ISOLATED")

    coid_prefix_max_len: int = Field(default=12, gt=0)   # hint шматочок у coid
    coid_max_len: int = Field(default=36, gt=0)          # Binance обмеження


__all__ = ["ExecConfig"]
