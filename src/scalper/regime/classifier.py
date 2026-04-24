"""MarketRegime — класифікує стан ринку у 8 регімів.

Slow-loop. Викликається або періодично (compute_interval_sec), або при
закритті 1m-бару. Hysteresis: щоб перейти в інший регім, новий кандидат
має триматись `hysteresis_bars` поспіль (виняток: HIGH_VOL/NEWS_RISK/DISABLED
— миттєво).

Див. DOCS/architecture/05-market-regime.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

from scalper.book.types import FootprintBar
from scalper.common import time as _time
from scalper.common.enums import Regime, SetupType
from scalper.regime.config import RegimeConfig

logger = logging.getLogger(__name__)


ClockFn = Callable[[], int]
RegimeChangeCallback = Callable[["RegimeChange"], Awaitable[None]]

INSTANT_REGIMES = {Regime.NEWS_RISK, Regime.DISABLED, Regime.HIGH_VOL}

# Дефолтна allow-list таблиця сетап×регім (DOCS, секція 'Взаємодія з DecisionEngine').
DEFAULT_SETUP_ALLOW: dict[SetupType, set[Regime]] = {
    SetupType.ABSORPTION_REVERSAL: {
        Regime.NORMAL_BALANCED, Regime.TRENDING_UP, Regime.TRENDING_DOWN,
        Regime.CHOPPY, Regime.HIGH_VOL,
    },
    SetupType.STACKED_IMBALANCE: {
        Regime.NORMAL_BALANCED, Regime.TRENDING_UP, Regime.TRENDING_DOWN,
        Regime.HIGH_VOL,
    },
    SetupType.DELTA_SPIKE_REJECTION: {
        Regime.NORMAL_BALANCED, Regime.TRENDING_UP, Regime.TRENDING_DOWN,
        Regime.CHOPPY, Regime.HIGH_VOL,
    },
    SetupType.MICRO_PULLBACK_CONTINUATION: {
        Regime.NORMAL_BALANCED, Regime.TRENDING_UP, Regime.TRENDING_DOWN,
        Regime.HIGH_VOL,
    },
    SetupType.LIQUIDITY_GRAB: {
        Regime.NORMAL_BALANCED, Regime.TRENDING_UP, Regime.TRENDING_DOWN,
        Regime.CHOPPY,
    },
}


@dataclass(frozen=True)
class RegimeState:
    symbol: str
    regime: Regime
    confidence: float
    computed_at_ms: int

    atr_1m: float
    atr_5m: float
    atr_ratio_1m_vs_avg: float
    spread_ticks_avg: float
    range_expansion: float
    cvd_slope_5m: float
    bar_direction_run: int
    next_news_minutes: int | None
    disabled_reason: str | None = None


@dataclass(frozen=True)
class RegimeChange:
    symbol: str
    from_regime: Regime
    to_regime: Regime
    timestamp_ms: int


@dataclass
class _RegimeCache:
    current: RegimeState
    pending_candidate: Regime | None = None
    pending_count: int = 0
    history: deque[RegimeState] = field(default_factory=lambda: deque(maxlen=100))


class _OBProtocol:
    """Лише два методи нам потрібні; не імпортуємо OrderBookEngine."""


def _atr(highs_lows: list[float], period: int) -> float:
    """Спрощений ATR: середнє true-range (тут — high-low) за останні N барів."""
    if not highs_lows:
        return 0.0
    tail = highs_lows[-period:]
    return sum(tail) / len(tail)


def _direction_run(bars: list[FootprintBar]) -> int:
    """+N зелених поспіль (close > open) або −N червоних з кінця."""
    if not bars:
        return 0
    last = bars[-1]
    sign = 1 if last.close > last.open else (-1 if last.close < last.open else 0)
    if sign == 0:
        return 0
    run = 0
    for b in reversed(bars):
        bsign = 1 if b.close > b.open else (-1 if b.close < b.open else 0)
        if bsign != sign:
            break
        run += 1
    return run * sign


def _cvd_slope(bars: list[FootprintBar]) -> float:
    """Нормалізований slope: (Σdelta_recent − Σdelta_old) / Σ|delta|."""
    if len(bars) < 2:
        return 0.0
    half = len(bars) // 2
    recent = sum(b.delta for b in bars[half:])
    older = sum(b.delta for b in bars[:half])
    denom = sum(abs(b.delta) for b in bars) or 1.0
    return (recent - older) / denom


def _conf_linear(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 1.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


class MarketRegime:
    """Один екземпляр на процес. Per-symbol кеш + slow loop."""

    def __init__(
        self,
        config: RegimeConfig,
        ob_engine: object,
        tape: object | None = None,
        news_calendar: object | None = None,
        *,
        clock_fn: ClockFn | None = None,
        setup_allow: dict[SetupType, set[Regime]] | None = None,
    ) -> None:
        self._config = config
        self._ob = ob_engine
        self._tape = tape
        self._news = news_calendar
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())
        self._setup_allow = setup_allow or DEFAULT_SETUP_ALLOW

        self._caches: dict[str, _RegimeCache] = {}
        self._symbols: list[str] = []
        self._change_cbs: list[RegimeChangeCallback] = []

        self._manual_disabled: dict[str, str | None] = {}
        self._task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()
        self._started = False

    # === Lifecycle ===

    async def start(self, symbols: list[str]) -> None:
        if self._started:
            raise RuntimeError("MarketRegime already started")
        self._symbols = [s.upper() for s in symbols]
        for sym in self._symbols:
            self._caches[sym] = _RegimeCache(current=self._default_state(sym))
        self._started = True
        self._task = asyncio.create_task(self._loop(), name="regime.loop")

    async def stop(self) -> None:
        self._shutdown.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._started = False

    def on_regime_change(self, cb: RegimeChangeCallback) -> None:
        self._change_cbs.append(cb)

    # === Query ===

    def get_regime(self, symbol: str) -> RegimeState:
        sym = symbol.upper()
        cache = self._caches.get(sym)
        if cache is None:
            return self._default_state(sym)
        return cache.current

    def is_trading_allowed(self, symbol: str) -> bool:
        r = self.get_regime(symbol).regime
        return r not in {Regime.DISABLED, Regime.NEWS_RISK}

    def setup_allowed(self, regime: Regime, setup_type: SetupType) -> bool:
        return regime in self._setup_allow.get(setup_type, set())

    # === Kill switch ===

    def force_disabled(self, symbol: str, reason: str) -> None:
        sym = symbol.upper()
        self._manual_disabled[sym] = reason
        cache = self._caches.get(sym)
        if cache is None:
            return
        if cache.current.regime != Regime.DISABLED:
            new_state = replace(
                cache.current, regime=Regime.DISABLED,
                confidence=1.0, disabled_reason=reason,
                computed_at_ms=self._clock(),
            )
            self._commit_change(sym, cache.current.regime, Regime.DISABLED, new_state)

    def clear_disabled(self, symbol: str) -> None:
        self._manual_disabled[symbol.upper()] = None

    # === Public re-classify hook (можна викликати з ob.on_bar_close) ===

    def reclassify(self, symbol: str) -> RegimeState:
        sym = symbol.upper()
        new_state = self._compute_regime(sym)
        self._apply_hysteresis(sym, new_state)
        return self._caches[sym].current

    # === Internals ===

    async def _loop(self) -> None:
        interval = self._config.compute_interval_sec
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._shutdown.is_set():
                return
            for sym in self._symbols:
                try:
                    self.reclassify(sym)
                except Exception:
                    logger.exception("regime compute failed for %s", sym)

    def _compute_regime(self, symbol: str) -> RegimeState:
        bars_1m = self._safe_recent_bars(symbol, "1m", 20)
        bars_5m = self._safe_recent_bars(symbol, "5m", 10)
        cfg = self._config

        atr_1m = _atr([b.high - b.low for b in bars_1m], cfg.atr.period_1m)
        atr_5m = _atr([b.high - b.low for b in bars_5m], cfg.atr.period_5m)
        avg_atr_1m = cfg.atr.avg_atr_1m_default
        atr_ratio = atr_1m / avg_atr_1m if avg_atr_1m > 0 else 1.0

        spread_ticks = self._safe_spread_ticks(symbol)

        if len(bars_1m) >= 2:
            last_range = bars_1m[-1].high - bars_1m[-1].low
            other = bars_1m[:-1]
            avg_range = sum(b.high - b.low for b in other) / max(1, len(other)) or 1e-9
            range_expansion = last_range / avg_range
        else:
            range_expansion = 1.0

        cvd_slope = _cvd_slope(bars_5m if bars_5m else bars_1m)
        direction_run = _direction_run(bars_1m)

        next_news_min = self._safe_next_news(symbol)

        regime, conf, reason = self._classify(
            symbol=symbol,
            atr_ratio=atr_ratio, spread_ticks=spread_ticks,
            range_expansion=range_expansion, cvd_slope=cvd_slope,
            direction_run=direction_run, next_news_min=next_news_min,
        )

        return RegimeState(
            symbol=symbol, regime=regime, confidence=conf,
            computed_at_ms=self._clock(),
            atr_1m=atr_1m, atr_5m=atr_5m, atr_ratio_1m_vs_avg=atr_ratio,
            spread_ticks_avg=spread_ticks, range_expansion=range_expansion,
            cvd_slope_5m=cvd_slope, bar_direction_run=direction_run,
            next_news_minutes=next_news_min,
            disabled_reason=reason if regime == Regime.DISABLED else None,
        )

    def _classify(
        self, *, symbol: str, atr_ratio: float, spread_ticks: float,
        range_expansion: float, cvd_slope: float, direction_run: int,
        next_news_min: int | None,
    ) -> tuple[Regime, float, str | None]:
        cfg = self._config
        manual = self._manual_disabled.get(symbol)
        if manual:
            return Regime.DISABLED, 1.0, manual

        if (
            cfg.news.enabled and next_news_min is not None
            and -cfg.news.after_minutes <= next_news_min <= cfg.news.before_minutes
        ):
            return Regime.NEWS_RISK, 1.0, None

        if spread_ticks > cfg.low_liq.spread_ticks:
            conf = _conf_linear(spread_ticks, cfg.low_liq.spread_ticks, cfg.low_liq.spread_ticks * 2)
            return Regime.LOW_LIQ, conf, None

        if atr_ratio > cfg.high_vol.atr_ratio_threshold:
            conf = _conf_linear(atr_ratio, cfg.high_vol.atr_ratio_threshold, cfg.high_vol.atr_ratio_threshold * 1.5)
            return Regime.HIGH_VOL, conf, None

        trending_up = (
            direction_run >= cfg.trending.min_run
            and cvd_slope > cfg.trending.cvd_slope
            and range_expansion > cfg.trending.range_expansion
        )
        trending_down = (
            direction_run <= -cfg.trending.min_run
            and cvd_slope < -cfg.trending.cvd_slope
            and range_expansion > cfg.trending.range_expansion
        )
        if trending_up:
            conf = min(1.0, (abs(direction_run) - cfg.trending.min_run + 1) / 3)
            return Regime.TRENDING_UP, conf, None
        if trending_down:
            conf = min(1.0, (abs(direction_run) - cfg.trending.min_run + 1) / 3)
            return Regime.TRENDING_DOWN, conf, None

        # CHOPPY: невелика експансія + слабкий тренд
        if range_expansion < cfg.choppy.max_range_expansion and abs(direction_run) < cfg.trending.min_run:
            return Regime.CHOPPY, 0.7, None

        return Regime.NORMAL_BALANCED, 0.6, None

    def _apply_hysteresis(self, symbol: str, new_state: RegimeState) -> None:
        cache = self._caches[symbol]
        current = cache.current.regime
        new = new_state.regime

        if new == current:
            cache.pending_candidate = None
            cache.pending_count = 0
            cache.current = new_state
            return

        if new in INSTANT_REGIMES or current in INSTANT_REGIMES:
            self._commit_change(symbol, current, new, new_state)
            return

        if cache.pending_candidate == new:
            cache.pending_count += 1
        else:
            cache.pending_candidate = new
            cache.pending_count = 1

        if cache.pending_count >= self._config.hysteresis_bars:
            self._commit_change(symbol, current, new, new_state)

    def _commit_change(
        self, symbol: str, from_regime: Regime, to_regime: Regime, state: RegimeState,
    ) -> None:
        cache = self._caches[symbol]
        cache.current = state
        cache.history.append(state)
        cache.pending_candidate = None
        cache.pending_count = 0
        change = RegimeChange(symbol, from_regime, to_regime, state.computed_at_ms)
        for cb in self._change_cbs:
            try:
                asyncio.get_event_loop().create_task(cb(change))
            except RuntimeError:
                # Не у event loop'і (тести синхронні) — пропускаємо
                pass
        logger.info(
            "regime_change %s: %s -> %s (conf=%.2f)",
            symbol, from_regime.value, to_regime.value, state.confidence,
        )

    # === Helpers (захищене читання залежностей, які можуть бути None або кинути) ===

    def _safe_recent_bars(self, symbol: str, tf: str, n: int) -> list[FootprintBar]:
        try:
            return list(self._ob.get_recent_footprints(symbol, tf, n))  # type: ignore[attr-defined]
        except Exception:
            return []

    def _safe_spread_ticks(self, symbol: str) -> float:
        try:
            book = self._ob.get_book(symbol)  # type: ignore[attr-defined]
            if not book.bids or not book.asks:
                return 999.0
            spread = book.asks[0].price - book.bids[0].price
            try:
                tick = float(self._ob._gateway.get_symbol_filters(symbol).tick_size)  # type: ignore[attr-defined]
            except Exception:
                tick = 0.0
            if tick > 0:
                return spread / tick
            return 0.0
        except Exception:
            return 999.0

    def _safe_next_news(self, symbol: str) -> int | None:
        if self._news is None:
            return None
        try:
            return self._news.next_high_impact(symbol)  # type: ignore[attr-defined]
        except Exception:
            return None

    def _default_state(self, symbol: str) -> RegimeState:
        return RegimeState(
            symbol=symbol, regime=Regime.NORMAL_BALANCED, confidence=0.0,
            computed_at_ms=self._clock(),
            atr_1m=0.0, atr_5m=0.0, atr_ratio_1m_vs_avg=1.0,
            spread_ticks_avg=0.0, range_expansion=1.0,
            cvd_slope_5m=0.0, bar_direction_run=0,
            next_news_minutes=None,
        )


__all__ = [
    "DEFAULT_SETUP_ALLOW",
    "MarketRegime",
    "RegimeChange",
    "RegimeState",
]
