"""RiskEngine — sizing, ліміти, loss streak, kill switch, ідемпотентність."""

from __future__ import annotations

from scalper.book.types import OrderBookLevel, OrderBookState
from scalper.common.enums import Direction, Regime, SetupType
from scalper.common.types import (
    InvalidationCondition,
    InvalidationKind,
    SetupCandidate,
    TradePlan,
)
from scalper.features.types import Features, MarketSnapshot
from scalper.risk import RiskConfig, RiskEngine, TradeOutcome
from scalper.tape.types import TapeWindow, TapeWindowsState


# ============================================================
# Helpers
# ============================================================

def _empty_win(d: int) -> TapeWindow:
    return TapeWindow(
        duration_ms=d, trade_count=0, buy_volume_qty=0, sell_volume_qty=0,
        buy_volume_usd=0, sell_volume_usd=0,
        delta_qty=0, delta_usd=0, last_trade_price=0, first_trade_ms=0, last_trade_ms=0,
    )


def _features() -> Features:
    book = OrderBookState(
        symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
        bids=[OrderBookLevel(100.0, 10.0)], asks=[OrderBookLevel(100.1, 10.0)],
        is_synced=True,
    )
    tape = TapeWindowsState(
        symbol="BTCUSDT", timestamp_ms=0,
        window_500ms=_empty_win(500), window_2s=_empty_win(2000), window_10s=_empty_win(10_000),
        cvd=0, cvd_reliable=True, delta_500ms=0, delta_2s=0, delta_10s=0, price_path=[],
    )
    snap = MarketSnapshot(
        timestamp_ms=1000, symbol="BTCUSDT", book=book, tape=tape,
        last_price=100.05, spread_ticks=1,
    )
    return Features(
        snapshot=snap,
        bid_ask_imbalance_5=0.0, bid_ask_imbalance_10=0.0,
        weighted_imbalance=0.0, book_pressure_side="NEUTRAL",
        delta_500ms=0.0, delta_2s=0.0, delta_10s=0.0, cvd=0.0,
        aggressive_buy_burst=False, aggressive_sell_burst=False, burst_size_usd=None,
        absorption_score=0.0, absorption_side="NONE",  # type: ignore[arg-type]
        spoof_score=0.0, spoof_side="NONE",
        micro_pullback=None, poc_offset_ticks=0, poc_location="MID",
        stacked_imbalance_long=False, stacked_imbalance_short=False,
        bar_finished=False, bar_delta=0.0,
        in_htf_poi=False, htf_poi_type=None, htf_poi_side=None, distance_to_poi_ticks=None,
    )


def _plan(
    *, direction: Direction = Direction.LONG,
    entry: float = 100.0, stop: float = 99.5,
    regime: Regime = Regime.NORMAL_BALANCED,
    setup_type: SetupType = SetupType.ABSORPTION_REVERSAL,
) -> TradePlan:
    cand = SetupCandidate(
        setup_type=setup_type, direction=direction, symbol="BTCUSDT",
        timestamp_ms=1000,
        entry_price=entry, stop_price=stop,
        tp1_price=entry + (entry - stop), tp2_price=entry + 2 * (entry - stop),
        tp3_price=entry + 3 * (entry - stop),
        stop_distance_ticks=5,
        invalidation_conditions=[
            InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={}),
        ],
        features_snapshot=_features(),
    )
    return TradePlan(
        candidate=cand,
        setup_type=setup_type, direction=direction, symbol="BTCUSDT",
        timestamp_ms=1000,
        entry_price=entry, stop_price=stop,
        tp1_price=cand.tp1_price, tp2_price=cand.tp2_price, tp3_price=cand.tp3_price,
        stop_distance_ticks=5,
        score=1.5, score_threshold=1.0, regime=regime,
        expectancy_multiplier=1.0,
        invalidation_conditions=cand.invalidation_conditions,
        time_stop_ms=None,
    )


def _re(**kwargs) -> RiskEngine:
    cfg = kwargs.pop("config", RiskConfig())
    clock = kwargs.pop("clock", lambda: 1_700_000_000_000)
    return RiskEngine(cfg, clock_fn=clock)


def _outcome(
    plan: TradePlan, *, realized_r: float, trade_id: str = "t1",
    closed_at_ms: int = 1_700_000_000_000,
) -> TradeOutcome:
    return TradeOutcome(
        plan=plan, trade_id=trade_id, symbol=plan.symbol,
        setup_type=plan.setup_type, direction=plan.direction,
        closed_at_ms=closed_at_ms, realized_r=realized_r, realized_usd=realized_r * 10.0,
        max_favorable_r=max(realized_r, 0.0), max_adverse_r=min(realized_r, 0.0),
        was_stopped=realized_r < 0, fees_usd=0.0,
    )


# ============================================================
# Sizing
# ============================================================

def test_compute_size_basic() -> None:
    # entry=100, stop=99.5, R=10$, buffer=1 тік (0.1) → qty = 10/(0.5+0.1) ≈ 16.666
    re = _re(config=RiskConfig(risk_per_trade_usd_abs=10.0, slippage_buffer_ticks=1,
                               fallback_tick_size=0.1, fallback_step_size=0.001,
                               fallback_max_qty=100.0))
    dec = re.evaluate(_plan(entry=100.0, stop=99.5), equity_usd=10_000)
    assert dec.plan is not None
    assert dec.reason is None
    assert 16.6 < dec.plan.position_size < 16.7
    assert dec.plan.risk_gate_passed is True
    assert dec.plan.risk_usd is not None and dec.plan.risk_usd <= 10.0 * 1.1


def test_rejects_invalid_stop_distance() -> None:
    re = _re()
    bad = _plan(entry=100.0, stop=100.0)
    dec = re.evaluate(bad, equity_usd=10_000)
    assert dec.plan is None
    assert dec.reason == "invalid_stop_distance"


def test_rejects_equity_unknown() -> None:
    re = _re()
    dec = re.evaluate(_plan(), equity_usd=0.0)
    assert dec.plan is None
    assert dec.reason == "equity_unknown"


def test_qty_below_min_rejected() -> None:
    # Ставимо дуже широкий стоп → qty мізерний
    re = _re(config=RiskConfig(risk_per_trade_usd_abs=10.0, fallback_min_qty=1.0,
                               fallback_step_size=0.001, fallback_max_qty=100.0,
                               fallback_min_notional=0.0))
    dec = re.evaluate(_plan(entry=100.0, stop=0.01), equity_usd=10_000)
    assert dec.plan is None
    assert dec.reason is not None and "qty_below_min" in dec.reason


def test_qty_clamped_to_max() -> None:
    # Дуже вузький стоп → qty великий, має кліпнутись до max_qty
    re = _re(config=RiskConfig(risk_per_trade_usd_abs=10.0, slippage_buffer_ticks=0,
                               fallback_tick_size=0.1, fallback_step_size=0.001,
                               fallback_max_qty=50.0, fallback_min_notional=0.0))
    dec = re.evaluate(_plan(entry=100.0, stop=99.999), equity_usd=10_000)
    assert dec.plan is not None
    assert dec.plan.position_size == 50.0


# ============================================================
# Daily/monthly/kill blockers
# ============================================================

def test_kill_switch_blocks() -> None:
    re = _re()
    re.engage_kill_switch("manual_test")
    dec = re.evaluate(_plan(), equity_usd=10_000)
    assert dec.plan is None
    assert dec.reason is not None and "kill_switch" in dec.reason


def test_daily_limit_blocks_when_reached() -> None:
    cfg = RiskConfig(daily_loss_limit_R=3.0, loss_streak_hard_limit=99,
                     loss_streak_cooldown_trigger=99)
    re = _re(config=cfg)
    # Штучно наповнюємо daily.r_accumulated = -3R
    plan = _plan()
    dec0 = re.evaluate(plan, equity_usd=10_000)
    assert dec0.plan is not None
    re.on_position_closed(_outcome(dec0.plan, realized_r=-3.0))
    # Наступний evaluate має бути заблокований daily_limit
    dec = re.evaluate(_plan(), equity_usd=10_000)
    assert dec.plan is None
    # Після -3R engage kill теж спрацював
    assert re.is_kill_switch_on() is True


def test_would_breach_daily_blocks_before_entry() -> None:
    # daily.r = -2.5R, ліміт 3R → -2.5 - 1 = -3.5 → блок
    cfg = RiskConfig(daily_loss_limit_R=3.0, loss_streak_hard_limit=99,
                     loss_streak_cooldown_trigger=99, max_trades_per_day=99)
    re = _re(config=cfg)
    p1 = re.evaluate(_plan(), equity_usd=10_000).plan
    assert p1 is not None
    re.on_position_closed(_outcome(p1, realized_r=-2.5, trade_id="t0"))
    assert re.is_kill_switch_on() is False
    dec = re.evaluate(_plan(), equity_usd=10_000)
    assert dec.plan is None
    assert dec.reason is not None and "would_breach_daily_limit" in dec.reason


def test_trade_count_cap() -> None:
    cfg = RiskConfig(max_trades_per_day=2, loss_streak_hard_limit=99,
                     loss_streak_cooldown_trigger=99)
    re = _re(config=cfg)
    for i in range(2):
        p = re.evaluate(_plan(), equity_usd=10_000).plan
        assert p is not None
        re.on_position_opened(p)   # Orchestrator робить це після успішного open()
        re.on_position_closed(_outcome(p, realized_r=0.5, trade_id=f"t{i}"))
    dec = re.evaluate(_plan(), equity_usd=10_000)
    assert dec.plan is None
    assert dec.reason == "trade_count_cap"


def test_max_concurrent_positions() -> None:
    cfg = RiskConfig(max_concurrent_positions=1)
    re = _re(config=cfg)
    d1 = re.evaluate(_plan(), equity_usd=10_000)
    assert d1.plan is not None
    re.on_position_opened(d1.plan)   # позиція реально відкрилась
    d2 = re.evaluate(_plan(), equity_usd=10_000)
    assert d2.plan is None
    assert d2.reason == "max_concurrent_positions"


def test_evaluate_does_not_increment_until_position_opened() -> None:
    """Регресія для бага "5 годин без трейдів": evaluate() не має робити
    side effects, бо position.open() може потім впасти, а counter залишиться."""
    cfg = RiskConfig(max_concurrent_positions=1)
    re = _re(config=cfg)
    re.evaluate(_plan(), equity_usd=10_000)   # accepted, але on_position_opened НЕ викликаний
    # Лічильник має лишитись 0, наступний evaluate має пройти
    d2 = re.evaluate(_plan(), equity_usd=10_000)
    assert d2.plan is not None, "evaluate() не має блокувати наступні setups до position_opened"


def test_initiative_quota() -> None:
    cfg = RiskConfig(max_initiative_trades_per_day=1, max_concurrent_positions=99,
                     loss_streak_hard_limit=99, loss_streak_cooldown_trigger=99)
    re = _re(config=cfg)
    # initiative: SHORT у TRENDING_UP
    p1 = re.evaluate(_plan(direction=Direction.SHORT, entry=100.0, stop=100.5,
                           regime=Regime.TRENDING_UP), equity_usd=10_000).plan
    assert p1 is not None
    re.on_position_opened(p1)
    re.on_position_closed(_outcome(p1, realized_r=0.5, trade_id="t1"))
    d2 = re.evaluate(_plan(direction=Direction.SHORT, entry=100.0, stop=100.5,
                           regime=Regime.TRENDING_UP), equity_usd=10_000)
    assert d2.plan is None
    assert d2.reason == "initiative_quota_exhausted"


# ============================================================
# Loss streak
# ============================================================

def test_loss_streak_cooldown_after_n_losses() -> None:
    cfg = RiskConfig(loss_streak_cooldown_trigger=3, loss_streak_cooldown_ms=1_800_000,
                     loss_streak_hard_limit=5, max_concurrent_positions=99,
                     daily_loss_limit_R=99, max_trades_per_day=99)
    re = _re(config=cfg)
    for i in range(3):
        p = re.evaluate(_plan(), equity_usd=10_000).plan
        assert p is not None
        re.on_position_closed(_outcome(p, realized_r=-0.5, trade_id=f"t{i}"))
    assert re.is_kill_switch_on() is True
    assert re.get_loss_streak() == 3


def test_loss_streak_resets_on_win() -> None:
    cfg = RiskConfig(loss_streak_cooldown_trigger=99, loss_streak_hard_limit=99,
                     max_concurrent_positions=99, daily_loss_limit_R=99)
    re = _re(config=cfg)
    p1 = re.evaluate(_plan(), equity_usd=10_000).plan
    assert p1 is not None
    re.on_position_closed(_outcome(p1, realized_r=-0.5, trade_id="t0"))
    p2 = re.evaluate(_plan(), equity_usd=10_000).plan
    assert p2 is not None
    re.on_position_closed(_outcome(p2, realized_r=+0.8, trade_id="t1"))
    assert re.get_loss_streak() == 0


def test_loss_streak_cooldown_expires_automatically() -> None:
    cfg = RiskConfig(loss_streak_cooldown_trigger=2, loss_streak_cooldown_ms=1000,
                     loss_streak_hard_limit=10, max_concurrent_positions=99,
                     daily_loss_limit_R=99)
    now = [1_700_000_000_000]
    re = _re(config=cfg, clock=lambda: now[0])
    for i in range(2):
        p = re.evaluate(_plan(), equity_usd=10_000).plan
        assert p is not None
        re.on_position_closed(_outcome(p, realized_r=-0.5, trade_id=f"t{i}"))
    assert re.is_kill_switch_on() is True
    # Перескочити expiry
    now[0] += 2000
    dec = re.evaluate(_plan(), equity_usd=10_000)
    assert dec.plan is not None, f"cooldown не знявся: {dec.reason}"
    assert re.is_kill_switch_on() is False


def test_scratch_trade_does_not_touch_loss_streak() -> None:
    re = _re()
    p = re.evaluate(_plan(), equity_usd=10_000).plan
    assert p is not None
    re.on_position_closed(_outcome(p, realized_r=0.0, trade_id="t0"))
    assert re.get_loss_streak() == 0


# ============================================================
# Idempotency / admin
# ============================================================

def test_on_position_closed_is_idempotent() -> None:
    re = _re()
    p = re.evaluate(_plan(), equity_usd=10_000).plan
    assert p is not None
    re.on_position_closed(_outcome(p, realized_r=-1.0, trade_id="dup"))
    re.on_position_closed(_outcome(p, realized_r=-1.0, trade_id="dup"))
    assert re.get_daily_r() == -1.0
    assert re.get_loss_streak() == 1


def test_engage_kill_idempotent() -> None:
    re = _re()
    re.engage_kill_switch("reason_a")
    re.engage_kill_switch("reason_b")   # не повинно перезаписати
    assert re._state.kill_reason == "reason_a"   # type: ignore[attr-defined]


def test_clear_kill_switch() -> None:
    re = _re()
    re.engage_kill_switch("x")
    assert re.is_kill_switch_on() is True
    re.clear_kill_switch()
    assert re.is_kill_switch_on() is False


def test_snapshot_has_open_positions_count() -> None:
    re = _re()
    snap0 = re.snapshot(10_000)
    assert snap0.open_positions == 0
    p = re.evaluate(_plan(), equity_usd=10_000).plan
    assert p is not None
    re.on_position_opened(p)   # Orchestrator fires this after successful open
    snap1 = re.snapshot(10_000)
    assert snap1.open_positions == 1
    re.on_position_closed(_outcome(p, realized_r=0.5, trade_id="t0"))
    snap2 = re.snapshot(10_000)
    assert snap2.open_positions == 0
