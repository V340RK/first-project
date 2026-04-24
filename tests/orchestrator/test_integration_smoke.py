"""End-to-end smoke: Orchestrator + real Risk/Position/Execution/Expectancy/Journal.

Feature computation і gateway — підмінені (не хочемо будувати справжню book/tape),
але все, що вище по пайплайну (decision → risk → position → execution → fill →
expectancy), — реальні інстанси. Тест доводить, що інтерфейси між модулями
сумісні і сесія може пройти від candidate до realized R.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scalper.common.enums import Direction, Regime, SetupType
from scalper.common.types import (
    DecisionResult,
    InvalidationCondition,
    InvalidationKind,
    SetupCandidate,
    TradePlan,
)
from scalper.book.types import OrderBookLevel, OrderBookState
from scalper.execution.types import SymbolFilters
from scalper.features.types import Features, MarketSnapshot
from scalper.tape.types import TapeWindow, TapeWindowsState
from scalper.expectancy import ExpectancyConfig, ExpectancyTracker
from scalper.journal.config import JournalConfig
from scalper.journal.logger import JournalLogger
from scalper.journal.types import EventKind
from scalper.orchestrator import Orchestrator
from scalper.position.config import PositionConfig
from scalper.position.manager import PositionManager
from scalper.replay.simulator import (
    FillPolicy,
    SimulatedExecutionEngine,
    SimulatorConfig,
    SlippageModel,
)
from scalper.risk.config import RiskConfig
from scalper.risk.engine import RiskEngine


# ============================================================
# Minimal fakes for upstream (gateway/features/regime/detector/decision/notifier)
# ============================================================

class _FakeGateway:
    def __init__(self) -> None:
        self._user_cbs: list = []
        self._trade_cbs: list = []

    def on_user_event(self, cb) -> None:   # type: ignore[no-untyped-def]
        self._user_cbs.append(cb)

    def on_agg_trade(self, cb) -> None:   # type: ignore[no-untyped-def]
        self._trade_cbs.append(cb)

    async def start(self, symbols) -> None:   # type: ignore[no-untyped-def]
        pass

    async def stop(self) -> None:
        pass


class _FakeFeatures:
    def compute(self, snapshot):   # type: ignore[no-untyped-def]
        return snapshot   # pass-through


class _FakeRegime:
    def on_regime_change(self, cb) -> None:   # type: ignore[no-untyped-def]
        pass

    def start(self, symbols) -> None:   # type: ignore[no-untyped-def]
        pass

    def stop(self) -> None:
        pass

    def reclassify(self, symbol: str) -> None:
        pass

    def is_trading_allowed(self, symbol: str) -> bool:
        return True


class _InjectedDetector:
    """Повертає наперед заданий список candidate-ів під час тесту."""

    def __init__(self) -> None:
        self.queue: list[list[SetupCandidate]] = []

    def detect(self, features) -> list[SetupCandidate]:   # type: ignore[no-untyped-def]
        return self.queue.pop(0) if self.queue else []


class _InjectedDecision:
    def __init__(self) -> None:
        self.queue: list[DecisionResult] = []

    def decide(self, candidates) -> DecisionResult:   # type: ignore[no-untyped-def]
        return self.queue.pop(0) if self.queue else DecisionResult(accepted=None, rejected=[])


class _FakeNotifier:
    def start(self) -> None: pass
    def stop(self) -> None: pass
    def send(self, text, level) -> None: pass   # type: ignore[no-untyped-def]


# ============================================================
# Factories
# ============================================================

SYMBOL = "BTCUSDT"

FILTERS = SymbolFilters(
    symbol=SYMBOL, tick_size=0.1, step_size=0.001,
    min_qty=0.001, max_qty=100.0, min_notional=5.0,
)


def _minimal_features(price: float, ts_ms: int) -> Features:
    empty_win = TapeWindow(
        duration_ms=500, trade_count=0, buy_volume_qty=0, sell_volume_qty=0,
        buy_volume_usd=0, sell_volume_usd=0,
        delta_qty=0, delta_usd=0,
        last_trade_price=price, first_trade_ms=ts_ms, last_trade_ms=ts_ms,
    )
    book = OrderBookState(
        symbol=SYMBOL, timestamp_ms=ts_ms, last_update_id=1,
        bids=[OrderBookLevel(price - 0.1, 10.0)],
        asks=[OrderBookLevel(price + 0.1, 10.0)], is_synced=True,
    )
    tape = TapeWindowsState(
        symbol=SYMBOL, timestamp_ms=ts_ms,
        window_500ms=empty_win,
        window_2s=TapeWindow(
            duration_ms=2000, trade_count=0, buy_volume_qty=0, sell_volume_qty=0,
            buy_volume_usd=0, sell_volume_usd=0,
            delta_qty=0, delta_usd=0,
            last_trade_price=price, first_trade_ms=ts_ms, last_trade_ms=ts_ms,
        ),
        window_10s=TapeWindow(
            duration_ms=10_000, trade_count=0, buy_volume_qty=0, sell_volume_qty=0,
            buy_volume_usd=0, sell_volume_usd=0,
            delta_qty=0, delta_usd=0,
            last_trade_price=price, first_trade_ms=ts_ms, last_trade_ms=ts_ms,
        ),
        cvd=0, cvd_reliable=True, delta_500ms=0, delta_2s=0, delta_10s=0, price_path=[],
    )
    snap = MarketSnapshot(
        timestamp_ms=ts_ms, symbol=SYMBOL, book=book, tape=tape,
        last_price=price, spread_ticks=1,
    )
    return Features(
        snapshot=snap,
        bid_ask_imbalance_5=0.0, bid_ask_imbalance_10=0.0,
        weighted_imbalance=0.0, book_pressure_side="NEUTRAL",
        delta_500ms=0, delta_2s=0, delta_10s=0, cvd=0,
        aggressive_buy_burst=False, aggressive_sell_burst=False, burst_size_usd=None,
        absorption_score=0, absorption_side="NONE",   # type: ignore[arg-type]
        spoof_score=0, spoof_side="NONE", micro_pullback=None,
        poc_offset_ticks=0, poc_location="MID",
        stacked_imbalance_long=False, stacked_imbalance_short=False,
        bar_finished=False, bar_delta=0,
        in_htf_poi=False, htf_poi_type=None, htf_poi_side=None, distance_to_poi_ticks=None,
    )


def _make_plan(entry: float, stop: float, tp1: float, tp2: float, tp3: float) -> TradePlan:
    cand = SetupCandidate(
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=Direction.LONG,
        symbol=SYMBOL, timestamp_ms=1000,
        entry_price=entry, stop_price=stop,
        tp1_price=tp1, tp2_price=tp2, tp3_price=tp3,
        stop_distance_ticks=int((entry - stop) / 0.1),
        invalidation_conditions=[
            InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={}),
        ],
        features_snapshot=None,   # type: ignore[arg-type]
    )
    return TradePlan(
        candidate=cand, setup_type=cand.setup_type, direction=cand.direction,
        symbol=cand.symbol, timestamp_ms=cand.timestamp_ms,
        entry_price=entry, stop_price=stop, tp1_price=tp1, tp2_price=tp2, tp3_price=tp3,
        stop_distance_ticks=cand.stop_distance_ticks,
        score=1.5, score_threshold=1.0, regime=Regime.NORMAL_BALANCED,
        expectancy_multiplier=1.0,
        invalidation_conditions=cand.invalidation_conditions, time_stop_ms=None,
    )


# ============================================================
# End-to-end: candidate → decision → risk → position → exec → fill → close
# ============================================================

@pytest.mark.asyncio
async def test_full_trade_lifecycle_real_modules(tmp_path: Path) -> None:
    # Timeline-controllable clock
    now = [1_000_000]
    clock = lambda: now[0]

    # Real modules
    journal = JournalLogger(
        JournalConfig(journal_dir=str(tmp_path / "journal")), clock_fn=clock,
    )
    risk = RiskEngine(RiskConfig(), clock_fn=clock)
    expectancy = ExpectancyTracker(ExpectancyConfig(), clock_fn=clock)

    sim_exec = SimulatedExecutionEngine(
        SimulatorConfig(
            limit_fill_policy=FillPolicy.TOUCH,
            slippage_model=SlippageModel.ZERO, latency_ms=0,
        ),
        clock_fn=clock,
    )
    sim_exec.register_symbol(FILTERS)

    position = PositionManager(
        PositionConfig(tick_size=0.1, entry_as_market=True),
        execution=sim_exec, risk=risk, clock_fn=clock,   # type: ignore[arg-type]
    )

    # Bridge: expectancy слухає закриті угоди з risk
    original_on_closed = risk.on_position_closed
    def wrap_on_closed(outcome) -> None:   # type: ignore[no-untyped-def]
        original_on_closed(outcome)
        expectancy.on_trade_outcome(outcome)
    risk.on_position_closed = wrap_on_closed   # type: ignore[assignment]

    # Upstream fakes
    gateway = _FakeGateway()
    detector = _InjectedDetector()
    decision = _InjectedDecision()

    orch = Orchestrator(
        config=None, gateway=gateway, features=_FakeFeatures(),   # type: ignore[arg-type]
        regime=_FakeRegime(), detector=detector, decision=decision,   # type: ignore[arg-type]
        risk=risk, execution=sim_exec, position=position,   # type: ignore[arg-type]
        expectancy=expectancy, journal=journal, notifier=_FakeNotifier(),   # type: ignore[arg-type]
        clock_fn=clock, equity_fn=lambda: 1000.0,
    )

    await orch.start([SYMBOL])

    # Orchestrator чекає свого candidate — inject його + accepted decision
    plan = _make_plan(entry=100.0, stop=99.5, tp1=100.5, tp2=101.0, tp3=101.5)
    detector.queue.append([plan.candidate])
    decision.queue.append(DecisionResult(accepted=plan, rejected=[]))

    # Book: ask=100.0 → MARKET entry філиться одразу
    sim_exec.update_book(SYMBOL, bid=99.9, ask=100.0, last_trade_price=99.95, tick_size=0.1)

    # Trigger pipeline через _run_pipeline (features тіло — пропускаємо snapshot)
    features = _minimal_features(price=100.0, ts_ms=now[0])
    await orch._run_pipeline(SYMBOL, features)

    # Перевірка: позиція відкрита після entry MARKET fill
    assert position.has_open_position(SYMBOL)
    pos = position.get(SYMBOL)
    assert pos is not None
    assert pos.remaining_qty > 0
    assert pos.avg_entry_price == pytest.approx(100.0)

    # Ціна проходить рівень TP1 → TP1 філиться (50% qty)
    sim_exec.update_book(SYMBOL, bid=100.4, ask=100.5, last_trade_price=100.5, tick_size=0.1)
    now[0] += 1000
    await sim_exec.on_clock_tick(now[0])
    # Далі TP2 (75% кумулятивно)
    sim_exec.update_book(SYMBOL, bid=101.0, ask=101.1, last_trade_price=101.0, tick_size=0.1)
    now[0] += 1000
    await sim_exec.on_clock_tick(now[0])
    # Нарешті TP3 (100%)
    sim_exec.update_book(SYMBOL, bid=101.5, ask=101.6, last_trade_price=101.5, tick_size=0.1)
    now[0] += 1000
    await sim_exec.on_clock_tick(now[0])

    # Позиція закрита, expectancy оновлений ненульовим R
    assert not position.has_open_position(SYMBOL)
    snap = expectancy.get(SetupType.ABSORPTION_REVERSAL, SYMBOL)
    assert snap is not None
    assert snap.samples == 1
    assert risk.get_daily_r() > 0

    await orch.stop()
