"""MOMENTUM_BREAKOUT — простий thrust-rule для волатильних альтів.

Тригериться коли ціна щойно зробила різкий рух (% за вікно) у напрямку
delta-bias. На відміну від reversal-сетапів (absorption/imbalance), тут
ловимо вже існуючий рух — підходить для волатильних пар з низькою
структурною інформацією, де reversal-патерни рідкі.
"""

from __future__ import annotations

from scalper.common.enums import Direction, SetupType
from scalper.common.types import SetupCandidate
from scalper.features.types import Features
from scalper.setups.config import MomentumBreakoutRuleConfig
from scalper.setups.rules._common import (
    compute_long_levels,
    compute_short_levels,
    standard_invalidations,
    stop_distance_in_ticks,
)


def _price_thrust_pct(price_path: list[tuple[int, float]], lookback_ms: int) -> float:
    """Відсоток руху ціни за останні lookback_ms. Позитивний = вгору."""
    if len(price_path) < 2:
        return 0.0
    last_ms, last_price = price_path[-1]
    cutoff = last_ms - lookback_ms
    # Найдальший pricetimestamp у вікні
    base_price = last_price
    for ts, px in price_path:
        if ts >= cutoff:
            base_price = px
            break
    if base_price <= 0:
        return 0.0
    return (last_price - base_price) / base_price * 100.0


class MomentumBreakoutLong:
    setup_type = SetupType.MOMENTUM_BREAKOUT

    def __init__(self, config: MomentumBreakoutRuleConfig, *, tick_size: float = 0.1) -> None:
        self._cfg = config
        self._tick = tick_size

    def check(self, f: Features) -> SetupCandidate | None:
        cfg = self._cfg
        snap = f.snapshot
        thrust = _price_thrust_pct(snap.tape.price_path, cfg.lookback_ms)
        if thrust < cfg.min_thrust_pct:
            return None
        if f.delta_10s < cfg.min_delta_usd:
            return None
        if not snap.book.asks or not snap.book.bids:
            return None

        entry = snap.last_price + self._tick
        # Stop = на величину thrust × stop_atr_mult вниз (даємо рух дихати)
        stop_distance_pct = thrust * cfg.stop_atr_mult
        stop = entry * (1 - stop_distance_pct / 100.0)
        if stop >= entry:
            return None
        tp1, tp2, tp3 = compute_long_levels(
            entry=entry, stop=stop, tp_multipliers=cfg.tp_r_multipliers,
        )
        return SetupCandidate(
            setup_type=self.setup_type, direction=Direction.LONG,
            symbol=snap.symbol, timestamp_ms=snap.timestamp_ms,
            entry_price=entry, stop_price=stop,
            tp1_price=tp1, tp2_price=tp2, tp3_price=tp3,
            stop_distance_ticks=stop_distance_in_ticks(entry, stop, self._tick),
            invalidation_conditions=standard_invalidations(
                direction=Direction.LONG, stop_price=stop,
                opposite_delta_threshold=cfg.min_delta_usd * 1.5,
                expires_at_ms=snap.timestamp_ms + cfg.expiry_ms,
            ),
            features_snapshot=f,
        )


class MomentumBreakoutShort:
    setup_type = SetupType.MOMENTUM_BREAKOUT

    def __init__(self, config: MomentumBreakoutRuleConfig, *, tick_size: float = 0.1) -> None:
        self._cfg = config
        self._tick = tick_size

    def check(self, f: Features) -> SetupCandidate | None:
        cfg = self._cfg
        snap = f.snapshot
        thrust = -_price_thrust_pct(snap.tape.price_path, cfg.lookback_ms)
        if thrust < cfg.min_thrust_pct:
            return None
        if f.delta_10s > -cfg.min_delta_usd:
            return None
        if not snap.book.asks or not snap.book.bids:
            return None

        entry = snap.last_price - self._tick
        stop_distance_pct = thrust * cfg.stop_atr_mult
        stop = entry * (1 + stop_distance_pct / 100.0)
        if stop <= entry:
            return None
        tp1, tp2, tp3 = compute_short_levels(
            entry=entry, stop=stop, tp_multipliers=cfg.tp_r_multipliers,
        )
        return SetupCandidate(
            setup_type=self.setup_type, direction=Direction.SHORT,
            symbol=snap.symbol, timestamp_ms=snap.timestamp_ms,
            entry_price=entry, stop_price=stop,
            tp1_price=tp1, tp2_price=tp2, tp3_price=tp3,
            stop_distance_ticks=stop_distance_in_ticks(entry, stop, self._tick),
            invalidation_conditions=standard_invalidations(
                direction=Direction.SHORT, stop_price=stop,
                opposite_delta_threshold=cfg.min_delta_usd * 1.5,
                expires_at_ms=snap.timestamp_ms + cfg.expiry_ms,
            ),
            features_snapshot=f,
        )


__all__ = ["MomentumBreakoutLong", "MomentumBreakoutShort"]
