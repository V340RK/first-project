"""RiskConfig — R-per-trade, ліміти, кулдауни, sizing fallback-и."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RiskConfig(BaseModel):
    # === Per-trade ===
    risk_per_trade_usd_abs: float = Field(default=10.0, gt=0)
    risk_per_trade_pct: float = Field(default=0.003, gt=0)      # 0.3%
    slippage_buffer_ticks: int = Field(default=1, ge=0)

    # === Size fallbacks (поки немає ExchangeInfo) ===
    fallback_tick_size: float = Field(default=0.1, gt=0)
    fallback_step_size: float = Field(default=0.001, gt=0)
    fallback_min_qty: float = Field(default=0.001, gt=0)
    fallback_max_qty: float = Field(default=10.0, gt=0)
    fallback_min_notional: float = Field(default=5.0, ge=0)

    # === Денні/місячні ліміти (в R) ===
    daily_loss_limit_R: float = Field(default=3.0, gt=0)
    monthly_loss_limit_R: float = Field(default=10.0, gt=0)
    max_trades_per_day: int = Field(default=10, gt=0)
    max_initiative_trades_per_day: int = Field(default=3, ge=0)
    max_concurrent_positions: int = Field(default=1, gt=0)

    # === Loss streak ===
    loss_streak_cooldown_trigger: int = Field(default=3, gt=0)
    loss_streak_cooldown_ms: int = Field(default=1_800_000, gt=0)   # 30 хв
    loss_streak_hard_limit: int = Field(default=5, gt=0)


__all__ = ["RiskConfig"]
