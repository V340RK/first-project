"""BotController — запуск/зупинка бота як subprocess.

Dashboard server не торгує сам; він конструює runtime config yaml + pускає
`python -m scalper --settings runtime.yaml` як окремий процес. Зупинка —
через SIGINT (Ctrl+C signal на POSIX) або CTRL_BREAK_EVENT (Windows) з fallback
на terminate() після таймауту.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotRunParams:
    """Параметри одного "сесійного" запуску бота з UI — одна пара, один процес.

    Sizing: ОДИН з двох режимів задається:
      sizing_mode='risk_usd'  → risk_per_trade_usd: $X втрати на угоду (R-based)
      sizing_mode='margin_pct' → margin_per_trade_pct: % balance як margin (notional = margin*leverage)
    """

    symbol: str
    leverage: int
    risk_per_trade_usd: float                 # використовується якщо sizing_mode=risk_usd
    equity_usd: float
    mode: str = "live"
    score_threshold_override: float | None = None
    relaxed_regime: bool = False
    sizing_mode: str = "risk_usd"             # "risk_usd" | "margin_pct"
    margin_per_trade_pct: float | None = None   # використовується якщо sizing_mode=margin_pct
    max_book_consumption_pct: float | None = None   # liquidity guard (None = off)
    max_expected_slippage_ticks: int | None = None  # slippage guard (None = off)
    stop_loss_pct: float | None = None               # fixed % SL від entry (None = structure-based)


@dataclass
class BotStatus:
    running: bool
    pid: int | None
    started_at_ms: int | None
    params: dict | None = None
    exit_code: int | None = None


class BotController:
    """Singleton-style controller. Не дозволяє запустити двох ботів одночасно."""

    def __init__(
        self,
        *,
        project_root: Path,
        runtime_config_path: Path,
        python_exe: Path | str | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self._project_root = project_root
        self._runtime_config_path = runtime_config_path
        self._python = str(python_exe) if python_exe else sys.executable
        self._log_dir = log_dir or (project_root / "logs")
        self._proc: subprocess.Popen[bytes] | None = None
        self._started_at_ms: int | None = None
        self._last_params: BotRunParams | None = None
        self._log_file: Any = None

    # === Public ===

    def is_running(self) -> bool:
        if self._proc is None:
            return False
        if self._proc.poll() is not None:
            # Процес вмер сам — оновлюємо стан.
            self._proc = None
            self._started_at_ms = None
            return False
        return True

    def status(self) -> BotStatus:
        running = self.is_running()
        return BotStatus(
            running=running,
            pid=self._proc.pid if running and self._proc else None,
            started_at_ms=self._started_at_ms if running else None,
            params=asdict(self._last_params) if self._last_params else None,
            exit_code=self._proc.returncode if self._proc and not running else None,
        )

    def start(self, params: BotRunParams) -> BotStatus:
        if self.is_running():
            raise RuntimeError("bot already running; stop first")

        self._write_runtime_config(params)

        cmd = [
            self._python, "-m", "scalper",
            "--settings", str(self._runtime_config_path),
        ]

        creationflags = 0
        if os.name == "nt":
            # CREATE_NEW_PROCESS_GROUP дозволяє посилати CTRL_BREAK_EVENT на Windows.
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP   # type: ignore[attr-defined]

        # CRITICAL: stdout/stderr → файл, інакше помилки бота невидимі.
        # Раніше було DEVNULL — 5 годин розбирали "чому немає трейдів",
        # а logger.error("entry order rejected") писалося в нікуди.
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / f"bot_{params.symbol}.log"
        self._log_file = log_path.open("a", encoding="utf-8", buffering=1)
        self._log_file.write(
            f"\n=== START {time.strftime('%Y-%m-%d %H:%M:%S')} pid=? "
            f"params={params} ===\n"
        )

        logger.info("starting bot subprocess: %s (logs → %s)", " ".join(cmd), log_path)
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self._project_root),
            creationflags=creationflags,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,   # stderr у той самий файл
        )
        self._started_at_ms = int(time.time() * 1000)
        self._last_params = params
        return self.status()

    def stop(self, *, timeout_sec: float = 10.0) -> BotStatus:
        if not self.is_running():
            return self.status()
        assert self._proc is not None
        try:
            if os.name == "nt":
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)   # type: ignore[attr-defined]
            else:
                self._proc.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError) as e:
            logger.warning("signal send failed: %s; falling back to terminate()", e)
            self._proc.terminate()

        try:
            self._proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            logger.warning("bot did not stop within %.1fs — terminate()", timeout_sec)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                logger.error("terminate() ignored, killing")
                self._proc.kill()
                self._proc.wait()

        final = self.status()
        self._proc = None
        self._started_at_ms = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        return final

    # === Internal ===

    def _write_runtime_config(self, params: BotRunParams) -> None:
        risk_section: dict = {}
        if params.sizing_mode == "margin_pct" and params.margin_per_trade_pct is not None:
            risk_section["margin_per_trade_pct"] = params.margin_per_trade_pct
        else:
            risk_section["risk_per_trade_usd_abs"] = params.risk_per_trade_usd
        if params.max_book_consumption_pct is not None:
            risk_section["max_book_consumption_pct"] = params.max_book_consumption_pct
        if params.max_expected_slippage_ticks is not None:
            risk_section["max_expected_slippage_ticks"] = params.max_expected_slippage_ticks
        if params.stop_loss_pct is not None:
            risk_section["stop_loss_pct"] = params.stop_loss_pct

        config_dict: dict = {
            "mode": params.mode,
            "symbols": [params.symbol],
            "equity_usd": params.equity_usd,
            "leverage": params.leverage,
            "risk": risk_section,
        }
        decision_section: dict = {}
        if params.score_threshold_override is not None:
            decision_section["base_score_threshold"] = params.score_threshold_override
        if params.relaxed_regime:
            decision_section["relaxed_regime"] = True
        if decision_section:
            config_dict["decision"] = decision_section

        self._runtime_config_path.parent.mkdir(parents=True, exist_ok=True)
        with self._runtime_config_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(config_dict, f, sort_keys=False, allow_unicode=True)


class BotRegistry:
    """Тримає по одному BotController на кожен символ.

    Паралельне виконання: кожна пара — окремий процес бота зі своїм
    runtime-config файлом (`configs/runtime_{SYMBOL}.yaml`). Це дає
    незалежний старт/стоп/конфіг/статистику для кожної пари.
    """

    def __init__(
        self,
        *,
        project_root: Path,
        runtime_configs_dir: Path,
        python_exe: Path | str | None = None,
    ) -> None:
        self._project_root = project_root
        self._runtime_dir = runtime_configs_dir
        self._python = python_exe
        self._controllers: dict[str, BotController] = {}

    def _controller_for(self, symbol: str) -> BotController:
        sym = symbol.upper()
        if sym not in self._controllers:
            self._controllers[sym] = BotController(
                project_root=self._project_root,
                runtime_config_path=self._runtime_dir / f"runtime_{sym}.yaml",
                python_exe=self._python,
            )
        return self._controllers[sym]

    def start(self, params: BotRunParams) -> BotStatus:
        return self._controller_for(params.symbol).start(params)

    def stop(self, symbol: str, *, timeout_sec: float = 10.0) -> BotStatus:
        return self._controller_for(symbol).stop(timeout_sec=timeout_sec)

    def status(self, symbol: str) -> BotStatus:
        return self._controller_for(symbol).status()

    def all_statuses(self) -> dict[str, BotStatus]:
        # Повертаємо тільки ті символи, які колись стартували (щоб не плодити зомбі).
        return {sym: c.status() for sym, c in self._controllers.items()}

    def stop_all(self, *, timeout_sec: float = 10.0) -> None:
        for c in self._controllers.values():
            if c.is_running():
                try:
                    c.stop(timeout_sec=timeout_sec)
                except Exception as e:
                    logger.warning("stop_all: %s", e)


__all__ = ["BotController", "BotRegistry", "BotRunParams", "BotStatus"]
