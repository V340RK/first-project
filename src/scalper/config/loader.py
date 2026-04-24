"""Завантаження settings.yaml + secrets.yaml у pydantic-модель.

Жодний модуль не читає файли напряму — все через AppConfig.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class AppConfig(BaseModel):
    """Кореневий конфіг застосунку.

    Поки stub — конкретні sub-config'и (GatewayConfig, RiskConfig, DecisionConfig тощо)
    додаємо коли реалізуємо відповідний модуль.
    """

    model_config = {"extra": "allow"}    # дозволяємо невідомі поля поки розробка триває

    symbols: list[str]
    mode: str = "live"                   # 'live' | 'replay' | 'paper'

    @classmethod
    def from_yaml(cls, path: Path) -> "AppConfig":
        raise NotImplementedError


def load_config(settings_path: Path, secrets_path: Path | None = None) -> AppConfig:
    """Зливає settings.yaml + secrets.yaml (якщо є) у єдиний AppConfig."""
    raise NotImplementedError
