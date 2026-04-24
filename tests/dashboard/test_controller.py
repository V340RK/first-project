"""BotController — start/stop/status, runtime.yaml writing."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import yaml

from scalper.dashboard.controller import BotController, BotRunParams


def _no_op_script(tmp_path: Path) -> Path:
    """Створює стаб-скрипт, що спить поки його не вб'ють. Імітує бот."""
    script = tmp_path / "sleeper.py"
    script.write_text(
        "import time, signal, sys\n"
        "def _quit(*a): sys.exit(0)\n"
        "signal.signal(signal.SIGINT, _quit)\n"
        "if hasattr(signal, 'SIGBREAK'):\n"
        "    signal.signal(signal.SIGBREAK, _quit)\n"
        "while True: time.sleep(0.1)\n",
        encoding="utf-8",
    )
    return script


@pytest.fixture()
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BotController:
    """Controller, що замість `python -m scalper` запускає стаб-скрипт."""
    script = _no_op_script(tmp_path)
    runtime_cfg = tmp_path / "configs" / "runtime.yaml"
    ctrl = BotController(
        project_root=tmp_path,
        runtime_config_path=runtime_cfg,
        python_exe=sys.executable,
    )
    # Підміна: замість "-m scalper" — наш стаб-скрипт.
    def patched_start(params: BotRunParams) -> object:
        if ctrl.is_running():
            raise RuntimeError("bot already running; stop first")
        ctrl._write_runtime_config(params)
        import subprocess, os as _os
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP   # type: ignore[attr-defined]
            if _os.name == "nt" else 0
        )
        ctrl._proc = subprocess.Popen(
            [sys.executable, str(script)],
            creationflags=creationflags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ctrl._started_at_ms = int(time.time() * 1000)
        ctrl._last_params = params
        return ctrl.status()
    monkeypatch.setattr(ctrl, "start", patched_start)
    return ctrl


def test_initial_status_not_running(controller: BotController) -> None:
    s = controller.status()
    assert s.running is False
    assert s.pid is None


def test_start_writes_runtime_config(controller: BotController, tmp_path: Path) -> None:
    params = BotRunParams(
        symbols=["BTCUSDT", "ETHUSDT"], leverage=10,
        risk_per_trade_usd=0.5, equity_usd=100.0, mode="paper",
    )
    controller.start(params)
    try:
        assert controller.is_running()
        cfg_path = tmp_path / "configs" / "runtime.yaml"
        assert cfg_path.exists()
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert cfg["symbols"] == ["BTCUSDT", "ETHUSDT"]
        assert cfg["leverage"] == 10
        assert cfg["risk"]["risk_per_trade_usd_abs"] == 0.5
        assert cfg["equity_usd"] == 100.0
        assert cfg["mode"] == "paper"
    finally:
        controller.stop(timeout_sec=3.0)


def test_stop_kills_subprocess(controller: BotController) -> None:
    params = BotRunParams(symbols=["BTCUSDT"], leverage=5,
                          risk_per_trade_usd=0.1, equity_usd=50.0)
    controller.start(params)
    assert controller.is_running()
    final = controller.stop(timeout_sec=3.0)
    assert final.running is False
    assert controller.is_running() is False


def test_double_start_raises(controller: BotController) -> None:
    params = BotRunParams(symbols=["BTCUSDT"], leverage=5,
                          risk_per_trade_usd=0.1, equity_usd=50.0)
    controller.start(params)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            controller.start(params)
    finally:
        controller.stop(timeout_sec=3.0)


def test_score_threshold_override_in_yaml(controller: BotController, tmp_path: Path) -> None:
    params = BotRunParams(
        symbols=["BTCUSDT"], leverage=5,
        risk_per_trade_usd=0.1, equity_usd=50.0,
        score_threshold_override=0.3,
    )
    controller.start(params)
    try:
        cfg = yaml.safe_load((tmp_path / "configs" / "runtime.yaml").read_text(encoding="utf-8"))
        assert cfg["decision"]["base_score_threshold"] == 0.3
    finally:
        controller.stop(timeout_sec=3.0)
