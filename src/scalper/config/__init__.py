"""Завантаження settings.yaml у типізовані pydantic-моделі."""

from scalper.config.loader import AppConfig, load_config

__all__ = ["AppConfig", "load_config"]
