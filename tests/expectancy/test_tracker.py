"""ExpectancyTracker — Wilson CI, snapshot, auto-suspend, resume, idempotency."""

from __future__ import annotations

import pytest

from scalper.common.enums import Direction, SetupType
from scalper.common.types import (
    InvalidationCondition,
    InvalidationKind,
    SetupCandidate,
    TradePlan,
)
from scalper.common.enums import Regime
from scalper.book.types import OrderBookLevel, OrderBookState
from scalper.expectancy import ExpectancyConfig, ExpectancyTracker, wilson_ci
from scalper.features.types import Features, MarketSnapshot
from scalper.risk import TradeOutcome
from scalper.tape.types import TapeWindow, TapeWindowsState


# ============================================================
# Wilson CI
# ============================================================

def test_wilson_ci_zero_samples() -> None:
    lo, hi = wilson_ci(0, 0)
    assert (lo, hi) == (0.0, 1.0)


def test_wilson_ci_half_wins() -> None:
    lo, hi = wilson_ci(10, 20)
    # Normal approx: 0.5 ± 1.96*0.112 = [0.28..0.72]; Wilson консервативніший, центр трохи інший
    assert 0.25 < lo < 0.35
    assert 0.65 < hi < 0.75


def test_wilson_ci_all_wins_small_sample() -> None:
    lo, hi = wilson_ci(3, 3)
    assert hi == pytest.approx(1.0, abs=1e-6)
    assert lo < 0.55   # не впевнено 100%


def test_wilson_ci_bounds_property() -> None:
    for s, n in [(1, 10), (5, 5), (0, 50), (49, 50)]:
        lo, hi = wilson_ci(s, n)
        assert 0.0 <= lo <= hi <= 1.0


# ============================================================
# Helpers
# ============================================================

def _empty_win(d: int) -> TapeWindow:
    return TapeWindow(
        duration_ms=d, trade_count=0, buy_volume_qty=0, sell_volume_qty=0,
        buy_volume_usd=0, sell_volume_usd=0,
        delta_qty=0, delta_usd=0, last_trade_price=0, first_trade_ms=0, last_trade_ms=0,
    )


def _features() -> Features:
    book = OrderBookState(
        symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
        bids=[OrderBookLevel(100.0, 10.0)], asks=[OrderBookLevel(100.1, 10.0)], is_synced=True,
    )
    tape = TapeWindowsState(
        symbol="BTCUSDT", timestamp_ms=0,
        window_500ms=_empty_win(500), window_2s=_empty_win(2000), window_10s=_empty_win(10_000),
        cvd=0, cvd_reliable=True, delta_500ms=0, delta_2s=0, delta_10s=0, price_path=[],
    )
    snap = MarketSnapshot(
        timestamp_ms=0, symbol="BTCUSDT", book=book, tape=tape,
        last_price=100.0, spread_ticks=1,
    )
    return Features(
        snapshot=snap,
        bid_ask_imbalance_5=0.0, bid_ask_imbalance_10=0.0,
        weighted_imbalance=0.0, book_pressure_side="NEUTRAL",
        delta_500ms=0, delta_2s=0, delta_10s=0, cvd=0,
        aggressive_buy_burst=False, aggressive_sell_burst=False, burst_size_usd=None,
        absorption_score=0, absorption_side="NONE",  # type: ignore[arg-type]
        spoof_score=0, spoof_side="NONE", micro_pullback=None,
        poc_offset_ticks=0, poc_location="MID",
        stacked_imbalance_long=False, stacked_imbalance_short=False,
        bar_finished=False, bar_delta=0,
        in_htf_poi=False, htf_poi_type=None, htf_poi_side=None, distance_to_poi_ticks=None,
    )


def _plan() -> TradePlan:
    cand = SetupCandidate(
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=Direction.LONG,
        symbol="BTCUSDT", timestamp_ms=0,
        entry_price=100.0, stop_price=99.5,
        tp1_price=100.5, tp2_price=101.0, tp3_price=101.5,
        stop_distance_ticks=5,
        invalidation_conditions=[
            InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={}),
        ],
        features_snapshot=_features(),
    )
    return TradePlan(
        candidate=cand, setup_type=cand.setup_type, direction=cand.direction,
        symbol=cand.symbol, timestamp_ms=0,
        entry_price=cand.entry_price, stop_price=cand.stop_price,
        tp1_price=cand.tp1_price, tp2_price=cand.tp2_price, tp3_price=cand.tp3_price,
        stop_distance_ticks=5,
        score=1.5, score_threshold=1.0, regime=Regime.NORMAL_BALANCED,
        expectancy_multiplier=1.0,
        invalidation_conditions=cand.invalidation_conditions, time_stop_ms=None,
    )


def _outcome(realized_r: float, *, trade_id: str, mfe: float = 0.0, mae: float = 0.0) -> TradeOutcome:
    p = _plan()
    return TradeOutcome(
        plan=p, trade_id=trade_id, symbol="BTCUSDT",
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=Direction.LONG,
        closed_at_ms=0, realized_r=realized_r, realized_usd=realized_r * 10,
        max_favorable_r=mfe, max_adverse_r=mae,
        was_stopped=realized_r < 0, fees_usd=0.0,
    )


# ============================================================
# Snapshot
# ============================================================

def test_empty_snapshot_returns_none() -> None:
    tr = ExpectancyTracker(ExpectancyConfig())
    assert tr.get(SetupType.ABSORPTION_REVERSAL, "BTCUSDT") is None


def test_snapshot_basic_math() -> None:
    tr = ExpectancyTracker(ExpectancyConfig())
    tr.on_trade_outcome(_outcome(+1.0, trade_id="t1"))
    tr.on_trade_outcome(_outcome(+1.0, trade_id="t2"))
    tr.on_trade_outcome(_outcome(-1.0, trade_id="t3"))
    tr.on_trade_outcome(_outcome(0.0, trade_id="t4"))     # breakeven

    snap = tr.get(SetupType.ABSORPTION_REVERSAL, "BTCUSDT")
    assert snap is not None
    assert snap.samples == 4
    assert snap.wins == 2
    assert snap.losses == 1
    assert snap.breakevens == 1
    assert snap.win_rate == pytest.approx(2 / 3, abs=1e-6)
    assert snap.avg_win_R == pytest.approx(1.0, abs=1e-6)
    assert snap.avg_loss_R == pytest.approx(1.0, abs=1e-6)
    # E = 0.667*1 - 0.333*1 = 0.333
    assert snap.rolling_E_R == pytest.approx(1 / 3, abs=1e-6)


def test_window_maxlen_enforced() -> None:
    tr = ExpectancyTracker(ExpectancyConfig(window_size=5))
    for i in range(10):
        tr.on_trade_outcome(_outcome(1.0, trade_id=f"t{i}"))
    snap = tr.get(SetupType.ABSORPTION_REVERSAL, "BTCUSDT")
    assert snap is not None
    assert snap.samples == 5


def test_breakeven_excluded_from_wr_denom() -> None:
    tr = ExpectancyTracker(ExpectancyConfig())
    tr.on_trade_outcome(_outcome(+1.0, trade_id="w"))
    tr.on_trade_outcome(_outcome(0.0, trade_id="b1"))
    tr.on_trade_outcome(_outcome(0.0, trade_id="b2"))
    snap = tr.get(SetupType.ABSORPTION_REVERSAL, "BTCUSDT")
    assert snap is not None
    assert snap.win_rate == 1.0      # 1 win / 1 decisive trade


def test_duplicate_trade_id_ignored() -> None:
    tr = ExpectancyTracker(ExpectancyConfig())
    tr.on_trade_outcome(_outcome(+1.0, trade_id="dup"))
    tr.on_trade_outcome(_outcome(+1.0, trade_id="dup"))
    snap = tr.get(SetupType.ABSORPTION_REVERSAL, "BTCUSDT")
    assert snap is not None
    assert snap.samples == 1


# ============================================================
# Auto-suspend
# ============================================================

def test_auto_suspend_when_bad_e_and_narrow_ci() -> None:
    cfg = ExpectancyConfig(
        auto_suspend_min_samples=10,
        auto_suspend_e_threshold_R=-0.3,
        auto_suspend_ci_upper=0.45,
    )
    tr = ExpectancyTracker(cfg)
    # 10 збитків поспіль → E = -1, CI_upper = low
    for i in range(10):
        tr.on_trade_outcome(_outcome(-1.0, trade_id=f"t{i}"))
    assert tr.is_suspended(SetupType.ABSORPTION_REVERSAL, "BTCUSDT") is True


def test_no_suspend_below_min_samples() -> None:
    cfg = ExpectancyConfig(auto_suspend_min_samples=30)
    tr = ExpectancyTracker(cfg)
    for i in range(25):
        tr.on_trade_outcome(_outcome(-1.0, trade_id=f"t{i}"))
    assert tr.is_suspended(SetupType.ABSORPTION_REVERSAL, "BTCUSDT") is False


def test_no_suspend_when_bad_e_but_ci_wide() -> None:
    # Малий, шумний результат — CI_upper залишиться високим → не suspend
    cfg = ExpectancyConfig(
        auto_suspend_min_samples=5,
        auto_suspend_e_threshold_R=-0.1,
        auto_suspend_ci_upper=0.45,
    )
    tr = ExpectancyTracker(cfg)
    # 3 збитки, 2 виграші — E негативна на малому n, CI широкий
    tr.on_trade_outcome(_outcome(-1.0, trade_id="a"))
    tr.on_trade_outcome(_outcome(-1.0, trade_id="b"))
    tr.on_trade_outcome(_outcome(-1.0, trade_id="c"))
    tr.on_trade_outcome(_outcome(+0.5, trade_id="d"))
    tr.on_trade_outcome(_outcome(+0.5, trade_id="e"))
    snap = tr.get(SetupType.ABSORPTION_REVERSAL, "BTCUSDT")
    assert snap is not None
    # CI upper на (2/5) широкий → не suspend
    assert snap.win_rate_ci_high > 0.45
    assert tr.is_suspended(SetupType.ABSORPTION_REVERSAL, "BTCUSDT") is False


def test_resume_is_manual() -> None:
    cfg = ExpectancyConfig(auto_suspend_min_samples=5)
    tr = ExpectancyTracker(cfg)
    for i in range(10):
        tr.on_trade_outcome(_outcome(-1.0, trade_id=f"t{i}"))
    assert tr.is_suspended(SetupType.ABSORPTION_REVERSAL, "BTCUSDT") is True

    # 10 прибуткових — suspend не знімається автоматично
    for i in range(10, 20):
        tr.on_trade_outcome(_outcome(+1.0, trade_id=f"t{i}"))
    assert tr.is_suspended(SetupType.ABSORPTION_REVERSAL, "BTCUSDT") is True

    # Ручний resume
    tr.resume(SetupType.ABSORPTION_REVERSAL, "BTCUSDT")
    assert tr.is_suspended(SetupType.ABSORPTION_REVERSAL, "BTCUSDT") is False


def test_manual_suspend_and_snapshot_updated() -> None:
    tr = ExpectancyTracker(ExpectancyConfig())
    tr.on_trade_outcome(_outcome(+1.0, trade_id="t1"))
    tr.suspend(SetupType.ABSORPTION_REVERSAL, "BTCUSDT", "manual_test")
    snap = tr.get(SetupType.ABSORPTION_REVERSAL, "BTCUSDT")
    assert snap is not None
    assert snap.suspended is True
    assert snap.suspended_reason == "manual_test"
