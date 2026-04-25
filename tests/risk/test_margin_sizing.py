"""RiskEngine — margin-based sizing (% balance як margin)."""

from __future__ import annotations

import pytest

from scalper.book.types import OrderBookLevel, OrderBookState
from scalper.common.enums import Direction, Regime, SetupType
from scalper.common.types import (
    InvalidationCondition,
    InvalidationKind,
    SetupCandidate,
    TradePlan,
)
from scalper.features.types import Features, MarketSnapshot
from scalper.risk import RiskConfig, RiskEngine
from scalper.tape.types import TapeWindow, TapeWindowsState


def _empty_win(d: int) -> TapeWindow:
    return TapeWindow(
        duration_ms=d, trade_count=0, buy_volume_qty=0, sell_volume_qty=0,
        buy_volume_usd=0, sell_volume_usd=0,
        delta_qty=0, delta_usd=0, last_trade_price=0, first_trade_ms=0, last_trade_ms=0,
    )


def _features(price: float = 100.0) -> Features:
    book = OrderBookState(
        symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
        bids=[OrderBookLevel(price, 10.0)], asks=[OrderBookLevel(price + 0.1, 10.0)],
        is_synced=True,
    )
    tape = TapeWindowsState(
        symbol="BTCUSDT", timestamp_ms=0,
        window_500ms=_empty_win(500), window_2s=_empty_win(2000), window_10s=_empty_win(10_000),
        cvd=0, cvd_reliable=True, delta_500ms=0, delta_2s=0, delta_10s=0, price_path=[],
    )
    snap = MarketSnapshot(
        timestamp_ms=1000, symbol="BTCUSDT", book=book, tape=tape,
        last_price=price, spread_ticks=1,
    )
    return Features(
        snapshot=snap,
        bid_ask_imbalance_5=0.0, bid_ask_imbalance_10=0.0,
        weighted_imbalance=0.0, book_pressure_side="NEUTRAL",
        delta_500ms=0, delta_2s=0, delta_10s=0, cvd=0,
        aggressive_buy_burst=False, aggressive_sell_burst=False, burst_size_usd=None,
        absorption_score=0, absorption_side="NONE",   # type: ignore[arg-type]
        spoof_score=0, spoof_side="NONE", micro_pullback=None,
        poc_offset_ticks=0, poc_location="MID",
        stacked_imbalance_long=False, stacked_imbalance_short=False,
        bar_finished=False, bar_delta=0,
        in_htf_poi=False, htf_poi_type=None, htf_poi_side=None, distance_to_poi_ticks=None,
    )


def _plan(*, entry: float = 100.0, stop: float = 99.5) -> TradePlan:
    cand = SetupCandidate(
        setup_type=SetupType.ABSORPTION_REVERSAL, direction=Direction.LONG,
        symbol="BTCUSDT", timestamp_ms=1000,
        entry_price=entry, stop_price=stop,
        tp1_price=entry + 0.5, tp2_price=entry + 1.0, tp3_price=entry + 1.5,
        stop_distance_ticks=int(abs(entry - stop) / 0.1),
        invalidation_conditions=[InvalidationCondition(
            kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={})],
        features_snapshot=_features(entry),
    )
    return TradePlan(
        candidate=cand, setup_type=cand.setup_type, direction=cand.direction,
        symbol=cand.symbol, timestamp_ms=1000,
        entry_price=entry, stop_price=stop,
        tp1_price=entry + 0.5, tp2_price=entry + 1.0, tp3_price=entry + 1.5,
        stop_distance_ticks=cand.stop_distance_ticks,
        score=1.5, score_threshold=1.0, regime=Regime.NORMAL_BALANCED,
        expectancy_multiplier=1.0,
        invalidation_conditions=cand.invalidation_conditions, time_stop_ms=None,
    )


def test_margin_pct_uses_fixed_margin_not_r_formula() -> None:
    """10% від balance=1000 з 5x leverage = $500 notional. На price=100 → qty=5."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5, max_concurrent_positions=99,
        fallback_max_qty=1000.0,
    )
    re = RiskEngine(cfg)
    dec = re.evaluate(_plan(entry=100.0, stop=99.5), equity_usd=1000.0)
    assert dec.plan is not None
    # margin = 1000 * 10% = $100. notional = 100 * 5 = $500. qty = 500/100 = 5
    assert dec.plan.position_size == pytest.approx(5.0, abs=0.01)


def test_margin_pct_ignores_stop_distance() -> None:
    """Margin sizing — qty залежить тільки від balance/leverage/price, НЕ від stop."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5, max_concurrent_positions=99,
        fallback_max_qty=1000.0,
    )
    re = RiskEngine(cfg)
    # Дві позиції з різними стопами — qty має бути ОДНАКОВЕ
    dec_tight = re.evaluate(_plan(entry=100.0, stop=99.9), equity_usd=1000.0)
    re2 = RiskEngine(cfg)   # fresh для незалежного counter
    dec_wide = re2.evaluate(_plan(entry=100.0, stop=95.0), equity_usd=1000.0)
    assert dec_tight.plan.position_size == dec_wide.plan.position_size


def test_risk_usd_mode_unchanged_when_margin_pct_none() -> None:
    """Default behavior (margin_per_trade_pct=None) лишається R-based."""
    cfg = RiskConfig(
        margin_per_trade_pct=None,
        risk_per_trade_usd_abs=10.0, leverage=5,
        max_concurrent_positions=99, fallback_max_qty=1000.0,
    )
    re = RiskEngine(cfg)
    dec = re.evaluate(_plan(entry=100.0, stop=99.5), equity_usd=10_000)
    assert dec.plan is not None
    # R-based: risk=$10, stop_distance=0.5 + buffer 0.1 = 0.6, qty=10/0.6=16.67
    assert 15 < dec.plan.position_size < 18


def test_margin_pct_still_capped_by_notional_usage() -> None:
    """100% margin allocated → cap до equity*leverage*0.9 (default usage)."""
    cfg = RiskConfig(
        margin_per_trade_pct=100.0, leverage=5,
        max_notional_usage=0.9, max_concurrent_positions=99,
        fallback_max_qty=1000.0,
    )
    re = RiskEngine(cfg)
    dec = re.evaluate(_plan(entry=100.0, stop=99.5), equity_usd=1000.0)
    assert dec.plan is not None
    # Без cap було б notional=5000. З cap 0.9: max_notional=4500, max_qty=45
    assert dec.plan.position_size == pytest.approx(45.0, abs=0.01)


# ============================================================
# Real per-symbol filters via resolver
# ============================================================

class _Filt:
    def __init__(self, step: float, min_qty: float, max_qty: float, min_notional: float):
        self.step_size = step
        self.min_qty = min_qty
        self.max_qty = max_qty
        self.min_notional = min_notional


def test_resolver_overrides_fallback_step_for_low_priced_coin() -> None:
    """На монеті з step=1 (цілі одиниці) qty не округлюється до 0."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5,
        fallback_step_size=0.001,    # BTC-style fallback
        fallback_min_notional=5.0,
        max_concurrent_positions=99, fallback_max_qty=1_000_000.0,
    )
    # Pair з price=$0.5 і real step=1 (типова cheap coin)
    filt = _Filt(step=1.0, min_qty=1.0, max_qty=100_000, min_notional=5.0)
    re = RiskEngine(cfg, filters_resolver=lambda s: filt)
    # margin = 1000*10% = $100, notional = $500, qty = 500/0.5 = 1000 (round step 1 → 1000)
    dec = re.evaluate(_plan(entry=0.5, stop=0.49), equity_usd=1000.0)
    assert dec.plan is not None
    assert dec.plan.position_size == 1000


def test_resolver_real_min_notional_used_not_fallback() -> None:
    """Pair з реальним min_notional=20 (а не 5 з fallback) — справжній check."""
    cfg = RiskConfig(
        margin_per_trade_pct=0.1, leverage=5,    # дрібний margin → notional малий
        fallback_min_notional=5.0,
        max_concurrent_positions=99, fallback_max_qty=1000.0,
    )
    filt = _Filt(step=0.001, min_qty=0.001, max_qty=1000, min_notional=20.0)
    re = RiskEngine(cfg, filters_resolver=lambda s: filt)
    # margin = 1000*0.1% = $1, notional bid = $5. Real min_notional=20.
    # Має auto-bump до min_notional/price = 20/100 = 0.2 (round step 0.001 → 0.2)
    dec = re.evaluate(_plan(entry=100.0, stop=99.5), equity_usd=1000.0)
    assert dec.plan is not None
    assert dec.plan.position_size * 100.0 >= 20.0   # дотягнуто до min_notional


def test_notional_below_min_rejects_only_when_bump_exceeds_cap() -> None:
    """Якщо для досягнення min_notional треба qty більший за notional cap →
    reject з зрозумілою причиною."""
    cfg = RiskConfig(
        margin_per_trade_pct=1.0, leverage=1,
        fallback_min_notional=5.0, max_notional_usage=0.5,
        max_concurrent_positions=99, fallback_max_qty=10000.0,
    )
    # equity 1000, margin 1% = 10, leverage 1 → notional=10, qty=0.1 на price=100
    # min_notional=5000 → bump qty до 50, але cap=500 → 50*100=5000 > 500 reject
    filt = _Filt(step=0.001, min_qty=0.001, max_qty=10000, min_notional=5000.0)
    re = RiskEngine(cfg, filters_resolver=lambda s: filt)
    dec = re.evaluate(_plan(entry=100.0, stop=99.5), equity_usd=1000.0)
    assert dec.plan is None
    assert "notional_below_min" in dec.reason
    assert "exceed notional cap" in dec.reason


def test_resolver_failure_falls_back_to_config() -> None:
    """Якщо resolver кидає → use defaults, не падаємо."""
    cfg = RiskConfig(
        margin_per_trade_pct=10.0, leverage=5,
        max_concurrent_positions=99, fallback_max_qty=1000.0,
    )
    def _broken(_): raise RuntimeError("boom")
    re = RiskEngine(cfg, filters_resolver=_broken)
    dec = re.evaluate(_plan(entry=100.0, stop=99.5), equity_usd=1000.0)
    assert dec.plan is not None   # fallback values use, не reject
