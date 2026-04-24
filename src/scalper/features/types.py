"""Features snapshot — вхід для SetupDetector / DecisionEngine.

Один зріз для одного символу на конкретний момент часу. Імутабельний.
Узгоджений із DOCS/architecture/04-feature-engine.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scalper.book.types import FootprintBar, OrderBookState
from scalper.tape.types import TapeWindowsState

Side = Literal["BID", "ASK", "NONE"]
PressureSide = Literal["BID", "ASK", "NEUTRAL"]
PocLocation = Literal["HIGH", "MID", "LOW"]


@dataclass(frozen=True)
class MarketSnapshot:
    """Сирий зріз: книга + стрічка + остання ціна. Вхід FeatureEngine.compute()."""

    timestamp_ms: int
    symbol: str
    book: OrderBookState
    tape: TapeWindowsState
    last_price: float
    spread_ticks: int
    footprint: FootprintBar | None = None


@dataclass(frozen=True)
class PullbackState:
    """Стан мікро-пулбеку (див. 04-feature-engine.md, розділ 'micro_pullback')."""

    direction: Literal["LONG_PULLBACK", "SHORT_PULLBACK"]
    depth_ticks: int
    bars_in_pullback: int
    delta_during_pullback: float


@dataclass(frozen=True)
class Features:
    """Компактний знімок ринку для scoring-у."""

    snapshot: MarketSnapshot

    # === Order book ===
    bid_ask_imbalance_5: float
    bid_ask_imbalance_10: float
    weighted_imbalance: float
    book_pressure_side: PressureSide

    # === Tape / flow ===
    delta_500ms: float
    delta_2s: float
    delta_10s: float
    cvd: float
    aggressive_buy_burst: bool
    aggressive_sell_burst: bool
    burst_size_usd: float | None

    # === Behavioral ===
    absorption_score: float
    absorption_side: Side
    spoof_score: float
    spoof_side: Side

    # === Micro structure ===
    micro_pullback: PullbackState | None

    # === Cluster (footprint) ===
    poc_offset_ticks: int
    poc_location: PocLocation
    stacked_imbalance_long: bool
    stacked_imbalance_short: bool
    bar_finished: bool
    bar_delta: float

    # === Zone context ===
    in_htf_poi: bool
    htf_poi_type: str | None
    htf_poi_side: Literal["SUPPORT", "RESISTANCE"] | None
    distance_to_poi_ticks: int | None


__all__ = [
    "Features",
    "MarketSnapshot",
    "PocLocation",
    "PressureSide",
    "PullbackState",
    "Side",
]
