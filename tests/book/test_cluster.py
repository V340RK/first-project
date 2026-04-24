"""Cluster-утиліти: imbalance, stacked, PoC location."""

from __future__ import annotations

import pytest

from scalper.book.cluster import classify_poc_location, detect_imbalances, detect_stacked
from scalper.book.types import FootprintBar, Imbalance, LevelVolume, PocLocation


def _fp_with_levels(levels: dict[float, tuple[float, float]]) -> FootprintBar:
    """Helper: {price: (bid_vol, ask_vol)} → FootprintBar."""
    bar = FootprintBar(symbol="X", timeframe="1m", open_time_ms=0, close_time_ms=60_000)
    for p, (bv, av) in levels.items():
        bar.levels[p] = LevelVolume(bid_vol=bv, ask_vol=av)
    return bar


def test_bull_imbalance_detected() -> None:
    # Нижній рівень: bid=1, Верхній: ask=5 → ratio 5.0 >= 2.5 → ASK imbalance на верхньому
    fp = _fp_with_levels({100.0: (1.0, 0.0), 100.1: (0.0, 5.0)})
    imbs = detect_imbalances(fp, ratio=2.5)
    assert len(imbs) == 1
    assert imbs[0].side == "ASK"
    assert imbs[0].price_level == 100.1
    assert imbs[0].ratio == pytest.approx(5.0)


def test_bear_imbalance_detected() -> None:
    fp = _fp_with_levels({100.0: (10.0, 0.0), 100.1: (0.0, 1.0)})
    imbs = detect_imbalances(fp, ratio=2.5)
    assert any(i.side == "BID" and i.price_level == 100.0 for i in imbs)


def test_no_imbalance_below_ratio() -> None:
    fp = _fp_with_levels({100.0: (1.0, 1.0), 100.1: (1.0, 2.0)})
    assert detect_imbalances(fp, ratio=2.5) == []


def test_stacked_needs_min_count() -> None:
    # 4 ASK imbalances поспіль
    imbs = [Imbalance(100.0 + i * 0.1, "ASK", 3.0, 5.0) for i in range(4)]
    stacked = detect_stacked(imbs, min_count=3)
    assert len(stacked) == 1
    assert stacked[0].level_count == 4
    assert stacked[0].side == "ASK"
    assert stacked[0].start_price == 100.0
    assert stacked[0].end_price == pytest.approx(100.3)


def test_stacked_splits_on_side_change() -> None:
    imbs = [
        Imbalance(100.0, "ASK", 3.0, 1.0),
        Imbalance(100.1, "ASK", 3.0, 1.0),
        Imbalance(100.2, "ASK", 3.0, 1.0),
        Imbalance(100.3, "BID", 3.0, 1.0),  # розриває
        Imbalance(100.4, "BID", 3.0, 1.0),
        Imbalance(100.5, "BID", 3.0, 1.0),
    ]
    stacked = detect_stacked(imbs, min_count=3)
    assert len(stacked) == 2
    assert {s.side for s in stacked} == {"ASK", "BID"}


def test_poc_location_no_poc_returns_center() -> None:
    fp = _fp_with_levels({})
    assert classify_poc_location(fp) == PocLocation.CENTER


def test_poc_upper_wick() -> None:
    fp = FootprintBar(symbol="X", timeframe="1m", open_time_ms=0, close_time_ms=60_000,
                       open=100.0, high=110.0, low=95.0, close=101.0, poc_price=108.0)
    assert classify_poc_location(fp) == PocLocation.UPPER_WICK


def test_poc_lower_wick() -> None:
    fp = FootprintBar(symbol="X", timeframe="1m", open_time_ms=0, close_time_ms=60_000,
                       open=100.0, high=110.0, low=95.0, close=101.0, poc_price=97.0)
    assert classify_poc_location(fp) == PocLocation.LOWER_WICK


def test_poc_top() -> None:
    # body [100, 101], range [95, 110] → PoC 106 всередині body? Ні, 106 > 101 → upper_wick.
    # Візьмемо PoC=100.8, в межах body, у верхній третині range-у.
    # range=15, pos=(100.8-95)/15=0.387 → CENTER. Нам треба >0.66.
    # PoC у body: range [95, 110], body [100, 101]. Для TOP треба PoC ≥ 105, але в body — максимум 101.
    # Отже TOP/BOTTOM вимагає «тонке» тіло у верхній/нижній зоні range.
    fp = FootprintBar(symbol="X", timeframe="1m", open_time_ms=0, close_time_ms=60_000,
                       open=108.0, high=110.0, low=95.0, close=109.0, poc_price=108.5)
    assert classify_poc_location(fp) == PocLocation.TOP


def test_poc_bottom() -> None:
    fp = FootprintBar(symbol="X", timeframe="1m", open_time_ms=0, close_time_ms=60_000,
                       open=96.0, high=110.0, low=95.0, close=97.0, poc_price=96.5)
    assert classify_poc_location(fp) == PocLocation.BOTTOM


def test_poc_center_when_degenerate_range() -> None:
    fp = FootprintBar(symbol="X", timeframe="1m", open_time_ms=0, close_time_ms=60_000,
                       open=100.0, high=100.0, low=100.0, close=100.0, poc_price=100.0)
    assert classify_poc_location(fp) == PocLocation.CENTER
