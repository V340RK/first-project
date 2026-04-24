"""FeatureConfig — пороги та ваги для FeatureEngine."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ImbalanceConfig(BaseModel):
    levels_short: int = Field(default=5, ge=1, le=50)
    levels_long: int = Field(default=10, ge=1, le=50)
    pressure_threshold: float = Field(default=0.35, gt=0.0, lt=1.0)


class BurstConfig(BaseModel):
    threshold_usd_500ms: float = Field(default=50_000, gt=0)
    threshold_usd_2s: float = Field(default=150_000, gt=0)


class AbsorptionConfig(BaseModel):
    delta_threshold_usd: float = Field(default=30_000, gt=0)
    full_score_delta_usd: float = Field(default=100_000, gt=0)
    book_top_size_retention: float = Field(default=0.9, gt=0.0, le=1.5)


class SpoofConfig(BaseModel):
    min_size_usd: float = Field(default=80_000, gt=0)
    max_lifetime_ms: int = Field(default=2000, ge=100)
    book_event_buffer: int = Field(default=200, ge=10)


class MicroPullbackConfig(BaseModel):
    impulse_min_ticks: int = Field(default=5, ge=1)
    impulse_window_ms: int = Field(default=2000, ge=100)
    pullback_max_fraction: float = Field(default=0.6, gt=0.0, lt=1.0)
    weak_counter_delta_usd: float = Field(default=20_000, gt=0)


class ClusterFeatureConfig(BaseModel):
    stacked_min_count: int = Field(default=3, ge=2)
    poc_mid_threshold_ticks: int = Field(default=1, ge=0)


class ZoneFeatureConfig(BaseModel):
    nearest_max_distance_ticks: int = Field(default=20, ge=1)


class FeatureConfig(BaseModel):
    tick_size_default: float = Field(default=0.1, gt=0)
    imbalance: ImbalanceConfig = Field(default_factory=ImbalanceConfig)
    burst: BurstConfig = Field(default_factory=BurstConfig)
    absorption: AbsorptionConfig = Field(default_factory=AbsorptionConfig)
    spoof: SpoofConfig = Field(default_factory=SpoofConfig)
    micro_pullback: MicroPullbackConfig = Field(default_factory=MicroPullbackConfig)
    cluster: ClusterFeatureConfig = Field(default_factory=ClusterFeatureConfig)
    zones: ZoneFeatureConfig = Field(default_factory=ZoneFeatureConfig)


__all__ = [
    "AbsorptionConfig",
    "BurstConfig",
    "ClusterFeatureConfig",
    "FeatureConfig",
    "ImbalanceConfig",
    "MicroPullbackConfig",
    "SpoofConfig",
    "ZoneFeatureConfig",
]
