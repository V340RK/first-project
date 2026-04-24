"""Footprint-бар: будується інкрементально з aggTrade подій.

Одна функція `update_bar(bar, trade, tick_size)` + `new_bar()` + `tf_to_ms()`. Немає класу —
стан живе у `FootprintBar` dataclass-і, мутуємо його напряму.
"""

from __future__ import annotations

import math

from scalper.book.types import FootprintBar, LevelVolume
from scalper.gateway.types import RawAggTrade

_TF_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
}


def tf_to_ms(tf: str) -> int:
    try:
        return _TF_MS[tf]
    except KeyError as e:
        raise ValueError(f"Unsupported timeframe: {tf!r}") from e


def bar_open_time_for(ts_ms: int, tf: str) -> int:
    """Початок бару, у який потрапляє ts_ms. Вирівнюємо на UTC-сітку."""
    ms = tf_to_ms(tf)
    return (ts_ms // ms) * ms


def new_bar(symbol: str, tf: str, ts_ms: int) -> FootprintBar:
    open_ms = bar_open_time_for(ts_ms, tf)
    return FootprintBar(
        symbol=symbol,
        timeframe=tf,
        open_time_ms=open_ms,
        close_time_ms=open_ms + tf_to_ms(tf),
    )


def round_to_tick(price: float, tick_size: float) -> float:
    """Ціна → найближчий кратний tick_size. Важливо для кластеризації footprint-рівнів.

    Використовуємо round() замість floor — симетрично ловить і купівлі, і продажі,
    коли тік дорівнює floating-drift від ціни на біржі.
    """
    if tick_size <= 0:
        return price
    steps = round(price / tick_size)
    # Нормалізуємо до кількості десяткових тіку, щоб уникнути 20000.000000001
    decimals = max(0, -int(math.floor(math.log10(tick_size)))) if tick_size < 1 else 0
    return round(steps * tick_size, decimals)


def update_bar(bar: FootprintBar, trade: RawAggTrade, tick_size: float) -> None:
    """Мутує `bar` однією операцією Trade. Викличний код має гарантувати, що trade
    належить цьому бару (bar.open_time_ms <= ts < bar.close_time_ms).
    """
    # OHLC
    if bar.trade_count == 0:
        bar.open = trade.price
        bar.high = trade.price
        bar.low = trade.price
    else:
        if trade.price > bar.high:
            bar.high = trade.price
        if trade.price < bar.low:
            bar.low = trade.price
    bar.close = trade.price
    bar.trade_count += 1

    # Footprint
    price_level = round_to_tick(trade.price, tick_size)
    lvl = bar.levels.get(price_level)
    if lvl is None:
        lvl = LevelVolume()
        bar.levels[price_level] = lvl

    if trade.is_buyer_maker:
        # taker hit bid → ринковий ПРОДАЖ
        lvl.bid_vol += trade.quantity
        bar.delta -= trade.quantity
    else:
        # taker hit ask → ринкова КУПІВЛЯ
        lvl.ask_vol += trade.quantity
        bar.delta += trade.quantity

    # PoC — інкрементально. Замість перерахунку сортуємо тільки якщо поточний рівень
    # ПЕРЕВИЩИВ попередній PoC, або PoC ще не встановлений.
    level_total = lvl.bid_vol + lvl.ask_vol
    if bar.poc_price is None:
        bar.poc_price = price_level
    elif price_level == bar.poc_price:
        pass  # це вже PoC, обсяг лише зростає
    else:
        poc_lvl = bar.levels[bar.poc_price]
        if level_total > poc_lvl.bid_vol + poc_lvl.ask_vol:
            bar.poc_price = price_level


def close_bar(bar: FootprintBar) -> None:
    """Фіналізує бар. Після цього він immutable за конвенцією (не даємо в нього писати)."""
    bar.is_closed = True


__all__ = ["bar_open_time_for", "close_bar", "new_bar", "round_to_tick", "tf_to_ms", "update_bar"]
