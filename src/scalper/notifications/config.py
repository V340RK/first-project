"""NotificationConfig — канали, rate-limit."""

from __future__ import annotations

from pydantic import BaseModel, Field


class NotificationConfig(BaseModel):
    enabled: bool = True
    console: bool = True
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    min_level: str = "info"                   # info|warning|error|critical
    rate_limit_per_minute: int = Field(default=30, gt=0)
    queue_size: int = Field(default=200, gt=0)


__all__ = ["NotificationConfig"]
