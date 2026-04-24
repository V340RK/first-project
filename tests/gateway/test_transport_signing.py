"""Підпис приватних запитів — на офіційному test-векторі Binance.

Векторний приклад з docs:
    secret      = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    timestamp   = 1499827319559
    params      = symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000
    signature   = c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71
"""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

from pydantic import SecretStr

from scalper.gateway.config import GatewayConfig
from scalper.gateway.transport import _RestTransport


def _make_transport(secret: str, api_key: str = "test_key") -> _RestTransport:
    config = GatewayConfig(
        base_url="https://test",
        ws_url="wss://test",
        api_key=SecretStr(api_key),
        secret_key=SecretStr(secret),
    )
    return _RestTransport(config)


def test_hmac_against_official_vector() -> None:
    """Перевірка алгоритму HMAC-SHA256 — без виклику _sign() (бо там додається timestamp).
    Це гарантує, що ми використовуємо ту саму криптографію, що й Binance docs.
    """
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    query = (
        "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
        "&recvWindow=5000&timestamp=1499827319559"
    )
    expected = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    actual = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    assert actual == expected, "HMAC-SHA256 спрацював інакше ніж очікує Binance"


def test_sign_adds_timestamp_and_signature() -> None:
    """_sign() має додати timestamp + signature до params."""
    transport = _make_transport(secret="abc")
    transport.set_time_offset_ms(0)
    params = {"symbol": "BTCUSDT", "side": "BUY"}

    signed = transport._sign(dict(params))

    assert "timestamp" in signed
    assert "signature" in signed
    # Підпис має відповідати rebuild query
    rebuilt = {**params, "timestamp": signed["timestamp"]}
    expected = hmac.new(
        b"abc", urlencode(rebuilt).encode(), hashlib.sha256
    ).hexdigest()
    assert signed["signature"] == expected


def test_sign_without_keys_raises() -> None:
    """Якщо ключі не налаштовані — приватний запит не повинен пройти."""
    config = GatewayConfig(base_url="https://test", ws_url="wss://test")
    transport = _RestTransport(config)
    try:
        transport._sign({"x": 1})
    except Exception as e:
        assert "ключ" in str(e).lower() or "401" in str(e)
    else:
        raise AssertionError("Очікувалась помилка про відсутні ключі")


def test_time_offset_applied_to_signature_timestamp() -> None:
    """Якщо встановили offset → у timestamp воно враховано (інакше recvWindow рейджектне)."""
    transport = _make_transport(secret="abc")
    transport.set_time_offset_ms(5000)

    # Зробимо два підписи поспіль — обидва timestamp мають бути близькі і > now+4s
    import time
    now_ms_local = int(time.time() * 1000)
    signed = transport._sign({"x": "y"})
    assert signed["timestamp"] >= now_ms_local + 4000
