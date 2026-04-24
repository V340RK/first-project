"""MICRO_PULLBACK_AFTER_AGGRESSION — burst → відкат зі слабким контр → продовження."""

from __future__ import annotations

from scalper.common.enums import Direction, SetupType
from scalper.common.types import SetupCandidate
from scalper.features.types import Features
from scalper.setups.config import MicroPullbackRuleConfig
from scalper.setups.rules._common import (
    compute_long_levels,
    compute_short_levels,
    standard_invalidations,
    stop_distance_in_ticks,
)


class MicroPullbackLong:
    setup_type = SetupType.MICRO_PULLBACK_CONTINUATION

    def __init__(self, config: MicroPullbackRuleConfig, *, tick_size: float = 0.1) -> None:
        self._cfg = config
        self._tick = tick_size

    def check(self, f: Features) -> SetupCandidate | None:
        cfg = self._cfg
        snap = f.snapshot
        pb = f.micro_pullback
        if pb is None or pb.direction != "LONG_PULLBACK":
            return None
        if pb.depth_ticks < cfg.min_depth_ticks:
            return None
        # До пуллбеку має бути buy-burst АБО зараз є залишковий тиск
        if not f.aggressive_buy_burst and f.delta_10s <= 0:
            return None
        # Контр-потік під час пуллбеку має бути СЛАБКИЙ
        if pb.delta_during_pullback < -cfg.max_counter_delta_usd:
            return None
        if not snap.book.bids:
            return None

        best_bid = snap.book.bids[0].price
        entry = snap.last_price + self._tick
        stop = best_bid - (pb.depth_ticks + cfg.stop_buffer_ticks) * self._tick
        if stop >= entry:
            return None
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
                opposite_delta_threshold=cfg.max_counter_delta_usd * 2,
                expires_at_ms=snap.timestamp_ms + cfg.expiry_ms,
            ),
            features_snapshot=f,
        )


class MicroPullbackShort:
    setup_type = SetupType.MICRO_PULLBACK_CONTINUATION

    def __init__(self, config: MicroPullbackRuleConfig, *, tick_size: float = 0.1) -> None:
        self._cfg = config
        self._tick = tick_size

    def check(self, f: Features) -> SetupCandidate | None:
        cfg = self._cfg
        snap = f.snapshot
        pb = f.micro_pullback
        if pb is None or pb.direction != "SHORT_PULLBACK":
            return None
        if pb.depth_ticks < cfg.min_depth_ticks:
            return None
        if not f.aggressive_sell_burst and f.delta_10s >= 0:
            return None
        if pb.delta_during_pullback > cfg.max_counter_delta_usd:
            return None
        if not snap.book.asks:
            return None

        best_ask = snap.book.asks[0].price
        entry = snap.last_price - self._tick
        stop = best_ask + (pb.depth_ticks + cfg.stop_buffer_ticks) * self._tick
        if stop <= entry:
            return None
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
                opposite_delta_threshold=cfg.max_counter_delta_usd * 2,
                expires_at_ms=snap.timestamp_ms + cfg.expiry_ms,
            ),
            features_snapshot=f,
        )


__all__ = ["MicroPullbackLong", "MicroPullbackShort"]
