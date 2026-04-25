"""RiskConfig — R-per-trade, ліміти, кулдауни, sizing fallback-и."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RiskConfig(BaseModel):
    # === Per-trade ===
    risk_per_trade_usd_abs: float = Field(default=10.0, gt=0)
    risk_per_trade_pct: float = Field(default=0.003, gt=0)      # 0.3%
    slippage_buffer_ticks: int = Field(default=1, ge=0)

    # === Notional cap ===
    # Захист від тонких стопів: setup з SL за $0.5 від entry на BTCUSDT @ 78000
    # дав би qty=4 BTC (notional ~$300k), що перевищує margin навіть з 10x плечем.
    # Cap = equity * leverage * usage. usage<1 щоб лишити запас на slip/fee.
    leverage: int = Field(default=5, ge=1, le=125)
    max_notional_usage: float = Field(default=0.9, gt=0, le=1.0)

    # === Stop-loss override (fixed % від entry) ===
    # Якщо встановлено — RiskEngine перепише plan.stop_price на
    # entry × (1 ± pct/100), а TP1/TP2/TP3 — на 1R/2R/3R від нового stop.
    # Це прозоре, передбачуване правило ризику замість structure-based stops
    # від setup-detector. Користувач знає: «упаде на 2% → SL спрацює».
    stop_loss_pct: float | None = Field(default=None, gt=0, le=50)

    # === Margin-based sizing (альтернативний режим) ===
    # Якщо встановлено — RiskEngine ігнорує R-based formula (risk/stop_distance)
    # і використовує fixed-margin sizing: notional = equity * pct/100 * leverage.
    # Зручно для трейдерів, що думають у "% balance allocated", не у "% balance to lose".
    # Реальний R-ризик плаваючий — залежить від того, як setup поставив стоп.
    margin_per_trade_pct: float | None = Field(default=None, ge=0, le=100)

    # === Liquidity guards (захист від price impact на тонкій книжці) ===
    # На пар з малою liquidity (HYPER, дрібні альти) велика позиція з'їдає
    # 50%+ top-of-book і зсуває ціну на % — entry filled значно гірше plan,
    # SL/TP не там де ми очікували. Walk the book ПЕРЕД place_order.
    # OPT-IN: None = guard вимкнено (default для BC). Рекомендовано для
    # дрібних альтів виставляти значення явно через UI/YAML.
    max_book_consumption_pct: float | None = Field(default=None, gt=0, le=100)
    """Не більше N% сумарної якості на top book_depth_levels рівнях."""

    max_expected_slippage_ticks: int | None = Field(default=None, ge=1)
    """Якщо очікуваний average fill price відхиляється від best by > N ticks → reject."""

    book_depth_levels: int = Field(default=5, ge=1, le=20)
    """Скільки рівнів книжки враховувати для walk."""

    # === Size fallbacks (поки немає ExchangeInfo) ===
    fallback_tick_size: float = Field(default=0.1, gt=0)
    fallback_step_size: float = Field(default=0.001, gt=0)
    fallback_min_qty: float = Field(default=0.001, gt=0)
    fallback_max_qty: float = Field(default=10.0, gt=0)
    fallback_min_notional: float = Field(default=5.0, ge=0)

    # === Денні/місячні ліміти (в R) ===
    daily_loss_limit_R: float = Field(default=3.0, gt=0)
    monthly_loss_limit_R: float = Field(default=10.0, gt=0)
    max_trades_per_day: int = Field(default=10, gt=0)
    max_initiative_trades_per_day: int = Field(default=3, ge=0)
    max_concurrent_positions: int = Field(default=1, gt=0)

    # === Loss streak ===
    loss_streak_cooldown_trigger: int = Field(default=3, gt=0)
    loss_streak_cooldown_ms: int = Field(default=1_800_000, gt=0)   # 30 хв
    loss_streak_hard_limit: int = Field(default=5, gt=0)


__all__ = ["RiskConfig"]
