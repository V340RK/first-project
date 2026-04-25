"""RiskEngine — останній бар'єр перед ExecutionEngine.

Sizing у валюті ризику (R), денні/місячні ліміти, loss-streak cooldown, kill switch.

Ця імплементація — in-memory. Persist у SQLite додамо пізніше
(буде окремий RiskStore з інтерфейсом save/load).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from scalper.common.enums import Direction, Regime, SetupType
from scalper.common.types import TradePlan
from scalper.risk.config import RiskConfig

logger = logging.getLogger(__name__)

ClockFn = Callable[[], int]


@dataclass(frozen=True)
class RiskSnapshot:
    """Короткий зріз стану ризику для логу/UI."""

    timestamp_ms: int
    equity_usd: float
    realized_r_today: float
    realized_r_month: float
    trades_today: int
    initiative_trades_today: int
    open_positions: int
    loss_streak: int
    kill_switch_on: bool
    kill_reason: str | None


@dataclass(frozen=True)
class RiskDecision:
    plan: TradePlan | None
    reason: str | None
    snapshot: RiskSnapshot


@dataclass(frozen=True)
class TradeOutcome:
    """Передається PositionManager-ом при закритті угоди."""

    plan: TradePlan
    trade_id: str
    symbol: str
    setup_type: SetupType
    direction: Direction
    closed_at_ms: int
    realized_r: float
    realized_usd: float
    max_favorable_r: float
    max_adverse_r: float
    was_stopped: bool
    fees_usd: float


@dataclass
class _DailyAccum:
    date_utc: str
    r_accumulated: float = 0.0
    trades_count: int = 0
    initiative_trades_count: int = 0


@dataclass
class _MonthlyAccum:
    year_month: str
    r_accumulated: float = 0.0


@dataclass
class _RiskState:
    daily: _DailyAccum
    monthly: _MonthlyAccum
    loss_streak: int = 0
    last_loss_ms: int | None = None
    open_positions_count: int = 0
    kill_switch: bool = False
    kill_reason: str | None = None
    kill_until_ms: int | None = None
    closed_trade_ids: set[str] = field(default_factory=set)


def _utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _utc_month(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m")


class RiskEngine:
    def __init__(
        self,
        config: RiskConfig,
        *,
        clock_fn: ClockFn | None = None,
    ) -> None:
        self._config = config
        from scalper.common import time as _time
        self._clock: ClockFn = clock_fn if clock_fn is not None else (lambda: _time.clock())
        now = self._clock()
        self._state = _RiskState(
            daily=_DailyAccum(date_utc=_utc_day(now)),
            monthly=_MonthlyAccum(year_month=_utc_month(now)),
        )

    # === Public API ===

    def evaluate(self, plan: TradePlan, equity_usd: float) -> RiskDecision:
        self._maybe_rollover()
        self._maybe_expire_cooldown()

        snapshot = self._snapshot(equity_usd)

        if reason := self._snapshot_blockers(snapshot):
            return RiskDecision(plan=None, reason=reason, snapshot=snapshot)

        if equity_usd <= 0:
            return RiskDecision(plan=None, reason="equity_unknown", snapshot=snapshot)

        stop_distance_price = abs(plan.entry_price - plan.stop_price)
        if stop_distance_price <= 0:
            return RiskDecision(plan=None, reason="invalid_stop_distance", snapshot=snapshot)

        qty, risk_usd = self._compute_size(plan, equity_usd, stop_distance_price)

        # Notional cap: тонкий стоп може дати qty з notional > equity*leverage,
        # який Binance відкине як "Margin is insufficient". Обмежуємо до
        # equity * leverage * usage. usage<1 — щоб лишити запас на slip/fee.
        max_notional = equity_usd * self._config.leverage * self._config.max_notional_usage
        max_qty_by_notional = max_notional / plan.entry_price
        if qty > max_qty_by_notional:
            qty = max_qty_by_notional
            risk_usd = qty * (stop_distance_price + self._buffer_price())

        if qty <= 0:
            return RiskDecision(plan=None, reason="qty_rounded_to_zero", snapshot=snapshot)
        if qty < self._config.fallback_min_qty:
            return RiskDecision(
                plan=None,
                reason=f"qty_below_min ({qty} < {self._config.fallback_min_qty})",
                snapshot=snapshot,
            )
        if qty > self._config.fallback_max_qty:
            qty = self._config.fallback_max_qty
            risk_usd = qty * (stop_distance_price + self._buffer_price())

        notional = qty * plan.entry_price
        if notional < self._config.fallback_min_notional:
            return RiskDecision(
                plan=None,
                reason=f"notional_below_min ({notional:.2f} < {self._config.fallback_min_notional})",
                snapshot=snapshot,
            )

        # Risk overshoot перевіряємо тільки в R-based mode. У margin-mode
        # real R-ризик плаваючий за дизайном (залежить від stop_distance),
        # тому не reject'имо позицію через нього.
        if (self._config.margin_per_trade_pct is None
                and risk_usd > self._config.risk_per_trade_usd_abs * 1.1):
            return RiskDecision(
                plan=None, reason=f"risk_overshoot ({risk_usd:.2f})", snapshot=snapshot,
            )

        projected_daily = snapshot.realized_r_today - 1.0
        if projected_daily < -self._config.daily_loss_limit_R:
            return RiskDecision(
                plan=None,
                reason=f"would_breach_daily_limit ({projected_daily:.2f}R)",
                snapshot=snapshot,
            )

        is_initiative = self._is_initiative(plan)
        if is_initiative and snapshot.initiative_trades_today >= self._config.max_initiative_trades_per_day:
            return RiskDecision(
                plan=None, reason="initiative_quota_exhausted", snapshot=snapshot,
            )

        if snapshot.open_positions >= self._config.max_concurrent_positions:
            return RiskDecision(
                plan=None, reason="max_concurrent_positions", snapshot=snapshot,
            )

        accepted = replace(
            plan,
            position_size=qty,
            risk_usd=risk_usd,
            risk_gate_passed=True,
        )
        # ВАЖЛИВО: НЕ інкрементуємо лічильники тут. Якщо position.open()
        # потім впаде (no symbol filters, exchange reject) — counter залишиться
        # переанкрементованим і всі майбутні setups блокуватимуться як
        # "max_concurrent_positions". Інкремент робить on_position_opened(),
        # який Orchestrator викликає лише при успішному відкритті.
        return RiskDecision(plan=accepted, reason=None, snapshot=snapshot)

    def on_position_opened(self, plan: TradePlan) -> None:
        """Викликається Orchestrator-ом ТІЛЬКИ після успішного position.open()."""
        self._state.open_positions_count += 1
        self._state.daily.trades_count += 1
        if self._is_initiative(plan):
            self._state.daily.initiative_trades_count += 1

    def on_position_closed(self, outcome: TradeOutcome) -> None:
        # Ідемпотентність: той самий trade_id не рахуємо двічі
        if outcome.trade_id in self._state.closed_trade_ids:
            logger.info("on_position_closed: duplicate trade_id=%s, ignored", outcome.trade_id)
            return
        self._state.closed_trade_ids.add(outcome.trade_id)

        self._state.open_positions_count = max(0, self._state.open_positions_count - 1)
        self._state.daily.r_accumulated += outcome.realized_r
        self._state.monthly.r_accumulated += outcome.realized_r

        if outcome.realized_r < 0:
            self._state.loss_streak += 1
            self._state.last_loss_ms = outcome.closed_at_ms
            self._check_loss_streak_triggers()
        elif outcome.realized_r > 0:
            self._state.loss_streak = 0
        # realized_r == 0 (scratch/time-stop) — streak не чіпаємо

        if self._state.daily.r_accumulated <= -self._config.daily_loss_limit_R:
            self.engage_kill_switch(
                f"daily_loss_limit ({self._state.daily.r_accumulated:.2f}R)",
            )

        if self._state.monthly.r_accumulated <= -self._config.monthly_loss_limit_R:
            self.engage_kill_switch(
                f"monthly_loss_limit ({self._state.monthly.r_accumulated:.2f}R)",
            )

    def is_kill_switch_on(self) -> bool:
        return self._state.kill_switch

    def get_loss_streak(self) -> int:
        return self._state.loss_streak

    def get_daily_r(self) -> float:
        self._maybe_rollover()
        return self._state.daily.r_accumulated

    def get_monthly_r(self) -> float:
        self._maybe_rollover()
        return self._state.monthly.r_accumulated

    def snapshot(self, equity_usd: float = 0.0) -> RiskSnapshot:
        return self._snapshot(equity_usd)

    # === Admin ===

    def engage_kill_switch(self, reason: str) -> None:
        if self._state.kill_switch:
            logger.info("kill already on (%s), new: %s", self._state.kill_reason, reason)
            return
        self._state.kill_switch = True
        self._state.kill_reason = reason
        logger.warning("KILL SWITCH ENGAGED: %s", reason)

    def clear_kill_switch(self) -> None:
        self._state.kill_switch = False
        self._state.kill_reason = None
        self._state.kill_until_ms = None
        logger.info("kill switch cleared")

    def reset_daily(self) -> None:
        now = self._clock()
        old = self._state.daily
        self._state.daily = _DailyAccum(date_utc=_utc_day(now))
        logger.info("daily_rollover: prev=%.2fR, %d trades", old.r_accumulated, old.trades_count)

    def reset_monthly(self) -> None:
        now = self._clock()
        old = self._state.monthly
        self._state.monthly = _MonthlyAccum(year_month=_utc_month(now))
        logger.info("monthly_rollover: prev=%.2fR", old.r_accumulated)

    # === Internals ===

    def _snapshot(self, equity_usd: float) -> RiskSnapshot:
        return RiskSnapshot(
            timestamp_ms=self._clock(),
            equity_usd=equity_usd,
            realized_r_today=self._state.daily.r_accumulated,
            realized_r_month=self._state.monthly.r_accumulated,
            trades_today=self._state.daily.trades_count,
            initiative_trades_today=self._state.daily.initiative_trades_count,
            open_positions=self._state.open_positions_count,
            loss_streak=self._state.loss_streak,
            kill_switch_on=self._state.kill_switch,
            kill_reason=self._state.kill_reason,
        )

    def _snapshot_blockers(self, snap: RiskSnapshot) -> str | None:
        if snap.kill_switch_on:
            return f"kill_switch:{snap.kill_reason}"
        if snap.realized_r_today <= -self._config.daily_loss_limit_R:
            return f"daily_limit_reached ({snap.realized_r_today:.2f}R)"
        if snap.realized_r_month <= -self._config.monthly_loss_limit_R:
            return f"monthly_limit_reached ({snap.realized_r_month:.2f}R)"
        if snap.trades_today >= self._config.max_trades_per_day:
            return "trade_count_cap"
        if snap.loss_streak >= self._config.loss_streak_hard_limit:
            return f"loss_streak_hard ({snap.loss_streak})"
        return None

    def _buffer_price(self) -> float:
        return self._config.slippage_buffer_ticks * self._config.fallback_tick_size

    def _compute_size(
        self, plan: TradePlan, equity: float, stop_distance_price: float,
    ) -> tuple[float, float]:
        c = self._config
        effective_distance = stop_distance_price + self._buffer_price()

        if c.margin_per_trade_pct is not None and c.margin_per_trade_pct > 0:
            # Margin-based sizing: фіксована частка balance як margin.
            # Notional = margin * leverage. R-ризик плаваючий (залежить від stop).
            margin_usd = equity * c.margin_per_trade_pct / 100.0
            notional = margin_usd * c.leverage
            qty = notional / plan.entry_price
        else:
            # R-based sizing (default): qty з заданого ризику.
            r_usd = min(c.risk_per_trade_usd_abs, equity * c.risk_per_trade_pct)
            qty = r_usd / effective_distance

        qty = self._round_step(qty, c.fallback_step_size)
        real_risk = qty * effective_distance
        return qty, real_risk

    @staticmethod
    def _round_step(qty: float, step: float) -> float:
        if step <= 0:
            return qty
        n = int(qty / step)
        return round(n * step, 12)

    def _is_initiative(self, plan: TradePlan) -> bool:
        if plan.regime == Regime.TRENDING_UP and plan.direction == Direction.SHORT:
            return True
        if plan.regime == Regime.TRENDING_DOWN and plan.direction == Direction.LONG:
            return True
        return False

    def _check_loss_streak_triggers(self) -> None:
        streak = self._state.loss_streak
        c = self._config
        if streak == c.loss_streak_cooldown_trigger and streak < c.loss_streak_hard_limit:
            self.engage_kill_switch(f"loss_streak={streak}_cooldown")
            self._state.kill_until_ms = self._clock() + c.loss_streak_cooldown_ms
        elif streak >= c.loss_streak_hard_limit:
            self.engage_kill_switch(f"loss_streak={streak}_hard")
            # hard-limit не має авто-expiry — знімається лише вручну

    def _maybe_expire_cooldown(self) -> None:
        st = self._state
        if not st.kill_switch or st.kill_until_ms is None:
            return
        if self._clock() < st.kill_until_ms:
            return
        # Автоматично знімаємо ТІЛЬКИ loss-streak cooldown
        if st.kill_reason and st.kill_reason.startswith("loss_streak=") and "_cooldown" in st.kill_reason:
            logger.info("kill cooldown expired, clearing")
            self.clear_kill_switch()
            self._state.loss_streak = 0

    def _maybe_rollover(self) -> None:
        now = self._clock()
        today = _utc_day(now)
        month = _utc_month(now)
        if today != self._state.daily.date_utc:
            self.reset_daily()
        if month != self._state.monthly.year_month:
            self.reset_monthly()


__all__ = ["RiskEngine", "RiskDecision", "RiskSnapshot", "TradeOutcome", "RiskConfig"]
