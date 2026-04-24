"""_RestTransport — спільна REST-інфраструктура для Gateway і ExecutionEngine.

Що тут є:
  * HMAC-SHA256 підпис приватних запитів
  * Rate-limit трекер з авто-блокуванням при перевищенні
  * Retry з backoff на 5xx / мережеві помилки
  * Time offset (синхронізація з serverTime — оновлюється Gateway-ем зовні)

Що НЕ тут:
  * WebSocket — окремо в gateway.py
  * Конкретні бізнес-методи (`place_order`, `fetch_klines`) — у викликачах
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any
from urllib.parse import urlencode

import aiohttp

from scalper.common.time import clock
from scalper.gateway.config import GatewayConfig

logger = logging.getLogger(__name__)


class RestError(Exception):
    """Генерик-помилка REST-виклику. Несе HTTP status + Binance code (-1021, -2010, ...)."""

    def __init__(self, status: int, code: int | None, message: str, retry_after: int | None = None) -> None:
        super().__init__(f"REST {status} (code={code}): {message}")
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class RateLimitBlocked(RestError):
    """418 / 429 — IP пригальмований. Чекати `retry_after` секунд."""


class _RestTransport:
    """Async HTTP-клієнт із підписом, rate-limit та retry.

    Підкреслене ім'я (_RestTransport) — внутрішній, експортується лише
    через Gateway/Execution. Інші модулі НЕ створюють його напряму.
    """

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None

        # Rate-limit стан (оновлюється з кожної відповіді через headers).
        self._used_weight_1m: int = 0
        self._weight_resets_at_ms: int = 0
        self._banned_until_ms: int = 0

        # Time offset = serverTime - localTime. Оновлюється Gateway-ем у time_sync_loop.
        # Тримаємо тут, бо потрібен для підпису timestamp у приватних запитах.
        self._time_offset_ms: int = 0

    # === Lifecycle ===

    async def start(self) -> None:
        """Створити aiohttp-сесію. Викликати перед першим запитом."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._config.rest.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    # === Time sync (зовнішній виклик з gateway) ===

    def set_time_offset_ms(self, offset_ms: int) -> None:
        self._time_offset_ms = offset_ms

    def get_time_offset_ms(self) -> int:
        return self._time_offset_ms

    # === Rate-limit трекінг ===

    def get_used_weight(self) -> int:
        return self._used_weight_1m

    # === Public REST ===

    async def public_get(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        weight: int = 1,
    ) -> Any:
        return await self._request("GET", endpoint, params=params, weight=weight, signed=False)

    # === Private REST (потребує api_key + secret_key) ===

    async def private_get(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        weight: int = 1,
    ) -> Any:
        return await self._request("GET", endpoint, params=params, weight=weight, signed=True)

    async def private_post(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        weight: int = 1,
    ) -> Any:
        return await self._request("POST", endpoint, params=params, weight=weight, signed=True)

    async def private_put(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        weight: int = 1,
    ) -> Any:
        return await self._request("PUT", endpoint, params=params, weight=weight, signed=True)

    async def private_delete(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        weight: int = 1,
    ) -> Any:
        return await self._request("DELETE", endpoint, params=params, weight=weight, signed=True)

    # === Internal: signing ===

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        """HMAC-SHA256 підпис query string. Додає timestamp + signature."""
        if self._config.api_key is None or self._config.secret_key is None:
            raise RestError(401, None, "API ключі не налаштовані для приватного запиту")
        signed = {**params, "timestamp": clock() + self._time_offset_ms}
        query = urlencode(signed, doseq=True)
        secret = self._config.secret_key.get_secret_value().encode()
        sig = hmac.new(secret, query.encode(), hashlib.sha256).hexdigest()
        signed["signature"] = sig
        return signed

    def _headers(self, signed: bool) -> dict[str, str]:
        headers = {"User-Agent": "scalper/0.0.1"}
        if signed:
            assert self._config.api_key is not None
            headers["X-MBX-APIKEY"] = self._config.api_key.get_secret_value()
        return headers

    # === Internal: rate-limit gating ===

    async def _check_rate_limit(self, incoming_weight: int) -> None:
        """Якщо ми над порогом — заснути до скидання вікна."""
        now = clock()
        if now < self._banned_until_ms:
            wait_ms = self._banned_until_ms - now
            logger.warning("Rate-limit ban active, sleeping %dms", wait_ms)
            await asyncio.sleep(wait_ms / 1000)
            return
        threshold = self._config.rate_limit.weight_threshold
        if (
            self._config.rate_limit.block_when_above
            and self._used_weight_1m + incoming_weight > threshold
        ):
            wait_ms = max(self._weight_resets_at_ms - now, 1000)
            logger.warning(
                "Weight %d + %d > %d, sleeping %dms",
                self._used_weight_1m, incoming_weight, threshold, wait_ms,
            )
            await asyncio.sleep(wait_ms / 1000)

    def _track_rate_limit(self, headers: aiohttp.typedefs.LooseHeaders) -> None:
        """Оновити трекер з заголовків відповіді Binance."""
        used = headers.get("X-MBX-USED-WEIGHT-1M") if hasattr(headers, "get") else None
        if used is not None:
            try:
                self._used_weight_1m = int(used)
            except (ValueError, TypeError):
                pass
        # Вікно скидається на повну хвилину UTC; беремо +60s від `now` як консервативну стелю.
        self._weight_resets_at_ms = clock() + 60_000

    # === Internal: запит з retry ===

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None,
        weight: int,
        signed: bool,
    ) -> Any:
        if self._session is None:
            await self.start()
        assert self._session is not None

        await self._check_rate_limit(weight)

        url = self._config.base_url.rstrip("/") + endpoint
        body_params = self._sign(params or {}) if signed else (params or {})

        last_exc: Exception | None = None
        for attempt in range(self._config.rest.max_retries):
            try:
                async with self._session.request(
                    method,
                    url,
                    params=body_params if method == "GET" else None,
                    data=body_params if method != "GET" else None,
                    headers=self._headers(signed),
                ) as resp:
                    self._track_rate_limit(resp.headers)
                    text = await resp.text()
                    if resp.status >= 400:
                        await self._raise_for_status(resp.status, resp.headers, text)
                    return await resp.json(content_type=None) if text else {}
            except RateLimitBlocked as e:
                # 418 — пауза, без retry в цьому виклику; чекатимемо у _check_rate_limit().
                self._banned_until_ms = clock() + (e.retry_after or 60) * 1000
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                logger.warning("REST %s %s attempt %d failed: %s", method, endpoint, attempt + 1, e)
                await asyncio.sleep(self._config.rest.retry_delay * (2 ** attempt))

        assert last_exc is not None
        raise RestError(0, None, f"REST {method} {endpoint}: всі retry-и впали ({last_exc})")

    async def _raise_for_status(self, status: int, headers: aiohttp.typedefs.LooseHeaders, text: str) -> None:
        """Конвертувати HTTP-помилку у типізовану. Binance повертає `{"code":-1021,"msg":"..."}`."""
        code: int | None = None
        msg = text
        try:
            import json
            payload = json.loads(text)
            code = payload.get("code")
            msg = payload.get("msg", text)
        except (ValueError, TypeError):
            pass
        retry_after_raw = headers.get("Retry-After") if hasattr(headers, "get") else None
        retry_after = int(retry_after_raw) if retry_after_raw else None
        if status in (418, 429):
            raise RateLimitBlocked(status, code, msg, retry_after=retry_after)
        raise RestError(status, code, msg, retry_after=retry_after)
