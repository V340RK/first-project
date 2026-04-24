"""FeatureEngine — обчислює `Features` з `MarketSnapshot`.

Майже-stateless: тримаємо лише `prev_book_top` per-symbol для absorption-детекції
та ring-buffer book-подій для spoof. Усе інше — pure functions.

Див. DOCS/architecture/04-feature-engine.md.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

from scalper.book.types import FootprintBar, OrderBookState
from scalper.features.config import FeatureConfig
from scalper.features.types import (
    Features,
    MarketSnapshot,
    PocLocation,
    PressureSide,
    PullbackState,
    Side,
)
from scalper.features.zones import ZoneRegistry


@dataclass
class _BookEvent:
    timestamp_ms: int
    side: Literal["BID", "ASK"]
    price: float
    size_delta: float


@dataclass
class _SymbolFeatureState:
    prev_book_top_bid_size: float = 0.0
    prev_book_top_ask_size: float = 0.0
    prev_book_top_bid_price: float = 0.0
    prev_book_top_ask_price: float = 0.0
    book_events: deque[_BookEvent] | None = None


class FeatureEngine:
    """Будує `Features` із `MarketSnapshot`. Один екземпляр на процес."""

    def __init__(
        self,
        config: FeatureConfig,
        zones: ZoneRegistry | None = None,
        *,
        tick_size_resolver: object | None = None,
    ) -> None:
        self._config = config
        self._zones = zones if zones is not None else ZoneRegistry()
        # Опційний об'єкт із .get_symbol_filters(symbol).tick_size — наприклад Gateway.
        self._tick_resolver = tick_size_resolver
        self._states: dict[str, _SymbolFeatureState] = {}

    def compute(self, snapshot: MarketSnapshot) -> Features:
        sym = snapshot.symbol.upper()
        state = self._states.get(sym)
        if state is None:
            state = _SymbolFeatureState(
                book_events=deque(maxlen=self._config.spoof.book_event_buffer),
            )
            self._states[sym] = state

        tick = self._tick_size(sym)

        imb5 = self._imbalance(snapshot.book, self._config.imbalance.levels_short)
        imb10 = self._imbalance(snapshot.book, self._config.imbalance.levels_long)
        wimb = self._weighted_imbalance(snapshot.book)
        pressure = self._pressure_side(imb5)

        burst_buy, burst_buy_usd = self._burst(snapshot, side="BUY")
        burst_sell, burst_sell_usd = self._burst(snapshot, side="SELL")
        burst_size = burst_buy_usd if burst_buy else burst_sell_usd

        absorption_score, absorption_side = self._score_absorption(snapshot, state, tick)
        spoof_score, spoof_side = self._score_spoof(snapshot, state)

        pullback = self._detect_pullback(snapshot, tick)
        cluster = self._cluster_features(snapshot, snapshot.footprint, tick)
        zone = self._zone_context(snapshot, tick)

        # Оновлюємо state ПІСЛЯ використання попереднього.
        self._update_state(snapshot, state)

        return Features(
            snapshot=snapshot,
            bid_ask_imbalance_5=imb5,
            bid_ask_imbalance_10=imb10,
            weighted_imbalance=wimb,
            book_pressure_side=pressure,
            delta_500ms=snapshot.tape.delta_500ms,
            delta_2s=snapshot.tape.delta_2s,
            delta_10s=snapshot.tape.delta_10s,
            cvd=snapshot.tape.cvd,
            aggressive_buy_burst=burst_buy,
            aggressive_sell_burst=burst_sell,
            burst_size_usd=burst_size,
            absorption_score=absorption_score,
            absorption_side=absorption_side,
            spoof_score=spoof_score,
            spoof_side=spoof_side,
            micro_pullback=pullback,
            **cluster,
            **zone,
        )

    # ============================================================
    # Internal — pure helpers
    # ============================================================

    def _tick_size(self, symbol: str) -> float:
        if self._tick_resolver is not None:
            try:
                f = self._tick_resolver.get_symbol_filters(symbol)  # type: ignore[attr-defined]
                ts = float(f.tick_size)
                if ts > 0:
                    return ts
            except Exception:
                pass
        return self._config.tick_size_default

    @staticmethod
    def _imbalance(book: OrderBookState, levels: int) -> float:
        bid_sum = sum(lvl.size for lvl in book.bids[:levels])
        ask_sum = sum(lvl.size for lvl in book.asks[:levels])
        total = bid_sum + ask_sum
        return (bid_sum - ask_sum) / total if total > 0 else 0.0

    @staticmethod
    def _weighted_imbalance(book: OrderBookState) -> float:
        bid_w = sum(lvl.size / (1 + i) for i, lvl in enumerate(book.bids))
        ask_w = sum(lvl.size / (1 + i) for i, lvl in enumerate(book.asks))
        total = bid_w + ask_w
        return (bid_w - ask_w) / total if total > 0 else 0.0

    def _pressure_side(self, imb5: float) -> PressureSide:
        thr = self._config.imbalance.pressure_threshold
        if imb5 >= thr:
            return "BID"
        if imb5 <= -thr:
            return "ASK"
        return "NEUTRAL"

    def _burst(
        self, snap: MarketSnapshot, *, side: Literal["BUY", "SELL"],
    ) -> tuple[bool, float | None]:
        thr_500 = self._config.burst.threshold_usd_500ms
        thr_2s = self._config.burst.threshold_usd_2s
        w500 = snap.tape.window_500ms
        w2s = snap.tape.window_2s
        flow_500 = w500.buy_volume_usd if side == "BUY" else w500.sell_volume_usd
        flow_2s = w2s.buy_volume_usd if side == "BUY" else w2s.sell_volume_usd
        is_burst = flow_500 >= thr_500 or flow_2s >= thr_2s
        return (is_burst, max(flow_500, flow_2s) if is_burst else None)

    def _score_absorption(
        self, snap: MarketSnapshot, state: _SymbolFeatureState, tick: float,
    ) -> tuple[float, Side]:
        cfg = self._config.absorption
        bid_score, ask_score = 0.0, 0.0
        delta = snap.tape.delta_500ms

        # Sell-агресія поглинута на bid: delta < 0, ціна не пробила best_bid, розмір не зник.
        if delta < -cfg.delta_threshold_usd and snap.book.bids:
            best_bid = snap.book.bids[0]
            if state.prev_book_top_bid_price == best_bid.price and state.prev_book_top_bid_size > 0:
                if best_bid.size >= cfg.book_top_size_retention * state.prev_book_top_bid_size:
                    if snap.last_price >= best_bid.price - tick:
                        bid_score = min(1.0, abs(delta) / cfg.full_score_delta_usd)

        # Buy-агресія поглинута на ask: delta > 0, ціна не пробила best_ask, розмір не зник.
        if delta > cfg.delta_threshold_usd and snap.book.asks:
            best_ask = snap.book.asks[0]
            if state.prev_book_top_ask_price == best_ask.price and state.prev_book_top_ask_size > 0:
                if best_ask.size >= cfg.book_top_size_retention * state.prev_book_top_ask_size:
                    if snap.last_price <= best_ask.price + tick:
                        ask_score = min(1.0, abs(delta) / cfg.full_score_delta_usd)

        if bid_score >= ask_score and bid_score > 0:
            return bid_score, "BID"
        if ask_score > 0:
            return ask_score, "ASK"
        return 0.0, "NONE"

    def _score_spoof(
        self, snap: MarketSnapshot, state: _SymbolFeatureState,
    ) -> tuple[float, Side]:
        """Спрощено: дивимось на поточний стан book-events ring buffer.

        Заглушка — тримаємо контракт `0..1, side`. Реальна логіка вимагає історії
        cancel-подій, чого зараз немає в Gateway. Score=0 безпечне дефолт-значення.
        TODO(spoof): підключити book-event tracker із Gateway diff-stream.
        """
        return 0.0, "NONE"

    def _detect_pullback(
        self, snap: MarketSnapshot, tick: float,
    ) -> PullbackState | None:
        cfg = self._config.micro_pullback
        path = snap.tape.price_path
        if len(path) < 3 or tick <= 0:
            return None
        cutoff = snap.timestamp_ms - cfg.impulse_window_ms
        recent = [(ts, p) for ts, p in path if ts >= cutoff]
        if len(recent) < 3:
            return None

        first_price = recent[0][1]
        peak = max(p for _, p in recent)
        trough = min(p for _, p in recent)
        last_price = snap.last_price

        up_size = (peak - first_price) / tick
        down_size = (first_price - trough) / tick

        if up_size >= cfg.impulse_min_ticks and up_size >= down_size:
            depth = (peak - last_price) / tick
            if 1 <= depth <= up_size * cfg.pullback_max_fraction:
                counter = snap.tape.delta_2s
                if counter > -cfg.weak_counter_delta_usd:
                    return PullbackState(
                        direction="LONG_PULLBACK",
                        depth_ticks=int(round(depth)),
                        bars_in_pullback=sum(1 for _, p in recent if p < peak),
                        delta_during_pullback=counter,
                    )
        if down_size >= cfg.impulse_min_ticks and down_size > up_size:
            depth = (last_price - trough) / tick
            if 1 <= depth <= down_size * cfg.pullback_max_fraction:
                counter = snap.tape.delta_2s
                if counter < cfg.weak_counter_delta_usd:
                    return PullbackState(
                        direction="SHORT_PULLBACK",
                        depth_ticks=int(round(depth)),
                        bars_in_pullback=sum(1 for _, p in recent if p > trough),
                        delta_during_pullback=counter,
                    )
        return None

    def _cluster_features(
        self, snap: MarketSnapshot, footprint: FootprintBar | None, tick: float,
    ) -> dict:
        empty = {
            "poc_offset_ticks": 0,
            "poc_location": "MID",
            "stacked_imbalance_long": False,
            "stacked_imbalance_short": False,
            "bar_finished": False,
            "bar_delta": 0.0,
        }
        if footprint is None or footprint.poc_price is None or tick <= 0:
            return empty
        if footprint.high == 0.0 and footprint.low == 0.0:
            return empty
        midpoint = (footprint.high + footprint.low) / 2
        poc_offset = int(round((footprint.poc_price - midpoint) / tick))
        thr = self._config.cluster.poc_mid_threshold_ticks
        location: PocLocation
        if abs(poc_offset) <= thr:
            location = "MID"
        elif poc_offset > 0:
            location = "HIGH"
        else:
            location = "LOW"

        stacked_long = self._has_stacked(footprint, side="ASK")
        stacked_short = self._has_stacked(footprint, side="BID")
        return {
            "poc_offset_ticks": poc_offset,
            "poc_location": location,
            "stacked_imbalance_long": stacked_long,
            "stacked_imbalance_short": stacked_short,
            "bar_finished": footprint.is_closed,
            "bar_delta": footprint.delta,
        }

    def _has_stacked(
        self, footprint: FootprintBar, *, side: Literal["BID", "ASK"],
    ) -> bool:
        """≥N послідовних рівнів, де ask_vol/bid_vol > ratio (для ASK side) або обернено."""
        ratio = 2.5  # дефолт; реальний — з OBClusterConfig, але FE сам не знає про нього
        min_count = self._config.cluster.stacked_min_count
        prices = sorted(footprint.levels.keys())
        if len(prices) < min_count:
            return False
        run = 0
        for p in prices:
            lv = footprint.levels[p]
            if side == "ASK":
                ok = lv.bid_vol > 0 and lv.ask_vol >= ratio * lv.bid_vol
            else:
                ok = lv.ask_vol > 0 and lv.bid_vol >= ratio * lv.ask_vol
            if ok:
                run += 1
                if run >= min_count:
                    return True
            else:
                run = 0
        return False

    def _zone_context(self, snap: MarketSnapshot, tick: float) -> dict:
        sym = snap.symbol
        price = snap.last_price
        inside = self._zones.find_containing(sym, price)
        if inside is not None:
            return {
                "in_htf_poi": True,
                "htf_poi_type": inside.type,
                "htf_poi_side": inside.side,
                "distance_to_poi_ticks": 0,
            }
        max_dist = self._config.zones.nearest_max_distance_ticks * (tick if tick > 0 else 1.0)
        nearest = self._zones.find_nearest(sym, price, max_dist)
        if nearest is None:
            return {
                "in_htf_poi": False,
                "htf_poi_type": None,
                "htf_poi_side": None,
                "distance_to_poi_ticks": None,
            }
        edge = nearest.price_low if abs(nearest.price_low - price) < abs(nearest.price_high - price) else nearest.price_high
        dist_ticks = int(round(abs(edge - price) / tick)) if tick > 0 else 0
        return {
            "in_htf_poi": False,
            "htf_poi_type": nearest.type,
            "htf_poi_side": nearest.side,
            "distance_to_poi_ticks": dist_ticks,
        }

    @staticmethod
    def _update_state(snap: MarketSnapshot, state: _SymbolFeatureState) -> None:
        if snap.book.bids:
            state.prev_book_top_bid_price = snap.book.bids[0].price
            state.prev_book_top_bid_size = snap.book.bids[0].size
        if snap.book.asks:
            state.prev_book_top_ask_price = snap.book.asks[0].price
            state.prev_book_top_ask_size = snap.book.asks[0].size


__all__ = ["FeatureEngine"]
