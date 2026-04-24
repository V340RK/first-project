"""FeatureEngine — pure-function unit tests."""

from __future__ import annotations

import pytest

from scalper.book.types import FootprintBar, LevelVolume, OrderBookLevel, OrderBookState
from scalper.features.config import FeatureConfig
from scalper.features.engine import FeatureEngine
from scalper.features.types import MarketSnapshot
from scalper.features.zones import HtfZone, ZoneRegistry
from scalper.tape.types import TapeWindow, TapeWindowsState


def _book(*, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> OrderBookState:
    return OrderBookState(
        symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
        bids=[OrderBookLevel(p, s) for p, s in bids],
        asks=[OrderBookLevel(p, s) for p, s in asks],
        is_synced=True,
    )


def _empty_window(duration_ms: int) -> TapeWindow:
    return TapeWindow(
        duration_ms=duration_ms, trade_count=0,
        buy_volume_qty=0.0, sell_volume_qty=0.0,
        buy_volume_usd=0.0, sell_volume_usd=0.0,
        delta_qty=0.0, delta_usd=0.0,
        last_trade_price=0.0, first_trade_ms=0, last_trade_ms=0,
    )


def _tape(
    *, buy_500=0.0, sell_500=0.0, buy_2s=0.0, sell_2s=0.0,
    cvd=0.0, price_path=None,
) -> TapeWindowsState:
    w500 = TapeWindow(
        duration_ms=500, trade_count=int((buy_500 + sell_500) > 0),
        buy_volume_qty=buy_500, sell_volume_qty=sell_500,
        buy_volume_usd=buy_500, sell_volume_usd=sell_500,
        delta_qty=buy_500 - sell_500, delta_usd=buy_500 - sell_500,
        last_trade_price=0.0, first_trade_ms=0, last_trade_ms=0,
    )
    w2s = TapeWindow(
        duration_ms=2000, trade_count=int((buy_2s + sell_2s) > 0),
        buy_volume_qty=buy_2s, sell_volume_qty=sell_2s,
        buy_volume_usd=buy_2s, sell_volume_usd=sell_2s,
        delta_qty=buy_2s - sell_2s, delta_usd=buy_2s - sell_2s,
        last_trade_price=0.0, first_trade_ms=0, last_trade_ms=0,
    )
    return TapeWindowsState(
        symbol="BTCUSDT", timestamp_ms=1000,
        window_500ms=w500, window_2s=w2s, window_10s=_empty_window(10_000),
        cvd=cvd, cvd_reliable=True,
        delta_500ms=buy_500 - sell_500,
        delta_2s=buy_2s - sell_2s,
        delta_10s=0.0,
        price_path=price_path or [],
    )


def _snap(book: OrderBookState, tape: TapeWindowsState, *, last_price: float = 100.0,
          footprint: FootprintBar | None = None, ts: int = 1000) -> MarketSnapshot:
    return MarketSnapshot(
        timestamp_ms=ts, symbol="BTCUSDT",
        book=book, tape=tape, last_price=last_price, spread_ticks=1,
        footprint=footprint,
    )


def _engine(**cfg) -> FeatureEngine:
    return FeatureEngine(FeatureConfig(tick_size_default=0.1, **cfg))


# ============================================================
# Imbalance
# ============================================================

def test_imbalance_balanced_returns_zero() -> None:
    book = _book(bids=[(100.0, 5.0), (99.9, 5.0)], asks=[(100.1, 5.0), (100.2, 5.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape()))
    assert f.bid_ask_imbalance_5 == 0.0
    assert f.book_pressure_side == "NEUTRAL"


def test_imbalance_bid_heavy() -> None:
    book = _book(bids=[(100.0, 90.0), (99.9, 10.0)], asks=[(100.1, 5.0), (100.2, 5.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape()))
    assert f.bid_ask_imbalance_5 > 0.5
    assert f.book_pressure_side == "BID"


def test_imbalance_ask_heavy() -> None:
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 90.0), (100.2, 10.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape()))
    assert f.bid_ask_imbalance_5 < -0.5
    assert f.book_pressure_side == "ASK"


def test_imbalance_empty_book_safe() -> None:
    book = _book(bids=[], asks=[])
    fe = _engine()
    f = fe.compute(_snap(book, _tape()))
    assert f.bid_ask_imbalance_5 == 0.0
    assert f.weighted_imbalance == 0.0


def test_weighted_imbalance_favors_near_levels() -> None:
    # Маленький near-best bid, великий ask далі — weighted має любити bid менше.
    book = _book(bids=[(100.0, 1.0), (99.0, 100.0)], asks=[(100.1, 1.0), (101.0, 100.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape()))
    # weighted_imbalance близько 0 бо near-best симетричні
    assert abs(f.weighted_imbalance) < 0.4


# ============================================================
# Burst
# ============================================================

def test_burst_buy_when_volume_above_threshold() -> None:
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape(buy_500=60_000)))
    assert f.aggressive_buy_burst is True
    assert f.aggressive_sell_burst is False
    assert f.burst_size_usd == 60_000


def test_burst_sell_when_volume_above_threshold() -> None:
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape(sell_500=60_000)))
    assert f.aggressive_sell_burst is True
    assert f.burst_size_usd == 60_000


def test_no_burst_below_threshold() -> None:
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape(buy_500=10_000, sell_500=10_000)))
    assert f.aggressive_buy_burst is False
    assert f.aggressive_sell_burst is False
    assert f.burst_size_usd is None


# ============================================================
# Absorption
# ============================================================

def test_absorption_first_call_returns_zero() -> None:
    book = _book(bids=[(100.0, 50.0)], asks=[(100.1, 50.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape(sell_500=50_000), last_price=100.0))
    assert f.absorption_score == 0.0
    assert f.absorption_side == "NONE"


def test_absorption_bid_when_sell_pressure_does_not_break_book() -> None:
    fe = _engine()
    book = _book(bids=[(100.0, 50.0)], asks=[(100.1, 5.0)])
    # Перший виклик — заповнити state
    fe.compute(_snap(book, _tape(), last_price=100.0))
    # Другий — сильна sell-агресія, ціна тримається, розмір не зник
    f = fe.compute(_snap(book, _tape(sell_500=80_000), last_price=100.0))
    assert f.absorption_side == "BID"
    assert f.absorption_score > 0


def test_absorption_no_score_when_book_top_disappears() -> None:
    fe = _engine()
    book1 = _book(bids=[(100.0, 50.0)], asks=[(100.1, 5.0)])
    fe.compute(_snap(book1, _tape(), last_price=100.0))
    # Розмір на best упав значно — абсорбції не було
    book2 = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    f = fe.compute(_snap(book2, _tape(sell_500=80_000), last_price=100.0))
    assert f.absorption_score == 0.0


# ============================================================
# Cluster
# ============================================================

def test_cluster_no_footprint_returns_defaults() -> None:
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape()))
    assert f.poc_offset_ticks == 0
    assert f.poc_location == "MID"
    assert f.bar_finished is False


def test_cluster_poc_high() -> None:
    fp = FootprintBar(
        symbol="BTCUSDT", timeframe="1m",
        open_time_ms=0, close_time_ms=60_000,
        open=100.0, high=101.0, low=99.0, close=100.5,
        levels={
            99.0: LevelVolume(bid_vol=10, ask_vol=2),
            100.0: LevelVolume(bid_vol=5, ask_vol=5),
            100.95: LevelVolume(bid_vol=3, ask_vol=30),  # PoC, near high
        },
        poc_price=100.95,
        delta=20.0, trade_count=10, is_closed=True,
    )
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape(), footprint=fp))
    assert f.poc_location == "HIGH"
    assert f.poc_offset_ticks > 0
    assert f.bar_finished is True
    assert f.bar_delta == 20.0


def test_cluster_stacked_imbalance_long() -> None:
    fp = FootprintBar(
        symbol="BTCUSDT", timeframe="1m",
        open_time_ms=0, close_time_ms=60_000,
        open=100.0, high=100.4, low=100.0, close=100.4,
        levels={
            100.0: LevelVolume(bid_vol=1, ask_vol=10),
            100.1: LevelVolume(bid_vol=1, ask_vol=10),
            100.2: LevelVolume(bid_vol=1, ask_vol=10),
            100.3: LevelVolume(bid_vol=1, ask_vol=10),
        },
        poc_price=100.0,
        delta=36.0, trade_count=4, is_closed=True,
    )
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    fe = _engine()
    f = fe.compute(_snap(book, _tape(), footprint=fp))
    assert f.stacked_imbalance_long is True
    assert f.stacked_imbalance_short is False


# ============================================================
# Pullback
# ============================================================

def test_pullback_long_after_up_impulse() -> None:
    # Імпульс +8 тіків (0.8 USD), потім відкат 4 тіки (50%)
    path = [
        (1, 100.0), (50, 100.2), (100, 100.4),
        (200, 100.6), (400, 100.8),  # peak
        (600, 100.6), (800, 100.4),  # pullback to 100.4
    ]
    fe = _engine()
    book = _book(bids=[(100.4, 5.0)], asks=[(100.5, 5.0)])
    f = fe.compute(_snap(book, _tape(price_path=path), last_price=100.4, ts=900))
    assert f.micro_pullback is not None
    assert f.micro_pullback.direction == "LONG_PULLBACK"
    assert f.micro_pullback.depth_ticks >= 3


def test_pullback_none_when_no_impulse() -> None:
    path = [(1, 100.0), (100, 100.05), (200, 100.0), (300, 100.05)]
    fe = _engine()
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    f = fe.compute(_snap(book, _tape(price_path=path), last_price=100.05, ts=400))
    assert f.micro_pullback is None


# ============================================================
# Zones
# ============================================================

def test_zone_in_htf_poi() -> None:
    zr = ZoneRegistry()
    zr.replace("BTCUSDT", [
        HtfZone(type="FVG", price_low=99.5, price_high=100.5, side="SUPPORT",
                timeframe="1h", created_at_ms=0),
    ])
    fe = FeatureEngine(FeatureConfig(tick_size_default=0.1), zones=zr)
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    f = fe.compute(_snap(book, _tape(), last_price=100.0))
    assert f.in_htf_poi is True
    assert f.htf_poi_type == "FVG"
    assert f.htf_poi_side == "SUPPORT"
    assert f.distance_to_poi_ticks == 0


def test_zone_nearest_when_outside() -> None:
    zr = ZoneRegistry()
    zr.replace("BTCUSDT", [
        HtfZone(type="OB", price_low=101.0, price_high=101.5, side="RESISTANCE",
                timeframe="1h", created_at_ms=0),
    ])
    fe = FeatureEngine(FeatureConfig(tick_size_default=0.1), zones=zr)
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    f = fe.compute(_snap(book, _tape(), last_price=100.0))
    assert f.in_htf_poi is False
    assert f.htf_poi_type == "OB"
    assert f.distance_to_poi_ticks is not None
    assert f.distance_to_poi_ticks > 0


def test_zone_empty_registry() -> None:
    fe = _engine()
    book = _book(bids=[(100.0, 5.0)], asks=[(100.1, 5.0)])
    f = fe.compute(_snap(book, _tape(), last_price=100.0))
    assert f.in_htf_poi is False
    assert f.htf_poi_type is None
    assert f.distance_to_poi_ticks is None


# ============================================================
# Symmetry / property
# ============================================================

@pytest.mark.parametrize("levels", [3, 5, 10])
def test_imbalance_in_valid_range(levels: int) -> None:
    book = _book(
        bids=[(100.0 - 0.1 * i, 5.0 + i) for i in range(levels)],
        asks=[(100.1 + 0.1 * i, 3.0 + i) for i in range(levels)],
    )
    fe = _engine()
    f = fe.compute(_snap(book, _tape()))
    assert -1.0 <= f.bid_ask_imbalance_5 <= 1.0
    assert -1.0 <= f.bid_ask_imbalance_10 <= 1.0
    assert -1.0 <= f.weighted_imbalance <= 1.0
