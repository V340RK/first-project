"""PositionManager — REST-trust + reconcile fallback (для silent user-stream).

Покриває фікс #18a (trust REST executedQty) та #18b (REST poll reconcile)
з 14-journey doc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from scalper.common.enums import Direction, Regime, SetupType
from scalper.common.types import (
    InvalidationCondition,
    InvalidationKind,
    SetupCandidate,
    TradePlan,
)
from scalper.execution import FillEvent, OrderRequest, OrderResult, OrderSide
from scalper.position import PositionConfig, PositionManager, PositionState
from scalper.risk import RiskConfig, RiskEngine


@dataclass
class _FakeExec:
    """Мінімальний exec stub для відстеження placed orders + контролю responses."""
    placed: list[OrderRequest] = field(default_factory=list)
    cancels: list[tuple[str, str]] = field(default_factory=list)
    open_orders_returns: dict[str, list[dict]] = field(default_factory=dict)
    fill_cbs: list[Any] = field(default_factory=list)
    _coid_counter: int = 0
    next_filled_qty: dict[int, float] = field(default_factory=dict)
    next_avg_price: dict[int, float] = field(default_factory=dict)
    next_status: dict[int, str] = field(default_factory=dict)

    def on_fill(self, cb): self.fill_cbs.append(cb)
    def on_order_update(self, cb): pass

    async def place_order(self, req: OrderRequest) -> OrderResult:
        self._coid_counter += 1
        coid = f"coid-{self._coid_counter}"
        idx = len(self.placed)
        self.placed.append(req)
        filled = self.next_filled_qty.get(idx, 0.0)
        return OrderResult(
            success=True, client_order_id=coid, exchange_order_id=1000 + idx,
            status=self.next_status.get(idx, "NEW"),
            filled_qty=filled, avg_fill_price=self.next_avg_price.get(idx),
            error_code=None, error_msg=None,
            request_sent_ms=0, response_received_ms=0,
        )

    async def cancel_order(self, symbol, coid):
        self.cancels.append((symbol, coid))
        return OrderResult(success=True, client_order_id=coid, exchange_order_id=None,
                          status="CANCELED", filled_qty=0, avg_fill_price=None,
                          error_code=None, error_msg=None,
                          request_sent_ms=0, response_received_ms=0)

    async def cancel_all(self, symbol): return []

    async def get_open_orders(self, symbol):
        return self.open_orders_returns.get(symbol, [])

    position_risk_returns: list[dict] = field(default_factory=list)
    async def get_position_risk(self, symbol):
        return self.position_risk_returns


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
        features_snapshot=None,   # type: ignore[arg-type]
    )
    return TradePlan(
        candidate=cand, setup_type=cand.setup_type, direction=cand.direction,
        symbol=cand.symbol, timestamp_ms=0,
        entry_price=100.0, stop_price=99.5,
        tp1_price=100.5, tp2_price=101.0, tp3_price=101.5,
        stop_distance_ticks=5,
        score=1.5, score_threshold=1.0, regime=Regime.NORMAL_BALANCED,
        expectancy_multiplier=1.0, position_size=0.1, risk_usd=0.5,
        risk_gate_passed=True,
        invalidation_conditions=cand.invalidation_conditions, time_stop_ms=None,
    )


def _mgr(now_ref: list[int]) -> tuple[PositionManager, _FakeExec, RiskEngine]:
    exec_ = _FakeExec()
    risk = RiskEngine(RiskConfig(), clock_fn=lambda: now_ref[0])
    cfg = PositionConfig(tick_size=0.1, entry_as_market=False)
    mgr = PositionManager(cfg, exec_, risk, clock_fn=lambda: now_ref[0])   # type: ignore[arg-type]
    return mgr, exec_, risk


# ============================================================
# Fix #18a: trust REST place_order response when executedQty>0
# ============================================================

@pytest.mark.asyncio
async def test_rest_response_with_filled_qty_triggers_protection() -> None:
    """Якщо REST повернув filled_qty > 0 (навіть status=NEW для PARTIAL),
    PositionManager одразу виставляє SL+TP, не чекаючи WS fill."""
    now_ref = [1000]
    mgr, exec_, _ = _mgr(now_ref)
    # Перший place — entry; повертаємо filled_qty=0.1 (full fill через IOC)
    exec_.next_filled_qty[0] = 0.1
    exec_.next_avg_price[0] = 100.05
    exec_.next_status[0] = "FILLED"

    opened = await mgr.open(_plan())
    assert opened is True
    pos = mgr.get("BTCUSDT")
    assert pos is not None
    assert pos.state == PositionState.ACTIVE
    assert pos.entry_processed_via_rest is True
    assert pos.filled_qty == 0.1
    # 1 entry + 1 SL + 3 TP = 5 placed orders
    assert len(exec_.placed) == 5
    types = [r.type.value for r in exec_.placed[1:]]
    assert "STOP_MARKET" in types
    assert types.count("TAKE_PROFIT_MARKET") == 3


@pytest.mark.asyncio
async def test_rest_response_with_partial_fill_also_triggers_protection() -> None:
    """Status=NEW але filled_qty>0 (часткове виконання LIMIT IOC) — теж protect."""
    now_ref = [1000]
    mgr, exec_, _ = _mgr(now_ref)
    exec_.next_filled_qty[0] = 0.05   # частково
    exec_.next_avg_price[0] = 100.04
    exec_.next_status[0] = "NEW"      # навіть без FILLED status

    await mgr.open(_plan())
    pos = mgr.get("BTCUSDT")
    assert pos.state == PositionState.ACTIVE   # все одно ACTIVE
    assert pos.entry_processed_via_rest is True
    assert pos.filled_qty == 0.05
    # protection placed
    assert len(exec_.placed) == 5


@pytest.mark.asyncio
async def test_ws_fill_for_already_processed_entry_is_deduped() -> None:
    """REST дав filled_qty>0 і protection placed. Якщо потім WS приносить
    той же fill для entry_coid — він має ігноруватись (інакше commission
    подвоюється і _handle_entry_fill перезапускає _place_protection)."""
    now_ref = [1000]
    mgr, exec_, _ = _mgr(now_ref)
    exec_.next_filled_qty[0] = 0.1
    exec_.next_avg_price[0] = 100.05
    exec_.next_status[0] = "FILLED"

    await mgr.open(_plan())
    pos = mgr.get("BTCUSDT")
    placed_before_ws = len(exec_.placed)
    fees_before = pos.fees_usd

    # Synthetic WS fill для того ж entry_coid
    ws_fill = FillEvent(
        symbol="BTCUSDT", client_order_id=pos.entry_coid,
        exchange_order_id=1000, side=OrderSide.BUY,
        qty=0.1, price=100.05, is_maker=False,
        commission_usd=0.04, filled_cumulative=0.1,
        order_status="FILLED", timestamp_ms=2000, realized_pnl_usd=0.0,
    )
    for cb in exec_.fill_cbs:
        await cb(ws_fill)

    # Нічого не додалось (ні нові orders, ні commission)
    assert len(exec_.placed) == placed_before_ws
    assert pos.fees_usd == fees_before


# ============================================================
# Fix #18b: REST poll reconcile for stuck PENDING_ENTRY
# ============================================================

@pytest.mark.asyncio
async def test_reconcile_uses_real_position_amt() -> None:
    """Position у PENDING_ENTRY > stale_after_ms. open_orders без entry_coid,
    positionRisk показує що реально на біржі є 0.04 BTC (з ordered 0.1 — IOC
    partial fill). Reconcile має використати РЕАЛЬНУ qty, не plan.position_size,
    інакше SL з reduce_only буде reject."""
    now_ref = [1000]
    mgr, exec_, _ = _mgr(now_ref)
    await mgr.open(_plan())
    pos = mgr.get("BTCUSDT")
    assert pos.state == PositionState.PENDING_ENTRY

    now_ref[0] += 6000
    exec_.open_orders_returns["BTCUSDT"] = []
    exec_.position_risk_returns = [
        {"symbol": "BTCUSDT", "positionAmt": "0.04", "entryPrice": "100.07"},
    ]

    await mgr.reconcile_pending_entries(stale_after_ms=5000)

    assert pos.state == PositionState.ACTIVE
    assert pos.entry_processed_via_rest is True
    assert pos.filled_qty == 0.04   # з positionRisk, не з plan.position_size (0.1)
    assert pos.avg_entry_price == 100.07
    assert len(exec_.placed) == 5   # entry + SL + 3 TP


@pytest.mark.asyncio
async def test_reconcile_drops_position_when_no_actual_fill() -> None:
    """positionRisk показує amt=0 → ордер expired/cancelled без fill →
    локальну position видаляємо без spam-у."""
    now_ref = [1000]
    mgr, exec_, _ = _mgr(now_ref)
    await mgr.open(_plan())
    pos = mgr.get("BTCUSDT")

    now_ref[0] += 6000
    exec_.open_orders_returns["BTCUSDT"] = []
    exec_.position_risk_returns = []   # нема позиції на біржі

    await mgr.reconcile_pending_entries(stale_after_ms=5000)

    assert mgr.get("BTCUSDT") is None
    assert len(exec_.placed) == 1   # лише оригінальний entry (без protection)


@pytest.mark.asyncio
async def test_reconcile_skips_recent_pending() -> None:
    """Свіжа PENDING_ENTRY (< stale_after_ms) не чіпається."""
    now_ref = [1000]
    mgr, exec_, _ = _mgr(now_ref)
    await mgr.open(_plan())
    pos = mgr.get("BTCUSDT")

    now_ref[0] += 1000   # лише 1с
    await mgr.reconcile_pending_entries(stale_after_ms=5000)

    assert pos.state == PositionState.PENDING_ENTRY   # без змін
    assert len(exec_.placed) == 1


@pytest.mark.asyncio
async def test_reconcile_skips_when_order_still_open() -> None:
    """Stale PENDING + entry_coid ВСЕ ЩЕ в open_orders → не trogаємо."""
    now_ref = [1000]
    mgr, exec_, _ = _mgr(now_ref)
    await mgr.open(_plan())
    pos = mgr.get("BTCUSDT")
    coid = pos.entry_coid

    now_ref[0] += 6000
    exec_.open_orders_returns["BTCUSDT"] = [{"clientOrderId": coid}]

    await mgr.reconcile_pending_entries(stale_after_ms=5000)

    assert pos.state == PositionState.PENDING_ENTRY
    assert len(exec_.placed) == 1


@pytest.mark.asyncio
async def test_reconcile_skips_already_processed_via_rest() -> None:
    """Position уже ACTIVE через #18a. Reconcile не повинен її чіпати."""
    now_ref = [1000]
    mgr, exec_, _ = _mgr(now_ref)
    exec_.next_filled_qty[0] = 0.1
    exec_.next_status[0] = "FILLED"
    await mgr.open(_plan())
    pos = mgr.get("BTCUSDT")
    assert pos.state == PositionState.ACTIVE
    placed_before = len(exec_.placed)

    now_ref[0] += 10_000
    await mgr.reconcile_pending_entries(stale_after_ms=5000)

    assert len(exec_.placed) == placed_before   # нічого не додалось
