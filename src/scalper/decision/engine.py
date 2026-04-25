"""DecisionEngine — оцінює SetupCandidate-ів, повертає один `TradePlan` або жодного.

Stateful (тримає per-(symbol, setup_type) і per-symbol кулдауни), але pure у сенсі
fun-in-fun-out: жодних ордерів/Journal не чіпає.

Див. DOCS/architecture/07-decision-engine.md.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from scalper.common import time as _time
from scalper.common.enums import Direction, Regime, SetupType
from scalper.common.types import (
    DecisionResult,
    RejectedCandidate,
    SetupCandidate,
    TradePlan,
)
from scalper.decision.config import DecisionConfig

logger = logging.getLogger(__name__)


ClockFn = Callable[[], int]


@dataclass
class _DecisionState:
    last_entry_ms: dict[tuple[str, SetupType], int] = field(default_factory=dict)
    last_symbol_entry_ms: dict[str, int] = field(default_factory=dict)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


class DecisionEngine:
    def __init__(
        self,
        config: DecisionConfig,
        regime: object,        # MarketRegime-like: get_regime(sym), setup_allowed(r, s) optional
        risk: object | None = None,           # RiskEngine — is_kill_switch_on(), get_loss_streak()
        expectancy: object | None = None,     # ExpectancyTracker — get(setup_type, symbol)
        position: object | None = None,       # PositionManager — has_open_position(symbol)
        *,
        clock_fn: ClockFn | None = None,
    ) -> None:
        self._config = config
        self._regime = regime
        self._risk = risk
        self._exp = expectancy
        self._pos = position
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())
        self._state = _DecisionState()

    # === Public ===

    def decide(self, candidates: list[SetupCandidate]) -> DecisionResult:
        now = self._clock()
        rejected: list[RejectedCandidate] = []

        if self._kill_switch_on():
            rejected = [
                RejectedCandidate(candidate=c, reason="risk_kill_switch",
                                  score=None, score_threshold=None)
                for c in candidates
            ]
            return DecisionResult(accepted=None, rejected=rejected)

        scored: list[tuple[float, float, float, SetupCandidate]] = []
        for cand in candidates:
            reason = self._pre_score_filter(cand, now)
            if reason:
                rejected.append(RejectedCandidate(
                    candidate=cand, reason=reason,
                    score=None, score_threshold=None,
                ))
                continue

            score, threshold, exp_mult = self._score(cand)
            if score < threshold:
                rejected.append(RejectedCandidate(
                    candidate=cand, reason="score_below_threshold",
                    score=score, score_threshold=threshold,
                ))
                continue

            scored.append((score, threshold, exp_mult, cand))

        if not scored:
            return DecisionResult(accepted=None, rejected=rejected)

        # Detereministic tie-break — беремо першого з найвищим score.
        scored.sort(key=lambda x: x[0], reverse=True)
        top_score, top_thresh, top_exp, top = scored[0]
        for s, t, _em, c in scored[1:]:
            rejected.append(RejectedCandidate(
                candidate=c, reason="lost_to_higher_score",
                score=s, score_threshold=t,
            ))

        plan = self._build_trade_plan(top, top_score, top_thresh, top_exp)
        self._state.last_entry_ms[(top.symbol, top.setup_type)] = now
        self._state.last_symbol_entry_ms[top.symbol] = now
        return DecisionResult(accepted=plan, rejected=rejected)

    # === Filters ===

    def _pre_score_filter(self, cand: SetupCandidate, now: int) -> str | None:
        rs = self._regime.get_regime(cand.symbol)  # type: ignore[attr-defined]
        if rs.regime in (Regime.DISABLED, Regime.NEWS_RISK):
            return f"regime_blocks_trading:{rs.regime.value}"
        if not self._config.relaxed_regime:
            allowed = self._config.regime_allow_map.get(rs.regime, set())
            if cand.setup_type not in allowed:
                return f"setup_blocked_in_regime:{rs.regime.value}"

        if self._pos is not None:
            try:
                if self._pos.has_open_position(cand.symbol):  # type: ignore[attr-defined]
                    return "position_already_open"
            except Exception:
                pass

        last = self._state.last_entry_ms.get((cand.symbol, cand.setup_type), 0)
        if now - last < self._config.cooldown_per_setup_ms:
            return f"cooldown_per_setup"

        last_sym = self._state.last_symbol_entry_ms.get(cand.symbol, 0)
        if now - last_sym < self._config.cooldown_per_symbol_ms:
            return "cooldown_per_symbol"

        if self._exp is not None:
            try:
                exp = self._exp.get(cand.setup_type, cand.symbol)  # type: ignore[attr-defined]
                if exp is not None and getattr(exp, "rolling_E_R", 0.0) < self._config.min_expectancy_R:
                    return "setup_expectancy_too_low"
            except Exception:
                pass

        return None

    # === Scoring ===

    def _score(self, cand: SetupCandidate) -> tuple[float, float, float]:
        cfg = self._config
        w = cfg.weights
        f = cand.features_snapshot
        rs = self._regime.get_regime(cand.symbol)  # type: ignore[attr-defined]

        score = 0.0

        # Позитивні — з Features
        if f.absorption_score > 0 and (
            (cand.direction == Direction.LONG and f.absorption_side == "BID")
            or (cand.direction == Direction.SHORT and f.absorption_side == "ASK")
        ):
            score += w.absorption_score * _clamp01(f.absorption_score)

        if cand.direction == Direction.LONG and f.stacked_imbalance_long:
            score += w.stacked_imbalance
        if cand.direction == Direction.SHORT and f.stacked_imbalance_short:
            score += w.stacked_imbalance

        # Book imbalance на правильному боці
        imb = f.bid_ask_imbalance_5
        if cand.direction == Direction.LONG and imb > 0:
            score += w.book_imbalance_5 * _clamp01(imb)
        if cand.direction == Direction.SHORT and imb < 0:
            score += w.book_imbalance_5 * _clamp01(-imb)

        wimb = f.weighted_imbalance
        if cand.direction == Direction.LONG and wimb > 0:
            score += w.weighted_imbalance * _clamp01(wimb)
        if cand.direction == Direction.SHORT and wimb < 0:
            score += w.weighted_imbalance * _clamp01(-wimb)

        # Delta magnitude на правильному боці
        delta_norm = _clamp01(abs(f.delta_500ms) / cfg.delta_magnitude_full_score_usd)
        if (cand.direction == Direction.LONG and f.delta_500ms > 0) or \
           (cand.direction == Direction.SHORT and f.delta_500ms < 0):
            score += w.delta_magnitude * delta_norm

        if f.micro_pullback is not None:
            score += w.micro_pullback_present

        if (cand.direction == Direction.LONG and f.aggressive_buy_burst) or \
           (cand.direction == Direction.SHORT and f.aggressive_sell_burst):
            score += w.aggressive_burst

        if f.spoof_score > 0:
            score += w.spoof_score * _clamp01(f.spoof_score)

        # Контекст
        if f.in_htf_poi:
            proper_side = (
                (cand.direction == Direction.LONG and f.htf_poi_side == "SUPPORT")
                or (cand.direction == Direction.SHORT and f.htf_poi_side == "RESISTANCE")
            )
            if proper_side:
                score += w.htf_poi_bonus
                if f.htf_poi_type in ("FVG", "OB"):
                    score += w.htf_poi_fvg_ob_extra

        # Регім-бонус/штраф
        if (rs.regime == Regime.TRENDING_UP and cand.direction == Direction.LONG) or \
           (rs.regime == Regime.TRENDING_DOWN and cand.direction == Direction.SHORT):
            score += w.regime_tailwind * rs.confidence
        if (rs.regime == Regime.TRENDING_UP and cand.direction == Direction.SHORT) or \
           (rs.regime == Regime.TRENDING_DOWN and cand.direction == Direction.LONG):
            score -= w.regime_headwind * rs.confidence

        if rs.spread_ticks_avg > w.spread_penalty_threshold_ticks:
            score -= w.spread_penalty

        loss_streak = self._loss_streak()
        if loss_streak >= 2:
            score -= w.loss_streak_penalty_per_loss * (loss_streak - 1)

        # Expectancy множник
        exp_mult = 1.0
        if self._exp is not None:
            try:
                exp = self._exp.get(cand.setup_type, cand.symbol)  # type: ignore[attr-defined]
                if exp is not None:
                    exp_mult = max(0.5, min(
                        1.5, 1.0 + getattr(exp, "rolling_E_R", 0.0) * w.expectancy_multiplier_scale,
                    ))
            except Exception:
                pass

        final = score * exp_mult
        threshold = self._score_threshold(rs, loss_streak)
        return final, threshold, exp_mult

    def _score_threshold(self, rs: object, loss_streak: int) -> float:
        cfg = self._config
        base = cfg.base_score_threshold
        regime: Regime = rs.regime  # type: ignore[attr-defined]
        if regime == Regime.HIGH_VOL:
            base += cfg.threshold_boost_high_vol
        if regime == Regime.CHOPPY:
            base += cfg.threshold_boost_choppy
        if loss_streak >= 2:
            base += cfg.threshold_boost_per_loss * (loss_streak - 1)
        return base

    # === Build plan ===

    def _build_trade_plan(
        self, cand: SetupCandidate, score: float, threshold: float, exp_mult: float,
    ) -> TradePlan:
        rs = self._regime.get_regime(cand.symbol)  # type: ignore[attr-defined]
        time_stop = self._config.time_stop_ms_by_setup.get(cand.setup_type)
        return TradePlan(
            candidate=cand,
            setup_type=cand.setup_type,
            direction=cand.direction,
            symbol=cand.symbol,
            timestamp_ms=cand.timestamp_ms,
            entry_price=cand.entry_price,
            stop_price=cand.stop_price,
            tp1_price=cand.tp1_price,
            tp2_price=cand.tp2_price,
            tp3_price=cand.tp3_price,
            stop_distance_ticks=cand.stop_distance_ticks,
            score=score,
            score_threshold=threshold,
            regime=rs.regime,
            expectancy_multiplier=exp_mult,
            invalidation_conditions=cand.invalidation_conditions,
            time_stop_ms=time_stop,
        )

    # === External helpers (безпечні wrapper'и) ===

    def _kill_switch_on(self) -> bool:
        if self._risk is None:
            return False
        try:
            return bool(self._risk.is_kill_switch_on())  # type: ignore[attr-defined]
        except Exception:
            return False

    def _loss_streak(self) -> int:
        if self._risk is None:
            return 0
        try:
            return int(self._risk.get_loss_streak())  # type: ignore[attr-defined]
        except Exception:
            return 0


__all__ = ["DecisionEngine"]
