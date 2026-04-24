"""Публічні типи для Order Book Engine.

Все що експортуємо назовні — frozen dataclasses. Внутрішній стан (mutable bar, book)
тримаємо окремо у відповідних файлах.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBookState:
    """Знімок топ-N рівнів. bids: desc, asks: asc."""

    symbol: str
    timestamp_ms: int
    last_update_id: int
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    is_synced: bool

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return self.asks[0].price - self.bids[0].price

    @property
    def mid_price(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2


@dataclass
class LevelVolume:
    """Обсяги ринкових ордерів, що торкнулися даної ціни всередині бару."""

    bid_vol: float = 0.0  # обсяг ринкових ПРОДАЖІВ (taker hit bid)
    ask_vol: float = 0.0  # обсяг ринкових КУПІВЕЛЬ (taker hit ask)


@dataclass
class FootprintBar:
    """Один бар: OHLC + cluster (ціна → bid/ask обсяги) + delta + PoC."""

    symbol: str
    timeframe: str
    open_time_ms: int
    close_time_ms: int
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    levels: dict[float, LevelVolume] = field(default_factory=dict)
    poc_price: float | None = None
    delta: float = 0.0  # Σ(ask_vol − bid_vol); > 0 = покупець переважав
    trade_count: int = 0
    is_closed: bool = False

    @property
    def total_volume(self) -> float:
        return sum(lv.bid_vol + lv.ask_vol for lv in self.levels.values())


@dataclass(frozen=True)
class BarOHLC:
    """Легка версія без footprint — для helpers, які приймають «просто бар»."""

    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float


Side = Literal["BID", "ASK"]


@dataclass(frozen=True)
class Imbalance:
    """Діагональний imbalance між сусідніми ціновими рівнями."""

    price_level: float
    side: Side
    ratio: float
    volume: float


@dataclass(frozen=True)
class StackedImbalance:
    """Послідовність ≥N сусідніх imbalance однієї сторони."""

    start_price: float
    end_price: float
    side: Side
    level_count: int
    total_volume: float


class PocLocation(str, Enum):
    TOP = "top"
    BOTTOM = "bottom"
    CENTER = "center"
    UPPER_WICK = "upper_wick"
    LOWER_WICK = "lower_wick"


__all__ = [
    "BarOHLC",
    "FootprintBar",
    "Imbalance",
    "LevelVolume",
    "OrderBookLevel",
    "OrderBookState",
    "PocLocation",
    "Side",
    "StackedImbalance",
]
