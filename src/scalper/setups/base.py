"""Контракт SetupRule — кожен сетап реалізує цей інтерфейс."""

from __future__ import annotations

from typing import Protocol

from scalper.common.enums import SetupType
from scalper.common.types import SetupCandidate
from scalper.features.types import Features


class SetupRule(Protocol):
    """Один сетап = одна реалізація. Pure-функція без стану.

    Реалізації живуть у окремих файлах (наприклад rules/absorption_reversal.py).
    """

    setup_type: SetupType

    def check(self, features: Features) -> SetupCandidate | None:
        """None = сетап не зʼявився. SetupCandidate = всі рівні (entry/stop/TP) розраховані."""
        ...
