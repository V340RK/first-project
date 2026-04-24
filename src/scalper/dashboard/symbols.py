"""SymbolService — живий список торгованих пар з Binance exchangeInfo.

Тягне `/fapi/v1/exchangeInfo`, фільтрує до TRADING + PERPETUAL + USDT-quote
(саме це наш бот обробляє). Кеш in-memory, TTL 10 хв — щоб не ганяти REST
щоразу коли UI відкриває сторінку.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from scalper.common import time as _time

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base: str
    quote: str
    tick_size: float
    step_size: float
    min_notional: float


class BinanceSymbolService:
    def __init__(self, base_url: str, ttl_sec: int = 600) -> None:
        self._base = base_url.rstrip("/")
        self._ttl_ms = ttl_sec * 1000
        self._cache: list[SymbolInfo] | None = None
        self._fetched_ms: int = 0
        self._lock = asyncio.Lock()

    async def list_symbols(self) -> list[SymbolInfo]:
        """Повертає (кешований) список USDT-M PERPETUAL пар у статусі TRADING."""
        async with self._lock:
            now = _time.clock()
            if self._cache is not None and now - self._fetched_ms <= self._ttl_ms:
                return self._cache
            try:
                self._cache = await self._fetch()
                self._fetched_ms = now
            except Exception as e:
                logger.warning("exchangeInfo fetch failed: %s", e)
                if self._cache is not None:
                    return self._cache
                raise
            return self._cache

    async def is_valid(self, symbol: str) -> bool:
        syms = await self.list_symbols()
        target = symbol.upper()
        return any(s.symbol == target for s in syms)

    async def _fetch(self) -> list[SymbolInfo]:
        url = f"{self._base}/fapi/v1/exchangeInfo"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return [
            _parse(entry) for entry in data.get("symbols", [])
            if entry.get("status") == "TRADING"
            and entry.get("contractType") == "PERPETUAL"
            and entry.get("quoteAsset") == "USDT"
        ]


def _parse(entry: dict) -> SymbolInfo:
    tick = 0.0
    step = 0.0
    notional = 0.0
    for f in entry.get("filters", []):
        ftype = f.get("filterType")
        if ftype == "PRICE_FILTER":
            tick = float(f.get("tickSize", 0))
        elif ftype == "LOT_SIZE":
            step = float(f.get("stepSize", 0))
        elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
            notional = float(f.get("notional", f.get("minNotional", 0)))
    return SymbolInfo(
        symbol=entry["symbol"],
        base=entry["baseAsset"],
        quote=entry["quoteAsset"],
        tick_size=tick,
        step_size=step,
        min_notional=notional,
    )


__all__ = ["BinanceSymbolService", "SymbolInfo"]
