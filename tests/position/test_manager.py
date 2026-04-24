"""PositionManager — state machine: entry, protection, TP1→BE, TP2→trail, invalidation, close."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from scalper.book.types import OrderBookLevel, OrderBookState
from scalper.common.enums import Direction, Regime, SetupType
from scalper.common.types import (
    InvalidationCondition,
    InvalidationKind,
    SetupCandidate,
    TradePlan,
)
from scalper.execution import FillEvent, OrderRequest, OrderResult, OrderSide
from scalper.features.types import Features, MarketSnapshot
from scalper.position import PositionConfig, PositionManager, PositionState
from scalper.risk import RiskEngine, RiskConfig, TradeOutcome
from scalper.tape.types import TapeWindow, TapeWindowsState


# ============================================================
# Fake Execution
# ============================================================

@dataclass
class FakeExec:
    placed: list[OrderRequest] = field(default_factory=list)
    cancels: list[tuple[str, str]] = field(default_factory=list)
    cancel_all_for: list[str] = field(default_factory=list)
    _coid_counter: int = 0
    fill_cbs: list[Any] = field(default_factory=list)
    next_filled_qty: dict[int, float] = field(default_factory=dict)     # index → qty на момент place
    next_avg_price: dict[int, float] = field(default_factory=dict)

    def on_fill(self, cb: Any) -> None:
        self.fill_cbs.append(cb)

    def on_order_update(self, cb: Any) -> None:
        pass

    async def place_order(self, req: OrderRequest) -> OrderResult:
        self._coid_counter += 1
        coid = f"coid-{self._coid_counter}"
        idx = len(self.placed)
        self.placed.append(req)
        filled = self.next_filled_qty.get(idx, 0.0)
        avg = self.next_avg_price.get(idx)
        status = "FILLED" if filled >= req.qty and filled > 0 else "NEW"
        return OrderResult(
            success=True, client_order_id=coid, exchange_order_id=1000 + idx,
            status=status, filled_qty=filled, avg_fill_price=avg,
            error_code=None, error_msg=None,
            request_sent_ms=0, response_received_ms=0,
        )

    async def cancel_order(self, symbol: str, coid: str) -> OrderResult:
        self.cancels.append((symbol, coid))
        return OrderResult(
            success=True, client_order_id=coid, exchange_order_id=None,
            status="CANCELED", filled_qty=0, avg_fill_price=None,
            error_code=None, error_msg=None,
            request_sent_ms=0, response_received_ms=0,
        )

    async def cancel_all(self, symbol: str) -> list[OrderResult]:
        self.cancel_all_for.append(symbol)
        return []


# ============================================================
# Helpers
# ============================================================

def _empty_win(d: int) -> TapeWindow:
    return TapeWindow(
        duration_ms=d, trade_count=0, buy_volume_qty=0, sell_volume_qty=0,
        buy_volume_usd=0, sell_volume_usd=0,
        delta_qty=0, delta_usd=0, last_trade_price=0, first_trade_ms=0, last_trade_ms=0,
    )


def _features(
    *, last_price: float = 100.0, delta_2s: float = 0.0,
    weighted_imbalance: float = 0.0, absorption_score: float = 0.0,
    absorption_side: str = "NONE",
) -> Features:
    book = OrderBookState(
        symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
        bids=[OrderBookLevel(100.0, 10.0)], asks=[OrderBookLevel(100.1, 10.0)],
        is_synced=True,
    )
    tape = TapeWindowsState(
        symbol="BTCUSDT", timestamp_ms=0,
        window_500ms=_empty_win(500), window_2s=_empty_win(2000), window_10s=_empty_win(10_000),
        cvd=0, cvd_reliable=True, delta_500ms=0, delta_2s=0, delta_10s=0, price_path=[],
    )
    snap = MarketSnapshot(
        timestamp_ms=1000, symbol="BTCUSDT", book=book, tape=tape,
        last_price=last_price, spread_ticks=1,
    )
    return Features(
        snapshot=snap,
        bid_ask_imbalance_5=0.0, bid_ask_imbalance_10=0.0,
        weighted_imbalance=weighted_imbalance, book_pressure_side="NEUTRAL",
        delta_500ms=0.0, delta_2s=delta_2s, delta_10s=0.0, cvd=0.0,
        aggressive_buy_burst=False, aggressive_sell_burst=False, burst_size_usd=None,
        absorption_score=absorption_score, absorption_side=absorption_side,  # type: ignore[arg-type]
        spoof_score=0.0, spoof_side="NONE",
        micro_pullback=None, poc_offset_ticks=0, poc_location="MID",
        stacked_imbalance_long=False, stacked_imbalance_short=False,
        bar_finished=False, bar_delta=0.0,
        in_htf_poi=False, htf_poi_type=None, htf_poi_side=None, distance_to_poi_ticks=None,
    )


def _plan(
    *, direction: Direction = Direction.LONG,
    entry: float = 100.0, stop: float = 99.5,
    qty: float = 10.0, time_stop_ms: int | None = None,
    invalidations: list[InvalidationCondition] | None = None,
) -> TradePlan:
    invs = invalidations or [
        InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={"price": 99.6}),
    ]
    r_distance = abs(entry - stop)
    cand = SetupCandidate(
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=direction,
        symbol="BTCUSDT", timestamp_ms=0,
        entry_price=entry, stop_price=stop,
        tp1_price=entry + r_distance if direction == Direction.LONG else entry - r_distance,
        tp2_price=entry + 2 * r_distance if direction == Direction.LONG else entry - 2 * r_distance,
        tp3_price=entry + 3 * r_distance if direction == Direction.LONG else entry - 3 * r_distance,
        stop_distance_ticks=5,
        invalidation_conditions=invs,
        features_snapshot=_features(),
    )
    return TradePlan(
        candidate=cand,
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=direction,
        symbol="BTCUSDT", timestamp_ms=0,
        entry_price=entry, stop_price=stop,
        tp1_price=cand.tp1_price, tp2_price=cand.tp2_price, tp3_price=cand.tp3_price,
        stop_distance_ticks=5, score=1.5, score_threshold=1.0,
        regime=Regime.NORMAL_BALANCED, expectancy_multiplier=1.0,
        invalidation_conditions=invs, time_stop_ms=time_stop_ms,
        position_size=qty, risk_usd=5.0, risk_gate_passed=True,
    )


def _pm(
    exec_: FakeExec, *, now_ref: list[int] | None = None,
) -> tuple[PositionManager, RiskEngine]:
    risk = RiskEngine(RiskConfig(), clock_fn=lambda: 1_700_000_000_000)
    ref = now_ref if now_ref is not None else [1000]
    pm = PositionManager(
        PositionConfig(tick_size=0.1, breakeven_buffer_ticks=1,
                       trailing_distance_ticks=5, trailing_min_move_ticks=1,
                       entry_as_market=True),
        exec_, risk, clock_fn=lambda: ref[0],
    )
    return pm, risk


async def _fire_fill(exec_: FakeExec, fill: FillEvent) -> None:
    for cb in exec_.fill_cbs:
        await cb(fill)


def _mkfill(coid: str, qty: float, price: float, status: str = "FILLED") -> FillEvent:
    return FillEvent(
        symbol="BTCUSDT", client_order_id=coid, exchange_order_id=1,
        side=OrderSide.SELL, qty=qty, price=price, is_maker=False,
        commission_usd=0.1, filled_cumulative=qty, order_status=status,
        timestamp_ms=0, realized_pnl_usd=qty * 0.1,
    )


# ============================================================
# Tests
# ============================================================

@pytest.mark.asyncio
async def test_open_places_entry_order() -> None:
    exec_ = FakeExec()
    pm, _ = _pm(exec_)
    ok = await pm.open(_plan())
    assert ok is True
    assert pm.has_open_position("BTCUSDT") is True
    pos = pm.get("BTCUSDT")
    assert pos is not None and pos.state == PositionState.PENDING_ENTRY
    # Entry-ордер виставлено
    assert len(exec_.placed) == 1
    assert exec_.placed[0].side == OrderSide.BUY


@pytest.mark.asyncio
async def test_rejects_if_already_open() -> None:
    exec_ = FakeExec()
    pm, _ = _pm(exec_)
    await pm.open(_plan())
    ok = await pm.open(_plan())
    assert ok is False


@pytest.mark.asyncio
async def test_entry_fill_places_sl_and_3_tps() -> None:
    exec_ = FakeExec()
    pm, _ = _pm(exec_)
    await pm.open(_plan(qty=10.0))
    pos = pm.get("BTCUSDT")
    assert pos is not None

    # Фіксуємо entry fill
    await _fire_fill(exec_, _mkfill(pos.entry_coid, qty=10.0, price=100.0))

    # Після entry fill стан ACTIVE і виставлено 4 захисних ордери
    assert pos.state == PositionState.ACTIVE
    protection = exec_.placed[1:]
    assert len(protection) == 4     # SL + 3 TP
    assert pos.sl_coid is not None
    assert len(pos.tp_coids) == 3
    # TP split sizes 50/25/25 × 10 = 5, 2.5, 2.5
    tp_qtys = [p.qty for p in protection[1:]]
    assert tp_qtys == [5.0, 2.5, 2.5]


@pytest.mark.asyncio
async def test_tp1_fill_moves_stop_to_breakeven() -> None:
    exec_ = FakeExec()
    pm, _ = _pm(exec_)
    await pm.open(_plan(qty=10.0, entry=100.0, stop=99.5))
    pos = pm.get("BTCUSDT")
    assert pos is not None

    await _fire_fill(exec_, _mkfill(pos.entry_coid, qty=10.0, price=100.0))
    old_sl_coid = pos.sl_coid
    tp1_coid = pos.tp_coids[0]

    await _fire_fill(exec_, _mkfill(tp1_coid, qty=5.0, price=100.5))

    assert pos.state == PositionState.TP1_HIT
    # Старий SL скасовано
    assert (pos.symbol, old_sl_coid) in exec_.cancels
    # Виставлено новий SL на BE + 1 тік = 100.1
    assert pos.current_stop_price == pytest.approx(100.1)
    assert pos.sl_coid != old_sl_coid
    assert pos.realized_r == pytest.approx(0.5)   # 5/10 * 1R = 0.5R


@pytest.mark.asyncio
async def test_tp2_fill_activates_trailing() -> None:
    exec_ = FakeExec()
    pm, _ = _pm(exec_)
    await pm.open(_plan(qty=10.0, entry=100.0, stop=99.5))
    pos = pm.get("BTCUSDT")
    assert pos is not None
    await _fire_fill(exec_, _mkfill(pos.entry_coid, qty=10.0, price=100.0))
    await _fire_fill(exec_, _mkfill(pos.tp_coids[0], qty=5.0, price=100.5))
    await _fire_fill(exec_, _mkfill(pos.tp_coids[1], qty=2.5, price=101.0))

    assert pos.state == PositionState.TP2_HIT
    assert pos.trailing_active is True
    # realized_r = 0.5 (tp1) + 2*0.25 (tp2) = 1.0
    assert pos.realized_r == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_trailing_moves_stop_only_in_profit_direction_long() -> None:
    exec_ = FakeExec()
    pm, _ = _pm(exec_)
    await pm.open(_plan(qty=10.0, entry=100.0, stop=99.5))
    pos = pm.get("BTCUSDT")
    assert pos is not None
    await _fire_fill(exec_, _mkfill(pos.entry_coid, qty=10.0, price=100.0))
    await _fire_fill(exec_, _mkfill(pos.tp_coids[0], qty=5.0, price=100.5))
    await _fire_fill(exec_, _mkfill(pos.tp_coids[1], qty=2.5, price=101.0))

    # last=102, trail=5 тіків (0.5) → бажаний стоп 101.5, поточний був 100.1
    await pm.on_features(_features(last_price=102.0))
    assert pos.current_stop_price == pytest.approx(101.5)

    # last=101.8 (вниз) → стоп не рухається назад
    prev_stop = pos.current_stop_price
    await pm.on_features(_features(last_price=101.8))
    assert pos.current_stop_price == prev_stop


@pytest.mark.asyncio
async def test_invalidation_price_closes_position() -> None:
    exec_ = FakeExec()
    pm, risk = _pm(exec_)
    plan = _plan(qty=10.0, entry=100.0, stop=99.5, invalidations=[
        InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL,
                              params={"price": 99.8}),
    ])
    await pm.open(plan)
    pos = pm.get("BTCUSDT")
    assert pos is not None
    await _fire_fill(exec_, _mkfill(pos.entry_coid, qty=10.0, price=100.0))

    await pm.on_features(_features(last_price=99.7))

    # cancel_all викликано
    assert "BTCUSDT" in exec_.cancel_all_for
    # Позиція прибрана (CLOSED → видалена)
    assert pm.get("BTCUSDT") is None


@pytest.mark.asyncio
async def test_invalidation_ignored_after_tp1() -> None:
    exec_ = FakeExec()
    pm, _ = _pm(exec_)
    plan = _plan(qty=10.0, entry=100.0, stop=99.5, invalidations=[
        InvalidationCondition(kind=InvalidationKind.DELTA_FLIP, params={"threshold": 1000}),
    ])
    await pm.open(plan)
    pos = pm.get("BTCUSDT")
    assert pos is not None
    await _fire_fill(exec_, _mkfill(pos.entry_coid, qty=10.0, price=100.0))
    await _fire_fill(exec_, _mkfill(pos.tp_coids[0], qty=5.0, price=100.5))
    assert pos.state == PositionState.TP1_HIT
    cancels_before = len(exec_.cancel_all_for)
    await pm.on_features(_features(delta_2s=-99_999))
    assert len(exec_.cancel_all_for) == cancels_before   # позиція не закрита


@pytest.mark.asyncio
async def test_time_stop_triggers_close() -> None:
    exec_ = FakeExec()
    now_ref = [1000]
    pm, _ = _pm(exec_, now_ref=now_ref)
    plan = _plan(qty=10.0, entry=100.0, stop=99.5, time_stop_ms=5000)
    await pm.open(plan)
    pos = pm.get("BTCUSDT")
    assert pos is not None
    await _fire_fill(exec_, _mkfill(pos.entry_coid, qty=10.0, price=100.0))

    # Перевести clock за deadline (1000 + 5000 = 6000)
    now_ref[0] = 100_000
    await pm.on_features(_features(last_price=100.2))
    assert "BTCUSDT" in exec_.cancel_all_for
    assert pm.get("BTCUSDT") is None


@pytest.mark.asyncio
async def test_force_close() -> None:
    exec_ = FakeExec()
    pm, _ = _pm(exec_)
    await pm.open(_plan(qty=10.0))
    pos = pm.get("BTCUSDT")
    assert pos is not None
    await _fire_fill(exec_, _mkfill(pos.entry_coid, qty=10.0, price=100.0))

    ok = await pm.force_close("BTCUSDT", "manual")
    assert ok is True
    assert pm.get("BTCUSDT") is None


@pytest.mark.asyncio
async def test_full_tp3_path_reports_outcome_to_risk() -> None:
    exec_ = FakeExec()
    pm, risk = _pm(exec_)
    await pm.open(_plan(qty=10.0, entry=100.0, stop=99.5))
    pos = pm.get("BTCUSDT")
    assert pos is not None
    await _fire_fill(exec_, _mkfill(pos.entry_coid, qty=10.0, price=100.0))
    await _fire_fill(exec_, _mkfill(pos.tp_coids[0], qty=5.0, price=100.5))
    await _fire_fill(exec_, _mkfill(pos.tp_coids[1], qty=2.5, price=101.0))
    await _fire_fill(exec_, _mkfill(pos.tp_coids[2], qty=2.5, price=101.5))

    # Очікуємо: TP1 = 5/10 * 1R = 0.5; TP2 = 2.5/10 * 2R = 0.5; TP3 = 2.5/10 * 3R = 0.75 → 1.75R
    assert risk.get_daily_r() == pytest.approx(1.75, abs=1e-6)


@pytest.mark.asyncio
async def test_sl_hit_reports_negative_r() -> None:
    exec_ = FakeExec()
    pm, risk = _pm(exec_)
    await pm.open(_plan(qty=10.0, entry=100.0, stop=99.5))
    pos = pm.get("BTCUSDT")
    assert pos is not None
    await _fire_fill(exec_, _mkfill(pos.entry_coid, qty=10.0, price=100.0))
    assert pos.sl_coid is not None
    # SL на 99.5 спрацював
    await _fire_fill(exec_, _mkfill(pos.sl_coid, qty=10.0, price=99.5))

    assert risk.get_daily_r() == pytest.approx(-1.0, abs=1e-6)
    assert risk.get_loss_streak() == 1
    assert pm.get("BTCUSDT") is None
