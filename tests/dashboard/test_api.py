"""Dashboard API: trader page + per-symbol bot lifecycle + symbol validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from scalper.dashboard.config import DashboardConfig
from scalper.dashboard.controller import BotRegistry, BotStatus
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
def app_with_fake_registry(tmp_path: Path):   # type: ignore[no-untyped-def]
    reg = MagicMock(spec=BotRegistry)
    reg.status.return_value = BotStatus(
        running=False, pid=None, started_at_ms=None, params=None, exit_code=None,
    )
    reg.all_statuses.return_value = {}
    cfg = DashboardConfig(journal_dir=tmp_path / "journal")
    cfg.journal_dir.mkdir()
    app = create_app(cfg, registry=reg, symbol_service=_fake_symbol_service())
    return app, reg


def test_trader_page_served(app_with_fake_registry) -> None:   # type: ignore[no-untyped-def]
    app, _ = app_with_fake_registry
    with TestClient(app) as client:
        r = client.get("/app")
        assert r.status_code == 200
        assert "V340RK" in r.text
        assert "trader.css" in r.text


def test_status_returns_empty_slots_initially(app_with_fake_registry) -> None:   # type: ignore[no-untyped-def]
    app, _ = app_with_fake_registry
    with TestClient(app) as client:
        r = client.get("/api/bot/status")
        assert r.status_code == 200
        body = r.json()
        assert body["slots"] == {}


def test_status_aggregates_per_symbol(app_with_fake_registry) -> None:   # type: ignore[no-untyped-def]
    app, reg = app_with_fake_registry
    reg.all_statuses.return_value = {
        "BTCUSDT": BotStatus(running=True, pid=100, started_at_ms=1000,
                             params={"symbol": "BTCUSDT", "leverage": 5,
                                     "risk_per_trade_usd": 0.5, "equity_usd": 100.0,
                                     "mode": "paper", "score_threshold_override": None},
                             exit_code=None),
        "ETHUSDT": BotStatus(running=False, pid=None, started_at_ms=None,
                             params=None, exit_code=0),
    }
    with TestClient(app) as client:
        r = client.get("/api/bot/status")
        assert r.status_code == 200
        slots = r.json()["slots"]
        assert "BTCUSDT" in slots
        assert "ETHUSDT" in slots
        assert slots["BTCUSDT"]["bot"]["running"] is True
        assert slots["BTCUSDT"]["bot"]["pid"] == 100
        assert slots["ETHUSDT"]["bot"]["running"] is False


def test_start_per_symbol_forwards_params(app_with_fake_registry) -> None:   # type: ignore[no-untyped-def]
    app, reg = app_with_fake_registry
    reg.start.return_value = BotStatus(
        running=True, pid=12345, started_at_ms=1000, params=None, exit_code=None,
    )
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbol": "BTCUSDT", "leverage": 10,
            "risk_per_trade_usd": 0.5, "equity_usd": 100.0, "mode": "paper",
        })
        assert r.status_code == 200
        reg.start.assert_called_once()
        called = reg.start.call_args.args[0]
        assert called.symbol == "BTCUSDT"
        assert called.leverage == 10
        assert called.risk_per_trade_usd == 0.5


def test_start_rejects_when_symbol_already_running(app_with_fake_registry) -> None:   # type: ignore[no-untyped-def]
    app, reg = app_with_fake_registry
    reg.status.return_value = BotStatus(
        running=True, pid=1, started_at_ms=0, params=None, exit_code=None,
    )
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbol": "BTCUSDT", "leverage": 5,
            "risk_per_trade_usd": 0.1, "equity_usd": 50.0, "mode": "paper",
        })
        assert r.status_code == 409


def test_stop_calls_registry_with_symbol(app_with_fake_registry) -> None:   # type: ignore[no-untyped-def]
    app, reg = app_with_fake_registry
    reg.stop.return_value = BotStatus(
        running=False, pid=None, started_at_ms=None, params=None, exit_code=0,
    )
    with TestClient(app) as client:
        r = client.post("/api/bot/stop", json={"symbol": "BTCUSDT"})
        assert r.status_code == 200
        reg.stop.assert_called_once_with("BTCUSDT")


def test_start_without_registry_returns_503(tmp_path: Path) -> None:
    cfg = DashboardConfig(journal_dir=tmp_path / "j")
    cfg.journal_dir.mkdir()
    app = create_app(cfg, registry=None)
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbol": "BTCUSDT", "leverage": 5,
            "risk_per_trade_usd": 0.1, "equity_usd": 50.0, "mode": "paper",
        })
        assert r.status_code == 503


def test_list_symbols_endpoint(app_with_fake_registry) -> None:   # type: ignore[no-untyped-def]
    app, _ = app_with_fake_registry
    with TestClient(app) as client:
        r = client.get("/api/symbols")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 2
        assert body[0]["symbol"] == "BTCUSDT"
        assert body[0]["base"] == "BTC"


def test_start_rejects_unknown_symbol(app_with_fake_registry) -> None:   # type: ignore[no-untyped-def]
    app, reg = app_with_fake_registry
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbol": "FAKEUSDT",
            "leverage": 5, "risk_per_trade_usd": 0.5,
            "equity_usd": 100.0, "mode": "paper",
        })
        assert r.status_code == 422
        assert "FAKEUSDT" in r.json()["detail"]
        reg.start.assert_not_called()


def test_start_normalizes_symbol_case(app_with_fake_registry) -> None:   # type: ignore[no-untyped-def]
    app, reg = app_with_fake_registry
    reg.start.return_value = BotStatus(
        running=True, pid=1, started_at_ms=0, params=None, exit_code=None,
    )
    with TestClient(app) as client:
        r = client.post("/api/bot/start", json={
            "symbol": "btcusdt",
            "leverage": 5, "risk_per_trade_usd": 0.5,
            "equity_usd": 100.0, "mode": "paper",
        })
        assert r.status_code == 200
        called = reg.start.call_args.args[0]
        assert called.symbol == "BTCUSDT"


def test_start_validates_payload(app_with_fake_registry) -> None:   # type: ignore[no-untyped-def]
    app, _ = app_with_fake_registry
    with TestClient(app) as client:
        # Empty symbol
        r = client.post("/api/bot/start", json={
            "symbol": "", "leverage": 5,
            "risk_per_trade_usd": 0.1, "equity_usd": 50.0, "mode": "paper",
        })
        assert r.status_code == 422
        # Leverage > 125
        r = client.post("/api/bot/start", json={
            "symbol": "BTCUSDT", "leverage": 200,
            "risk_per_trade_usd": 0.1, "equity_usd": 50.0, "mode": "paper",
        })
        assert r.status_code == 422
        # Risk = 0
        r = client.post("/api/bot/start", json={
            "symbol": "BTCUSDT", "leverage": 5,
            "risk_per_trade_usd": 0, "equity_usd": 50.0, "mode": "paper",
        })
        assert r.status_code == 422
