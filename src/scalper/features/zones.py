"""ZoneRegistry — список активних HTF POI (FVG / OB / VAH / VAL / POC).

Оновлюється повільним фоновим loop'ом (поки не реалізовано — порожній стан).
FeatureEngine лише читає.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ZoneType = Literal["FVG", "OB", "VAH", "VAL", "POC", "IB_HIGH", "IB_LOW"]
ZoneSide = Literal["SUPPORT", "RESISTANCE"]


@dataclass(frozen=True)
class HtfZone:
    type: ZoneType
    price_low: float
    price_high: float
    side: ZoneSide
    timeframe: str
    created_at_ms: int
    touched_count: int = 0


class ZoneRegistry:
    """Per-symbol список зон. Усі операції синхронні, без блокувань."""

    def __init__(self) -> None:
        self._by_symbol: dict[str, list[HtfZone]] = {}

    def replace(self, symbol: str, zones: list[HtfZone]) -> None:
        self._by_symbol[symbol.upper()] = list(zones)

    def all(self, symbol: str) -> list[HtfZone]:
        return self._by_symbol.get(symbol.upper(), [])

    def find_containing(self, symbol: str, price: float) -> HtfZone | None:
        for z in self._by_symbol.get(symbol.upper(), []):
            if z.price_low <= price <= z.price_high:
                return z
        return None

    def find_nearest(self, symbol: str, price: float, max_distance: float) -> HtfZone | None:
        best: HtfZone | None = None
        best_dist = max_distance
        for z in self._by_symbol.get(symbol.upper(), []):
            if z.price_low <= price <= z.price_high:
                continue  # це containing, не nearest
            dist = min(abs(z.price_low - price), abs(z.price_high - price))
            if dist <= best_dist:
                best_dist = dist
                best = z
        return best


__all__ = ["HtfZone", "ZoneRegistry", "ZoneSide", "ZoneType"]
