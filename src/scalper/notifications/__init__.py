"""NotificationService — єдина точка алертів (Telegram/email/stdout).

Жоден модуль не шле повідомлення напряму — тільки через цей сервіс.
Див. DOCS/architecture/00-overview.md, секція 'NotificationService'.
"""

from scalper.notifications.service import NotificationService

__all__ = ["NotificationService"]
