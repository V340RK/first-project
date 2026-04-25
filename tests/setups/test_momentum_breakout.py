"""MomentumBreakout — простий thrust rule для волатильних альтів."""

from __future__ import annotations

import pytest

from scalper.book.types import OrderBookLevel, OrderBookState
from scalper.common.enums import Direction, SetupType
from scalper.features.types import Features, MarketSnapshot
from scalper.setups.config import MomentumBreakoutRuleConfig
from scalper.setups.rules.momentum_breakout import (
    MomentumBreakoutLong,
    MomentumBreakoutShort,
)
from scalper.tape.types import TapeWindow, TapeWindowsState


def _empty_win(d: int) -> TapeWindow:
    return TapeWindow(
        duration_ms=d, trade_count=0, buy_volume_qty=0, sell_volume_qty=0,
        buy_volume_usd=0, sell_volume_usd=0,
        delta_qty=0, delta_usd=0, last_trade_price=0, first_trade_ms=0, last_trade_ms=0,
    )


def _features_with_thrust(
    *, last_price: float, price_path: list[tuple[int, float]],
    delta_10s: float, ts: int = 10_000,
) -> Features:
    book = OrderBookState(
        symbol="HYPERUSDT", timestamp_ms=ts, last_update_id=1,
        bids=[OrderBookLevel(last_price - 0.001, 100.0)],
        asks=[OrderBookLevel(last_price + 0.001, 100.0)],
        is_synced=True,
    )
    tape = TapeWindowsState(
        symbol="HYPERUSDT", timestamp_ms=ts,
        window_500ms=_empty_win(500), window_2s=_empty_win(2000),
        window_10s=_empty_win(10_000),
        cvd=0, cvd_reliable=True, delta_500ms=0, delta_2s=0, delta_10s=delta_10s,
        price_path=price_path,
    )
    snap = MarketSnapshot(
        timestamp_ms=ts, symbol="HYPERUSDT", book=book, tape=tape,
        last_price=last_price, spread_ticks=1,
    )
    return Features(
        snapshot=snap,
        bid_ask_imbalance_5=0.0, bid_ask_imbalance_10=0.0,
        weighted_imbalance=0.0, book_pressure_side="NEUTRAL",
        delta_500ms=0, delta_2s=0, delta_10s=delta_10s, cvd=0,
        aggressive_buy_burst=False, aggressive_sell_burst=False, burst_size_usd=None,
        absorption_score=0, absorption_side="NONE",   # type: ignore[arg-type]
        spoof_score=0, spoof_side="NONE", micro_pullback=None,
        poc_offset_ticks=0, poc_location="MID",
        stacked_imbalance_long=False, stacked_imbalance_short=False,
        bar_finished=False, bar_delta=0,
        in_htf_poi=False, htf_poi_type=None, htf_poi_side=None,
        distance_to_poi_ticks=None,
    )


def test_long_thrust_above_threshold_with_buy_delta_triggers() -> None:
    """Ціна виросла на 0.5% за 10с з buy delta → LONG breakout."""
    cfg = MomentumBreakoutRuleConfig(
        min_thrust_pct=0.3, lookback_ms=10_000,
        min_delta_usd=5_000,
    )
    rule = MomentumBreakoutLong(cfg, tick_size=0.001)
    f = _features_with_thrust(
        last_price=10.05,
        price_path=[(0, 10.0), (5_000, 10.02), (10_000, 10.05)],
        delta_10s=10_000,
    )
    cand = rule.check(f)
    assert cand is not None
    assert cand.direction == Direction.LONG
    assert cand.setup_type == SetupType.MOMENTUM_BREAKOUT
    assert cand.entry_price > f.snapshot.last_price
    assert cand.stop_price < cand.entry_price
    assert cand.tp1_price > cand.entry_price


def test_long_skipped_when_thrust_below_threshold() -> None:
    """Тільки 0.1% за 10с — мало для breakout."""
    cfg = MomentumBreakoutRuleConfig(
        min_thrust_pct=0.3, min_delta_usd=5_000,
    )
    rule = MomentumBreakoutLong(cfg, tick_size=0.001)
    f = _features_with_thrust(
        last_price=10.01,
        price_path=[(0, 10.0), (10_000, 10.01)],
        delta_10s=10_000,
    )
    assert rule.check(f) is None


def test_long_skipped_when_delta_negative() -> None:
    """Ціна виросла, але delta негативна (sell pressure) — false breakout, пропускаємо."""
    cfg = MomentumBreakoutRuleConfig(min_thrust_pct=0.3, min_delta_usd=5_000)
    rule = MomentumBreakoutLong(cfg, tick_size=0.001)
    f = _features_with_thrust(
        last_price=10.05,
        price_path=[(0, 10.0), (10_000, 10.05)],
        delta_10s=-3_000,
    )
    assert rule.check(f) is None


def test_short_mirror_logic() -> None:
    """Ціна впала на 0.5% за 10с з sell delta → SHORT breakout."""
    cfg = MomentumBreakoutRuleConfig(min_thrust_pct=0.3, min_delta_usd=5_000)
    rule = MomentumBreakoutShort(cfg, tick_size=0.001)
    f = _features_with_thrust(
        last_price=9.95,
        price_path=[(0, 10.0), (10_000, 9.95)],
        delta_10s=-10_000,
    )
    cand = rule.check(f)
    assert cand is not None
    assert cand.direction == Direction.SHORT
    assert cand.entry_price < f.snapshot.last_price
    assert cand.stop_price > cand.entry_price


def test_empty_price_path_returns_none() -> None:
    cfg = MomentumBreakoutRuleConfig()
    rule = MomentumBreakoutLong(cfg, tick_size=0.001)
    f = _features_with_thrust(
        last_price=10.0, price_path=[], delta_10s=10_000,
    )
    assert rule.check(f) is None
