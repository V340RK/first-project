"""Dashboard API: /app page + /api/bot/status + start/stop guards."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from scalper.dashboard.config import DashboardConfig
from scalper.dashboard.controller import BotController, BotStatus
from scalper.dashboard.server import create_app
from scalper.dashboard.symbols import BinanceSymbolService, SymbolInfo


def _fake_symbol_service(symbols: list[str] = ["BTCUSDT", "ETHUSDT"]):   # type: ignore[no-untyped-def]
    svc = MagicMock(spec=BinanceSymbolService)
    infos = [
        SymbolInfo(symbol=s, base=s.replace("USDT", ""), quote="USDT",
                   tick_size=0.1, step_size=0.001, min_notional=5.0)
        for s in symbols
    ]
    async def _list(): return infos
    async def _is_valid(x): return x.upper() in symbols
    svc.list_symbols = _list
    svc.is_valid = _is_valid
    return svc


@pytest.fixture()
def app_with_fake_controller(tmp_path: Path):   # type: ignore[no-untyped-def]
    ctrl = MagicMock(spec=BotController)
    ctrl.is_running.return_value = False
    ctrl.status.return_value = BotStatus(
        running=False, pid=None, started_at_ms=None, params=None, exit_code=None,
    )
    cfg = DashboardConfig(journal_dir=tmp_path / "journal")
    cfg.journal_dir.mkdir()
    app = create_app(cfg, controller=ctrl, symbol_service=_fake_symbol_service())
    return app, ctrl


def test_trader_page_served(app_with_fake_controller) -> None:   # type: ignore[no-untyped-def]
    app, _ = app_with_fake_controller
    with TestClient(app) as client:
        r = client.get("/app")
        assert r.status_code == 200
        assert "V340RK" in r.text
        assert "trader.css" in r.text


def test_bot_status_returns_running_false_initially(app_with_fake_controller) -> None:   # type: ignore[no-untyped-def]
    app, _ = app_with_fake_controller
    with TestClient(app) as client:
        r = client.get("/api/bot/status")
        assert r.status_code == 200
        body = r.json()
        assert body["bot"]["running"] is False
        assert "session" in body


def test_start_forwards_params_to_controller(app_with_fake_controller) -> None:   # type: ignore[no-untyped-def]
    app, ctrl = app_with_fake_controller
    ctrl.start.return_value = BotStatus(
        running=True, pid=12345, started_at_ms=1000, params=None, exit_code=None,
    )
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "leverage": 10,
            "risk_per_trade_usd": 0.5,
            "equity_usd": 100.0,
            "mode": "paper",
        })
        assert r.status_code == 200
        ctrl.start.assert_called_once()
        called_params = ctrl.start.call_args.args[0]
        assert called_params.symbols == ["BTCUSDT", "ETHUSDT"]
        assert called_params.leverage == 10
        assert called_params.risk_per_trade_usd == 0.5


def test_start_rejects_when_already_running(app_with_fake_controller) -> None:   # type: ignore[no-untyped-def]
    app, ctrl = app_with_fake_controller
    ctrl.is_running.return_value = True
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbols": ["BTCUSDT"], "leverage": 5,
            "risk_per_trade_usd": 0.1, "equity_usd": 50.0, "mode": "paper",
        })
        assert r.status_code == 409


def test_stop_calls_controller(app_with_fake_controller) -> None:   # type: ignore[no-untyped-def]
    app, ctrl = app_with_fake_controller
    ctrl.stop.return_value = BotStatus(
        running=False, pid=None, started_at_ms=None, params=None, exit_code=0,
    )
    with TestClient(app) as client:
        r = client.post("/api/bot/stop")
        assert r.status_code == 200
        ctrl.stop.assert_called_once()


def test_start_without_controller_returns_503(tmp_path: Path) -> None:
    cfg = DashboardConfig(journal_dir=tmp_path / "j")
    cfg.journal_dir.mkdir()
    app = create_app(cfg, controller=None)
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbols": ["BTCUSDT"], "leverage": 5,
            "risk_per_trade_usd": 0.1, "equity_usd": 50.0, "mode": "paper",
        })
        assert r.status_code == 503


def test_list_symbols_endpoint(app_with_fake_controller) -> None:   # type: ignore[no-untyped-def]
    app, _ = app_with_fake_controller
    with TestClient(app) as client:
        r = client.get("/api/symbols")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 2
        assert body[0]["symbol"] == "BTCUSDT"
        assert body[0]["base"] == "BTC"
        assert body[0]["quote"] == "USDT"
        assert body[0]["tick_size"] == 0.1


def test_start_rejects_unknown_symbols(app_with_fake_controller) -> None:   # type: ignore[no-untyped-def]
    app, ctrl = app_with_fake_controller
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbols": ["BTCUSDT", "FAKEUSDT"],
            "leverage": 5, "risk_per_trade_usd": 0.5,
            "equity_usd": 100.0, "mode": "paper",
        })
        assert r.status_code == 422
        assert "FAKEUSDT" in r.json()["detail"]
        ctrl.start.assert_not_called()


def test_start_normalizes_symbol_case(app_with_fake_controller) -> None:   # type: ignore[no-untyped-def]
    app, ctrl = app_with_fake_controller
    ctrl.start.return_value = BotStatus(
        running=True, pid=1, started_at_ms=0, params=None, exit_code=None,
    )
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbols": ["btcusdt", "Ethusdt"],
            "leverage": 5, "risk_per_trade_usd": 0.5,
            "equity_usd": 100.0, "mode": "paper",
        })
        assert r.status_code == 200
        called = ctrl.start.call_args.args[0]
        assert called.symbols == ["BTCUSDT", "ETHUSDT"]


def test_start_validates_payload(app_with_fake_controller) -> None:   # type: ignore[no-untyped-def]
    app, _ = app_with_fake_controller
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbols": [], "leverage": 5,
            "risk_per_trade_usd": 0.1, "equity_usd": 50.0, "mode": "paper",
        })
        assert r.status_code == 422
        r = client.post("/api/bot/start", json={
            "symbols": ["BTCUSDT"], "leverage": 200,  # > 125
            "risk_per_trade_usd": 0.1, "equity_usd": 50.0, "mode": "paper",
        })
        assert r.status_code == 422
        r = client.post("/api/bot/start", json={
            "symbols": ["BTCUSDT"], "leverage": 5,
            "risk_per_trade_usd": 0,   # 0 not > 0
            "equity_usd": 50.0, "mode": "paper",
        })
        assert r.status_code == 422
