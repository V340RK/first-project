"""BookSnapshotService — REST `/fapi/v1/depth` для UI mini-orderbook.

Public endpoint (без API key). Кеш per-symbol з TTL ~1.5с — щоб UI міг
poll-ити часто без перевитрати rate-limit weight (depth limit=10 → weight 2).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from scalper.common import time as _time

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class BookSnapshot:
    symbol: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    fetched_at_ms: int
    last_update_id: int


class BookSnapshotService:
    def __init__(self, base_url: str, ttl_sec: float = 1.5) -> None:
        self._base = base_url.rstrip("/")
        self._ttl_ms = int(ttl_sec * 1000)
        self._cache: dict[str, BookSnapshot] = {}
        self._fetched_at: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, symbol: str, depth: int = 10) -> BookSnapshot:
        sym = symbol.upper()
        depth = max(5, min(20, depth))
        # Per-symbol lock — не дублюємо паралельні fetch для тієї ж пари
        if sym not in self._locks:
            self._locks[sym] = asyncio.Lock()
        async with self._locks[sym]:
            now = _time.clock()
            cached = self._cache.get(sym)
            if cached is not None and now - self._fetched_at[sym] <= self._ttl_ms:
                return cached
            try:
                snap = await self._fetch(sym, depth)
                self._cache[sym] = snap
                self._fetched_at[sym] = now
                return snap
            except Exception as e:
                logger.warning("depth fetch failed for %s: %s", sym, e)
                if cached is not None:
                    return cached
                raise

    async def _fetch(self, symbol: str, depth: int) -> BookSnapshot:
        url = f"{self._base}/fapi/v1/depth"
        params = {"symbol": symbol, "limit": depth}
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return BookSnapshot(
            symbol=symbol,
            bids=[BookLevel(float(p), float(s)) for p, s in data.get("bids", [])],
            asks=[BookLevel(float(p), float(s)) for p, s in data.get("asks", [])],
            fetched_at_ms=_time.clock(),
            last_update_id=int(data.get("lastUpdateId", 0)),
        )


__all__ = ["BookLevel", "BookSnapshot", "BookSnapshotService"]
