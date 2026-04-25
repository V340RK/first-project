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

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotRunParams:
    """Параметри одного "сесійного" запуску бота з UI — одна пара, один процес."""

    symbol: str
    leverage: int
    risk_per_trade_usd: float
    equity_usd: float
    mode: str = "live"                    # "live" | "paper"
    score_threshold_override: float | None = None


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
    ) -> None:
        self._project_root = project_root
        self._runtime_config_path = runtime_config_path
        self._python = str(python_exe) if python_exe else sys.executable
        self._proc: subprocess.Popen[bytes] | None = None
        self._started_at_ms: int | None = None
        self._last_params: BotRunParams | None = None

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

        logger.info("starting bot subprocess: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self._project_root),
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
        return final

    # === Internal ===

    def _write_runtime_config(self, params: BotRunParams) -> None:
        config_dict: dict = {
            "mode": params.mode,
            "symbols": [params.symbol],
            "equity_usd": params.equity_usd,
            "leverage": params.leverage,
            "risk": {
                "risk_per_trade_usd_abs": params.risk_per_trade_usd,
            },
        }
        if params.score_threshold_override is not None:
            config_dict["decision"] = {
                "base_score_threshold": params.score_threshold_override,
            }

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
