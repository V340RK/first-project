"""BinanceAccountService — REST fetch, cache, time sync, error fallback."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from scalper.dashboard.account import AccountBalance, BinanceAccountService


def _fake_transport(account_data: dict, server_time_ms: int = 1_000_000):   # type: ignore[no-untyped-def]
    """Транспорт-mock з public_get('/fapi/v1/time') + private_get('/fapi/v2/account')."""
    t = MagicMock()
    async def _public_get(endpoint, *, weight=1):
        if endpoint == "/fapi/v1/time":
            return {"serverTime": server_time_ms}
        raise AssertionError(f"unexpected public endpoint: {endpoint}")
    async def _private_get(endpoint, *, weight=1):
        if endpoint == "/fapi/v2/account":
            return account_data
        raise AssertionError(f"unexpected private endpoint: {endpoint}")
    t.public_get = _public_get
    t.private_get = _private_get
    t.set_time_offset_ms = MagicMock()
    return t


@pytest.mark.asyncio
async def test_get_balance_parses_account_response() -> None:
    transport = _fake_transport({
        "totalWalletBalance": "5000.0",
        "totalMarginBalance": "5005.5",
        "availableBalance": "4500.0",
        "totalUnrealizedProfit": "5.5",
    })
    svc = BinanceAccountService(transport)
    bal = await svc.get_balance()
    assert isinstance(bal, AccountBalance)
    assert bal.wallet_balance == 5000.0
    assert bal.available_balance == 4500.0
    assert bal.margin_balance == 5005.5
    assert bal.total_unrealized_pnl == 5.5
    assert bal.quote_asset == "USDT"


@pytest.mark.asyncio
async def test_first_call_syncs_time() -> None:
    transport = _fake_transport({
        "totalWalletBalance": "100", "totalMarginBalance": "100",
        "availableBalance": "100", "totalUnrealizedProfit": "0",
    })
    svc = BinanceAccountService(transport)
    await svc.get_balance()
    transport.set_time_offset_ms.assert_called_once()


@pytest.mark.asyncio
async def test_cache_hits_within_ttl() -> None:
    transport = _fake_transport({
        "totalWalletBalance": "100", "totalMarginBalance": "100",
        "availableBalance": "100", "totalUnrealizedProfit": "0",
    })
    svc = BinanceAccountService(transport, ttl_sec=600)
    a = await svc.get_balance()
    b = await svc.get_balance()
    assert a is b   # тот самий об'єкт, з кешу
    # set_time_offset_ms викликається тільки раз
    assert transport.set_time_offset_ms.call_count == 1


@pytest.mark.asyncio
async def test_fallback_to_stale_cache_on_fetch_error() -> None:
    """Якщо API падає під час refresh — використовується останній відомий баланс."""
    transport = _fake_transport({
        "totalWalletBalance": "100", "totalMarginBalance": "100",
        "availableBalance": "100", "totalUnrealizedProfit": "0",
    })
    svc = BinanceAccountService(transport, ttl_sec=0.001)
    first = await svc.get_balance()

    # Симулюємо помилку при наступному виклику
    async def _broken(*a, **kw):
        raise RuntimeError("network down")
    transport.private_get = _broken
    svc._fetched_ms = 0   # force expiry
    second = await svc.get_balance()
    assert second.available_balance == first.available_balance
    assert svc.last_error is not None and "network down" in svc.last_error


@pytest.mark.asyncio
async def test_first_call_raises_if_no_cache_and_api_broken() -> None:
    transport = MagicMock()
    async def _broken_public(*a, **kw): raise RuntimeError("503")
    transport.public_get = _broken_public
    transport.set_time_offset_ms = MagicMock()
    svc = BinanceAccountService(transport)
    with pytest.raises(RuntimeError):
        await svc.get_balance()
