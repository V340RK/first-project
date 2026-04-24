"""SimulatedExecutionEngine — MARKET/LIMIT/STOP/TP, slippage, fees, callbacks."""

from __future__ import annotations

import pytest

from scalper.execution.types import (
    FillEvent,
    OrderRequest,
    OrderSide,
    OrderType,
    OrderUpdate,
    SymbolFilters,
    TimeInForce,
)
from scalper.replay.simulator import (
    FillPolicy,
    SimulatedExecutionEngine,
    SimulatorConfig,
    SlippageModel,
)


# ============================================================
# Helpers
# ============================================================

FILTERS = SymbolFilters(
    symbol="BTCUSDT", tick_size=0.1, step_size=0.001,
    min_qty=0.001, max_qty=100.0, min_notional=5.0,
)


def _sim(
    *, latency_ms: int = 0,
    limit_policy: FillPolicy = FillPolicy.TOUCH,
    slippage: SlippageModel = SlippageModel.ZERO,
    slippage_ticks: int = 1,
    clock_ms: int = 1000,
) -> SimulatedExecutionEngine:
    cfg = SimulatorConfig(
        limit_fill_policy=limit_policy, slippage_model=slippage,
        slippage_fixed_ticks=slippage_ticks, latency_ms=latency_ms,
    )
    now_ref = [clock_ms]
    eng = SimulatedExecutionEngine(cfg, clock_fn=lambda: now_ref[0])
    eng.register_symbol(FILTERS)
    eng._test_clock_ref = now_ref  # type: ignore[attr-defined]
    return eng


def _set_book(eng: SimulatedExecutionEngine, bid: float, ask: float, last: float) -> None:
    eng.update_book("BTCUSDT", bid=bid, ask=ask, last_trade_price=last, tick_size=0.1)


# ============================================================
# MARKET fills
# ============================================================

@pytest.mark.asyncio
async def test_market_fills_at_ask_for_buy_no_slippage() -> None:
    eng = _sim(slippage=SlippageModel.ZERO)
    _set_book(eng, bid=100.0, ask=100.1, last=100.05)
    fills: list[FillEvent] = []
    eng.on_fill(lambda f: _collect(fills, f))
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.MARKET, qty=0.1,
    ))
    assert res.success
    assert res.status == "FILLED"
    assert res.avg_fill_price == pytest.approx(100.1)
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(100.1)
    assert fills[0].is_maker is False


@pytest.mark.asyncio
async def test_market_fills_at_bid_for_sell() -> None:
    eng = _sim(slippage=SlippageModel.ZERO)
    _set_book(eng, bid=100.0, ask=100.1, last=100.05)
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.SELL, type=OrderType.MARKET, qty=0.1,
    ))
    assert res.avg_fill_price == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_market_with_spread_based_slippage() -> None:
    eng = _sim(slippage=SlippageModel.SPREAD_BASED)
    _set_book(eng, bid=100.0, ask=100.2, last=100.1)   # spread=0.2 → slip=0.1
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.MARKET, qty=0.1,
    ))
    assert res.avg_fill_price == pytest.approx(100.3)  # ask + 0.1


@pytest.mark.asyncio
async def test_market_with_fixed_ticks_slippage() -> None:
    eng = _sim(slippage=SlippageModel.FIXED_TICKS, slippage_ticks=2)
    _set_book(eng, bid=100.0, ask=100.1, last=100.05)
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.MARKET, qty=0.1,
    ))
    # 2 ticks × 0.1 = 0.2 slip
    assert res.avg_fill_price == pytest.approx(100.3)


# ============================================================
# LIMIT fills
# ============================================================

@pytest.mark.asyncio
async def test_limit_buy_not_filled_when_ask_above_price() -> None:
    eng = _sim()
    _set_book(eng, bid=99.8, ask=99.9, last=99.85)
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT, qty=0.1,
        price=99.5, time_in_force=TimeInForce.GTC,
    ))
    assert res.status == "NEW"
    assert res.filled_qty == 0.0


@pytest.mark.asyncio
async def test_limit_buy_fills_on_touch() -> None:
    eng = _sim(limit_policy=FillPolicy.TOUCH)
    _set_book(eng, bid=99.8, ask=99.9, last=99.85)
    fills: list[FillEvent] = []
    eng.on_fill(lambda f: _collect(fills, f))
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT, qty=0.1, price=99.9,
    ))
    assert res.status == "FILLED"
    assert fills[0].is_maker is True
    assert fills[0].price == pytest.approx(99.9)


@pytest.mark.asyncio
async def test_limit_cross_policy_requires_strict_cross() -> None:
    eng = _sim(limit_policy=FillPolicy.CROSS)
    _set_book(eng, bid=99.8, ask=99.9, last=99.85)
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT, qty=0.1, price=99.9,
    ))
    assert res.status == "NEW"   # touch insufficient
    _set_book(eng, bid=99.7, ask=99.8, last=99.75)
    await eng.on_clock_tick(eng._test_clock_ref[0])   # type: ignore[attr-defined]
    # тепер ask<price → fill
    assert res.client_order_id not in eng._state.pending   # type: ignore[attr-defined]


# ============================================================
# STOP_MARKET + TAKE_PROFIT_MARKET
# ============================================================

@pytest.mark.asyncio
async def test_stop_market_sell_triggers_when_price_drops() -> None:
    eng = _sim(slippage=SlippageModel.ZERO)
    _set_book(eng, bid=100.0, ask=100.1, last=100.05)
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.SELL, type=OrderType.STOP_MARKET,
        qty=0.1, stop_price=99.5,
    ))
    assert res.status == "NEW"
    # Ціна падає нижче стопу
    _set_book(eng, bid=99.3, ask=99.4, last=99.4)
    fills: list[FillEvent] = []
    eng.on_fill(lambda f: _collect(fills, f))
    await eng.on_clock_tick(eng._test_clock_ref[0])   # type: ignore[attr-defined]
    assert len(fills) == 1
    assert fills[0].price == pytest.approx(99.3)      # sell on bid


@pytest.mark.asyncio
async def test_take_profit_market_buy_triggers_when_price_dips() -> None:
    # BUY TP = закриття SHORT — спрацьовує коли ціна падає до tp_price
    eng = _sim(slippage=SlippageModel.ZERO)
    _set_book(eng, bid=100.0, ask=100.1, last=100.05)
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.TAKE_PROFIT_MARKET,
        qty=0.1, stop_price=99.5, reduce_only=True,
    ))
    assert res.status == "NEW"
    _set_book(eng, bid=99.3, ask=99.4, last=99.4)
    fills: list[FillEvent] = []
    eng.on_fill(lambda f: _collect(fills, f))
    await eng.on_clock_tick(eng._test_clock_ref[0])   # type: ignore[attr-defined]
    assert len(fills) == 1


# ============================================================
# Latency
# ============================================================

@pytest.mark.asyncio
async def test_latency_delays_fill_until_trigger_ms() -> None:
    eng = _sim(latency_ms=50, slippage=SlippageModel.ZERO)
    _set_book(eng, bid=100.0, ask=100.1, last=100.05)
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.MARKET, qty=0.1,
    ))
    assert res.status == "NEW"
    assert res.filled_qty == 0.0
    # Клок не досяг trigger_ms → без філу
    await eng.on_clock_tick(1000 + 40)
    assert res.client_order_id in eng._state.pending   # type: ignore[attr-defined]
    # Досяг — філ
    fills: list[FillEvent] = []
    eng.on_fill(lambda f: _collect(fills, f))
    await eng.on_clock_tick(1000 + 50)
    assert len(fills) == 1


# ============================================================
# Fees
# ============================================================

@pytest.mark.asyncio
async def test_taker_fee_on_market() -> None:
    eng = _sim(slippage=SlippageModel.ZERO)
    _set_book(eng, bid=100.0, ask=100.0, last=100.0)
    fills: list[FillEvent] = []
    eng.on_fill(lambda f: _collect(fills, f))
    await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.MARKET, qty=1.0,
    ))
    # 1 * 100 * 0.0004 = 0.04
    assert fills[0].commission_usd == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_maker_fee_on_limit_fill() -> None:
    eng = _sim()
    _set_book(eng, bid=99.9, ask=100.0, last=99.95)
    fills: list[FillEvent] = []
    eng.on_fill(lambda f: _collect(fills, f))
    await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT, qty=1.0, price=100.0,
    ))
    # 1 * 100 * 0.0002 = 0.02
    assert fills[0].commission_usd == pytest.approx(0.02)


# ============================================================
# Cancel + filters
# ============================================================

@pytest.mark.asyncio
async def test_cancel_pending_limit() -> None:
    eng = _sim()
    _set_book(eng, bid=99.0, ask=99.1, last=99.05)
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT, qty=0.1, price=98.0,
    ))
    assert res.status == "NEW"
    cancel = await eng.cancel_order("BTCUSDT", res.client_order_id)
    assert cancel.status == "CANCELED"


@pytest.mark.asyncio
async def test_cancel_unknown_order_returns_not_found() -> None:
    eng = _sim()
    res = await eng.cancel_order("BTCUSDT", "nonexistent-coid")
    assert res.status == "NOT_FOUND"
    assert res.success is True


@pytest.mark.asyncio
async def test_reject_qty_below_min() -> None:
    eng = _sim()
    _set_book(eng, bid=100.0, ask=100.1, last=100.05)
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.MARKET, qty=0.0005,
    ))
    assert res.status == "REJECTED"
    assert res.error_code == -4003


@pytest.mark.asyncio
async def test_reject_notional_below_min() -> None:
    eng = _sim()
    _set_book(eng, bid=100.0, ask=100.0, last=100.0)
    # qty=0.01 * price=1.0 = 0.01 notional < 5.0
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT, qty=0.01, price=1.0,
    ))
    assert res.status == "REJECTED"
    assert res.error_code == -4164


@pytest.mark.asyncio
async def test_qty_and_price_rounding() -> None:
    eng = _sim()
    _set_book(eng, bid=100.0, ask=100.0, last=100.0)
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT,
        qty=0.12345678, price=100.123,
    ))
    # step=0.001 → 0.123; tick=0.1 → 100.1
    assert res.status == "FILLED"   # touch policy, ask=100 <= price=100.1
    assert res.filled_qty == pytest.approx(0.123)
    assert res.avg_fill_price == pytest.approx(100.1)


# ============================================================
# Order updates
# ============================================================

@pytest.mark.asyncio
async def test_order_update_emitted_on_fill_and_cancel() -> None:
    eng = _sim()
    _set_book(eng, bid=100.0, ask=100.0, last=100.0)
    updates: list[OrderUpdate] = []
    eng.on_order_update(lambda u: _collect(updates, u))
    res = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT, qty=0.1, price=100.0,
    ))
    # filled immediately → NEW→FILLED update
    assert len(updates) >= 1
    assert updates[-1].new_status == "FILLED"

    res2 = await eng.place_order(OrderRequest(
        symbol="BTCUSDT", side=OrderSide.BUY, type=OrderType.LIMIT, qty=0.1, price=90.0,
    ))
    assert res2.status == "NEW"
    await eng.cancel_order("BTCUSDT", res2.client_order_id)
    assert updates[-1].new_status == "CANCELED"


# ============================================================
# Small util
# ============================================================

async def _collect(lst, item) -> None:   # type: ignore[no-untyped-def]
    lst.append(item)
