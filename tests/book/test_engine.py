"""OrderBookEngine: init flow, gap→reinit, bar close, queries — з фейковим Gateway."""

from __future__ import annotations

import asyncio

import pytest

from scalper.book.config import OBConfig, OBReinitConfig
from scalper.book.engine import OrderBookEngine
from scalper.gateway.types import DepthSnapshot, RawAggTrade, RawDepthDiff, SymbolFilters


class FakeGateway:
    """Достатньо, щоб OrderBookEngine працював: callback-и diff/trade, snapshot, filters."""

    def __init__(self, snapshot_factory) -> None:
        self._snapshot_factory = snapshot_factory
        self._depth_cb = None
        self._trade_cb = None

    def on_depth_diff(self, cb) -> None:
        self._depth_cb = cb

    def on_agg_trade(self, cb) -> None:
        self._trade_cb = cb

    async def fetch_depth_snapshot(self, symbol, limit=1000) -> DepthSnapshot:
        return self._snapshot_factory(symbol)

    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        return SymbolFilters(
            tick_size=0.1, step_size=0.001, min_qty=0.001, max_qty=1000.0,
            min_notional=5.0, price_precision=2, qty_precision=3,
        )

    async def feed_diff(self, diff: RawDepthDiff) -> None:
        assert self._depth_cb is not None
        await self._depth_cb(diff)

    async def feed_trade(self, trade: RawAggTrade) -> None:
        assert self._trade_cb is not None
        await self._trade_cb(trade)


def _snap(last_id: int) -> DepthSnapshot:
    return DepthSnapshot(
        symbol="BTCUSDT", last_update_id=last_id,
        bids=[(100.0, 1.0), (99.0, 2.0)],
        asks=[(101.0, 1.0), (102.0, 1.0)],
        timestamp_ms=1000,
    )


def _diff(U: int, u: int, **kw) -> RawDepthDiff:
    return RawDepthDiff(symbol="BTCUSDT", first_update_id=U, final_update_id=u,
                         bids=kw.get("bids", []), asks=kw.get("asks", []))


def _trade(price: float, qty: float, ts: int, is_maker: bool = False) -> RawAggTrade:
    return RawAggTrade(timestamp_ms=ts, symbol="BTCUSDT", price=price, quantity=qty,
                         is_buyer_maker=is_maker, agg_id=0)


@pytest.mark.asyncio
async def test_engine_init_applies_snapshot_and_replays_buffer() -> None:
    gw = FakeGateway(lambda sym: _snap(last_id=100))
    config = OBConfig(timeframes=["1m"], reinit=OBReinitConfig(warmup_diff_timeout_ms=2000))
    engine = OrderBookEngine(config, gw, clock_fn=lambda: 1000)

    # Шлюзуємо diff раніше за snapshot: engine має їх буферизувати.
    start_task = asyncio.create_task(engine.start(["BTCUSDT"]))
    await asyncio.sleep(0.05)
    # Один diff, який «старіший» за snapshot — має бути пропущений
    await gw.feed_diff(_diff(U=80, u=95, bids=[(99.0, 5.0)]))
    # Валідний перший: U=99 <= 101 <= u=105
    await gw.feed_diff(_diff(U=99, u=105, bids=[(100.0, 0.0), (98.0, 3.0)]))
    # Подальший — суворо U == prev_u + 1 = 106
    await gw.feed_diff(_diff(U=106, u=108, asks=[(103.0, 4.0)]))
    await start_task

    try:
        book = engine.get_book("BTCUSDT")
        assert book.is_synced is True
        # bid 100.0 видалено, bid 98.0 додано, ask 103.0 додано
        bid_prices = [lv.price for lv in book.bids]
        ask_prices = [lv.price for lv in book.asks]
        assert 100.0 not in bid_prices
        assert 98.0 in bid_prices
        assert 103.0 in ask_prices
        assert book.last_update_id == 108
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_engine_detects_gap_and_schedules_reinit() -> None:
    # Підготуємо дві версії snapshot: перша — last=100, після gap друга — last=200.
    snapshots = [_snap(last_id=100), _snap(last_id=200)]

    def factory(sym: str) -> DepthSnapshot:
        return snapshots.pop(0) if snapshots else _snap(last_id=200)

    gw = FakeGateway(factory)
    engine = OrderBookEngine(OBConfig(timeframes=["1m"]), gw, clock_fn=lambda: 1000)

    start = asyncio.create_task(engine.start(["BTCUSDT"]))
    await asyncio.sleep(0.05)
    await gw.feed_diff(_diff(99, 105))
    await gw.feed_diff(_diff(106, 108))
    await start

    assert engine.get_book("BTCUSDT").is_synced is True

    # Введемо штучний gap: очікуємо U=109, шлемо U=300.
    await gw.feed_diff(_diff(300, 310))
    # Книга має одразу позначитись як НЕ synced і запланувати reinit.
    await asyncio.sleep(0.05)
    assert engine.get_book("BTCUSDT").is_synced is False

    # Reinit-таск чекає на новий diff. Шлемо валідний для НОВОГО snapshot (last=200):
    # U<=201<=u, виходимо з єдиного diff-у щоб не плутати порядок.
    await gw.feed_diff(_diff(201, 205, bids=[(100.0, 7.0)]))
    await asyncio.sleep(0.3)

    # Після reinit книга знову synced + застосувався diff (видно по новому qty bid 100→7).
    book = engine.get_book("BTCUSDT")
    assert book.is_synced is True
    assert book.last_update_id == 205
    assert any(lv.price == 100.0 and lv.size == 7.0 for lv in book.bids)

    await engine.stop()


@pytest.mark.asyncio
async def test_trade_updates_footprint_and_closes_bar() -> None:
    gw = FakeGateway(lambda sym: _snap(last_id=100))
    config = OBConfig(timeframes=["1m"], closed_history_size=10)
    engine = OrderBookEngine(config, gw, clock_fn=lambda: 0)

    closed_bars: list = []
    engine.on_bar_close(lambda bar: _append(closed_bars, bar))

    # Швидкий init
    start = asyncio.create_task(engine.start(["BTCUSDT"]))
    await asyncio.sleep(0.05)
    await gw.feed_diff(_diff(99, 101))
    await start

    # Bar 1m: [0, 60_000). Закинемо 3 trade-и всередині, потім 1 у наступному барі.
    await gw.feed_trade(_trade(100.0, 1.0, ts=100))
    await gw.feed_trade(_trade(100.1, 2.0, ts=200, is_maker=True))
    await gw.feed_trade(_trade(100.2, 1.5, ts=300))
    # Перетинаємо границю бара
    await gw.feed_trade(_trade(100.3, 0.5, ts=61_000))

    # Перший бар має закритись і потрапити в callback
    await asyncio.sleep(0.05)
    assert len(closed_bars) == 1
    closed = closed_bars[0]
    assert closed.is_closed is True
    assert closed.open == 100.0
    assert closed.high == 100.2
    assert closed.close == 100.2
    assert closed.trade_count == 3
    # delta = ask_vol - bid_vol. Купівлі: 1+1.5=2.5, продажі: 2. delta=0.5
    assert closed.delta == pytest.approx(0.5)

    # Поточний бар містить лише останній trade
    current = engine.get_current_footprint("BTCUSDT", "1m")
    assert current.trade_count == 1
    assert current.open_time_ms == 60_000

    # Історія бачить 1 закритий бар
    recent = engine.get_recent_footprints("BTCUSDT", "1m", n=5)
    assert len(recent) == 1

    await engine.stop()


async def _append(lst, item) -> None:
    lst.append(item)


@pytest.mark.asyncio
async def test_get_book_raises_for_unknown_symbol() -> None:
    gw = FakeGateway(lambda sym: _snap(100))
    engine = OrderBookEngine(OBConfig(timeframes=["1m"]), gw, clock_fn=lambda: 0)
    with pytest.raises(RuntimeError):
        engine.get_book("NEVER_SEEN")
