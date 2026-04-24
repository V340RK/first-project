"""NotificationService — fire-and-forget сповіщення з rate-limit + async queue."""

from __future__ import annotations

from scalper.common.enums import AlertLevel


class NotificationService:
    """API:
        await service.send("kill switch engaged: -3R day", AlertLevel.CRITICAL)

    Реалізація НЕ повинна блокувати hot loop. Помилка доставки → лог + WARNING-подія в Journal.
    """

    def __init__(self, config: object) -> None:
        # config: NotificationConfig (telegram_token, chat_id, channels, rate_limits)
        raise NotImplementedError

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def send(self, text: str, level: AlertLevel) -> None:
        raise NotImplementedError
