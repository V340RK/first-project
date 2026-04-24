"""_BookState — внутрішній mutable стан однієї книги.

Використовуємо звичайні dict-и (price → size) + sort при віддачі топ-N.
SortedContainers дали б O(log N) на upsert і O(K) на топ-K, але:
  • одне оновлення торкається зазвичай 1-3 рівнів → вартість dict[p]=q мінімальна;
  • top-K читається не на кожен diff, а коли його просить FeatureEngine/дашборд;
  • плюс-один dep на sortedcontainers непропорційно великий для нашої ваги.

Якщо стане вузьким місцем — замінимо прозоро, API не постраждає.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scalper.book.types import OrderBookLevel, OrderBookState
from scalper.gateway.types import DepthSnapshot, RawDepthDiff


class SequenceGapError(RuntimeError):
    """Ключовий сигнал: треба reinit книгу. Engine перехоплює і перезапускає snapshot+replay."""


@dataclass
class _BookState:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)  # price desc → qty
    asks: dict[float, float] = field(default_factory=dict)  # price asc → qty
    last_update_id: int = 0
    initialized: bool = False
    last_tick_ms: int = 0

    def load_snapshot(self, snap: DepthSnapshot) -> None:
        """Повне заміщення стану REST-знімком. Після нього валідний lastUpdateId."""
        self.bids = {p: q for p, q in snap.bids if q > 0}
        self.asks = {p: q for p, q in snap.asks if q > 0}
        self.last_update_id = snap.last_update_id
        self.last_tick_ms = snap.timestamp_ms
        self.initialized = False  # стане True після replay буфера

    def apply_diff(self, diff: RawDepthDiff) -> None:
        """Застосовує live diff. Кидає SequenceGapError якщо gap — викликач має зробити reinit."""
        if self.initialized and diff.first_update_id != self.last_update_id + 1:
            raise SequenceGapError(
                f"{self.symbol}: expected U={self.last_update_id + 1}, "
                f"got U={diff.first_update_id} u={diff.final_update_id}"
            )
        self._apply_unchecked(diff)

    def apply_warmup_diff(self, diff: RawDepthDiff, snap_last_id: int) -> bool:
        """Тих що йдуть ПІД ЧАС ініціалізації (після REST-snapshot-у).

        Правила Binance docs:
          • скіп, якщо `u <= snap_last_id` (вже в snapshot)
          • перший прийнятий: `U <= snap_last_id + 1 <= u`
          • далі строго `U == prev_u + 1` — інакше retry init

        Повертає True, якщо diff застосовано (і мав бути застосовано).
        """
        if diff.final_update_id <= snap_last_id:
            return False

        if self.last_update_id == snap_last_id:
            # Шукаємо перший валідний
            if not (diff.first_update_id <= snap_last_id + 1 <= diff.final_update_id):
                raise SequenceGapError(
                    f"{self.symbol}: snapshot mismatch (U={diff.first_update_id}, "
                    f"u={diff.final_update_id}, snap={snap_last_id})"
                )
        else:
            if diff.first_update_id != self.last_update_id + 1:
                raise SequenceGapError(
                    f"{self.symbol}: warmup gap (U={diff.first_update_id}, "
                    f"prev u={self.last_update_id})"
                )
        self._apply_unchecked(diff)
        return True

    def _apply_unchecked(self, diff: RawDepthDiff) -> None:
        for price, qty in diff.bids:
            if qty == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty
        for price, qty in diff.asks:
            if qty == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty
        self.last_update_id = diff.final_update_id

    def top_snapshot(self, depth: int, timestamp_ms: int) -> OrderBookState:
        """Замороджений топ-N. bids desc, asks asc."""
        bids = sorted(self.bids.items(), key=lambda kv: -kv[0])[:depth]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:depth]
        return OrderBookState(
            symbol=self.symbol,
            timestamp_ms=timestamp_ms or self.last_tick_ms,
            last_update_id=self.last_update_id,
            bids=[OrderBookLevel(price=p, size=q) for p, q in bids],
            asks=[OrderBookLevel(price=p, size=q) for p, q in asks],
            is_synced=self.initialized,
        )


__all__ = ["SequenceGapError", "_BookState"]
