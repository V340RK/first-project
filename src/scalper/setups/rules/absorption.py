"""ABSORPTION_REVERSAL — лімітник на best тримає тиск, очікуємо розворот."""

from __future__ import annotations

from scalper.common.enums import Direction, SetupType
from scalper.common.types import SetupCandidate
from scalper.features.types import Features
from scalper.setups.config import AbsorptionRuleConfig
from scalper.setups.rules._common import (
    compute_long_levels,
    compute_short_levels,
    standard_invalidations,
    stop_distance_in_ticks,
)


class AbsorptionReversalLong:
    setup_type = SetupType.ABSORPTION_REVERSAL

    def __init__(self, config: AbsorptionRuleConfig, *, tick_size: float = 0.1) -> None:
        self._cfg = config
        self._tick = tick_size

    def check(self, f: Features) -> SetupCandidate | None:
        cfg = self._cfg
        snap = f.snapshot
        if f.absorption_score < cfg.min_score or f.absorption_side != "BID":
            return None
        if f.delta_500ms > -cfg.min_pressure_usd:
            return None
        if snap.spread_ticks > cfg.max_spread_ticks:
            return None
        if not snap.book.bids or not snap.book.asks:
            return None

        confirmed = (
            f.stacked_imbalance_long
            or f.delta_2s > cfg.confirm_recovery_delta
            or f.weighted_imbalance > cfg.confirm_book_pressure
        )
        if not confirmed:
            return None

        best_bid = snap.book.bids[0].price
        entry = best_bid + self._tick
        stop = best_bid - cfg.stop_buffer_ticks * self._tick
        tp1, tp2, tp3 = compute_long_levels(
            entry=entry, stop=stop, tp_multipliers=cfg.tp_r_multipliers,
        )
        return SetupCandidate(
            setup_type=self.setup_type,
            direction=Direction.LONG,
            symbol=snap.symbol,
            timestamp_ms=snap.timestamp_ms,
            entry_price=entry, stop_price=stop,
            tp1_price=tp1, tp2_price=tp2, tp3_price=tp3,
            stop_distance_ticks=stop_distance_in_ticks(entry, stop, self._tick),
            invalidation_conditions=standard_invalidations(
                direction=Direction.LONG, stop_price=stop,
                opposite_delta_threshold=cfg.invalidation_counter_delta,
                expires_at_ms=snap.timestamp_ms + cfg.expiry_ms,
            ),
            features_snapshot=f,
        )


class AbsorptionReversalShort:
    setup_type = SetupType.ABSORPTION_REVERSAL

    def __init__(self, config: AbsorptionRuleConfig, *, tick_size: float = 0.1) -> None:
        self._cfg = config
        self._tick = tick_size

    def check(self, f: Features) -> SetupCandidate | None:
        cfg = self._cfg
        snap = f.snapshot
        if f.absorption_score < cfg.min_score or f.absorption_side != "ASK":
            return None
        if f.delta_500ms < cfg.min_pressure_usd:
            return None
        if snap.spread_ticks > cfg.max_spread_ticks:
            return None
        if not snap.book.bids or not snap.book.asks:
            return None

        confirmed = (
            f.stacked_imbalance_short
            or f.delta_2s < -cfg.confirm_recovery_delta
            or f.weighted_imbalance < -cfg.confirm_book_pressure
        )
        if not confirmed:
            return None

        best_ask = snap.book.asks[0].price
        entry = best_ask - self._tick
        stop = best_ask + cfg.stop_buffer_ticks * self._tick
        tp1, tp2, tp3 = compute_short_levels(
            entry=entry, stop=stop, tp_multipliers=cfg.tp_r_multipliers,
        )
        return SetupCandidate(
            setup_type=self.setup_type,
            direction=Direction.SHORT,
            symbol=snap.symbol,
            timestamp_ms=snap.timestamp_ms,
            entry_price=entry, stop_price=stop,
            tp1_price=tp1, tp2_price=tp2, tp3_price=tp3,
            stop_distance_ticks=stop_distance_in_ticks(entry, stop, self._tick),
            invalidation_conditions=standard_invalidations(
                direction=Direction.SHORT, stop_price=stop,
                opposite_delta_threshold=cfg.invalidation_counter_delta,
                expires_at_ms=snap.timestamp_ms + cfg.expiry_ms,
            ),
            features_snapshot=f,
        )


__all__ = ["AbsorptionReversalLong", "AbsorptionReversalShort"]
