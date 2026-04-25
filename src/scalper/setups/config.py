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


class MomentumBreakoutRuleConfig(BaseModel):
    """Простий momentum-rule: ціна щойно зробила різкий рух, у напрямку
    delta-bias. На відміну від reversal-сетапів, тут ловимо вже існуючий
    рух, а не його розворот. Підходить для волатильних альтів."""
    min_thrust_pct: float = Field(default=0.3, gt=0)
    """Мінімальний рух ціни за `lookback_ms` у % (наприклад 0.3% за 10с)."""
    lookback_ms: int = Field(default=10_000, ge=1000)
    """Вікно для виміру руху (співпадає з window_10s tape data)."""
    min_delta_usd: float = Field(default=5_000, gt=0)
    """Мінімальний bias delta_10s в USD у напрямку руху."""
    stop_atr_mult: float = Field(default=1.0, gt=0)
    """Stop = entry ± thrust_pct × stop_atr_mult (умовно ATR-based, але через
    реалізований thrust)."""
    tp_r_multipliers: tuple[float, float, float] = (1.0, 1.8, 3.0)
    expiry_ms: int = Field(default=3000, gt=0)


class SetupConfig(BaseModel):
    tick_size_default: float = Field(default=0.1, gt=0)
    absorption: AbsorptionRuleConfig = Field(default_factory=AbsorptionRuleConfig)
    imbalance_cont: ImbalanceContRuleConfig = Field(default_factory=ImbalanceContRuleConfig)
    spoof: SpoofRuleConfig = Field(default_factory=SpoofRuleConfig)
    micro_pullback: MicroPullbackRuleConfig = Field(default_factory=MicroPullbackRuleConfig)
    momentum_breakout: MomentumBreakoutRuleConfig = Field(default_factory=MomentumBreakoutRuleConfig)


__all__ = [
    "AbsorptionRuleConfig",
    "ImbalanceContRuleConfig",
    "MicroPullbackRuleConfig",
    "MomentumBreakoutRuleConfig",
    "SetupConfig",
    "SpoofRuleConfig",
]
