"""TapeAnalyzer: gateway hookup, gap detection, CVD, fan-out, query API."""

from __future__ import annotations

import asyncio

import pytest

from scalper.gateway.types import RawAggTrade
from scalper.tape.analyzer import TapeAnalyzer
from scalper.tape.config import TapeConfig, TapeGapConfig, TapeWindowsConfig


class FakeGateway:
    def __init__(self) -> None:
        self._cb = None

    def on_agg_trade(self, cb) -> None:
        self._cb = cb

    async def feed(self, trade: RawAggTrade) -> None:
        assert self._cb is not None
        await self._cb(trade)


def _trade(ts: int, price: float, qty: float, *, maker: bool, agg_id: int = 1, symbol: str = "BTCUSDT") -> RawAggTrade:
    return RawAggTrade(timestamp_ms=ts, symbol=symbol, price=price, quantity=qty,
                         is_buyer_maker=maker, agg_id=agg_id)


def _config(**overrides) -> TapeConfig:
    base = dict(
        windows=TapeWindowsConfig(short_ms=500, medium_ms=2000, long_ms=10_000),
        tape_gap=TapeGapConfig(unreliable_window_min=5),
        trade_buffer_maxlen=100,
        price_path_maxlen=50,
    )
    base.update(overrides)
    return TapeConfig(**base)


@pytest.mark.asyncio
async def test_get_windows_empty_before_any_trade() -> None:
    gw = FakeGateway()
    tape = TapeAnalyzer(_config(), gw, clock_fn=lambda: 1000)
    await tape.start(["BTCUSDT"])
    state = tape.get_windows("BTCUSDT")
    assert state.window_500ms.trade_count == 0
    assert state.window_2s.trade_count == 0
    assert state.cvd == 0.0
    assert state.cvd_reliable is True
    assert state.delta_500ms == 0.0
    await tape.stop()


@pytest.mark.asyncio
async def test_cvd_accumulates_in_usd() -> None:
    gw = FakeGateway()
    tape = TapeAnalyzer(_config(), gw, clock_fn=lambda: 100)
    await tape.start(["BTCUSDT"])
    # Купівля 100 USD
    await gw.feed(_trade(ts=100, price=50.0, qty=2.0, maker=False, agg_id=1))
    # Продаж 50 USD
    await gw.feed(_trade(ts=110, price=50.0, qty=1.0, maker=True, agg_id=2))
    # Купівля 30 USD
    await gw.feed(_trade(ts=120, price=10.0, qty=3.0, maker=False, agg_id=3))
    # CVD = +100 -50 +30 = 80
    assert tape.get_cvd("BTCUSDT") == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_first_trade_does_not_trigger_gap() -> None:
    gw = FakeGateway()
    tape = TapeAnalyzer(_config(), gw, clock_fn=lambda: 100)
    await tape.start(["BTCUSDT"])
    await gw.feed(_trade(ts=100, price=50.0, qty=1.0, maker=False, agg_id=42))
    assert tape.is_cvd_reliable("BTCUSDT") is True


@pytest.mark.asyncio
async def test_agg_id_gap_marks_cvd_unreliable() -> None:
    gw = FakeGateway()
    cfg = _config(tape_gap=TapeGapConfig(unreliable_window_min=5))
    tape = TapeAnalyzer(cfg, gw, clock_fn=lambda: 100)
    await tape.start(["BTCUSDT"])
    await gw.feed(_trade(ts=100, price=50.0, qty=1.0, maker=False, agg_id=10))
    await gw.feed(_trade(ts=200, price=50.0, qty=1.0, maker=False, agg_id=15))  # gap=4
    assert tape.is_cvd_reliable("BTCUSDT") is False
    # Знімок теж знає про unreliable
    state = tape.get_windows("BTCUSDT")
    assert state.cvd_reliable is False


@pytest.mark.asyncio
async def test_unreliable_clears_after_window() -> None:
    gw = FakeGateway()
    now = [100]

    def clock() -> int:
        return now[0]

    cfg = _config(tape_gap=TapeGapConfig(unreliable_window_min=1))  # 1 хв
    tape = TapeAnalyzer(cfg, gw, clock_fn=clock)
    await tape.start(["BTCUSDT"])
    await gw.feed(_trade(ts=100, price=50.0, qty=1.0, maker=False, agg_id=1))
    await gw.feed(_trade(ts=110, price=50.0, qty=1.0, maker=False, agg_id=5))  # gap
    assert tape.is_cvd_reliable("BTCUSDT") is False

    now[0] = 110 + 60_001  # понад 1 хв
    assert tape.is_cvd_reliable("BTCUSDT") is True


@pytest.mark.asyncio
async def test_rolling_windows_aggregate_correctly() -> None:
    gw = FakeGateway()
    now = [0]
    tape = TapeAnalyzer(_config(), gw, clock_fn=lambda: now[0])
    await tape.start(["BTCUSDT"])
    # 4 трейди впродовж 100мс — мають бути у вікні 500ms.
    await gw.feed(_trade(ts=100, price=10.0, qty=1.0, maker=False, agg_id=1))   # buy 10
    await gw.feed(_trade(ts=150, price=10.0, qty=2.0, maker=True, agg_id=2))    # sell 20
    await gw.feed(_trade(ts=200, price=10.0, qty=3.0, maker=False, agg_id=3))   # buy 30
    await gw.feed(_trade(ts=300, price=10.0, qty=1.0, maker=True, agg_id=4))    # sell 10
    now[0] = 300

    state = tape.get_windows("BTCUSDT")
    w = state.window_500ms
    assert w.trade_count == 4
    assert w.buy_volume_usd == pytest.approx(40.0)
    assert w.sell_volume_usd == pytest.approx(30.0)
    assert state.delta_500ms == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_eviction_on_get_windows_when_no_recent_trades() -> None:
    gw = FakeGateway()
    now = [0]
    tape = TapeAnalyzer(_config(), gw, clock_fn=lambda: now[0])
    await tape.start(["BTCUSDT"])
    await gw.feed(_trade(ts=100, price=10.0, qty=1.0, maker=False, agg_id=1))
    # Просуваємо годинник далеко вперед — без жодного нового trade-у.
    now[0] = 60_000
    state = tape.get_windows("BTCUSDT")
    # Усі вікна (500ms, 2s, 10s) — порожні
    assert state.window_500ms.trade_count == 0
    assert state.window_2s.trade_count == 0
    assert state.window_10s.trade_count == 0
    # Last price лишається відомою (FeatureEngine на це розраховує)
    assert state.window_10s.last_trade_price == 10.0


@pytest.mark.asyncio
async def test_fan_out_to_subscribers() -> None:
    gw = FakeGateway()
    tape = TapeAnalyzer(_config(), gw, clock_fn=lambda: 0)
    await tape.start(["BTCUSDT"])

    received: list[RawAggTrade] = []

    async def cb(t: RawAggTrade) -> None:
        received.append(t)

    tape.on_trade(cb)
    await gw.feed(_trade(ts=100, price=10.0, qty=1.0, maker=False, agg_id=1))
    await gw.feed(_trade(ts=200, price=11.0, qty=2.0, maker=True, agg_id=2))
    assert len(received) == 2
    assert received[0].agg_id == 1
    assert received[1].agg_id == 2


@pytest.mark.asyncio
async def test_get_recent_trades_returns_tail() -> None:
    gw = FakeGateway()
    tape = TapeAnalyzer(_config(), gw, clock_fn=lambda: 0)
    await tape.start(["BTCUSDT"])
    for i in range(1, 11):
        await gw.feed(_trade(ts=i * 100, price=10.0, qty=1.0, maker=False, agg_id=i))
    recent = tape.get_recent_trades("BTCUSDT", n=3)
    assert [t.agg_id for t in recent] == [8, 9, 10]


@pytest.mark.asyncio
async def test_price_path_capped_at_maxlen() -> None:
    gw = FakeGateway()
    cfg = _config(price_path_maxlen=20)
    tape = TapeAnalyzer(cfg, gw, clock_fn=lambda: 0)
    await tape.start(["BTCUSDT"])
    for i in range(1, 31):
        await gw.feed(_trade(ts=i, price=float(i), qty=1.0, maker=False, agg_id=i))
    state = tape.get_windows("BTCUSDT")
    assert len(state.price_path) == 20
    assert state.price_path[-1] == (30, 30.0)


@pytest.mark.asyncio
async def test_unknown_symbol_returns_empty_state() -> None:
    gw = FakeGateway()
    tape = TapeAnalyzer(_config(), gw, clock_fn=lambda: 100)
    await tape.start(["BTCUSDT"])
    state = tape.get_windows("ETHUSDT")
    assert state.symbol == "ETHUSDT"
    assert state.window_500ms.trade_count == 0
    assert tape.get_cvd("ETHUSDT") == 0.0
    assert tape.get_recent_trades("ETHUSDT", 5) == []


@pytest.mark.asyncio
async def test_callback_exception_does_not_break_pipeline() -> None:
    gw = FakeGateway()
    tape = TapeAnalyzer(_config(), gw, clock_fn=lambda: 0)
    await tape.start(["BTCUSDT"])

    received: list[int] = []

    async def good(t: RawAggTrade) -> None:
        received.append(t.agg_id)

    async def bad(t: RawAggTrade) -> None:
        raise RuntimeError("boom")

    tape.on_trade(bad)
    tape.on_trade(good)

    await gw.feed(_trade(ts=100, price=10.0, qty=1.0, maker=False, agg_id=1))
    await gw.feed(_trade(ts=200, price=10.0, qty=1.0, maker=False, agg_id=2))
    # Поганий callback не зупинив pipeline, гарний усе одно отримав події
    assert received == [1, 2]
    # CVD теж рахується
    assert tape.get_cvd("BTCUSDT") == pytest.approx(20.0)
