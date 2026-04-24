"""Stateless cluster-утиліти. Вхід: FootprintBar + параметри. Вихід: списки знахідок.

Застосовуються FeatureEngine-ом. Тут немає доступу до біржі, стакану чи стрічки —
чисті функції, легко тестуються.
"""

from __future__ import annotations

from scalper.book.types import (
    BarOHLC,
    FootprintBar,
    Imbalance,
    PocLocation,
    Side,
    StackedImbalance,
)


def detect_imbalances(fp: FootprintBar, ratio: float = 2.5) -> list[Imbalance]:
    """Діагональний imbalance між сусідніми рівнями.

    Послідовність рівнів: низ → верх (ціна asc).

    BULL-imbalance (сторона ASK): на верхньому рівні taker активно КУПУВАВ (ask_vol_hi),
    а на нижньому рівні taker мало ПРОДАВ (bid_vol_lo). Співвідношення
    `ask_vol_hi / bid_vol_lo >= ratio` — ініціатива на купівлю «їсть» ліміти.

    BEAR-imbalance (сторона BID): дзеркально — taker продає агресивно внизу vs
    слабка купівля нагорі. `bid_vol_lo / ask_vol_hi >= ratio`.
    """
    levels = sorted(fp.levels.items())  # (price, LevelVolume), asc
    out: list[Imbalance] = []
    for i in range(1, len(levels)):
        p_lo, v_lo = levels[i - 1]
        p_hi, v_hi = levels[i]

        # Bull: ASK-сторона панує нагорі
        if v_lo.bid_vol > 0:
            r = v_hi.ask_vol / v_lo.bid_vol
            if r >= ratio and v_hi.ask_vol > 0:
                out.append(Imbalance(price_level=p_hi, side="ASK", ratio=r, volume=v_hi.ask_vol))

        # Bear: BID-сторона панує внизу
        if v_hi.ask_vol > 0:
            r = v_lo.bid_vol / v_hi.ask_vol
            if r >= ratio and v_lo.bid_vol > 0:
                out.append(Imbalance(price_level=p_lo, side="BID", ratio=r, volume=v_lo.bid_vol))
    return out


def detect_stacked(imbs: list[Imbalance], min_count: int = 3) -> list[StackedImbalance]:
    """Групує послідовні imbalance однієї сторони у stacked-знахідки.

    «Послідовні» = сортуємо по ціні, розриви між ціновими рівнями поки ігноруємо.
    Це відповідає типовому тлумаченню: декілька imbalance поспіль = сильний impuls.
    """
    if not imbs:
        return []
    out: list[StackedImbalance] = []
    current: list[Imbalance] = []
    for imb in sorted(imbs, key=lambda x: x.price_level):
        if not current or current[-1].side != imb.side:
            if len(current) >= min_count:
                out.append(_build_stacked(current))
            current = [imb]
        else:
            current.append(imb)
    if len(current) >= min_count:
        out.append(_build_stacked(current))
    return out


def _build_stacked(group: list[Imbalance]) -> StackedImbalance:
    side: Side = group[0].side
    return StackedImbalance(
        start_price=min(i.price_level for i in group),
        end_price=max(i.price_level for i in group),
        side=side,
        level_count=len(group),
        total_volume=sum(i.volume for i in group),
    )


def classify_poc_location(fp: FootprintBar, bar: BarOHLC | None = None) -> PocLocation:
    """Де стоїть PoC відносно тіла/тіней бару.

    UPPER_WICK / LOWER_WICK — PoC у тіні (використовується для detect reversal-bar).
    TOP / BOTTOM — у верхній/нижній третині діапазону.
    CENTER — або в середині, або бар виродився (range≈0).
    """
    if fp.poc_price is None:
        return PocLocation.CENTER
    if bar is None:
        bar = BarOHLC(
            open_time_ms=fp.open_time_ms, close_time_ms=fp.close_time_ms,
            open=fp.open, high=fp.high, low=fp.low, close=fp.close,
        )
    body_lo = min(bar.open, bar.close)
    body_hi = max(bar.open, bar.close)
    if fp.poc_price > body_hi:
        return PocLocation.UPPER_WICK
    if fp.poc_price < body_lo:
        return PocLocation.LOWER_WICK
    rng = bar.high - bar.low
    if rng <= 0:
        return PocLocation.CENTER
    pos = (fp.poc_price - bar.low) / rng
    if pos > 2 / 3:
        return PocLocation.TOP
    if pos < 1 / 3:
        return PocLocation.BOTTOM
    return PocLocation.CENTER


__all__ = ["classify_poc_location", "detect_imbalances", "detect_stacked"]
