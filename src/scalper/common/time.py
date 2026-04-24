"""Time injection. У коді ніколи не викликаємо `time.time()` напряму —
завжди через `clock()`, щоб у тестах і Replay можна було підмінити годинник.
"""

from __future__ import annotations

import time
from typing import Callable

ClockFn = Callable[[], int]


def now_ms() -> int:
    """Реальний годинник, UNIX ms."""
    return int(time.time() * 1000)


# За замовчуванням — реальний час.
# Orchestrator / Replay перевизначать через DI.
clock: ClockFn = now_ms
