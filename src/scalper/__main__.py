"""CLI entrypoint: `python -m scalper [--settings path.yaml] [--env .env]`.

Composition root: збирає всі модулі й запускає Orchestrator. Працює у трьох
режимах з config.mode:
  - paper   — dry-run без реальних ордерів (SimulatedExecutionEngine)
  - live    — реальна Binance (testnet або prod за BINANCE_TESTNET)
  - replay  — історичний бектест (ReplayGateway — не реалізовано)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from scalper.book.engine import OrderBookEngine
from scalper.config import AppConfig, load_config
from scalper.decision.engine import DecisionEngine
from scalper.execution import BinanceOrderTransport, ExecutionEngine
from scalper.execution.types import SymbolFilters as ExecSymbolFilters
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

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def run(cfg: AppConfig) -> None:
    logger.info("starting scalper in mode=%s symbols=%s", cfg.mode, cfg.symbols)

    notifier = NotificationService(cfg.notifications)
    journal = JournalLogger(cfg.journal)

    # === Gateway + Execution transport ===
    rest_transport = _RestTransport(cfg.gateway)
    gateway = MarketDataGateway(cfg.gateway, notifier, transport=rest_transport)

    if cfg.mode == "live":
        order_transport = BinanceOrderTransport(rest_transport)
        execution: ExecutionEngine | SimulatedExecutionEngine = ExecutionEngine(
            cfg.execution, order_transport,
        )
        # Leverage встановлюємо після gateway.start() (нижче), щоб REST працював
    elif cfg.mode == "paper":
        execution = SimulatedExecutionEngine(SimulatorConfig())
    elif cfg.mode == "replay":
        raise SystemExit("replay mode не реалізований: потрібен ReplayGateway з файлами")
    else:
        raise SystemExit(f"unknown mode: {cfg.mode}")

    # === Market data pipeline ===
    book = OrderBookEngine(cfg.book, gateway)
    tape = TapeFlowAnalyzer(cfg.tape, gateway)
    features = FeatureEngine(cfg.features)
    regime = MarketRegime(cfg.regime, book, tape)
    detector = SetupDetector(default_rules(cfg.setups))
    expectancy = ExpectancyTracker(cfg.expectancy)
    risk = RiskEngine(cfg.risk)
    position = PositionManager(cfg.position, execution, risk)   # type: ignore[arg-type]
    decision = DecisionEngine(cfg.decision, regime, risk=risk, expectancy=expectancy, position=position)

    # Bridge risk.on_position_closed → expectancy
    original_on_closed = risk.on_position_closed
    def _on_closed_bridge(outcome) -> None:   # type: ignore[no-untyped-def]
        original_on_closed(outcome)
        expectancy.on_trade_outcome(outcome)
    risk.on_position_closed = _on_closed_bridge   # type: ignore[assignment]

    orchestrator = Orchestrator(
        config=cfg, gateway=gateway, features=features, regime=regime,
        detector=detector, decision=decision, risk=risk,
        execution=execution, position=position, expectancy=expectancy,   # type: ignore[arg-type]
        journal=journal, notifier=notifier, book=book, tape=tape,
        equity_fn=lambda: cfg.equity_usd,
    )

    # === Graceful shutdown ===
    stop_event = asyncio.Event()

    def _on_signal(signum: int, _frame: object) -> None:
        logger.info("signal %d received, stopping...", signum)
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except (AttributeError, ValueError):
        pass   # Windows: signals обмежені; stop_event може спрацьовувати ззовні

    await orchestrator.start(cfg.symbols)

    # CRITICAL: реєструємо symbol filters в execution. Без цього place_order
    # реджектиться як "no_symbol_filters" → position.open() повертає False
    # → жодних трейдів. ExchangeInfo вже завантажений під час gateway.start().
    for sym in cfg.symbols:
        try:
            gw_filt = gateway.get_symbol_filters(sym)
        except Exception as e:
            logger.error("symbol filters not available for %s: %s", sym, e)
            continue
        execution.register_symbol(ExecSymbolFilters(
            symbol=sym,
            tick_size=gw_filt.tick_size, step_size=gw_filt.step_size,
            min_qty=gw_filt.min_qty, max_qty=gw_filt.max_qty,
            min_notional=gw_filt.min_notional,
        ))
        logger.info("filters registered: %s tick=%g step=%g min_qty=%g min_notional=%g",
                    sym, gw_filt.tick_size, gw_filt.step_size,
                    gw_filt.min_qty, gw_filt.min_notional)

    # Paper mode: симулятор фіксує ордери на основі поточного book → треба
    # годувати його book-ticker подіями з gateway, інакше fill-у не буде.
    if cfg.mode == "paper" and isinstance(execution, SimulatedExecutionEngine):
        async def _feed_book(bt) -> None:   # type: ignore[no-untyped-def]
            try:
                execution.update_book(
                    bt.symbol, bid=bt.best_bid, ask=bt.best_ask,
                    last_trade_price=(bt.best_bid + bt.best_ask) / 2,
                    ts_ms=bt.timestamp_ms,
                )
            except Exception as e:
                logger.debug("update_book failed for %s: %s", bt.symbol, e)
        gateway.on_book_ticker(_feed_book)
        # А також on_clock_tick треба — для resolving pending LIMIT/STOP
        async def _tick_pending(trade) -> None:   # type: ignore[no-untyped-def]
            try:
                execution.update_book(
                    trade.symbol, bid=trade.price, ask=trade.price,
                    last_trade_price=trade.price, ts_ms=trade.timestamp_ms,
                )
                await execution.on_clock_tick(trade.timestamp_ms)
            except Exception as e:
                logger.debug("sim tick failed for %s: %s", trade.symbol, e)
        gateway.on_agg_trade(_tick_pending)

    # Встановити leverage для кожного символу (тільки в live mode — paper не
    # шле REST ордери, та й leverage не потрібен для SimulatedExecutionEngine).
    if cfg.mode == "live":
        for sym in cfg.symbols:
            try:
                await gateway.set_leverage(sym, cfg.leverage)
                logger.info("leverage set: %s x%d", sym, cfg.leverage)
            except Exception as e:
                logger.error("failed to set leverage for %s: %s", sym, e)

    logger.info("scalper up. Press Ctrl+C to stop.")

    try:
        slow_tick_task = asyncio.create_task(_slow_tick_loop(orchestrator, stop_event))
        fast_tick_task = asyncio.create_task(
            _fast_tick_loop(orchestrator, cfg.symbols, stop_event),
        )
        await stop_event.wait()
        slow_tick_task.cancel()
        fast_tick_task.cancel()
    finally:
        await orchestrator.stop()
        logger.info("scalper stopped.")


async def _fast_tick_loop(
    orch: Orchestrator, symbols: list[str], stop_event: asyncio.Event,
) -> None:
    """Heartbeat-tick 250ms: викликає on_tick(symbol) для кожного символу.

    Потрібен як fallback, коли aggTrade розріджений (наприклад, на testnet
    BTCUSDT може не мати жодного trade-а хвилинами). Якщо приходить aggTrade
    callback, pipeline і так викликається одразу — це другий шар, не заміна.
    """
    from scalper.common import time as _time
    while not stop_event.is_set():
        for sym in symbols:
            try:
                await orch.on_tick(sym, _time.clock())
            except Exception as e:
                logger.exception("fast_tick on_tick(%s): %s", sym, e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.25)
        except TimeoutError:
            continue


async def _slow_tick_loop(orch: Orchestrator, stop_event: asyncio.Event) -> None:
    """Кожну секунду: регласифікація регіму + heartbeat у журнал."""
    while not stop_event.is_set():
        try:
            await orch.on_slow_tick()
        except Exception as e:   # slow loop не має падати разом з ботом
            logger.exception("slow_tick exception: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except TimeoutError:
            continue


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scalper",
        description="Binance Futures scalper (live / paper / replay)",
    )
    parser.add_argument("--settings", type=Path, default=Path("configs/settings.yaml"),
                        help="Шлях до settings.yaml (опційно, є defaults)")
    parser.add_argument("--env", type=Path, default=Path(".env"),
                        help="Шлях до .env з API ключами (за замовчанням .env у кореня)")
    args = parser.parse_args()

    _configure_logging()
    cfg = load_config(
        settings_path=args.settings if args.settings.exists() else None,
        env_path=args.env if args.env.exists() else None,
    )
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
