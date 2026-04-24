"""Публічні типи Tape Analyzer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TapeWindow:
    """Зведення по rolling-вікну заданої тривалості."""

    duration_ms: int
    trade_count: int
    buy_volume_qty: float           # base asset
    sell_volume_qty: float
    buy_volume_usd: float           # quote asset (USDT)
    sell_volume_usd: float
    delta_qty: float                # buy - sell
    delta_usd: float
    last_trade_price: float
    first_trade_ms: int             # 0 якщо вікно порожнє
    last_trade_ms: int


@dataclass(frozen=True)
class TapeWindowsState:
    """Замороджений знімок усіх вікон + кумулятивний CVD + price_path."""

    symbol: str
    timestamp_ms: int

    window_500ms: TapeWindow
    window_2s: TapeWindow
    window_10s: TapeWindow

    cvd: float
    cvd_reliable: bool

    delta_500ms: float              # USD shortcuts (FeatureEngine любить швидкий доступ)
    delta_2s: float
    delta_10s: float

    price_path: list[tuple[int, float]] = field(default_factory=list)


__all__ = ["TapeWindow", "TapeWindowsState"]
