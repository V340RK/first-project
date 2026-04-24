"""Footprint-бар: OHLC, level volumes, delta, PoC, tick rounding."""

from __future__ import annotations

import pytest

from scalper.book.footprint import (
    bar_open_time_for,
    close_bar,
    new_bar,
    round_to_tick,
    tf_to_ms,
    update_bar,
)
from scalper.gateway.types import RawAggTrade


def _trade(price: float, qty: float, is_maker: bool, ts: int = 0) -> RawAggTrade:
    return RawAggTrade(timestamp_ms=ts, symbol="BTCUSDT", price=price, quantity=qty,
                         is_buyer_maker=is_maker, agg_id=0)


def test_tf_to_ms_known() -> None:
    assert tf_to_ms("1m") == 60_000
    assert tf_to_ms("5m") == 300_000
    assert tf_to_ms("15m") == 900_000


def test_tf_to_ms_unknown_raises() -> None:
    with pytest.raises(ValueError):
        tf_to_ms("2s")


def test_bar_open_aligns_to_grid() -> None:
    # 2026-04-22 12:03:47 UTC → початок 1m бару 12:03:00
    ts = 1_776_873_827_000
    assert bar_open_time_for(ts, "1m") % 60_000 == 0
    # 5m → 12:00:00
    assert bar_open_time_for(ts, "5m") % 300_000 == 0


def test_round_to_tick() -> None:
    assert round_to_tick(20000.37, 0.1) == pytest.approx(20000.4)
    assert round_to_tick(20000.34, 0.1) == pytest.approx(20000.3)
    assert round_to_tick(20000.123456, 0.01) == pytest.approx(20000.12)
    assert round_to_tick(5.0, 0.0) == 5.0  # 0 tick = passthrough


def test_update_bar_ohlc_and_delta() -> None:
    bar = new_bar("BTCUSDT", "1m", ts_ms=0)
    update_bar(bar, _trade(100.0, 1.0, is_maker=False), tick_size=0.1)  # taker купив
    update_bar(bar, _trade(101.0, 2.0, is_maker=True), tick_size=0.1)   # taker продав
    update_bar(bar, _trade(99.5, 1.5, is_maker=False), tick_size=0.1)   # taker купив

    assert bar.open == 100.0
    assert bar.high == 101.0
    assert bar.low == 99.5
    assert bar.close == 99.5
    assert bar.trade_count == 3
    # delta = ask_vol - bid_vol = (1.0 + 1.5) - 2.0 = 0.5
    assert bar.delta == pytest.approx(0.5)


def test_poc_updates_incrementally() -> None:
    bar = new_bar("X", "1m", 0)
    # Купимо 10 на 100.0, 5 на 101.0, ще 6 на 101.0 → PoC має стати 101.0
    update_bar(bar, _trade(100.0, 10.0, is_maker=False), tick_size=0.1)
    assert bar.poc_price == 100.0
    update_bar(bar, _trade(101.0, 5.0, is_maker=False), tick_size=0.1)
    assert bar.poc_price == 100.0  # ще не перевищив
    update_bar(bar, _trade(101.0, 6.0, is_maker=False), tick_size=0.1)  # 11 > 10
    assert bar.poc_price == 101.0


def test_levels_clustered_by_tick() -> None:
    bar = new_bar("X", "1m", 0)
    update_bar(bar, _trade(20000.11, 1.0, is_maker=False), tick_size=0.1)
    update_bar(bar, _trade(20000.14, 1.0, is_maker=False), tick_size=0.1)  # округлиться до 20000.1
    update_bar(bar, _trade(20000.16, 1.0, is_maker=False), tick_size=0.1)  # до 20000.2
    assert 20000.1 in bar.levels
    assert 20000.2 in bar.levels
    assert bar.levels[20000.1].ask_vol == pytest.approx(2.0)


def test_close_bar_marks_closed() -> None:
    bar = new_bar("X", "1m", 0)
    close_bar(bar)
    assert bar.is_closed is True


def test_total_volume_matches_sum_of_trades() -> None:
    bar = new_bar("X", "1m", 0)
    trades_qty = [1.0, 2.5, 0.7, 3.0, 1.2]
    for q in trades_qty:
        update_bar(bar, _trade(100.0 + q * 0.01, q, is_maker=False), tick_size=0.01)
    assert bar.total_volume == pytest.approx(sum(trades_qty))
