"""_RollingWindow: add/evict, invariants, snapshot."""

from __future__ import annotations

import pytest

from scalper.tape.rolling import _RollingWindow, _TradeContribution, add, evict_until, snapshot


def _c(ts: int, price: float, qty: float, maker: bool) -> _TradeContribution:
    return _TradeContribution(ts_ms=ts, price=price, qty=qty, is_buyer_maker=maker)


def test_add_updates_incremental_sums() -> None:
    w = _RollingWindow(duration_ms=1000)
    add(w, _c(100, 50.0, 2.0, maker=False))   # buy 100 USD
    add(w, _c(200, 50.0, 1.0, maker=True))    # sell 50 USD
    assert w.sum_buy_qty == 2.0
    assert w.sum_sell_qty == 1.0
    assert w.sum_buy_usd == 100.0
    assert w.sum_sell_usd == 50.0


def test_evict_removes_old_entries() -> None:
    w = _RollingWindow(duration_ms=1000)
    add(w, _c(100, 50.0, 2.0, maker=False))
    add(w, _c(500, 50.0, 1.0, maker=True))
    add(w, _c(800, 50.0, 3.0, maker=False))
    # Поточний час 1200 → cutoff=200 → перший (ts=100) випадає
    evict_until(w, now_ms=1200)
    assert len(w.entries) == 2
    assert w.sum_buy_qty == 3.0          # лише 3.0 залишилось з buys (викинули 2.0)
    assert w.sum_sell_qty == 1.0


def test_evict_zeroes_sums_when_empty() -> None:
    w = _RollingWindow(duration_ms=1000)
    add(w, _c(100, 50.0, 2.0, maker=False))
    evict_until(w, now_ms=5_000)
    assert w.sum_buy_qty == 0.0
    assert w.sum_buy_usd == 0.0
    assert len(w.entries) == 0


def test_invariant_sums_match_entries() -> None:
    """Property-style: після довільної серії add/evict інкрементальні суми = sum(entries)."""
    import random
    random.seed(42)
    w = _RollingWindow(duration_ms=1000)
    now = 0
    for _ in range(200):
        now += random.randint(1, 200)
        add(w, _c(now, random.uniform(10, 100), random.uniform(0.1, 5.0),
                   maker=random.choice([True, False])))
        evict_until(w, now)

    expected_buy_qty = sum(c.qty for c in w.entries if not c.is_buyer_maker)
    expected_sell_qty = sum(c.qty for c in w.entries if c.is_buyer_maker)
    expected_buy_usd = sum(c.price * c.qty for c in w.entries if not c.is_buyer_maker)
    expected_sell_usd = sum(c.price * c.qty for c in w.entries if c.is_buyer_maker)

    assert w.sum_buy_qty == pytest.approx(expected_buy_qty)
    assert w.sum_sell_qty == pytest.approx(expected_sell_qty)
    assert w.sum_buy_usd == pytest.approx(expected_buy_usd)
    assert w.sum_sell_usd == pytest.approx(expected_sell_usd)


def test_snapshot_reports_counts_and_deltas() -> None:
    w = _RollingWindow(duration_ms=1000)
    add(w, _c(100, 10.0, 3.0, maker=False))   # buy 30 USD, 3 qty
    add(w, _c(200, 10.0, 1.0, maker=True))    # sell 10 USD, 1 qty
    snap = snapshot(w, last_price=10.5)
    assert snap.trade_count == 2
    assert snap.delta_qty == pytest.approx(2.0)
    assert snap.delta_usd == pytest.approx(20.0)
    assert snap.first_trade_ms == 100
    assert snap.last_trade_ms == 200
    assert snap.last_trade_price == 10.5


def test_snapshot_empty_window() -> None:
    w = _RollingWindow(duration_ms=1000)
    snap = snapshot(w, last_price=0.0)
    assert snap.trade_count == 0
    assert snap.delta_qty == 0.0
    assert snap.first_trade_ms == 0
    assert snap.last_trade_ms == 0
