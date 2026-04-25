"""DecisionConfig — ваги, threshold-и, кулдауни DecisionEngine."""

from __future__ import annotations

from pydantic import BaseModel, Field

from scalper.common.enums import Regime, SetupType


class WeightsConfig(BaseModel):
    # Ваги per фактор (береться з Features через _feature_score)
    absorption_score: float = 1.0
    stacked_imbalance: float = 0.8
    weighted_imbalance: float = 0.5
    book_imbalance_5: float = 0.4
    delta_magnitude: float = 0.5             # |delta_500ms| нормалізоване
    micro_pullback_present: float = 0.3
    aggressive_burst: float = 0.4
    spoof_score: float = 0.9

    # Контекст
    htf_poi_bonus: float = 0.5
    htf_poi_fvg_ob_extra: float = 0.2
    regime_tailwind: float = 0.3
    regime_headwind: float = 0.4

    # Штрафи
    spread_penalty_threshold_ticks: int = 3
    spread_penalty: float = 0.3
    loss_streak_penalty_per_loss: float = 0.15

    # Expectancy множник
    expectancy_multiplier_scale: float = 0.5


class DecisionConfig(BaseModel):
    base_score_threshold: float = Field(default=1.0, gt=0)
    threshold_boost_high_vol: float = 0.2
    threshold_boost_choppy: float = 0.2
    threshold_boost_per_loss: float = 0.1

    # Testnet/dev режим: знімає блок regime для LOW_LIQ/CHOPPY/HIGH_VOL — там
    # дозволяються всі setup-и. Прод-default = False (сувора фільтрація).
    # Без цього на testnet (де майже завжди LOW_LIQ через малу активність)
    # бот ніколи не торгує.
    relaxed_regime: bool = False

    cooldown_per_setup_ms: int = Field(default=30_000, ge=0)
    cooldown_per_symbol_ms: int = Field(default=5_000, ge=0)

    min_expectancy_R: float = Field(default=-0.3)

    # Нормалізації — для clamp01 у scoring-у
    delta_magnitude_full_score_usd: float = Field(default=100_000, gt=0)

    weights: WeightsConfig = Field(default_factory=WeightsConfig)

    time_stop_ms_by_setup: dict[SetupType, int | None] = Field(
        default_factory=lambda: {
            SetupType.ABSORPTION_REVERSAL: None,
            SetupType.STACKED_IMBALANCE: 30_000,
            SetupType.DELTA_SPIKE_REJECTION: 15_000,
            SetupType.MICRO_PULLBACK_CONTINUATION: 20_000,
            SetupType.LIQUIDITY_GRAB: 10_000,
            SetupType.MOMENTUM_BREAKOUT: 10_000,
        }
    )

    regime_allow_map: dict[Regime, set[SetupType]] = Field(
        default_factory=lambda: {
            Regime.NORMAL_BALANCED: {
                SetupType.ABSORPTION_REVERSAL, SetupType.STACKED_IMBALANCE,
                SetupType.DELTA_SPIKE_REJECTION,
                SetupType.MICRO_PULLBACK_CONTINUATION, SetupType.LIQUIDITY_GRAB,
                SetupType.MOMENTUM_BREAKOUT,
            },
            Regime.TRENDING_UP: {
                SetupType.ABSORPTION_REVERSAL, SetupType.STACKED_IMBALANCE,
                SetupType.MICRO_PULLBACK_CONTINUATION,
                SetupType.MOMENTUM_BREAKOUT,
            },
            Regime.TRENDING_DOWN: {
                SetupType.ABSORPTION_REVERSAL, SetupType.STACKED_IMBALANCE,
                SetupType.MICRO_PULLBACK_CONTINUATION,
                SetupType.MOMENTUM_BREAKOUT,
            },
            Regime.CHOPPY: {
                SetupType.ABSORPTION_REVERSAL, SetupType.DELTA_SPIKE_REJECTION,
                SetupType.MOMENTUM_BREAKOUT,
            },
            Regime.HIGH_VOL: {SetupType.ABSORPTION_REVERSAL, SetupType.MOMENTUM_BREAKOUT},
            Regime.LOW_LIQ: set(),
            Regime.NEWS_RISK: set(),
            Regime.DISABLED: set(),
        }
    )

    model_config = {"arbitrary_types_allowed": True}


__all__ = ["DecisionConfig", "WeightsConfig"]
