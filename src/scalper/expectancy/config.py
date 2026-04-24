"""ExpectancyConfig — window, auto-suspend пороги."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExpectancyConfig(BaseModel):
    window_size: int = Field(default=50, gt=0)
    min_samples_for_multiplier: int = Field(default=10, gt=0)

    auto_suspend_e_threshold_R: float = Field(default=-0.3, lt=0)
    auto_suspend_min_samples: int = Field(default=30, gt=0)
    auto_suspend_ci_upper: float = Field(default=0.45, gt=0, le=1.0)

    win_threshold_R: float = Field(default=0.05, gt=0)    # R > 0.05 → win
    loss_threshold_R: float = Field(default=0.05, gt=0)   # R < -0.05 → loss


__all__ = ["ExpectancyConfig"]
