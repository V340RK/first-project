"""Спільні хелпери для setup-rules: розрахунок entry/stop/TP, побудова invalidation."""

from __future__ import annotations

from scalper.common.enums import Direction
from scalper.common.types import InvalidationCondition, InvalidationKind


def compute_long_levels(
    *, entry: float, stop: float, tp_multipliers: tuple[float, float, float],
) -> tuple[float, float, float]:
    risk = entry - stop
    if risk <= 0:
        return entry, entry, entry
    return (
        entry + tp_multipliers[0] * risk,
        entry + tp_multipliers[1] * risk,
        entry + tp_multipliers[2] * risk,
    )


def compute_short_levels(
    *, entry: float, stop: float, tp_multipliers: tuple[float, float, float],
) -> tuple[float, float, float]:
    risk = stop - entry
    if risk <= 0:
        return entry, entry, entry
    return (
        entry - tp_multipliers[0] * risk,
        entry - tp_multipliers[1] * risk,
        entry - tp_multipliers[2] * risk,
    )


def stop_distance_in_ticks(entry: float, stop: float, tick: float) -> int:
    if tick <= 0:
        return 0
    return max(1, int(round(abs(entry - stop) / tick)))


def standard_invalidations(
    *, direction: Direction, stop_price: float, opposite_delta_threshold: float,
    expires_at_ms: int,
) -> list[InvalidationCondition]:
    return [
        InvalidationCondition(
            kind=InvalidationKind.PRICE_BEYOND_LEVEL,
            params={"price": stop_price, "side": direction.value},
        ),
        InvalidationCondition(
            kind=InvalidationKind.OPPOSITE_ABSORPTION,
            params={"delta_threshold_usd": opposite_delta_threshold},
        ),
        InvalidationCondition(
            kind=InvalidationKind.DELTA_FLIP,
            params={"window_ms": 2000, "expires_at_ms": expires_at_ms},
        ),
    ]


__all__ = [
    "compute_long_levels",
    "compute_short_levels",
    "standard_invalidations",
    "stop_distance_in_ticks",
]
