"""Orchestrator — runtime-граф pipeline.

Тримає посилання на всі модулі. Маршрутизує події. Логує decision/setup/risk
події у Journal. Один вхідний хот-лупа: `on_tick(symbol, event_time_ms)`, що
будує snapshot і женить його через features → detector → decision → risk →
position. Повільний тик `on_slow_tick` — регласифікація режиму + heartbeat.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from scalper.book.engine import OrderBookEngine
from scalper.common import time as _time
from scalper.common.enums import AlertLevel
from scalper.common.types import TradePlan
from scalper.decision.engine import DecisionEngine
from scalper.execution.engine import ExecutionEngine
from scalper.expectancy.tracker import ExpectancyTracker
from scalper.features.engine import FeatureEngine
from scalper.features.types import Features, MarketSnapshot
from scalper.gateway.gateway import MarketDataGateway
from scalper.journal.logger import JournalLogger
from scalper.journal.types import EventKind, JournalEvent
from scalper.notifications.service import NotificationService
from scalper.position.manager import PositionManager
from scalper.regime.classifier import MarketRegime
from scalper.risk.engine import RiskEngine
from scalper.setups.detector import SetupDetector
from scalper.tape.analyzer import TapeFlowAnalyzer

logger = logging.getLogger(__name__)

ClockFn = Callable[[], int]
EquityFn = Callable[[], float]


class Orchestrator:
    def __init__(
        self,
        config: object,
        gateway: MarketDataGateway,
        features: FeatureEngine,
        regime: MarketRegime,
        detector: SetupDetector,
        decision: DecisionEngine,
        risk: RiskEngine,
        execution: ExecutionEngine,
        position: PositionManager,
        expectancy: ExpectancyTracker,
        journal: JournalLogger,
        notifier: NotificationService,
        *,
        book: OrderBookEngine | None = None,
        tape: TapeFlowAnalyzer | None = None,
        clock_fn: ClockFn | None = None,
        equity_fn: EquityFn | None = None,
    ) -> None:
        self._config = config
        self._gateway = gateway
        self._features = features
        self._regime = regime
        self._detector = detector
        self._decision = decision
        self._risk = risk
        self._execution = execution
        self._position = position
        self._expectancy = expectancy
        self._journal = journal
        self._notifier = notifier
        self._book = book
        self._tape = tape
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())
        self._equity: EquityFn = equity_fn if equity_fn is not None else (lambda: 1000.0)

        self._symbols: list[str] = []
        self._running = False
        self._seq = 0

        self._wire_callbacks()

    # === Wiring ===

    def _wire_callbacks(self) -> None:
        self._gateway.on_user_event(self._on_user_event)
        self._regime.on_regime_change(self._on_regime_change)

    async def _on_user_event(self, event: Any) -> None:
        payload = event.payload if hasattr(event, "payload") else event
        await self._execution.handle_user_event(payload)

    async def _on_regime_change(self, symbol: str, old_regime: Any, new_regime: Any) -> None:
        self._log(EventKind.REGIME_CHANGED, symbol=symbol, payload={
            "old": getattr(old_regime, "value", str(old_regime)),
            "new": getattr(new_regime, "value", str(new_regime)),
        })

    # === Lifecycle ===

    async def start(self, symbols: list[str]) -> None:
        self._symbols = list(symbols)
        self._journal.start()
        self._notifier.start()
        if self._book is not None:
            self._book.start(symbols)
        if self._tape is not None:
            self._tape.start(symbols)
        await self._gateway.start(symbols)
        self._regime.start(symbols)
        for sym in symbols:
            self._regime.reclassify(sym)
        self._running = True
        self._log(EventKind.STARTUP, payload={"symbols": symbols})

    async def stop(self) -> None:
        self._running = False
        for pos in list(self._position.all_open()):
            self._position.force_close(pos.plan.symbol, "shutdown")
        for sym in self._symbols:
            try:
                await self._execution.cancel_all(sym)
            except Exception as e:
                logger.warning("cancel_all failed for %s: %s", sym, e)
        self._regime.stop()
        if self._tape is not None:
            self._tape.stop()
        if self._book is not None:
            self._book.stop()
        await self._gateway.stop()
        self._log(EventKind.SHUTDOWN, payload={})
        self._notifier.stop()
        self._journal.stop()

    # === Hot loop ===

    async def on_tick(self, symbol: str, event_time_ms: int) -> None:
        if not self._running:
            return
        snapshot = self._build_snapshot(symbol, event_time_ms)
        if snapshot is None:
            return

        features = self._features.compute(snapshot)
        await self._run_pipeline(symbol, features)

    async def _run_pipeline(self, symbol: str, features: Features) -> None:
        self._position.on_features(features)

        if self._position.has_open_position(symbol):
            return
        if not self._regime.is_trading_allowed(symbol):
            return

        candidates = self._detector.detect(features)
        for cand in candidates:
            self._log(EventKind.SETUP_CANDIDATE_GENERATED, symbol=symbol, payload={
                "setup_type": cand.setup_type.value,
                "direction": cand.direction.value,
                "entry": cand.entry_price,
            })

        if not candidates:
            return

        decision_result = self._decision.decide(candidates)
        for rej in decision_result.rejected:
            self._log(EventKind.DECISION_REJECTED, symbol=symbol, payload={
                "setup_type": rej.candidate.setup_type.value,
                "reason": rej.reason,
                "score": rej.score,
                "threshold": rej.score_threshold,
            })
        if decision_result.accepted is None:
            return

        plan: TradePlan = decision_result.accepted
        self._log(EventKind.DECISION_ACCEPTED, symbol=symbol, payload={
            "setup_type": plan.setup_type.value,
            "score": plan.score,
            "regime": plan.regime.value,
        })

        risk_decision = self._risk.evaluate(plan, self._equity())
        if risk_decision.plan is None:
            self._log(EventKind.RISK_REJECTED, symbol=symbol, payload={
                "reason": risk_decision.reason,
                "setup_type": plan.setup_type.value,
            })
            return

        sized_plan = risk_decision.plan
        self._log(EventKind.RISK_ACCEPTED, symbol=symbol, payload={
            "qty": sized_plan.position_size,
            "risk_usd": sized_plan.risk_usd,
        })

        opened = await self._position.open(sized_plan)
        if opened:
            self._risk.on_position_opened(sized_plan)
            self._log(EventKind.POSITION_OPENED, symbol=symbol, payload={
                "setup_type": sized_plan.setup_type.value,
                "direction": sized_plan.direction.value,
                "entry": sized_plan.entry_price,
                "stop": sized_plan.stop_price,
            })

    def _build_snapshot(self, symbol: str, ts_ms: int) -> MarketSnapshot | None:
        if self._book is None or self._tape is None:
            return None
        try:
            book_state = self._book.get_book(symbol)
            tape_state = self._tape.get_windows(symbol)
        except Exception as e:
            logger.debug("snapshot build skip %s: %s", symbol, e)
            return None
        if not book_state.bids or not book_state.asks:
            return None
        last_price = tape_state.window_500ms.last_trade_price or book_state.bids[0].price
        spread = book_state.asks[0].price - book_state.bids[0].price
        return MarketSnapshot(
            timestamp_ms=ts_ms, symbol=symbol,
            book=book_state, tape=tape_state,
            last_price=last_price,
            spread_ticks=max(1, int(spread / 0.1)),   # fallback tick=0.1
            footprint=None,
        )

    # === Slow loop ===

    async def on_slow_tick(self) -> None:
        if not self._running:
            return
        for sym in self._symbols:
            self._regime.reclassify(sym)
        self._log(EventKind.HEARTBEAT, payload={
            "daily_r": self._risk.get_daily_r(),
            "monthly_r": self._risk.get_monthly_r(),
            "open_positions": len(self._position.all_open()),
        })

    # === Journal helper ===

    def _log(
        self, kind: EventKind, *, symbol: str | None = None,
        trade_id: str | None = None, payload: dict[str, Any],
    ) -> None:
        self._seq += 1
        event = JournalEvent(
            seq=self._seq, timestamp_ms=self._clock(), kind=kind,
            trade_id=trade_id, symbol=symbol, payload=payload,
        )
        self._journal.log(event)


__all__ = ["Orchestrator"]
