"""RegimeConfig — налаштування MarketRegime."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HighVolConfig(BaseModel):
    atr_ratio_threshold: float = Field(default=1.8, gt=1.0)


class LowLiqConfig(BaseModel):
    spread_ticks: float = Field(default=4.0, gt=0)


class TrendingConfig(BaseModel):
    min_run: int = Field(default=4, ge=2)
    cvd_slope: float = Field(default=0.5, gt=0)
    range_expansion: float = Field(default=1.3, gt=1.0)


class ChoppyConfig(BaseModel):
    cvd_flip_count: int = Field(default=4, ge=1)
    max_range_expansion: float = Field(default=0.8, gt=0)


class NewsConfig(BaseModel):
    enabled: bool = True
    before_minutes: int = Field(default=5, ge=0)
    after_minutes: int = Field(default=10, ge=0)


class AtrConfig(BaseModel):
    period_1m: int = Field(default=14, ge=2)
    period_5m: int = Field(default=7, ge=2)
    avg_atr_1m_default: float = Field(default=10.0, gt=0)


class RegimeConfig(BaseModel):
    compute_interval_sec: float = Field(default=30.0, gt=0)
    hysteresis_bars: int = Field(default=3, ge=1)
    debounce_min_seconds: float = Field(default=5.0, ge=0)

    high_vol: HighVolConfig = Field(default_factory=HighVolConfig)
    low_liq: LowLiqConfig = Field(default_factory=LowLiqConfig)
    trending: TrendingConfig = Field(default_factory=TrendingConfig)
    choppy: ChoppyConfig = Field(default_factory=ChoppyConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    atr: AtrConfig = Field(default_factory=AtrConfig)


__all__ = [
    "AtrConfig",
    "ChoppyConfig",
    "HighVolConfig",
    "LowLiqConfig",
    "NewsConfig",
    "RegimeConfig",
    "TrendingConfig",
]
