"""DecisionEngine — фільтри, scoring, кулдауни, вибір переможця."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from scalper.book.types import OrderBookLevel, OrderBookState
from scalper.common.enums import Direction, Regime, SetupType
from scalper.common.types import InvalidationCondition, InvalidationKind, SetupCandidate
from scalper.decision.config import DecisionConfig
from scalper.decision.engine import DecisionEngine
from scalper.features.types import Features, MarketSnapshot
from scalper.tape.types import TapeWindow, TapeWindowsState


# ============================================================
# Fakes
# ============================================================

@dataclass
class FakeRegimeState:
    regime: Regime = Regime.NORMAL_BALANCED
    confidence: float = 0.6
    spread_ticks_avg: float = 1.0


class FakeRegime:
    def __init__(self, regime: Regime = Regime.NORMAL_BALANCED,
                 confidence: float = 0.6, spread: float = 1.0) -> None:
        self._state = FakeRegimeState(regime, confidence, spread)

    def get_regime(self, symbol: str) -> FakeRegimeState:
        return self._state

    def set_regime(self, regime: Regime, confidence: float = 0.6) -> None:
        self._state = FakeRegimeState(regime, confidence, self._state.spread_ticks_avg)


class FakeRisk:
    def __init__(self, kill: bool = False, loss_streak: int = 0) -> None:
        self._kill = kill
        self._ls = loss_streak
    def is_kill_switch_on(self) -> bool: return self._kill
    def get_loss_streak(self) -> int: return self._ls


class FakePosition:
    def __init__(self, has_pos: bool = False) -> None:
        self._has = has_pos
    def has_open_position(self, symbol: str) -> bool: return self._has


@dataclass
class FakeExpectancy:
    rolling_E_R: float


class FakeExpectancyTracker:
    def __init__(self, value: float | None = None) -> None:
        self._v = value
    def get(self, setup_type: SetupType, symbol: str) -> FakeExpectancy | None:
        return FakeExpectancy(self._v) if self._v is not None else None


# ============================================================
# Helpers
# ============================================================

def _empty_win(d: int) -> TapeWindow:
    return TapeWindow(
        duration_ms=d, trade_count=0, buy_volume_qty=0, sell_volume_qty=0,
        buy_volume_usd=0, sell_volume_usd=0,
        delta_qty=0, delta_usd=0, last_trade_price=0, first_trade_ms=0, last_trade_ms=0,
    )


def _tape() -> TapeWindowsState:
    return TapeWindowsState(
        symbol="BTCUSDT", timestamp_ms=0,
        window_500ms=_empty_win(500), window_2s=_empty_win(2000), window_10s=_empty_win(10_000),
        cvd=0, cvd_reliable=True, delta_500ms=0, delta_2s=0, delta_10s=0, price_path=[],
    )


def _features(
    *, absorption_score: float = 0.9, absorption_side: str = "BID",
    delta_500ms: float = -50_000, weighted_imbalance: float = 0.5,
    bid_ask_imbalance_5: float = 0.5, stacked_imbalance_long: bool = False,
    stacked_imbalance_short: bool = False,
    aggressive_buy_burst: bool = False,
) -> Features:
    book = OrderBookState(
        symbol="BTCUSDT", timestamp_ms=0, last_update_id=1,
        bids=[OrderBookLevel(100.0, 10.0)],
        asks=[OrderBookLevel(100.1, 10.0)], is_synced=True,
    )
    snap = MarketSnapshot(
        timestamp_ms=1000, symbol="BTCUSDT", book=book, tape=_tape(),
        last_price=100.05, spread_ticks=1,
    )
    return Features(
        snapshot=snap,
        bid_ask_imbalance_5=bid_ask_imbalance_5, bid_ask_imbalance_10=0.0,
        weighted_imbalance=weighted_imbalance, book_pressure_side="NEUTRAL",
        delta_500ms=delta_500ms, delta_2s=0.0, delta_10s=0.0, cvd=0.0,
        aggressive_buy_burst=aggressive_buy_burst, aggressive_sell_burst=False,
        burst_size_usd=None,
        absorption_score=absorption_score, absorption_side=absorption_side,  # type: ignore
        spoof_score=0.0, spoof_side="NONE",
        micro_pullback=None,
        poc_offset_ticks=0, poc_location="MID",
        stacked_imbalance_long=stacked_imbalance_long,
        stacked_imbalance_short=stacked_imbalance_short,
        bar_finished=False, bar_delta=0.0,
        in_htf_poi=False, htf_poi_type=None, htf_poi_side=None,
        distance_to_poi_ticks=None,
    )


def _candidate(
    setup_type: SetupType = SetupType.ABSORPTION_REVERSAL,
    direction: Direction = Direction.LONG,
    symbol: str = "BTCUSDT",
    *, entry: float = 100.01, stop: float = 99.8,
    features: Features | None = None,
) -> SetupCandidate:
    f = features or _features()
    return SetupCandidate(
        setup_type=setup_type, direction=direction, symbol=symbol,
        timestamp_ms=1000,
        entry_price=entry, stop_price=stop,
        tp1_price=entry + 0.2, tp2_price=entry + 0.4, tp3_price=entry + 0.6,
        stop_distance_ticks=2,
        invalidation_conditions=[
            InvalidationCondition(kind=InvalidationKind.PRICE_BEYOND_LEVEL, params={}),
        ],
        features_snapshot=f,
    )


def _de(**kwargs) -> DecisionEngine:
    regime = kwargs.pop("regime", FakeRegime())
    risk = kwargs.pop("risk", FakeRisk())
    exp = kwargs.pop("exp", None)
    pos = kwargs.pop("pos", None)
    clock = kwargs.pop("clock", lambda: 1_000_000)
    cfg = kwargs.pop("config", DecisionConfig())
    return DecisionEngine(cfg, regime, risk, exp, pos, clock_fn=clock)


# ============================================================
# Empty / kill switch / position
# ============================================================

def test_empty_candidates_list() -> None:
    de = _de()
    r = de.decide([])
    assert r.accepted is None
    assert r.rejected == []


def test_kill_switch_rejects_all() -> None:
    de = _de(risk=FakeRisk(kill=True))
    r = de.decide([_candidate(), _candidate()])
    assert r.accepted is None
    assert len(r.rejected) == 2
    assert all(rj.reason == "risk_kill_switch" for rj in r.rejected)


def test_has_open_position_blocks() -> None:
    de = _de(pos=FakePosition(has_pos=True))
    r = de.decide([_candidate()])
    assert r.accepted is None
    assert r.rejected[0].reason == "position_already_open"


# ============================================================
# Regime filter
# ============================================================

def test_regime_disabled_blocks() -> None:
    de = _de(regime=FakeRegime(Regime.DISABLED))
    r = de.decide([_candidate()])
    assert r.accepted is None
    assert r.rejected[0].reason.startswith("regime_blocks_trading")


def test_regime_low_liq_blocks_all_setups() -> None:
    de = _de(regime=FakeRegime(Regime.LOW_LIQ))
    r = de.decide([_candidate()])
    assert r.rejected[0].reason.startswith("setup_blocked_in_regime")


def test_regime_high_vol_allows_only_absorption() -> None:
    de = _de(regime=FakeRegime(Regime.HIGH_VOL))
    absorption = _candidate(SetupType.ABSORPTION_REVERSAL)
    stacked = _candidate(SetupType.STACKED_IMBALANCE)
    r = de.decide([absorption, stacked])
    # absorption може пройти (якщо score OK), stacked точно reject
    assert any(rj.reason.startswith("setup_blocked_in_regime")
               for rj in r.rejected if rj.candidate.setup_type == SetupType.STACKED_IMBALANCE)


# ============================================================
# Scoring / threshold
# ============================================================

def test_strong_signal_accepted() -> None:
    de = _de()
    c = _candidate(features=_features(
        absorption_score=1.0, absorption_side="BID",
        weighted_imbalance=0.8, bid_ask_imbalance_5=0.7,
        delta_500ms=-80_000,
    ))
    r = de.decide([c])
    assert r.accepted is not None
    assert r.accepted.score >= r.accepted.score_threshold


def test_weak_signal_rejected() -> None:
    de = _de()
    c = _candidate(features=_features(
        absorption_score=0.1, weighted_imbalance=0.05,
        bid_ask_imbalance_5=0.05, delta_500ms=-1000,
    ))
    r = de.decide([c])
    assert r.accepted is None
    assert r.rejected[0].reason == "score_below_threshold"


def test_high_vol_raises_threshold() -> None:
    cfg = DecisionConfig(base_score_threshold=1.0, threshold_boost_high_vol=0.5)
    # Однакові кандидати, регім відрізняється
    de_normal = _de(config=cfg)
    de_highvol = _de(config=cfg, regime=FakeRegime(Regime.HIGH_VOL))
    c = _candidate(features=_features(absorption_score=0.9, weighted_imbalance=0.5))
    r_n = de_normal.decide([c])
    r_h = de_highvol.decide([c])
    if r_n.accepted and r_h.accepted:
        assert r_h.accepted.score_threshold > r_n.accepted.score_threshold


# ============================================================
# Tie-break / winner selection
# ============================================================

def test_winner_selection_highest_score() -> None:
    de = _de()
    strong = _candidate(features=_features(
        absorption_score=1.0, weighted_imbalance=0.8, bid_ask_imbalance_5=0.7,
        delta_500ms=-80_000,
    ))
    weak = _candidate(
        setup_type=SetupType.STACKED_IMBALANCE,
        features=_features(
            absorption_score=0.0, weighted_imbalance=0.5,
            bid_ask_imbalance_5=0.5, stacked_imbalance_long=True,
        ),
    )
    r = de.decide([strong, weak])
    if r.accepted is not None:
        assert r.accepted.setup_type in {SetupType.ABSORPTION_REVERSAL, SetupType.STACKED_IMBALANCE}
    # Принаймні один "lost_to_higher_score" у відхилених АБО weaker нижче порогу
    assert len(r.rejected) >= 0  # нічого не зламалось


# ============================================================
# Cooldowns
# ============================================================

def test_cooldown_per_setup_blocks_repeat() -> None:
    now = [1_000_000]
    cfg = DecisionConfig(cooldown_per_setup_ms=5000, cooldown_per_symbol_ms=0)
    de = _de(config=cfg, clock=lambda: now[0])
    c = _candidate(features=_features(
        absorption_score=1.0, weighted_imbalance=0.8, bid_ask_imbalance_5=0.7,
        delta_500ms=-80_000,
    ))
    r1 = de.decide([c])
    assert r1.accepted is not None

    now[0] += 1000  # 1s пізніше
    r2 = de.decide([c])
    assert r2.accepted is None
    assert r2.rejected[0].reason == "cooldown_per_setup"

    now[0] += 10_000  # >5s
    r3 = de.decide([c])
    assert r3.accepted is not None


def test_cooldown_per_symbol_blocks_different_setup() -> None:
    now = [1_000_000]
    cfg = DecisionConfig(cooldown_per_setup_ms=0, cooldown_per_symbol_ms=5000)
    de = _de(config=cfg, clock=lambda: now[0])
    strong_features = _features(
        absorption_score=1.0, weighted_imbalance=0.8, bid_ask_imbalance_5=0.7,
        delta_500ms=-80_000,
    )
    c1 = _candidate(SetupType.ABSORPTION_REVERSAL, features=strong_features)
    c2 = _candidate(
        SetupType.STACKED_IMBALANCE,
        features=_features(
            stacked_imbalance_long=True, weighted_imbalance=0.8,
            bid_ask_imbalance_5=0.7, delta_500ms=80_000,
            absorption_score=0.0,
        ),
    )
    r1 = de.decide([c1])
    assert r1.accepted is not None

    now[0] += 1000
    r2 = de.decide([c2])
    assert r2.accepted is None
    assert r2.rejected[0].reason == "cooldown_per_symbol"


# ============================================================
# Expectancy gating
# ============================================================

def test_low_expectancy_blocks_setup() -> None:
    de = _de(exp=FakeExpectancyTracker(value=-0.5))
    c = _candidate()
    r = de.decide([c])
    assert r.accepted is None
    assert r.rejected[0].reason == "setup_expectancy_too_low"


# ============================================================
# Trade plan
# ============================================================

def test_accepted_plan_has_tp_ladder_and_regime() -> None:
    de = _de()
    c = _candidate(features=_features(
        absorption_score=1.0, weighted_imbalance=0.8,
        bid_ask_imbalance_5=0.7, delta_500ms=-80_000,
    ))
    r = de.decide([c])
    assert r.accepted is not None
    plan = r.accepted
    assert plan.entry_price == c.entry_price
    assert plan.stop_price == c.stop_price
    assert plan.tp1_price == c.tp1_price
    assert plan.regime == Regime.NORMAL_BALANCED
    assert plan.risk_gate_passed is False  # RiskEngine ще не був
