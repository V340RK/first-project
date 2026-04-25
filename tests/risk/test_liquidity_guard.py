"""RiskEngine — walk-the-book liquidity guard (opt-in)."""

from __future__ import annotations

import pytest

from scalper.book.types import OrderBookLevel, OrderBookState
from scalper.common.enums import Direction, Regime, SetupType
from scalper.common.types import (
    InvalidationCondition,
    InvalidationKind,
    SetupCandidate,
    TradePlan,
)
from scalper.features.types import Features, MarketSnapshot
from scalper.risk import RiskConfig, RiskEngine
from scalper.tape.types import TapeWindow, TapeWindowsState


def _empty_win(d: int) -> TapeWindow:
    return TapeWindow(
        duration_ms=d, trade_count=0, buy_volume_qty=0, sell_volume_qty=0,
        buy_volume_usd=0, sell_volume_usd=0,
        delta_qty=0, delta_usd=0, last_trade_price=0, first_trade_ms=0, last_trade_ms=0,
    )


def _features_with_book(
    *, asks: list[tuple[float, float]], bids: list[tuple[float, float]],
) -> Features:
    book = OrderBookState(
        symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
        bids=[OrderBookLevel(p, s) for p, s in bids],
        asks=[OrderBookLevel(p, s) for p, s in asks],
        is_synced=True,
    )
    tape = TapeWindowsState(
        symbol="BTCUSDT", timestamp_ms=0,
        window_500ms=_empty_win(500), window_2s=_empty_win(2000),
        window_10s=_empty_win(10_000),
        cvd=0, cvd_reliable=True, delta_500ms=0, delta_2s=0, delta_10s=0,
        price_path=[],
    )
    snap = MarketSnapshot(
        timestamp_ms=1000, symbol="BTCUSDT", book=book, tape=tape,
        last_price=asks[0][0] if asks else 100.0, spread_ticks=1,
    )
    return Features(
        snapshot=snap,
        bid_ask_imbalance_5=0.0, bid_ask_imbalance_10=0.0,
        weighted_imbalance=0.0, book_pressure_side="NEUTRAL",
        delta_500ms=0, delta_2s=0, delta_10s=0, cvd=0,
        aggressive_buy_burst=False, aggressive_sell_burst=False, burst_size_usd=None,
        absorption_score=0, absorption_side="NONE",   # type: ignore[arg-type]
        spoof_score=0, spoof_side="NONE", micro_pullback=None,
        poc_offset_ticks=0, poc_location="MID",
        stacked_imbalance_long=False, stacked_imbalance_short=False,
        bar_finished=False, bar_delta=0,
        in_htf_poi=False, htf_poi_type=None, htf_poi_side=None,
        distance_to_poi_ticks=None,
    )


def _plan_with_book(
    *, entry: float = 100.0, stop: float = 99.5,
    asks: list[tuple[float, float]] | None = None,
    bids: list[tuple[float, float]] | None = None,
) -> TradePlan:
    feat = _features_with_book(
        asks=asks or [(100.1, 1000.0), (100.2, 1000.0)],
        bids=bids or [(100.0, 1000.0), (99.9, 1000.0)],
    )
    cand = SetupCandidate(
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=Direction.LONG,
        symbol="BTCUSDT", timestamp_ms=1000,
        entry_price=entry, stop_price=stop,
        tp1_price=entry + 0.5, tp2_price=entry + 1.0, tp3_price=entry + 1.5,
        stop_distance_ticks=int(abs(entry - stop) / 0.1),
        invalidation_conditions=[
            InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={}),
        ],
        features_snapshot=feat,
    )
    return TradePlan(
        candidate=cand, setup_type=cand.setup_type, direction=cand.direction,
        symbol=cand.symbol, timestamp_ms=1000,
        entry_price=entry, stop_price=stop,
        tp1_price=entry + 0.5, tp2_price=entry + 1.0, tp3_price=entry + 1.5,
        stop_distance_ticks=cand.stop_distance_ticks,
        score=1.5, score_threshold=1.0, regime=Regime.NORMAL_BALANCED,
        expectancy_multiplier=1.0,
        invalidation_conditions=cand.invalidation_conditions, time_stop_ms=None,
    )


# ============================================================
# qty cap from book consumption
# ============================================================

def test_thin_book_caps_qty_to_consumption_pct() -> None:
    """5 levels по 1 BTC = 5 BTC total. max_consumption=20% → cap=1 BTC.
    Margin sizing хотів би більше — обмежимо."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5,
        max_book_consumption_pct=20.0, book_depth_levels=5,
        max_concurrent_positions=99, fallback_max_qty=1000.0,
    )
    re = RiskEngine(cfg)
    # margin 10% з 10000 = $1000, leverage 5 → notional $5000, qty=50 на price=100
    # без guard. З guard: total top5 = 5 BTC, 20% = 1 BTC → cap до 1.
    plan = _plan_with_book(
        entry=100.0, stop=99.5,
        asks=[(100.1, 1.0), (100.2, 1.0), (100.3, 1.0), (100.4, 1.0), (100.5, 1.0)],
    )
    dec = re.evaluate(plan, equity_usd=10_000)
    assert dec.plan is not None
    assert dec.plan.position_size == pytest.approx(1.0, abs=0.01)


def test_deep_book_does_not_cap() -> None:
    """Якщо top-5 levels мають 10000 BTC, 20% = 2000 BTC — наша qty 50 проходить."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5,
        max_book_consumption_pct=20.0, book_depth_levels=5,
        max_concurrent_positions=99, fallback_max_qty=10_000.0,
    )
    re = RiskEngine(cfg)
    plan = _plan_with_book(
        entry=100.0, stop=99.5,
        asks=[(100.1, 10000.0)] * 5,
    )
    dec = re.evaluate(plan, equity_usd=10_000)
    assert dec.plan is not None
    assert dec.plan.position_size == pytest.approx(50.0, abs=0.5)


# ============================================================
# slippage reject
# ============================================================

def test_huge_slippage_rejects_position() -> None:
    """Книжка з великими gap'ами між levels — qty cap дасть avg price >> best."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5,
        max_book_consumption_pct=100.0,   # cap не блокує
        max_expected_slippage_ticks=5,    # дозволяємо 5 тіків
        book_depth_levels=5,
        max_concurrent_positions=99, fallback_max_qty=10_000.0,
    )
    re = RiskEngine(cfg)
    # asks: 100.1 small, потім дикі стрибки до 105, 110, 115, 120
    plan = _plan_with_book(
        entry=100.0, stop=99.5,
        asks=[(100.1, 0.01), (105.0, 1.0), (110.0, 1.0), (115.0, 1.0), (120.0, 1.0)],
    )
    dec = re.evaluate(plan, equity_usd=10_000)
    assert dec.plan is None
    assert "expected_slippage" in dec.reason


def test_no_book_data_falls_through() -> None:
    """Якщо features_snapshot немає (paper тести) — guard skipped, не падаємо."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5,
        max_book_consumption_pct=20.0, max_expected_slippage_ticks=5,
        max_concurrent_positions=99, fallback_max_qty=10_000.0,
    )
    re = RiskEngine(cfg)
    cand = SetupCandidate(
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=Direction.LONG,
        symbol="BTCUSDT", timestamp_ms=1000,
        entry_price=100.0, stop_price=99.5,
        tp1_price=100.5, tp2_price=101.0, tp3_price=101.5,
        stop_distance_ticks=5,
        invalidation_conditions=[
            InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={}),
        ],
        features_snapshot=None,   # type: ignore[arg-type]
    )
    plan = TradePlan(
        candidate=cand, setup_type=cand.setup_type, direction=cand.direction,
        symbol=cand.symbol, timestamp_ms=1000,
        entry_price=100.0, stop_price=99.5,
        tp1_price=100.5, tp2_price=101.0, tp3_price=101.5,
        stop_distance_ticks=5,
        score=1.5, score_threshold=1.0, regime=Regime.NORMAL_BALANCED,
        expectancy_multiplier=1.0,
        invalidation_conditions=cand.invalidation_conditions, time_stop_ms=None,
    )
    dec = re.evaluate(plan, equity_usd=10_000)
    assert dec.plan is not None   # не падаємо


def test_short_walks_bid_side() -> None:
    """SHORT entry — walk по bids, не по asks."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5,
        max_book_consumption_pct=20.0,
        max_concurrent_positions=99, fallback_max_qty=10_000.0,
    )
    re = RiskEngine(cfg)
    # Asks товсті, bids тонкі
    feat = _features_with_book(
        asks=[(100.1, 10000.0)] * 5,
        bids=[(100.0, 1.0)] * 5,
    )
    cand = SetupCandidate(
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=Direction.SHORT,
        symbol="BTCUSDT", timestamp_ms=1000,
        entry_price=100.0, stop_price=100.5,
        tp1_price=99.5, tp2_price=99.0, tp3_price=98.5,
        stop_distance_ticks=5,
        invalidation_conditions=[
            InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={}),
        ],
        features_snapshot=feat,
    )
    plan = TradePlan(
        candidate=cand, setup_type=cand.setup_type, direction=cand.direction,
        symbol=cand.symbol, timestamp_ms=1000,
        entry_price=100.0, stop_price=100.5,
        tp1_price=99.5, tp2_price=99.0, tp3_price=98.5,
        stop_distance_ticks=5,
        score=1.5, score_threshold=1.0, regime=Regime.NORMAL_BALANCED,
        expectancy_multiplier=1.0,
        invalidation_conditions=cand.invalidation_conditions, time_stop_ms=None,
    )
    dec = re.evaluate(plan, equity_usd=10_000)
    assert dec.plan is not None
    # bids = 5 BTC total, 20% = 1 BTC
    assert dec.plan.position_size == pytest.approx(1.0, abs=0.01)


def test_guard_off_by_default() -> None:
    """Default config без guard — qty unrestricted by liquidity."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5,
        max_concurrent_positions=99, fallback_max_qty=1000.0,
    )
    re = RiskEngine(cfg)
    plan = _plan_with_book(
        entry=100.0, stop=99.5,
        asks=[(100.1, 0.001)] * 5,   # дуже тонко
    )
    dec = re.evaluate(plan, equity_usd=1000.0)
    assert dec.plan is not None
    assert dec.plan.position_size > 1.0   # з margin 10% з 1000, leverage 5 → 50/100 = 5 (без cap)
