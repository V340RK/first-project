"""Конкретні setup-rules + factory `default_rules(config)`."""

from __future__ import annotations

from scalper.setups.base import SetupRule
from scalper.setups.config import SetupConfig
from scalper.setups.rules.absorption import AbsorptionReversalLong, AbsorptionReversalShort
from scalper.setups.rules.imbalance_continuation import (
    ImbalanceContinuationLong,
    ImbalanceContinuationShort,
)
from scalper.setups.rules.micro_pullback import MicroPullbackLong, MicroPullbackShort
from scalper.setups.rules.momentum_breakout import (
    MomentumBreakoutLong,
    MomentumBreakoutShort,
)


def default_rules(config: SetupConfig) -> list[SetupRule]:
    """5 setup-типів x 2 напрями = 10 rule-ів. Передається у SetupDetector."""
    tick = config.tick_size_default
    return [
        AbsorptionReversalLong(config.absorption, tick_size=tick),
        AbsorptionReversalShort(config.absorption, tick_size=tick),
        ImbalanceContinuationLong(config.imbalance_cont, tick_size=tick),
        ImbalanceContinuationShort(config.imbalance_cont, tick_size=tick),
        MicroPullbackLong(config.micro_pullback, tick_size=tick),
        MicroPullbackShort(config.micro_pullback, tick_size=tick),
        MomentumBreakoutLong(config.momentum_breakout, tick_size=tick),
        MomentumBreakoutShort(config.momentum_breakout, tick_size=tick),
    ]


__all__ = [
    "AbsorptionReversalLong",
    "AbsorptionReversalShort",
    "ImbalanceContinuationLong",
    "ImbalanceContinuationShort",
    "MicroPullbackLong",
    "MicroPullbackShort",
    "MomentumBreakoutLong",
    "MomentumBreakoutShort",
    "default_rules",
]
