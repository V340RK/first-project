"""AppConfig / load_config — .env + settings.yaml злиття."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scalper.config import AppConfig, load_config


def test_load_from_env_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BINANCE_API_KEY=fake_key_123\n"
        "BINANCE_API_SECRET=fake_secret_456\n"
        "BINANCE_TESTNET=true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)

    cfg = load_config(env_path=env_file)
    assert cfg.gateway.testnet is True
    assert cfg.gateway.api_key is not None
    assert cfg.gateway.api_key.get_secret_value() == "fake_key_123"
    assert cfg.gateway.secret_key is not None
    assert cfg.gateway.secret_key.get_secret_value() == "fake_secret_456"
    assert "testnet" in cfg.gateway.base_url


def test_load_prod_urls_when_testnet_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BINANCE_API_KEY=k\nBINANCE_API_SECRET=s\nBINANCE_TESTNET=false\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)

    cfg = load_config(env_path=env_file)
    assert cfg.gateway.testnet is False
    assert cfg.gateway.base_url == "https://fapi.binance.com"
    assert cfg.gateway.ws_url == "wss://fstream.binance.com"


def test_yaml_overrides_env_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BINANCE_API_KEY=k\nBINANCE_API_SECRET=s\n", encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        """
symbols: [ETHUSDT, SOLUSDT]
mode: paper
equity_usd: 500.0
risk:
  risk_per_trade_usd_abs: 2.5
  max_trades_per_day: 5
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    cfg = load_config(settings_path=settings, env_path=env_file)
    assert cfg.symbols == ["ETHUSDT", "SOLUSDT"]
    assert cfg.equity_usd == 500.0
    assert cfg.risk.risk_per_trade_usd_abs == 2.5
    assert cfg.risk.max_trades_per_day == 5


def test_missing_yaml_path_uses_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BINANCE_API_KEY=k\nBINANCE_API_SECRET=s\n", encoding="utf-8")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    cfg = load_config(settings_path=tmp_path / "no_such_file.yaml", env_path=env_file)
    assert cfg.symbols == ["BTCUSDT"]
    assert cfg.gateway.api_key is not None


def test_telegram_env_vars_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BINANCE_API_KEY=k\nBINANCE_API_SECRET=s\n"
        "TELEGRAM_BOT_TOKEN=tg_token_abc\n"
        "TELEGRAM_CHAT_ID=12345\n",
        encoding="utf-8",
    )
    for var in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)

    cfg = load_config(env_path=env_file)
    assert cfg.notifications.telegram_bot_token == "tg_token_abc"
    assert cfg.notifications.telegram_chat_id == "12345"


def test_from_yaml_requires_file() -> None:
    with pytest.raises(FileNotFoundError):
        AppConfig.from_yaml("no_such_file.yaml")
