"""Pure functions: Binance JSON payload → нашi dataclass-и.

Ніяких side-effect-ів. Беремо `dict` (як після `json.loads`), повертаємо typed.
Це найдешевший шар для юніт-тестів — фікстури ставимо із Binance docs.

Binance WS payload поля (документація біржі):
    aggTrade  : E (event time), s (symbol), p (price), q (qty),
                m (is_buyer_maker), a (agg trade id)
    depthDiff : E, s, U (first update id), u (final update id),
                b (bids: [[price, qty], ...]), a (asks: ...)
    kline     : E, s, k.{t,T,o,h,l,c,v,x,i}
    bookTicker: E, s, b (best bid), B (bid qty), a (best ask), A (ask qty)
"""

from __future__ import annotations

from typing import Any, cast

from scalper.gateway.types import (
    DepthSnapshot,
    ExchangeInfo,
    RawAggTrade,
    RawBookTicker,
    RawDepthDiff,
    RawKline,
    RawUserEvent,
    SymbolFilters,
)


def parse_agg_trade(data: dict[str, Any]) -> RawAggTrade:
    """`{e,E,s,a,p,q,f,l,T,m}` → RawAggTrade."""
    return RawAggTrade(
        timestamp_ms=int(data["T"]),                 # Trade time (а не E — event time)
        symbol=str(data["s"]),
        price=float(data["p"]),
        quantity=float(data["q"]),
        is_buyer_maker=bool(data["m"]),
        agg_id=int(data["a"]),
    )


def parse_depth_diff(data: dict[str, Any]) -> RawDepthDiff:
    """`{e,E,s,U,u,b,a}` → RawDepthDiff. Списки bid/ask конвертуємо в (price, qty)."""
    return RawDepthDiff(
        symbol=str(data["s"]),
        first_update_id=int(data["U"]),
        final_update_id=int(data["u"]),
        bids=[(float(p), float(q)) for p, q in data["b"]],
        asks=[(float(p), float(q)) for p, q in data["a"]],
    )


def parse_kline(data: dict[str, Any]) -> RawKline:
    """`{e,E,s,k:{t,T,s,i,o,c,h,l,v,x,...}}` → RawKline.

    Зверни увагу: WS-payload загорнутий у `data['k']`. REST `/klines` повертає
    масив масивів — для цього використовуй `parse_kline_rest()`.
    """
    k = data["k"]
    return RawKline(
        symbol=str(k["s"]),
        interval=str(k["i"]),
        open_time_ms=int(k["t"]),
        close_time_ms=int(k["T"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        is_closed=bool(k["x"]),
    )


def parse_kline_rest(symbol: str, interval: str, row: list[Any]) -> RawKline:
    """REST `/fapi/v1/klines` повертає масив масивів — індекси Binance docs:
    [openTime, open, high, low, close, volume, closeTime, ...]
    REST даних — завжди закриті свічки, тож is_closed=True.
    """
    return RawKline(
        symbol=symbol,
        interval=interval,
        open_time_ms=int(row[0]),
        close_time_ms=int(row[6]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        is_closed=True,
    )


def parse_book_ticker(data: dict[str, Any]) -> RawBookTicker:
    """`{e,u,E,T,s,b,B,a,A}` → RawBookTicker."""
    return RawBookTicker(
        symbol=str(data["s"]),
        timestamp_ms=int(data.get("T") or data.get("E", 0)),
        best_bid=float(data["b"]),
        best_bid_qty=float(data["B"]),
        best_ask=float(data["a"]),
        best_ask_qty=float(data["A"]),
    )


def parse_depth_snapshot(symbol: str, data: dict[str, Any], received_at_ms: int) -> DepthSnapshot:
    """REST `/fapi/v1/depth` → DepthSnapshot.

    `received_at_ms` — час, коли ми отримали відповідь (Binance не повертає E у snapshot).
    """
    return DepthSnapshot(
        symbol=symbol,
        last_update_id=int(data["lastUpdateId"]),
        bids=[(float(p), float(q)) for p, q in data["bids"]],
        asks=[(float(p), float(q)) for p, q in data["asks"]],
        timestamp_ms=received_at_ms,
    )


def parse_user_event(data: dict[str, Any]) -> RawUserEvent:
    """User Data Stream подія. Тип — у полі `e`."""
    event_type = str(data.get("e", ""))
    valid_types = {"ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE", "MARGIN_CALL", "listenKeyExpired"}
    if event_type not in valid_types:
        # Невідомий тип — записуємо як listenKeyExpired-fallback щоб caller відреагував.
        # (Краще явний raise тут, але хочемо resilience: dispatcher просто пропустить.)
        event_type = "listenKeyExpired"
    return RawUserEvent(
        event_type=cast(Any, event_type),
        timestamp_ms=int(data.get("E", 0)),
        payload=dict(data),
    )


# === ExchangeInfo ===

_KEEP_RATE_LIMITS = {"REQUEST_WEIGHT", "ORDERS"}


def _filters_by_type(filters: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(f["filterType"]): f for f in filters}


def parse_symbol_filters(symbol_data: dict[str, Any]) -> SymbolFilters:
    """Один елемент `exchangeInfo.symbols[i]` → SymbolFilters.

    Binance тримає filters у списку обʼєктів виду `{"filterType":"PRICE_FILTER","tickSize":"0.10"}`.
    Тут зводимо до пласкої структури.
    """
    by_type = _filters_by_type(symbol_data["filters"])
    price = by_type["PRICE_FILTER"]
    lot = by_type["LOT_SIZE"]
    # MIN_NOTIONAL у Binance Futures може бути або "MIN_NOTIONAL" або "NOTIONAL" (новіше).
    notional = by_type.get("MIN_NOTIONAL") or by_type.get("NOTIONAL", {})
    min_notional_value = notional.get("notional") or notional.get("minNotional", "0")

    return SymbolFilters(
        tick_size=float(price["tickSize"]),
        step_size=float(lot["stepSize"]),
        min_qty=float(lot["minQty"]),
        max_qty=float(lot["maxQty"]),
        min_notional=float(min_notional_value),
        price_precision=int(symbol_data.get("pricePrecision", 0)),
        qty_precision=int(symbol_data.get("quantityPrecision", 0)),
    )


def parse_exchange_info(data: dict[str, Any], fetched_at_ms: int) -> ExchangeInfo:
    """REST `/fapi/v1/exchangeInfo` → ExchangeInfo (нормалізована мапа за символом)."""
    symbols = {
        str(s["symbol"]): parse_symbol_filters(s)
        for s in data.get("symbols", [])
        if s.get("status", "TRADING") == "TRADING"
    }

    # Binance повертає `rateLimits: [{rateLimitType,interval,intervalNum,limit}, ...]`.
    # Зводимо до простої мапи "TYPE_INTERVALm/s" → limit. Зберігаємо лише relevant types.
    rate_limits: dict[str, int] = {}
    for rl in data.get("rateLimits", []):
        rl_type = str(rl.get("rateLimitType", ""))
        if rl_type not in _KEEP_RATE_LIMITS:
            continue
        key = f"{rl_type}_{rl['intervalNum']}{rl['interval'][0].upper()}"
        rate_limits[key] = int(rl["limit"])

    return ExchangeInfo(
        server_time_ms=int(data.get("serverTime", 0)),
        fetched_at_ms=fetched_at_ms,
        symbols=symbols,
        rate_limits=rate_limits,
    )
