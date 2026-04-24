"""Orchestrator — wiring через fake модулі (без реального gateway/execution)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from scalper.common.enums import Direction, SetupType
from scalper.common.types import (
    DecisionResult,
    InvalidationCondition,
    InvalidationKind,
    RejectedCandidate,
    SetupCandidate,
    TradePlan,
)
from scalper.common.enums import Regime
from scalper.journal.types import EventKind, JournalEvent
from scalper.orchestrator import Orchestrator
from scalper.risk.engine import RiskDecision


# ============================================================
# Fakes
# ============================================================

@dataclass
class FakeGateway:
    user_cbs: list = field(default_factory=list)

    def on_user_event(self, cb) -> None:   # type: ignore[no-untyped-def]
        self.user_cbs.append(cb)

    async def start(self, symbols):   # type: ignore[no-untyped-def]
        self.started_symbols = symbols

    async def stop(self) -> None:
        pass


class FakeFeatures:
    def __init__(self, out: Any) -> None:
        self._out = out

    def compute(self, snapshot):   # type: ignore[no-untyped-def]
        return self._out


class FakeRegime:
    def __init__(self) -> None:
        self.cb = None
        self.started: list[str] = []
        self.reclassified: list[str] = []
        self.trading_ok = True

    def on_regime_change(self, cb) -> None:   # type: ignore[no-untyped-def]
        self.cb = cb

    def start(self, symbols) -> None:   # type: ignore[no-untyped-def]
        self.started = symbols

    def stop(self) -> None:
        pass

    def reclassify(self, symbol: str) -> None:
        self.reclassified.append(symbol)

    def is_trading_allowed(self, symbol: str) -> bool:
        return self.trading_ok


class FakeDetector:
    def __init__(self, candidates: list[SetupCandidate]) -> None:
        self._c = candidates

    def detect(self, features) -> list[SetupCandidate]:   # type: ignore[no-untyped-def]
        return self._c


class FakeDecision:
    def __init__(self, result: DecisionResult) -> None:
        self._r = result
        self.seen: list = []

    def decide(self, candidates):   # type: ignore[no-untyped-def]
        self.seen.append(candidates)
        return self._r


class FakeRisk:
    def __init__(self, decision: RiskDecision) -> None:
        self._d = decision
        self.opened: list = []
        self.closed: list = []

    def evaluate(self, plan, equity_usd):   # type: ignore[no-untyped-def]
        return self._d

    def on_position_opened(self, plan) -> None:   # type: ignore[no-untyped-def]
        self.opened.append(plan)

    def on_position_closed(self, outcome) -> None:   # type: ignore[no-untyped-def]
        self.closed.append(outcome)

    def get_daily_r(self) -> float: return 0.0
    def get_monthly_r(self) -> float: return 0.0


class FakeExec:
    def __init__(self) -> None:
        self.events: list = []
        self.cancelled_symbols: list[str] = []

    async def handle_user_event(self, event) -> None:   # type: ignore[no-untyped-def]
        self.events.append(event)

    async def cancel_all(self, symbol: str):   # type: ignore[no-untyped-def]
        self.cancelled_symbols.append(symbol)
        return []


class FakePosition:
    def __init__(self, *, open_ok: bool = True, has_open: bool = False) -> None:
        self._open_ok = open_ok
        self._has_open = has_open
        self.opens: list = []
        self.features_seen: list = []

    async def open(self, plan) -> bool:   # type: ignore[no-untyped-def]
        self.opens.append(plan)
        return self._open_ok

    def has_open_position(self, symbol: str) -> bool:
        return self._has_open

    def on_features(self, f) -> None:   # type: ignore[no-untyped-def]
        self.features_seen.append(f)

    def all_open(self) -> list:
        return []

    def force_close(self, symbol: str, reason: str) -> bool:
        return False


class FakeExpectancy:
    pass


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[JournalEvent] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def log(self, event: JournalEvent) -> None:
        self.events.append(event)


class FakeNotifier:
    def start(self) -> None: pass
    def stop(self) -> None: pass
    def send(self, text: str, level) -> None: pass   # type: ignore[no-untyped-def]


# ============================================================
# Factories
# ============================================================

def _plan(*, score: float = 1.5, position_size: float = 0.1) -> TradePlan:
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
        entry_price=cand.entry_price, stop_price=cand.stop_price,
        tp1_price=cand.tp1_price, tp2_price=cand.tp2_price, tp3_price=cand.tp3_price,
        stop_distance_ticks=5,
        score=score, score_threshold=1.0, regime=Regime.NORMAL_BALANCED,
        expectancy_multiplier=1.0, position_size=position_size, risk_usd=10.0,
        risk_gate_passed=True, invalidation_conditions=cand.invalidation_conditions,
        time_stop_ms=None,
    )


def _candidate() -> SetupCandidate:
    return _plan().candidate


def _orch(**overrides) -> tuple[Orchestrator, dict[str, Any]]:   # type: ignore[no-untyped-def]
    gw = overrides.get("gateway", FakeGateway())
    feat = overrides.get("features", FakeFeatures("FEATS"))
    reg = overrides.get("regime", FakeRegime())
    det = overrides.get("detector", FakeDetector([_candidate()]))
    dec = overrides.get("decision", FakeDecision(DecisionResult(accepted=_plan(), rejected=[])))
    risk = overrides.get("risk", FakeRisk(RiskDecision(plan=_plan(), reason="accepted", snapshot=None)))
    exc = overrides.get("execution", FakeExec())
    pos = overrides.get("position", FakePosition())
    expc = overrides.get("expectancy", FakeExpectancy())
    jrn = overrides.get("journal", FakeJournal())
    ntf = overrides.get("notifier", FakeNotifier())

    orch = Orchestrator(
        config=None, gateway=gw, features=feat, regime=reg,   # type: ignore[arg-type]
        detector=det, decision=dec, risk=risk,   # type: ignore[arg-type]
        execution=exc, position=pos, expectancy=expc,   # type: ignore[arg-type]
        journal=jrn, notifier=ntf,   # type: ignore[arg-type]
        clock_fn=lambda: 1000, equity_fn=lambda: 500.0,
    )
    return orch, {
        "gw": gw, "feat": feat, "regime": reg, "det": det, "dec": dec,
        "risk": risk, "exec": exc, "pos": pos, "journal": jrn,
    }


# ============================================================
# Lifecycle
# ============================================================

@pytest.mark.asyncio
async def test_start_wires_symbols_and_starts_journal() -> None:
    orch, refs = _orch()
    await orch.start(["BTCUSDT", "ETHUSDT"])
    assert refs["journal"].started is True
    assert refs["gw"].started_symbols == ["BTCUSDT", "ETHUSDT"]
    assert refs["regime"].reclassified == ["BTCUSDT", "ETHUSDT"]
    kinds = [e.kind for e in refs["journal"].events]
    assert EventKind.STARTUP in kinds


@pytest.mark.asyncio
async def test_stop_cancels_orders_and_closes_journal() -> None:
    orch, refs = _orch()
    await orch.start(["BTCUSDT"])
    await orch.stop()
    assert refs["exec"].cancelled_symbols == ["BTCUSDT"]
    assert refs["journal"].started is False
    kinds = [e.kind for e in refs["journal"].events]
    assert EventKind.SHUTDOWN in kinds


# ============================================================
# Hot loop pipeline
# ============================================================

@pytest.mark.asyncio
async def test_pipeline_end_to_end_accepted() -> None:
    orch, refs = _orch()
    await orch.start(["BTCUSDT"])
    # orchestrator не має book/tape → _build_snapshot повертає None → run_pipeline
    # не запускається. Викличемо _run_pipeline напряму з fake-features.
    await orch._run_pipeline("BTCUSDT", "FEATURES_MOCK")   # type: ignore[arg-type]

    assert len(refs["pos"].opens) == 1
    assert refs["pos"].opens[0].symbol == "BTCUSDT"
    assert len(refs["risk"].opened) == 1
    kinds = [e.kind for e in refs["journal"].events]
    assert EventKind.SETUP_CANDIDATE_GENERATED in kinds
    assert EventKind.DECISION_ACCEPTED in kinds
    assert EventKind.RISK_ACCEPTED in kinds
    assert EventKind.POSITION_OPENED in kinds


@pytest.mark.asyncio
async def test_pipeline_skips_when_position_open() -> None:
    pos = FakePosition(has_open=True)
    orch, refs = _orch(position=pos)
    await orch.start(["BTCUSDT"])
    await orch._run_pipeline("BTCUSDT", "F")   # type: ignore[arg-type]
    assert len(pos.opens) == 0
    # on_features все одно має викликатися для трейлінгу/інвалідації
    assert len(pos.features_seen) == 1


@pytest.mark.asyncio
async def test_pipeline_skips_when_regime_blocks() -> None:
    reg = FakeRegime()
    reg.trading_ok = False
    orch, refs = _orch(regime=reg)
    await orch.start(["BTCUSDT"])
    await orch._run_pipeline("BTCUSDT", "F")   # type: ignore[arg-type]
    assert len(refs["pos"].opens) == 0


@pytest.mark.asyncio
async def test_decision_rejection_logged() -> None:
    cand = _candidate()
    rej = RejectedCandidate(candidate=cand, reason="low_score", score=0.3, score_threshold=1.0)
    dec = FakeDecision(DecisionResult(accepted=None, rejected=[rej]))
    orch, refs = _orch(decision=dec)
    await orch.start(["BTCUSDT"])
    await orch._run_pipeline("BTCUSDT", "F")   # type: ignore[arg-type]
    kinds = [e.kind for e in refs["journal"].events]
    assert EventKind.DECISION_REJECTED in kinds
    assert len(refs["pos"].opens) == 0


@pytest.mark.asyncio
async def test_risk_rejection_stops_pipeline() -> None:
    risk = FakeRisk(RiskDecision(plan=None, reason="daily_loss_limit", snapshot=None))
    orch, refs = _orch(risk=risk)
    await orch.start(["BTCUSDT"])
    await orch._run_pipeline("BTCUSDT", "F")   # type: ignore[arg-type]
    kinds = [e.kind for e in refs["journal"].events]
    assert EventKind.RISK_REJECTED in kinds
    assert len(refs["pos"].opens) == 0


@pytest.mark.asyncio
async def test_user_event_routes_to_execution() -> None:
    orch, refs = _orch()
    await orch.start(["BTCUSDT"])
    # симулюємо user event від gateway
    for cb in refs["gw"].user_cbs:
        # event with .payload attribute
        class E:
            payload = {"some": "order"}
        await cb(E())
    assert refs["exec"].events == [{"some": "order"}]


# ============================================================
# Slow loop
# ============================================================

@pytest.mark.asyncio
async def test_slow_tick_reclassifies_and_heartbeats() -> None:
    orch, refs = _orch()
    await orch.start(["BTCUSDT", "ETHUSDT"])
    # reset reclassification counter that start already populated
    refs["regime"].reclassified.clear()
    await orch.on_slow_tick()
    assert refs["regime"].reclassified == ["BTCUSDT", "ETHUSDT"]
    hb_events = [e for e in refs["journal"].events if e.kind == EventKind.HEARTBEAT]
    assert len(hb_events) == 1
    assert "daily_r" in hb_events[0].payload


@pytest.mark.asyncio
async def test_slow_tick_noop_before_start() -> None:
    orch, refs = _orch()
    await orch.on_slow_tick()
    hb_events = [e for e in refs["journal"].events if e.kind == EventKind.HEARTBEAT]
    assert hb_events == []
