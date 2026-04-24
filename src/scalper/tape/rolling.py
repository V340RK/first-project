"""_RollingWindow — інкрементальний агрегат `(buy/sell × qty/usd)` за фіксований період.

Інваріант: `sum_buy_qty == sum(c.qty for c in entries if not c.is_buyer_maker)`.
Підтримуємо обидві суми (qty + usd) одночасно — щоб не множити в hot-path при read.

Evict робимо ЛІНИВО при кожному add() та при snapshot read — без окремого таймера.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from scalper.tape.types import TapeWindow


@dataclass
class _TradeContribution:
    ts_ms: int
    price: float
    qty: float
    is_buyer_maker: bool        # True → ринок ПРОДАВ; False → ринок КУПИВ


@dataclass
class _RollingWindow:
    duration_ms: int
    entries: deque[_TradeContribution] = field(default_factory=deque)
    sum_buy_qty: float = 0.0
    sum_sell_qty: float = 0.0
    sum_buy_usd: float = 0.0
    sum_sell_usd: float = 0.0


def add(window: _RollingWindow, contrib: _TradeContribution) -> None:
    window.entries.append(contrib)
    qty_usd = contrib.price * contrib.qty
    if contrib.is_buyer_maker:
        window.sum_sell_qty += contrib.qty
        window.sum_sell_usd += qty_usd
    else:
        window.sum_buy_qty += contrib.qty
        window.sum_buy_usd += qty_usd


def evict_until(window: _RollingWindow, now_ms: int) -> None:
    """Видаляє всі entries з ts < now_ms - duration_ms. Через float-арифметику робимо
    нижню межу нулем (захист від накопиченого drift-у при тривалій роботі)."""
    cutoff = now_ms - window.duration_ms
    while window.entries and window.entries[0].ts_ms < cutoff:
        old = window.entries.popleft()
        qty_usd = old.price * old.qty
        if old.is_buyer_maker:
            window.sum_sell_qty -= old.qty
            window.sum_sell_usd -= qty_usd
        else:
            window.sum_buy_qty -= old.qty
            window.sum_buy_usd -= qty_usd
    if not window.entries:
        # Без entries усе — нуль; запобігає накопиченню float-drift.
        window.sum_buy_qty = 0.0
        window.sum_sell_qty = 0.0
        window.sum_buy_usd = 0.0
        window.sum_sell_usd = 0.0


def snapshot(window: _RollingWindow, last_price: float) -> TapeWindow:
    if window.entries:
        first_ms = window.entries[0].ts_ms
        last_ms = window.entries[-1].ts_ms
    else:
        first_ms = 0
        last_ms = 0
    return TapeWindow(
        duration_ms=window.duration_ms,
        trade_count=len(window.entries),
        buy_volume_qty=window.sum_buy_qty,
        sell_volume_qty=window.sum_sell_qty,
        buy_volume_usd=window.sum_buy_usd,
        sell_volume_usd=window.sum_sell_usd,
        delta_qty=window.sum_buy_qty - window.sum_sell_qty,
        delta_usd=window.sum_buy_usd - window.sum_sell_usd,
        last_trade_price=last_price,
        first_trade_ms=first_ms,
        last_trade_ms=last_ms,
    )


__all__ = ["_RollingWindow", "_TradeContribution", "add", "evict_until", "snapshot"]
