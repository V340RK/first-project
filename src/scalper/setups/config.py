"""SetupConfig — пороги для всіх 4 базових сетапів."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AbsorptionRuleConfig(BaseModel):
    min_score: float = Field(default=0.6, ge=0, le=1)
    min_pressure_usd: float = Field(default=30_000, gt=0)
    max_spread_ticks: int = Field(default=2, ge=1)
    confirm_recovery_delta: float = Field(default=20_000, gt=0)
    confirm_book_pressure: float = Field(default=0.35, gt=0, lt=1)
    stop_buffer_ticks: int = Field(default=2, ge=0)
    invalidation_counter_delta: float = Field(default=50_000, gt=0)
    tp_r_multipliers: tuple[float, float, float] = (1.0, 2.0, 3.0)
    expiry_ms: int = Field(default=8000, gt=0)


class ImbalanceContRuleConfig(BaseModel):
    min_buy_pressure_usd: float = Field(default=40_000, gt=0)
    min_book_imbalance: float = Field(default=0.3, gt=0, lt=1)
    pullback_min_depth_ticks: int = Field(default=3, ge=1)
    stop_buffer_ticks: int = Field(default=2, ge=0)
    opposing_delta_usd: float = Field(default=60_000, gt=0)
    tp_r_multipliers: tuple[float, float, float] = (1.0, 2.0, 3.0)
    expiry_ms: int = Field(default=5000, gt=0)


class SpoofRuleConfig(BaseModel):
    min_score: float = Field(default=0.5, ge=0, le=1)
    confirm_pressure_usd: float = Field(default=25_000, gt=0)
    stop_buffer_ticks: int = Field(default=2, ge=0)
    invalidation_counter_delta: float = Field(default=40_000, gt=0)
    tp_r_multipliers: tuple[float, float, float] = (1.0, 2.0, 3.0)
    expiry_ms: int = Field(default=5000, gt=0)


class MicroPullbackRuleConfig(BaseModel):
    min_depth_ticks: int = Field(default=2, ge=1)
    max_counter_delta_usd: float = Field(default=15_000, gt=0)
    stop_buffer_ticks: int = Field(default=2, ge=0)
    tp_r_multipliers: tuple[float, float, float] = (1.0, 2.0, 3.0)
    expiry_ms: int = Field(default=5000, gt=0)


class SetupConfig(BaseModel):
    tick_size_default: float = Field(default=0.1, gt=0)
    absorption: AbsorptionRuleConfig = Field(default_factory=AbsorptionRuleConfig)
    imbalance_cont: ImbalanceContRuleConfig = Field(default_factory=ImbalanceContRuleConfig)
    spoof: SpoofRuleConfig = Field(default_factory=SpoofRuleConfig)
    micro_pullback: MicroPullbackRuleConfig = Field(default_factory=MicroPullbackRuleConfig)


__all__ = [
    "AbsorptionRuleConfig",
    "ImbalanceContRuleConfig",
    "MicroPullbackRuleConfig",
    "SetupConfig",
    "SpoofRuleConfig",
]
