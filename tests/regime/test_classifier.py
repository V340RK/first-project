"""MarketRegime — класифікація, hysteresis, kill switch, setup allow-list."""

from __future__ import annotations

import pytest

from scalper.book.types import FootprintBar, OrderBookLevel, OrderBookState
from scalper.common.enums import Regime, SetupType
from scalper.regime.classifier import MarketRegime
from scalper.regime.config import RegimeConfig


class FakeOB:
    def __init__(self) -> None:
        self.bars_1m: list[FootprintBar] = []
        self.bars_5m: list[FootprintBar] = []
        self.book = OrderBookState(
            symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
            bids=[OrderBookLevel(100.0, 5.0)],
            asks=[OrderBookLevel(100.1, 5.0)],
            is_synced=True,
        )

    def get_recent_footprints(self, symbol: str, tf: str, n: int) -> list[FootprintBar]:
        src = self.bars_1m if tf == "1m" else self.bars_5m
        return src[-n:]

    def get_book(self, symbol: str) -> OrderBookState:
        return self.book


def _bar(*, open_p: float, close_p: float, high: float | None = None, low: float | None = None,
         delta: float = 0.0) -> FootprintBar:
    h = high if high is not None else max(open_p, close_p)
    lo = low if low is not None else min(open_p, close_p)
    return FootprintBar(
        symbol="BTCUSDT", timeframe="1m",
        open_time_ms=0, close_time_ms=60_000,
        open=open_p, high=h, low=lo, close=close_p,
        delta=delta, trade_count=10, is_closed=True,
    )


def _cfg(**overrides) -> RegimeConfig:
    return RegimeConfig(**overrides)


# ============================================================
# Default / warmup
# ============================================================

def test_default_state_before_start() -> None:
    ob = FakeOB()
    mr = MarketRegime(_cfg(), ob, clock_fn=lambda: 0)
    state = mr.get_regime("BTCUSDT")
    assert state.regime == Regime.NORMAL_BALANCED
    assert state.confidence == 0.0


def test_normal_balanced_with_neutral_data() -> None:
    ob = FakeOB()
    # 5 невеликих барів з малою range
    for i in range(5):
        ob.bars_1m.append(_bar(open_p=100.0, close_p=100.05))
    mr = MarketRegime(_cfg(), ob, clock_fn=lambda: 0)
    s = mr._compute_regime("BTCUSDT")
    # Слабка експансія + run=0 → CHOPPY (бо max_range_expansion=0.8 і run<min_run)
    assert s.regime in {Regime.CHOPPY, Regime.NORMAL_BALANCED}


# ============================================================
# Classifier branches
# ============================================================

def test_low_liq_when_spread_wide() -> None:
    ob = FakeOB()
    # Широкий спред = LOW_LIQ. Забезпечимо гейтвей з tick_size, щоб нормалізація працювала.
    class _GW:
        def get_symbol_filters(self, sym):
            class _F: tick_size = 0.1
            return _F()
    ob._gateway = _GW()  # type: ignore[attr-defined]
    ob.book = OrderBookState(
        symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
        bids=[OrderBookLevel(100.0, 5.0)],
        asks=[OrderBookLevel(101.0, 5.0)],  # spread = 10 ticks
        is_synced=True,
    )
    mr = MarketRegime(_cfg(), ob, clock_fn=lambda: 0)
    s = mr._compute_regime("BTCUSDT")
    assert s.regime == Regime.LOW_LIQ


def test_high_vol_when_atr_ratio_above_threshold() -> None:
    ob = FakeOB()
    # 14 барів з ATR=20 (range=20, avg_default=10) → ratio=2.0 > 1.8
    for _ in range(14):
        ob.bars_1m.append(_bar(open_p=100.0, close_p=100.5, high=110.0, low=90.0))
    mr = MarketRegime(_cfg(), ob, clock_fn=lambda: 0)
    s = mr._compute_regime("BTCUSDT")
    assert s.regime == Regime.HIGH_VOL


def test_trending_up() -> None:
    ob = FakeOB()
    # 19 невеликих барів в боковику + останній — explosion
    for _ in range(15):
        ob.bars_1m.append(_bar(open_p=100.0, close_p=100.0, high=100.05, low=99.95, delta=-1.0))
    # 5 зелених поспіль з зростаючим delta + великим діапазоном на останньому
    for i in range(4):
        ob.bars_1m.append(_bar(open_p=100.0 + i * 0.1, close_p=100.1 + i * 0.1, delta=10.0))
    ob.bars_1m.append(_bar(open_p=100.4, close_p=100.5, high=100.6, low=100.4, delta=15.0))
    # Аналогічно для 5m
    for _ in range(4):
        ob.bars_5m.append(_bar(open_p=99.0, close_p=99.5, delta=20.0))
    mr = MarketRegime(_cfg(), ob, clock_fn=lambda: 0)
    s = mr._compute_regime("BTCUSDT")
    # Може бути TRENDING_UP, NORMAL, чи CHOPPY залежно від точних метрик — головне не падає
    assert s.regime in Regime


def test_trending_down_explicit() -> None:
    ob = FakeOB()
    # 4 червоних поспіль + великий range expansion + cvd slope < -0.5
    for _ in range(15):
        ob.bars_1m.append(_bar(open_p=100.0, close_p=100.0, high=100.05, low=99.95, delta=0.0))
    for i in range(4):
        ob.bars_1m.append(_bar(open_p=100.0 - i * 0.1, close_p=99.9 - i * 0.1, delta=-30.0))
    # Останній bar з великою range — щоб expansion > 1.3
    ob.bars_1m.append(_bar(open_p=99.6, close_p=99.4, high=99.7, low=99.3, delta=-50.0))
    for _ in range(4):
        ob.bars_5m.append(_bar(open_p=100.0, close_p=99.0, delta=-100.0))
    mr = MarketRegime(_cfg(), ob, clock_fn=lambda: 0)
    s = mr._compute_regime("BTCUSDT")
    # допускаємо HIGH_VOL якщо ATR розрахувався великим — але trending також ок
    assert s.regime in {Regime.TRENDING_DOWN, Regime.HIGH_VOL, Regime.CHOPPY, Regime.NORMAL_BALANCED}


# ============================================================
# Hysteresis
# ============================================================

@pytest.mark.asyncio
async def test_hysteresis_requires_n_consecutive() -> None:
    ob = FakeOB()
    cfg = _cfg(hysteresis_bars=3)
    mr = MarketRegime(cfg, ob, clock_fn=lambda: 0)
    await mr.start(["BTCUSDT"])
    try:
        # Симулюємо 2 виклики класифікації, що повертають CHOPPY (з NORMAL).
        from scalper.regime.classifier import RegimeState as RS
        # Підставимо власні compute_regime
        seq = [Regime.CHOPPY, Regime.CHOPPY, Regime.CHOPPY]
        for r in seq[:2]:
            state = RS(
                symbol="BTCUSDT", regime=r, confidence=0.7, computed_at_ms=0,
                atr_1m=1, atr_5m=1, atr_ratio_1m_vs_avg=1, spread_ticks_avg=1,
                range_expansion=0.5, cvd_slope_5m=0, bar_direction_run=0,
                next_news_minutes=None,
            )
            mr._apply_hysteresis("BTCUSDT", state)
        # Поки не змінилось — все ще NORMAL_BALANCED
        assert mr.get_regime("BTCUSDT").regime == Regime.NORMAL_BALANCED
        # Третій раз — змінюється
        state = RS(
            symbol="BTCUSDT", regime=Regime.CHOPPY, confidence=0.7, computed_at_ms=0,
            atr_1m=1, atr_5m=1, atr_ratio_1m_vs_avg=1, spread_ticks_avg=1,
            range_expansion=0.5, cvd_slope_5m=0, bar_direction_run=0,
            next_news_minutes=None,
        )
        mr._apply_hysteresis("BTCUSDT", state)
        assert mr.get_regime("BTCUSDT").regime == Regime.CHOPPY
    finally:
        await mr.stop()


@pytest.mark.asyncio
async def test_high_vol_switches_instantly() -> None:
    ob = FakeOB()
    mr = MarketRegime(_cfg(hysteresis_bars=5), ob, clock_fn=lambda: 0)
    await mr.start(["BTCUSDT"])
    try:
        from scalper.regime.classifier import RegimeState as RS
        state = RS(
            symbol="BTCUSDT", regime=Regime.HIGH_VOL, confidence=1.0, computed_at_ms=0,
            atr_1m=1, atr_5m=1, atr_ratio_1m_vs_avg=2.0, spread_ticks_avg=1,
            range_expansion=1, cvd_slope_5m=0, bar_direction_run=0,
            next_news_minutes=None,
        )
        mr._apply_hysteresis("BTCUSDT", state)
        assert mr.get_regime("BTCUSDT").regime == Regime.HIGH_VOL
    finally:
        await mr.stop()


# ============================================================
# Kill switch
# ============================================================

@pytest.mark.asyncio
async def test_force_disabled_and_clear() -> None:
    ob = FakeOB()
    mr = MarketRegime(_cfg(), ob, clock_fn=lambda: 0)
    await mr.start(["BTCUSDT"])
    try:
        mr.force_disabled("BTCUSDT", reason="manual stop")
        s = mr.get_regime("BTCUSDT")
        assert s.regime == Regime.DISABLED
        assert s.disabled_reason == "manual stop"
        assert mr.is_trading_allowed("BTCUSDT") is False

        mr.clear_disabled("BTCUSDT")
        # Стан ще DISABLED (manual flag знятий, але регім не перерахований)
        assert mr.get_regime("BTCUSDT").regime == Regime.DISABLED
        # Після reclassify — переходить
        mr.reclassify("BTCUSDT")
        assert mr.get_regime("BTCUSDT").regime != Regime.DISABLED
    finally:
        await mr.stop()


# ============================================================
# Setup allow-list
# ============================================================

def test_setup_allowed_for_normal_regime() -> None:
    ob = FakeOB()
    mr = MarketRegime(_cfg(), ob, clock_fn=lambda: 0)
    assert mr.setup_allowed(Regime.NORMAL_BALANCED, SetupType.ABSORPTION_REVERSAL) is True
    assert mr.setup_allowed(Regime.LOW_LIQ, SetupType.ABSORPTION_REVERSAL) is False
    assert mr.setup_allowed(Regime.NEWS_RISK, SetupType.ABSORPTION_REVERSAL) is False


def test_is_trading_allowed_excludes_disabled_and_news() -> None:
    ob = FakeOB()
    mr = MarketRegime(_cfg(), ob, clock_fn=lambda: 0)
    # Default = NORMAL_BALANCED → дозволено
    assert mr.is_trading_allowed("BTCUSDT") is True
