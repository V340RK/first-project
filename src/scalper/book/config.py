"""OBConfig — параметри Order Book Engine."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OBClusterConfig(BaseModel):
    imbalance_ratio: float = Field(default=2.5, gt=1.0)
    stacked_min_count: int = Field(default=3, ge=2)


class OBReinitConfig(BaseModel):
    snapshot_limit: int = Field(default=1000, ge=5, le=1000)
    max_attempts: int = Field(default=3, ge=1)
    backoff_ms: int = Field(default=500, ge=50)
    warmup_diff_timeout_ms: int = Field(default=5000, ge=500)
    relaxed_sync: bool = Field(
        default=False,
        description=(
            "True → якщо строгий Binance-протокол не сходиться (testnet часто "
            "повертає застарілий snapshot), ініціалізувати book по snapshot і "
            "далі приймати diffs з поточного потоку, пропускаючи ті що <= snap.U. "
            "Допустимо невелика короткочасна розсинхронізація."
        ),
    )


class OBConfig(BaseModel):
    levels_to_keep: int = Field(default=20, ge=1, le=1000)
    timeframes: list[str] = Field(default_factory=lambda: ["1m", "5m", "15m"])
    closed_history_size: int = Field(default=200, ge=1)
    cluster: OBClusterConfig = Field(default_factory=OBClusterConfig)
    reinit: OBReinitConfig = Field(default_factory=OBReinitConfig)
