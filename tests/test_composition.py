"""Smoke: main.py composition — всі модулі інстанціюються від AppConfig без помилок.

Не запускає start() — network-free. Ловить regressions у сигнатурах конструкторів.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scalper.book.engine import OrderBookEngine
from scalper.config import load_config
from scalper.decision.engine import DecisionEngine
from scalper.expectancy import ExpectancyTracker
from scalper.features.engine import FeatureEngine
from scalper.gateway.gateway import MarketDataGateway
from scalper.gateway.transport import _RestTransport
from scalper.journal.logger import JournalLogger
from scalper.notifications import NotificationService
from scalper.orchestrator import Orchestrator
from scalper.position.manager import PositionManager
from scalper.regime.classifier import MarketRegime
from scalper.replay.simulator import SimulatedExecutionEngine, SimulatorConfig
from scalper.risk.engine import RiskEngine
from scalper.setups.detector import SetupDetector
from scalper.setups.rules import default_rules
from scalper.tape.analyzer import TapeFlowAnalyzer


def test_full_composition_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BINANCE_API_KEY=test_k\nBINANCE_API_SECRET=test_s\nBINANCE_TESTNET=true\n",
        encoding="utf-8",
    )
    for var in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_TESTNET"):
        monkeypatch.delenv(var, raising=False)

    settings = tmp_path / "settings.yaml"
    journal_dir = (tmp_path / "journal").as_posix()
    settings.write_text(
        f"""
symbols: [BTCUSDT]
mode: paper
equity_usd: 200.0
journal:
  journal_dir: {journal_dir!r}
""",
        encoding="utf-8",
    )

    cfg = load_config(settings_path=settings, env_path=env_file)
    assert cfg.mode == "paper"

    # Composition — той самий порядок, що в __main__.run()
    notifier = NotificationService(cfg.notifications)
    journal = JournalLogger(cfg.journal)

    rest = _RestTransport(cfg.gateway)
    gateway = MarketDataGateway(cfg.gateway, notifier, transport=rest)

    execution = SimulatedExecutionEngine(SimulatorConfig())
    book = OrderBookEngine(cfg.book, gateway)
    tape = TapeFlowAnalyzer(cfg.tape, gateway)
    features = FeatureEngine(cfg.features)
    regime = MarketRegime(cfg.regime, book, tape)
    detector = SetupDetector(default_rules(cfg.setups))
    expectancy = ExpectancyTracker(cfg.expectancy)
    risk = RiskEngine(cfg.risk)
    position = PositionManager(cfg.position, execution, risk)   # type: ignore[arg-type]
    decision = DecisionEngine(cfg.decision, regime, risk=risk, expectancy=expectancy, position=position)

    orchestrator = Orchestrator(
        config=cfg, gateway=gateway, features=features, regime=regime,
        detector=detector, decision=decision, risk=risk,
        execution=execution, position=position, expectancy=expectancy,   # type: ignore[arg-type]
        journal=journal, notifier=notifier, book=book, tape=tape,
        equity_fn=lambda: cfg.equity_usd,
    )
    assert orchestrator is not None
