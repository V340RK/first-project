"""RiskEngine — fixed % stop-loss override (per-slot user setting)."""

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


def _features() -> Features:
    book = OrderBookState(
        symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
        bids=[OrderBookLevel(100.0, 100.0)],
        asks=[OrderBookLevel(100.1, 100.0)], is_synced=True,
    )
    tape = TapeWindowsState(
        symbol="BTCUSDT", timestamp_ms=0,
        window_500ms=_empty_win(500), window_2s=_empty_win(2000),
        window_10s=_empty_win(10_000),
        cvd=0, cvd_reliable=True, delta_500ms=0, delta_2s=0, delta_10s=0, price_path=[],
    )
    snap = MarketSnapshot(
        timestamp_ms=1000, symbol="BTCUSDT", book=book, tape=tape,
        last_price=100.05, spread_ticks=1,
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
        in_htf_poi=False, htf_poi_type=None, htf_poi_side=None, distance_to_poi_ticks=None,
    )


def _plan_long() -> TradePlan:
    cand = SetupCandidate(
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=Direction.LONG,
        symbol="BTCUSDT", timestamp_ms=1000,
        entry_price=100.0, stop_price=98.0,   # structure-based: 2% дальній SL
        tp1_price=104.0, tp2_price=110.0, tp3_price=120.0,
        stop_distance_ticks=200,
        invalidation_conditions=[
            InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={}),
        ],
        features_snapshot=_features(),
    )
    return TradePlan(
        candidate=cand, setup_type=cand.setup_type, direction=cand.direction,
        symbol=cand.symbol, timestamp_ms=1000,
        entry_price=100.0, stop_price=98.0,
        tp1_price=104.0, tp2_price=110.0, tp3_price=120.0,
        stop_distance_ticks=200,
        score=1.5, score_threshold=1.0, regime=Regime.NORMAL_BALANCED,
        expectancy_multiplier=1.0,
        invalidation_conditions=cand.invalidation_conditions, time_stop_ms=None,
    )


def _plan_short() -> TradePlan:
    cand = SetupCandidate(
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=Direction.SHORT,
        symbol="BTCUSDT", timestamp_ms=1000,
        entry_price=100.0, stop_price=102.0,
        tp1_price=96.0, tp2_price=92.0, tp3_price=88.0,
        stop_distance_ticks=200,
        invalidation_conditions=[
            InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={}),
        ],
        features_snapshot=_features(),
    )
    return TradePlan(
        candidate=cand, setup_type=cand.setup_type, direction=cand.direction,
        symbol=cand.symbol, timestamp_ms=1000,
        entry_price=100.0, stop_price=102.0,
        tp1_price=96.0, tp2_price=92.0, tp3_price=88.0,
        stop_distance_ticks=200,
        score=1.5, score_threshold=1.0, regime=Regime.NORMAL_BALANCED,
        expectancy_multiplier=1.0,
        invalidation_conditions=cand.invalidation_conditions, time_stop_ms=None,
    )


def test_stop_loss_pct_override_long_recomputes_stop_and_tps() -> None:
    """stop_loss_pct=2 → SL = entry × 0.98, TPs = 1R/2R/3R від нового stop."""
    cfg = RiskConfig(
        stop_loss_pct=2.0,
        margin_per_trade_pct=10.0, leverage=5,
        max_concurrent_positions=99, fallback_max_qty=10000.0,
    )
    re = RiskEngine(cfg)
    dec = re.evaluate(_plan_long(), equity_usd=10_000)
    assert dec.plan is not None
    p = dec.plan
    # entry=100, SL = 100 * 0.98 = 98.0 (R=2.0)
    assert p.stop_price == pytest.approx(98.0, abs=0.001)
    # TP1=1R=102, TP2=2R=104, TP3=3R=106
    assert p.tp1_price == pytest.approx(102.0, abs=0.001)
    assert p.tp2_price == pytest.approx(104.0, abs=0.001)
    assert p.tp3_price == pytest.approx(106.0, abs=0.001)


def test_stop_loss_pct_override_short_mirror() -> None:
    cfg = RiskConfig(
        stop_loss_pct=2.0,
        margin_per_trade_pct=10.0, leverage=5,
        max_concurrent_positions=99, fallback_max_qty=10000.0,
    )
    re = RiskEngine(cfg)
    dec = re.evaluate(_plan_short(), equity_usd=10_000)
    assert dec.plan is not None
    p = dec.plan
    # entry=100, SL = 100 * 1.02 = 102.0
    assert p.stop_price == pytest.approx(102.0, abs=0.001)
    # TP1=98, TP2=96, TP3=94
    assert p.tp1_price == pytest.approx(98.0, abs=0.001)
    assert p.tp2_price == pytest.approx(96.0, abs=0.001)
    assert p.tp3_price == pytest.approx(94.0, abs=0.001)


def test_stop_loss_pct_none_keeps_setup_stop() -> None:
    """Default config (stop_loss_pct=None) — структурний stop з setup-detector зберігається."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5,
        max_concurrent_positions=99, fallback_max_qty=10000.0,
    )
    re = RiskEngine(cfg)
    plan_orig = _plan_long()
    dec = re.evaluate(plan_orig, equity_usd=10_000)
    assert dec.plan is not None
    # stop і TPs ті самі як setup-detector згенерував
    assert dec.plan.stop_price == plan_orig.stop_price
    assert dec.plan.tp1_price == plan_orig.tp1_price


def test_stop_loss_pct_changes_qty_in_risk_usd_mode() -> None:
    """У R-based sizing вузький stop = більший qty (на той же risk_usd)."""
    cfg = RiskConfig(
        stop_loss_pct=1.0,
        risk_per_trade_usd_abs=10.0,
        max_concurrent_positions=99, fallback_max_qty=10000.0,
    )
    re = RiskEngine(cfg)
    dec = re.evaluate(_plan_long(), equity_usd=10_000)
    assert dec.plan is not None
    # SL=99.0, R=1.0 + buffer=0.1 → effective_distance≈1.1
    # qty = 10 / 1.1 ≈ 9.09. round до step 0.001 → 9.090
    assert dec.plan.position_size > 5.0  # значно більше ніж було при stop=98


def test_stop_loss_pct_validation_rejects_too_large() -> None:
    """50% — макс. 51% → ValidationError."""
    with pytest.raises(Exception):
        RiskConfig(stop_loss_pct=51.0)
