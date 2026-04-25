"""BotRegistry — паралельні процеси по одному на пару."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from scalper.dashboard.controller import BotController, BotRegistry, BotRunParams


def _patch_start(reg: BotRegistry, sleeper_script: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Підмінюємо BotController.start на запуск стаб-скрипту замість -m scalper."""
    orig_for = reg._controller_for
    def wrap(symbol: str) -> BotController:
        ctrl = orig_for(symbol)
        if not hasattr(ctrl, "_patched"):
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
                    [sys.executable, str(sleeper_script)],
                    creationflags=creationflags,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                ctrl._started_at_ms = int(time.time() * 1000)
                ctrl._last_params = params
                return ctrl.status()
            monkeypatch.setattr(ctrl, "start", patched_start)
            ctrl._patched = True   # type: ignore[attr-defined]
        return ctrl
    monkeypatch.setattr(reg, "_controller_for", wrap)


@pytest.fixture()
def sleeper(tmp_path: Path) -> Path:
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


def test_registry_creates_separate_runtime_yaml_per_symbol(
    tmp_path: Path, sleeper: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "configs"
    reg = BotRegistry(
        project_root=tmp_path, runtime_configs_dir=runtime_dir,
        python_exe=sys.executable,
    )
    _patch_start(reg, sleeper, monkeypatch)

    btc_params = BotRunParams(symbol="BTCUSDT", leverage=10,
                              risk_per_trade_usd=0.5, equity_usd=100.0)
    eth_params = BotRunParams(symbol="ETHUSDT", leverage=20,
                              risk_per_trade_usd=1.0, equity_usd=200.0)
    try:
        reg.start(btc_params)
        reg.start(eth_params)

        assert (runtime_dir / "runtime_BTCUSDT.yaml").exists()
        assert (runtime_dir / "runtime_ETHUSDT.yaml").exists()

        # Обидва процеси живі й різні
        statuses = reg.all_statuses()
        assert statuses["BTCUSDT"].running is True
        assert statuses["ETHUSDT"].running is True
        assert statuses["BTCUSDT"].pid != statuses["ETHUSDT"].pid
    finally:
        reg.stop_all(timeout_sec=3.0)


def test_stop_one_does_not_affect_other(
    tmp_path: Path, sleeper: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = BotRegistry(
        project_root=tmp_path, runtime_configs_dir=tmp_path / "configs",
        python_exe=sys.executable,
    )
    _patch_start(reg, sleeper, monkeypatch)
    try:
        reg.start(BotRunParams(symbol="BTCUSDT", leverage=5,
                               risk_per_trade_usd=0.1, equity_usd=50.0))
        reg.start(BotRunParams(symbol="ETHUSDT", leverage=5,
                               risk_per_trade_usd=0.1, equity_usd=50.0))
        reg.stop("BTCUSDT", timeout_sec=3.0)
        statuses = reg.all_statuses()
        assert statuses["BTCUSDT"].running is False
        assert statuses["ETHUSDT"].running is True
    finally:
        reg.stop_all(timeout_sec=3.0)


def test_status_unknown_symbol_returns_not_running(tmp_path: Path) -> None:
    reg = BotRegistry(
        project_root=tmp_path, runtime_configs_dir=tmp_path / "configs",
    )
    s = reg.status("NEVER_STARTED")
    assert s.running is False
    assert s.pid is None


def test_all_statuses_only_includes_known_controllers(tmp_path: Path) -> None:
    reg = BotRegistry(
        project_root=tmp_path, runtime_configs_dir=tmp_path / "configs",
    )
    # Без жодного start — пусто
    assert reg.all_statuses() == {}
    # Звертаємось до controller_for → створює запис у dict
    reg.status("BTCUSDT")
    assert "BTCUSDT" in reg.all_statuses()
