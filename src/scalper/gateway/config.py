"""GatewayConfig — типізована конфігурація 01 Market Data Gateway."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class WebSocketConfig(BaseModel):
    ping_interval: int = 20                          # секунд між WS PING
    reconnect_delay_min: int = 1                     # старт backoff
    reconnect_delay_max: int = 60                    # стеля backoff
    silence_alert_threshold: int = 30                # сек без повідомлень → ERROR


class RestConfig(BaseModel):
    timeout: int = 10                                # сек на 1 запит
    max_retries: int = 3
    retry_delay: int = 1


class RateLimitConfig(BaseModel):
    weight_threshold: int = 1920                     # 80% від 2400 (стандартний Binance Futures cap)
    block_when_above: bool = True


class TimeSyncConfig(BaseModel):
    interval_sec: int = 60
    drift_alert_ms: int = 1000


class UserStreamConfig(BaseModel):
    listen_key_renewal_min: int = 30                 # листен-кей жиє 60 хв, оновлюємо кожні 30


class GatewayConfig(BaseModel):
    """Кореневий конфіг Gateway. Завантажується з settings.yaml.gateway секції."""

    exchange: str = "binance_futures"
    testnet: bool = True
    base_url: str
    ws_url: str
    api_key: SecretStr | None = None                 # None → лише публічні ендпоїнти доступні
    secret_key: SecretStr | None = None
    depth_levels: int = 50

    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    rest: RestConfig = Field(default_factory=RestConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    time_sync: TimeSyncConfig = Field(default_factory=TimeSyncConfig)
    user_stream: UserStreamConfig = Field(default_factory=UserStreamConfig)
