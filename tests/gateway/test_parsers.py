"""Юніт-тести parsers — на фікстурах із Binance Futures docs."""

from __future__ import annotations

from scalper.gateway.parsers import (
    parse_agg_trade,
    parse_book_ticker,
    parse_depth_diff,
    parse_depth_snapshot,
    parse_exchange_info,
    parse_kline,
    parse_kline_rest,
    parse_symbol_filters,
    parse_user_event,
)


# === aggTrade ===

def test_parse_agg_trade_buyer_taker() -> None:
    """is_buyer_maker=False → агресивний покупець (ринок купив)."""
    payload = {
        "e": "aggTrade",
        "E": 1672304486106,
        "s": "BTCUSDT",
        "a": 5933014,
        "p": "16700.50",
        "q": "0.012",
        "f": 100,
        "l": 105,
        "T": 1672304486100,
        "m": False,
    }
    trade = parse_agg_trade(payload)
    assert trade.symbol == "BTCUSDT"
    assert trade.timestamp_ms == 1672304486100      # T, не E
    assert trade.price == 16700.50
    assert trade.quantity == 0.012
    assert trade.is_buyer_maker is False
    assert trade.agg_id == 5933014


def test_parse_agg_trade_seller_taker() -> None:
    payload = {
        "T": 1, "s": "ETHUSDT", "p": "1000", "q": "1", "m": True, "a": 42, "E": 1,
    }
    trade = parse_agg_trade(payload)
    assert trade.is_buyer_maker is True


# === depth diff ===

def test_parse_depth_diff_levels() -> None:
    payload = {
        "e": "depthUpdate", "E": 123456789, "T": 123456788, "s": "BTCUSDT",
        "U": 157, "u": 160, "pu": 149,
        "b": [["16700.0", "0.5"], ["16699.5", "1.2"]],
        "a": [["16701.0", "0.8"]],
    }
    diff = parse_depth_diff(payload)
    assert diff.symbol == "BTCUSDT"
    assert diff.first_update_id == 157
    assert diff.final_update_id == 160
    assert diff.bids == [(16700.0, 0.5), (16699.5, 1.2)]
    assert diff.asks == [(16701.0, 0.8)]


# === kline ===

def test_parse_kline_ws() -> None:
    payload = {
        "e": "kline", "E": 1672515782136, "s": "BTCUSDT",
        "k": {
            "t": 1672515780000, "T": 1672515839999, "s": "BTCUSDT", "i": "1m",
            "f": 100, "L": 200,
            "o": "16700.0", "c": "16710.5", "h": "16715.0", "l": "16695.0",
            "v": "12.345", "n": 100, "x": True, "q": "...", "V": "...", "Q": "...",
        },
    }
    k = parse_kline(payload)
    assert k.symbol == "BTCUSDT"
    assert k.interval == "1m"
    assert k.open == 16700.0
    assert k.close == 16710.5
    assert k.is_closed is True


def test_parse_kline_rest() -> None:
    """REST повертає масив масивів. Закриті свічки → is_closed=True."""
    row = [
        1672515780000, "16700.0", "16715.0", "16695.0", "16710.5", "12.345",
        1672515839999, "206384.4", 100, "6.0", "100192", "0",
    ]
    k = parse_kline_rest("BTCUSDT", "1m", row)
    assert k.open_time_ms == 1672515780000
    assert k.close_time_ms == 1672515839999
    assert k.high == 16715.0
    assert k.is_closed is True


# === bookTicker ===

def test_parse_book_ticker() -> None:
    payload = {
        "e": "bookTicker", "u": 400900217, "s": "BNBUSDT", "T": 1568014460893, "E": 1568014460891,
        "b": "25.35190000", "B": "31.21000000",
        "a": "25.36520000", "A": "40.66000000",
    }
    bt = parse_book_ticker(payload)
    assert bt.symbol == "BNBUSDT"
    assert bt.timestamp_ms == 1568014460893        # T має пріоритет над E
    assert bt.best_bid == 25.3519
    assert bt.best_ask == 25.3652


# === depth snapshot ===

def test_parse_depth_snapshot() -> None:
    data = {
        "lastUpdateId": 1027024,
        "bids": [["16700.0", "0.5"]],
        "asks": [["16701.0", "0.8"]],
    }
    snap = parse_depth_snapshot("BTCUSDT", data, received_at_ms=999)
    assert snap.last_update_id == 1027024
    assert snap.timestamp_ms == 999
    assert snap.bids == [(16700.0, 0.5)]


# === user event ===

def test_parse_user_event_order_update() -> None:
    payload = {
        "e": "ORDER_TRADE_UPDATE", "E": 1568879465651,
        "T": 1568879465650,
        "o": {"s": "BTCUSDT", "c": "scl-1", "S": "BUY", "X": "FILLED"},
    }
    ev = parse_user_event(payload)
    assert ev.event_type == "ORDER_TRADE_UPDATE"
    assert ev.timestamp_ms == 1568879465651
    assert ev.payload["o"]["X"] == "FILLED"


def test_parse_user_event_unknown_type_fallback() -> None:
    """Невідомий тип → 'listenKeyExpired' як fallback (caller перевідкриє стрім)."""
    ev = parse_user_event({"e": "SOMETHING_NEW", "E": 1})
    assert ev.event_type == "listenKeyExpired"


# === ExchangeInfo ===

def _binance_symbol_payload() -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "status": "TRADING",
        "pricePrecision": 1,
        "quantityPrecision": 3,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0.10", "maxPrice": "1000000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ],
    }


def test_parse_symbol_filters_basic() -> None:
    f = parse_symbol_filters(_binance_symbol_payload())
    assert f.tick_size == 0.10
    assert f.step_size == 0.001
    assert f.min_qty == 0.001
    assert f.max_qty == 1000.0
    assert f.min_notional == 5.0
    assert f.price_precision == 1
    assert f.qty_precision == 3


def test_parse_exchange_info_full() -> None:
    data = {
        "serverTime": 1700000000000,
        "rateLimits": [
            {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 2400},
            {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 300},
            {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 1200},
        ],
        "symbols": [
            _binance_symbol_payload(),
            {**_binance_symbol_payload(), "symbol": "DELISTED", "status": "BREAK"},
        ],
    }
    info = parse_exchange_info(data, fetched_at_ms=42)
    assert info.server_time_ms == 1700000000000
    assert info.fetched_at_ms == 42
    assert "BTCUSDT" in info.symbols
    assert "DELISTED" not in info.symbols              # status != TRADING — викидаємо
    assert info.rate_limits["REQUEST_WEIGHT_1M"] == 2400
    assert info.rate_limits["ORDERS_10S"] == 300
    assert info.rate_limits["ORDERS_1M"] == 1200
