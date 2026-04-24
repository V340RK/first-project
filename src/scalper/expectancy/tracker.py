"""ExpectancyTracker — rolling expectancy per (setup_type, symbol), Wilson CI, auto-suspend.

In-memory реалізація. Persistence через SQLite додамо пізніше.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from statistics import mean

from scalper.common import time as _time
from scalper.common.enums import SetupType
from scalper.expectancy.config import ExpectancyConfig
from scalper.risk import TradeOutcome

logger = logging.getLogger(__name__)

ClockFn = Callable[[], int]


@dataclass(frozen=True)
class ExpectancySnapshot:
    setup_type: SetupType
    symbol: str
    samples: int
    wins: int
    losses: int
    breakevens: int
    win_rate: float
    win_rate_ci_low: float
    win_rate_ci_high: float
    avg_win_R: float
    avg_loss_R: float        # абсолют
    rolling_E_R: float
    max_mfe_R: float
    max_mae_R: float
    last_updated_ms: int
    suspended: bool
    suspended_reason: str | None


@dataclass(frozen=True)
class _Sample:
    trade_id: str
    closed_at_ms: int
    realized_r: float
    mfe_r: float
    mae_r: float
    was_stopped: bool


def wilson_ci(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% довірчий інтервал Wilson-а для частки (консервативніший за нормальне)."""
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    n = total
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


class ExpectancyTracker:
    def __init__(
        self,
        config: ExpectancyConfig,
        *,
        clock_fn: ClockFn | None = None,
    ) -> None:
        self._config = config
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())
        self._windows: dict[tuple[SetupType, str], deque[_Sample]] = {}
        self._snapshots: dict[tuple[SetupType, str], ExpectancySnapshot] = {}
        self._suspended: dict[tuple[SetupType, str], str] = {}
        self._seen_trade_ids: set[str] = set()

    # === Public ===

    def on_trade_outcome(self, outcome: TradeOutcome) -> None:
        if outcome.trade_id in self._seen_trade_ids:
            return
        self._seen_trade_ids.add(outcome.trade_id)

        key = (outcome.setup_type, outcome.symbol)
        dq = self._windows.setdefault(key, deque(maxlen=self._config.window_size))
        dq.append(_Sample(
            trade_id=outcome.trade_id,
            closed_at_ms=outcome.closed_at_ms,
            realized_r=outcome.realized_r,
            mfe_r=outcome.max_favorable_r,
            mae_r=outcome.max_adverse_r,
            was_stopped=outcome.was_stopped,
        ))
        self._snapshots[key] = self._compute_snapshot(key, dq)
        self._check_auto_suspend(key)

    def get(self, setup_type: SetupType, symbol: str) -> ExpectancySnapshot | None:
        return self._snapshots.get((setup_type, symbol))

    def is_suspended(self, setup_type: SetupType, symbol: str) -> bool:
        return (setup_type, symbol) in self._suspended

    def all_snapshots(self) -> dict[tuple[SetupType, str], ExpectancySnapshot]:
        return dict(self._snapshots)

    def suspend(self, setup_type: SetupType, symbol: str, reason: str) -> None:
        key = (setup_type, symbol)
        self._suspended[key] = reason
        if key in self._snapshots:
            old = self._snapshots[key]
            self._snapshots[key] = _replace_snapshot(old, suspended=True, suspended_reason=reason)
        logger.warning("SETUP SUSPENDED: %s/%s — %s", setup_type.value, symbol, reason)

    def resume(self, setup_type: SetupType, symbol: str) -> None:
        key = (setup_type, symbol)
        self._suspended.pop(key, None)
        if key in self._snapshots:
            old = self._snapshots[key]
            self._snapshots[key] = _replace_snapshot(old, suspended=False, suspended_reason=None)

    # === Internals ===

    def _compute_snapshot(
        self, key: tuple[SetupType, str], samples: deque[_Sample],
    ) -> ExpectancySnapshot:
        setup, symbol = key
        win_t = self._config.win_threshold_R
        loss_t = self._config.loss_threshold_R

        wins = [s for s in samples if s.realized_r > win_t]
        losses = [s for s in samples if s.realized_r < -loss_t]
        be = len(samples) - len(wins) - len(losses)

        wr_denom = len(wins) + len(losses)
        wr = len(wins) / wr_denom if wr_denom > 0 else 0.0
        avg_win = mean(s.realized_r for s in wins) if wins else 0.0
        avg_loss = abs(mean(s.realized_r for s in losses)) if losses else 0.0
        e = wr * avg_win - (1 - wr) * avg_loss if wr_denom > 0 else 0.0

        lo, hi = wilson_ci(len(wins), wr_denom) if wr_denom > 0 else (0.0, 1.0)

        mfe_max = max((s.mfe_r for s in samples), default=0.0)
        mae_min = min((s.mae_r for s in samples), default=0.0)

        suspended = key in self._suspended

        return ExpectancySnapshot(
            setup_type=setup, symbol=symbol,
            samples=len(samples),
            wins=len(wins), losses=len(losses), breakevens=be,
            win_rate=wr, win_rate_ci_low=lo, win_rate_ci_high=hi,
            avg_win_R=avg_win, avg_loss_R=avg_loss,
            rolling_E_R=e,
            max_mfe_R=mfe_max, max_mae_R=mae_min,
            last_updated_ms=self._clock(),
            suspended=suspended,
            suspended_reason=self._suspended.get(key),
        )

    def _check_auto_suspend(self, key: tuple[SetupType, str]) -> None:
        snap = self._snapshots.get(key)
        if snap is None or snap.suspended:
            return
        cfg = self._config
        if snap.samples < cfg.auto_suspend_min_samples:
            return
        bad_e = snap.rolling_E_R <= cfg.auto_suspend_e_threshold_R
        bad_ci = snap.win_rate_ci_high < cfg.auto_suspend_ci_upper
        if bad_e and bad_ci:
            reason = (
                f"auto: E={snap.rolling_E_R:.2f}R over {snap.samples} samples, "
                f"CI_upper={snap.win_rate_ci_high:.2f}"
            )
            self.suspend(key[0], key[1], reason)


def _replace_snapshot(
    snap: ExpectancySnapshot, *, suspended: bool, suspended_reason: str | None,
) -> ExpectancySnapshot:
    return ExpectancySnapshot(
        setup_type=snap.setup_type, symbol=snap.symbol, samples=snap.samples,
        wins=snap.wins, losses=snap.losses, breakevens=snap.breakevens,
        win_rate=snap.win_rate, win_rate_ci_low=snap.win_rate_ci_low,
        win_rate_ci_high=snap.win_rate_ci_high,
        avg_win_R=snap.avg_win_R, avg_loss_R=snap.avg_loss_R,
        rolling_E_R=snap.rolling_E_R,
        max_mfe_R=snap.max_mfe_R, max_mae_R=snap.max_mae_R,
        last_updated_ms=snap.last_updated_ms,
        suspended=suspended, suspended_reason=suspended_reason,
    )


__all__ = ["ExpectancyConfig", "ExpectancySnapshot", "ExpectancyTracker", "wilson_ci"]
