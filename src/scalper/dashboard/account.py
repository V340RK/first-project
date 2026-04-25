"""BinanceAccountService — реальний баланс акаунту з Binance Futures.

Тягне `/fapi/v2/account` (private, потребує API key/secret), повертає
агреговані числа: wallet, available, unrealized PnL. Кеш TTL ~3с — щоб
UI міг poll-ити часто без спалювання rate-limit weight (account = weight 5).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from scalper.common import time as _time
from scalper.gateway.transport import _RestTransport

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountBalance:
    wallet_balance: float
    available_balance: float
    margin_balance: float
    total_unrealized_pnl: float
    quote_asset: str = "USDT"
    fetched_at_ms: int = 0


class BinanceAccountService:
    """Тягне баланс з Binance Futures з in-memory кешем.

    Параметри:
      transport: _RestTransport з API key/secret + base_url. Час має бути
                 синхронізований (set_time_offset_ms) — викликаємо sync_time()
                 на першому fetch.
      ttl_sec:   TTL кешу. Рекомендовано 3с — UI поллить кожні 1с, але це
                 баланс не міняється так часто.
    """

    def __init__(self, transport: _RestTransport, ttl_sec: float = 3.0) -> None:
        self._transport = transport
        self._ttl_ms = int(ttl_sec * 1000)
        self._cache: AccountBalance | None = None
        self._fetched_ms: int = 0
        self._lock = asyncio.Lock()
        self._time_synced: bool = False
        self._last_error: str | None = None

    async def get_balance(self) -> AccountBalance:
        """Повертає (можливо кешований) баланс. Кидає на перший fetch якщо API не працює."""
        async with self._lock:
            now = _time.clock()
            if self._cache is not None and now - self._fetched_ms <= self._ttl_ms:
                return self._cache
            try:
                if not self._time_synced:
                    await self._sync_time()
                self._cache = await self._fetch()
                self._fetched_ms = now
                self._last_error = None
            except Exception as e:
                self._last_error = str(e)
                logger.warning("account fetch failed: %s", e)
                if self._cache is not None:
                    return self._cache   # fallback to stale
                raise
            return self._cache

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def _sync_time(self) -> None:
        """Server time → time_offset_ms у транспорті. Інакше private запити отримують -1021."""
        srv = await self._transport.public_get("/fapi/v1/time", weight=1)
        offset = int(srv["serverTime"]) - _time.clock()
        self._transport.set_time_offset_ms(offset)
        self._time_synced = True
        logger.info("time synced, offset=%dms", offset)

    async def _fetch(self) -> AccountBalance:
        data: dict[str, Any] = await self._transport.private_get(
            "/fapi/v2/account", weight=5,
        )
        return AccountBalance(
            wallet_balance=float(data.get("totalWalletBalance", 0)),
            available_balance=float(data.get("availableBalance", 0)),
            margin_balance=float(data.get("totalMarginBalance", 0)),
            total_unrealized_pnl=float(data.get("totalUnrealizedProfit", 0)),
            fetched_at_ms=_time.clock(),
        )


__all__ = ["AccountBalance", "BinanceAccountService"]
