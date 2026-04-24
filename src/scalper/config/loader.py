"""Завантаження settings.yaml + .env у pydantic-модель AppConfig.

AppConfig — єдина точка конфігурації. Жоден модуль не читає .env/файли напряму —
усе через AppConfig. Секрети (API ключі) приходять з .env, структурні параметри
(ваги, пороги, символи) — з settings.yaml.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr

from scalper.book.config import OBConfig
from scalper.decision.config import DecisionConfig
from scalper.execution.config import ExecConfig
from scalper.expectancy.config import ExpectancyConfig
from scalper.features.config import FeatureConfig
from scalper.gateway.config import GatewayConfig
from scalper.journal.config import JournalConfig
from scalper.notifications.config import NotificationConfig
from scalper.position.config import PositionConfig
from scalper.regime.config import RegimeConfig
from scalper.risk.config import RiskConfig
from scalper.setups.config import SetupConfig
from scalper.tape.config import TapeConfig


BINANCE_TESTNET_REST = "https://testnet.binancefuture.com"
BINANCE_TESTNET_WS = "wss://stream.binancefuture.com"
BINANCE_PROD_REST = "https://fapi.binance.com"
BINANCE_PROD_WS = "wss://fstream.binance.com"


class AppConfig(BaseModel):
    """Кореневий конфіг застосунку. Усі sub-config'и типізовані й мають defaults."""

    model_config = {"arbitrary_types_allowed": True}

    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    mode: str = "paper"                   # 'live' | 'replay' | 'paper'
    equity_usd: float = 1000.0
    leverage: int = Field(default=5, ge=1, le=125)

    gateway: GatewayConfig
    book: OBConfig = Field(default_factory=OBConfig)
    tape: TapeConfig = Field(default_factory=TapeConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    setups: SetupConfig = Field(default_factory=SetupConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecConfig = Field(default_factory=ExecConfig)
    position: PositionConfig = Field(default_factory=PositionConfig)
    expectancy: ExpectancyConfig = Field(default_factory=ExpectancyConfig)
    journal: JournalConfig
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)

    @classmethod
    def from_yaml(cls, path: Path | str) -> AppConfig:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"settings file not found: {p}")
        with p.open(encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}
        return cls.model_validate(raw)


def _bool_env(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_config(
    settings_path: Path | str | None = None,
    env_path: Path | str | None = None,
) -> AppConfig:
    """Збирає AppConfig з .env (секрети) + settings.yaml (структура, опційно).

    Якщо settings_path не задано або файл відсутній — використовуємо defaults +
    лише те, що потрібно для живого running: api keys, symbols, journal dir.
    """
    if env_path is not None:
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)

    api_key = os.environ.get("BINANCE_API_KEY", "").strip()
    api_secret = os.environ.get("BINANCE_API_SECRET", "").strip()
    testnet = _bool_env("BINANCE_TESTNET", default=True)

    if settings_path is not None and Path(settings_path).exists():
        raw: dict[str, Any] = yaml.safe_load(Path(settings_path).read_text(encoding="utf-8")) or {}
    else:
        raw = {}

    gw_raw = dict(raw.get("gateway") or {})
    gw_raw.setdefault("testnet", testnet)
    gw_raw.setdefault("base_url", BINANCE_TESTNET_REST if testnet else BINANCE_PROD_REST)
    gw_raw.setdefault("ws_url", BINANCE_TESTNET_WS if testnet else BINANCE_PROD_WS)
    if api_key:
        gw_raw["api_key"] = SecretStr(api_key)
    if api_secret:
        gw_raw["secret_key"] = SecretStr(api_secret)
    raw["gateway"] = gw_raw

    # Testnet: REST depth snapshot хронічно застарілий на ~5k update IDs
    # відносно WS. Вмикаємо relaxed_sync за замовчуванням, інакше book ніколи
    # не зінціалізується. Проді відключає це (strict Binance protocol).
    book_raw = dict(raw.get("book") or {})
    reinit_raw = dict(book_raw.get("reinit") or {})
    reinit_raw.setdefault("relaxed_sync", testnet)
    book_raw["reinit"] = reinit_raw
    raw["book"] = book_raw

    journal_raw = dict(raw.get("journal") or {})
    journal_raw.setdefault("journal_dir", "journal")
    raw["journal"] = journal_raw

    notif_raw = dict(raw.get("notifications") or {})
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tg_token:
        notif_raw.setdefault("telegram_bot_token", tg_token)
    if tg_chat:
        notif_raw.setdefault("telegram_chat_id", tg_chat)
    raw["notifications"] = notif_raw

    return AppConfig.model_validate(raw)


__all__ = ["AppConfig", "load_config"]
