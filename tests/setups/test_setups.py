"""Тести SetupDetector + конкретні rule-и."""

from __future__ import annotations

from scalper.book.types import OrderBookLevel, OrderBookState
from scalper.common.enums import Direction, SetupType
from scalper.features.types import Features, MarketSnapshot, PullbackState
from scalper.setups import SetupConfig, SetupDetector, default_rules
from scalper.setups.rules.absorption import AbsorptionReversalLong, AbsorptionReversalShort
from scalper.setups.rules.imbalance_continuation import (
    ImbalanceContinuationLong,
    ImbalanceContinuationShort,
)
from scalper.setups.rules.micro_pullback import MicroPullbackLong, MicroPullbackShort
from scalper.tape.types import TapeWindow, TapeWindowsState


def _book(*, best_bid: float = 100.0, best_ask: float = 100.1) -> OrderBookState:
    return OrderBookState(
        symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
        bids=[OrderBookLevel(best_bid, 10.0)],
        asks=[OrderBookLevel(best_ask, 10.0)],
        is_synced=True,
    )


def _empty_win(d: int) -> TapeWindow:
    return TapeWindow(
        duration_ms=d, trade_count=0,
        buy_volume_qty=0, sell_volume_qty=0, buy_volume_usd=0, sell_volume_usd=0,
        delta_qty=0, delta_usd=0, last_trade_price=0, first_trade_ms=0, last_trade_ms=0,
    )


def _tape() -> TapeWindowsState:
    return TapeWindowsState(
        symbol="BTCUSDT", timestamp_ms=1000,
        window_500ms=_empty_win(500), window_2s=_empty_win(2000), window_10s=_empty_win(10_000),
        cvd=0.0, cvd_reliable=True,
        delta_500ms=0.0, delta_2s=0.0, delta_10s=0.0,
        price_path=[],
    )


def _features(
    *, book: OrderBookState | None = None, last_price: float = 100.0,
    spread_ticks: int = 1,
    absorption_score: float = 0.0, absorption_side: str = "NONE",
    delta_500ms: float = 0.0, delta_2s: float = 0.0, delta_10s: float = 0.0,
    weighted_imbalance: float = 0.0, bid_ask_imbalance_5: float = 0.0,
    stacked_imbalance_long: bool = False, stacked_imbalance_short: bool = False,
    micro_pullback: PullbackState | None = None,
    aggressive_buy_burst: bool = False, aggressive_sell_burst: bool = False,
) -> Features:
    b = book or _book()
    snap = MarketSnapshot(
        timestamp_ms=1000, symbol="BTCUSDT",
        book=b, tape=_tape(), last_price=last_price, spread_ticks=spread_ticks,
    )
    return Features(
        snapshot=snap,
        bid_ask_imbalance_5=bid_ask_imbalance_5,
        bid_ask_imbalance_10=0.0,
        weighted_imbalance=weighted_imbalance,
        book_pressure_side="NEUTRAL",
        delta_500ms=delta_500ms, delta_2s=delta_2s, delta_10s=delta_10s,
        cvd=0.0,
        aggressive_buy_burst=aggressive_buy_burst,
        aggressive_sell_burst=aggressive_sell_burst,
        burst_size_usd=None,
        absorption_score=absorption_score, absorption_side=absorption_side,  # type: ignore[arg-type]
        spoof_score=0.0, spoof_side="NONE",
        micro_pullback=micro_pullback,
        poc_offset_ticks=0, poc_location="MID",
        stacked_imbalance_long=stacked_imbalance_long,
        stacked_imbalance_short=stacked_imbalance_short,
        bar_finished=False, bar_delta=0.0,
        in_htf_poi=False, htf_poi_type=None, htf_poi_side=None,
        distance_to_poi_ticks=None,
    )


# ============================================================
# SetupDetector pipeline
# ============================================================

def test_detector_empty_when_no_match() -> None:
    det = SetupDetector(default_rules(SetupConfig()))
    assert det.detect(_features()) == []


def test_detector_isolates_rule_exceptions() -> None:
    class Bad:
        setup_type = SetupType.ABSORPTION_REVERSAL
        def check(self, f):
            raise RuntimeError("boom")

    class Good:
        setup_type = SetupType.ABSORPTION_REVERSAL
        def check(self, f):
            return None

    det = SetupDetector([Bad(), Good()])
    assert det.detect(_features()) == []  # не впало


# ============================================================
# Absorption reversal
# ============================================================

def test_absorption_long_positive() -> None:
    cfg = SetupConfig()
    rule = AbsorptionReversalLong(cfg.absorption, tick_size=cfg.tick_size_default)
    f = _features(
        absorption_score=0.8, absorption_side="BID",
        delta_500ms=-40_000, delta_2s=30_000,
        weighted_imbalance=0.5,
    )
    cand = rule.check(f)
    assert cand is not None
    assert cand.direction == Direction.LONG
    assert cand.setup_type == SetupType.ABSORPTION_REVERSAL
    assert cand.entry_price > cand.stop_price
    assert cand.tp1_price > cand.entry_price
    assert cand.stop_distance_ticks >= 1
    assert any(ic.kind.value for ic in cand.invalidation_conditions)


def test_absorption_long_requires_trigger() -> None:
    cfg = SetupConfig()
    rule = AbsorptionReversalLong(cfg.absorption, tick_size=cfg.tick_size_default)
    # Слабкий absorption_score
    f = _features(absorption_score=0.3, absorption_side="BID", delta_500ms=-40_000)
    assert rule.check(f) is None


def test_absorption_long_requires_confirmation() -> None:
    cfg = SetupConfig()
    rule = AbsorptionReversalLong(cfg.absorption, tick_size=cfg.tick_size_default)
    # Триггер ок, конфірмації нема
    f = _features(
        absorption_score=0.8, absorption_side="BID", delta_500ms=-40_000,
        delta_2s=0, weighted_imbalance=0.0, stacked_imbalance_long=False,
    )
    assert rule.check(f) is None


def test_absorption_short_positive() -> None:
    cfg = SetupConfig()
    rule = AbsorptionReversalShort(cfg.absorption, tick_size=cfg.tick_size_default)
    f = _features(
        absorption_score=0.8, absorption_side="ASK",
        delta_500ms=40_000, delta_2s=-30_000,
        weighted_imbalance=-0.5,
    )
    cand = rule.check(f)
    assert cand is not None
    assert cand.direction == Direction.SHORT
    assert cand.entry_price < cand.stop_price
    assert cand.tp1_price < cand.entry_price


def test_absorption_rejects_wide_spread() -> None:
    cfg = SetupConfig()
    rule = AbsorptionReversalLong(cfg.absorption, tick_size=cfg.tick_size_default)
    f = _features(
        absorption_score=0.8, absorption_side="BID",
        delta_500ms=-40_000, delta_2s=30_000,
        spread_ticks=10,
    )
    assert rule.check(f) is None


# ============================================================
# Imbalance continuation
# ============================================================

def test_imbalance_continuation_long_positive() -> None:
    cfg = SetupConfig()
    rule = ImbalanceContinuationLong(cfg.imbalance_cont, tick_size=cfg.tick_size_default)
    pb = PullbackState(
        direction="LONG_PULLBACK", depth_ticks=5,
        bars_in_pullback=2, delta_during_pullback=-1000,
    )
    f = _features(
        stacked_imbalance_long=True,
        delta_2s=60_000,
        bid_ask_imbalance_5=0.5,
        micro_pullback=pb,
        last_price=100.2,
    )
    cand = rule.check(f)
    assert cand is not None
    assert cand.direction == Direction.LONG
    assert cand.entry_price > cand.stop_price


def test_imbalance_continuation_long_requires_pullback() -> None:
    cfg = SetupConfig()
    rule = ImbalanceContinuationLong(cfg.imbalance_cont, tick_size=cfg.tick_size_default)
    f = _features(
        stacked_imbalance_long=True, delta_2s=60_000, bid_ask_imbalance_5=0.5,
        micro_pullback=None,
    )
    assert rule.check(f) is None


def test_imbalance_continuation_short_positive() -> None:
    cfg = SetupConfig()
    rule = ImbalanceContinuationShort(cfg.imbalance_cont, tick_size=cfg.tick_size_default)
    pb = PullbackState(
        direction="SHORT_PULLBACK", depth_ticks=5,
        bars_in_pullback=2, delta_during_pullback=1000,
    )
    f = _features(
        stacked_imbalance_short=True,
        delta_2s=-60_000,
        bid_ask_imbalance_5=-0.5,
        micro_pullback=pb,
        last_price=100.0,
        book=_book(best_bid=99.9, best_ask=100.0),
    )
    cand = rule.check(f)
    assert cand is not None
    assert cand.direction == Direction.SHORT


# ============================================================
# Micro pullback
# ============================================================

def test_micro_pullback_long_positive() -> None:
    cfg = SetupConfig()
    rule = MicroPullbackLong(cfg.micro_pullback, tick_size=cfg.tick_size_default)
    pb = PullbackState(
        direction="LONG_PULLBACK", depth_ticks=3,
        bars_in_pullback=1, delta_during_pullback=-5_000,
    )
    f = _features(micro_pullback=pb, aggressive_buy_burst=True, last_price=100.3)
    cand = rule.check(f)
    assert cand is not None
    assert cand.direction == Direction.LONG


def test_micro_pullback_long_requires_weak_counter() -> None:
    cfg = SetupConfig()
    rule = MicroPullbackLong(cfg.micro_pullback, tick_size=cfg.tick_size_default)
    pb = PullbackState(
        direction="LONG_PULLBACK", depth_ticks=3,
        bars_in_pullback=1, delta_during_pullback=-50_000,  # забагато контр
    )
    f = _features(micro_pullback=pb, aggressive_buy_burst=True, last_price=100.3)
    assert rule.check(f) is None


def test_micro_pullback_short_positive() -> None:
    cfg = SetupConfig()
    rule = MicroPullbackShort(cfg.micro_pullback, tick_size=cfg.tick_size_default)
    pb = PullbackState(
        direction="SHORT_PULLBACK", depth_ticks=3,
        bars_in_pullback=1, delta_during_pullback=5_000,
    )
    f = _features(
        micro_pullback=pb, aggressive_sell_burst=True,
        last_price=99.9, book=_book(best_bid=99.9, best_ask=100.0),
    )
    cand = rule.check(f)
    assert cand is not None
    assert cand.direction == Direction.SHORT


# ============================================================
# Invariants
# ============================================================

def test_all_candidates_have_positive_risk() -> None:
    cfg = SetupConfig()
    det = SetupDetector(default_rules(cfg))
    pb = PullbackState(
        direction="LONG_PULLBACK", depth_ticks=5,
        bars_in_pullback=2, delta_during_pullback=-1000,
    )
    f = _features(
        absorption_score=0.9, absorption_side="BID",
        delta_500ms=-40_000, delta_2s=60_000,
        weighted_imbalance=0.6, bid_ask_imbalance_5=0.6,
        stacked_imbalance_long=True, micro_pullback=pb,
        aggressive_buy_burst=True, last_price=100.2,
    )
    cands = det.detect(f)
    assert len(cands) >= 1
    for c in cands:
        if c.direction == Direction.LONG:
            assert c.entry_price > c.stop_price
            assert c.tp1_price > c.entry_price
        else:
            assert c.entry_price < c.stop_price
            assert c.tp1_price < c.entry_price
        assert c.stop_distance_ticks >= 1
        assert len(c.invalidation_conditions) >= 2
