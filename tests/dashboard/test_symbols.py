"""BinanceSymbolService — кеш TTL, фільтр TRADING/PERPETUAL/USDT, is_valid."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from scalper.dashboard.symbols import BinanceSymbolService, SymbolInfo


_FAKE_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL",
            "baseAsset": "BTC", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "5.0"},
            ],
        },
        {
            "symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL",
            "baseAsset": "ETH", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            ],
        },
        # Фільтрується: статус інший
        {"symbol": "OLDUSDT", "status": "SETTLING", "contractType": "PERPETUAL",
         "baseAsset": "OLD", "quoteAsset": "USDT", "filters": []},
        # Фільтрується: не perpetual
        {"symbol": "BTCUSDT_250301", "status": "TRADING", "contractType": "CURRENT_QUARTER",
         "baseAsset": "BTC", "quoteAsset": "USDT", "filters": []},
        # Фільтрується: не USDT
        {"symbol": "BTCBUSD", "status": "TRADING", "contractType": "PERPETUAL",
         "baseAsset": "BTC", "quoteAsset": "BUSD", "filters": []},
    ],
}


def _mock_session(response_json: dict):   # type: ignore[no-untyped-def]
    """Мокаємо aiohttp.ClientSession, який повертає response_json на .get()."""
    import aiohttp

    class FakeResp:
        status = 200
        def raise_for_status(self): pass
        async def json(self): return response_json
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class FakeSession:
        def __init__(self, *a, **kw): pass
        def get(self, url): return FakeResp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    return patch.object(aiohttp, "ClientSession", FakeSession)


@pytest.mark.asyncio
async def test_list_symbols_filters_and_parses() -> None:
    with _mock_session(_FAKE_EXCHANGE_INFO):
        svc = BinanceSymbolService("https://testnet.binancefuture.com")
        syms = await svc.list_symbols()
    assert {s.symbol for s in syms} == {"BTCUSDT", "ETHUSDT"}
    btc = next(s for s in syms if s.symbol == "BTCUSDT")
    assert btc.base == "BTC"
    assert btc.quote == "USDT"
    assert btc.tick_size == 0.1
    assert btc.step_size == 0.001
    assert btc.min_notional == 5.0


@pytest.mark.asyncio
async def test_cache_hits_within_ttl() -> None:
    with _mock_session(_FAKE_EXCHANGE_INFO) as m:
        svc = BinanceSymbolService("https://testnet.binancefuture.com", ttl_sec=600)
        r1 = await svc.list_symbols()
        r2 = await svc.list_symbols()
    assert r1 is r2   # той самий об'єкт з кешу


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    from scalper.common import time as _time
    now = [1_000_000]
    monkeypatch.setattr(_time, "clock", lambda: now[0])

    with _mock_session(_FAKE_EXCHANGE_INFO):
        svc = BinanceSymbolService("https://testnet.binancefuture.com", ttl_sec=10)
        first = await svc.list_symbols()
        # Рухаємо час на 11 секунд → кеш має протухнути
        now[0] += 11_000
        second = await svc.list_symbols()
    assert first is not second   # новий об'єкт — refetched


@pytest.mark.asyncio
async def test_is_valid_accepts_mixed_case() -> None:
    with _mock_session(_FAKE_EXCHANGE_INFO):
        svc = BinanceSymbolService("https://testnet.binancefuture.com")
        assert await svc.is_valid("BTCUSDT") is True
        assert await svc.is_valid("btcusdt") is True
        assert await svc.is_valid("BtcUsdt") is True
        assert await svc.is_valid("NOTAPAIR") is False
        # Відфільтровані - також False
        assert await svc.is_valid("OLDUSDT") is False
        assert await svc.is_valid("BTCBUSD") is False


@pytest.mark.asyncio
async def test_fallback_to_stale_cache_on_fetch_error() -> None:
    """Якщо після успішного першого fetch мережа впала — повертається старий кеш."""
    with _mock_session(_FAKE_EXCHANGE_INFO):
        svc = BinanceSymbolService("https://testnet.binancefuture.com", ttl_sec=1)
        await svc.list_symbols()   # заповнили кеш

    # Симулюємо помилку мережі при наступному fetch-і (після expiry)
    from scalper.common import time as _time
    orig_clock = _time.clock
    import aiohttp
    with patch.object(aiohttp, "ClientSession", side_effect=RuntimeError("boom")):
        # Примусово expire: замість monkeypatch.setattr + restore — force refetch
        svc._fetched_ms = 0
        syms = await svc.list_symbols()
    # Стара-кеш дані залишилися
    assert {s.symbol for s in syms} == {"BTCUSDT", "ETHUSDT"}
