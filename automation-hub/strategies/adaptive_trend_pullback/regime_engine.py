"""Primary-HTF regime classification. No entry logic belongs in this module."""
from __future__ import annotations

from bot.data.indicators import atr
from bot.types import Bar

from .config import AdaptiveTrendPullbackConfig
from .indicators import adx, atr_expansion, ema_read, structure_direction
from .models import MarketRegime, RegimeAssessment


class MarketRegimeEngine:
    def __init__(self, config: AdaptiveTrendPullbackConfig):
        self.config = config

    def assess(self, bars: list[Bar]) -> RegimeAssessment:
        cfg = self.config
        if len(bars) < cfg.minimum_bars[cfg.regime_timeframe]:
            return RegimeAssessment(MarketRegime.UNCERTAIN, 0, 0, 0, 0, 0,
                                    failed=(f"insufficient completed {cfg.regime_timeframe} candles",))
        read = ema_read(bars, cfg.fast_ema, cfg.slow_ema)
        current_atr = atr(bars, cfg.atr_period)
        current_adx = adx(bars, cfg.adx_period)
        price = bars[-1].close
        atr_pct = current_atr / price if price else 0.0
        expansion = atr_expansion(bars, cfg.atr_period)
        structure = structure_direction(bars, cfg.structure_lookback)
        separation = abs(read["fast"] - read["slow"]) / current_atr if current_atr else 0.0
        extension = abs(price - read["fast"]) / current_atr if current_atr else 999.0
        volatility_ok = atr_pct <= cfg.maximum_atr_pct and expansion <= cfg.maximum_atr_expansion
        if not volatility_ok:
            return RegimeAssessment(
                MarketRegime.HIGH_VOLATILITY, 100, 0, 0, current_adx, atr_pct,
                reasons=(f"ATR {atr_pct:.2%}", f"ATR expansion {expansion:.2f}x"),
                failed=("volatility safety limit exceeded",),
            )

        def directional(sign: int) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
            tests = (
                (20, (read["fast"] - read["slow"]) * sign > 0, "EMA20/EMA50 aligned"),
                (15, read["fast_slope"] * sign > 0, "EMA20 slope aligned"),
                (10, read["slow_slope"] * sign >= 0, "EMA50 slope aligned or neutral"),
                (20, structure == sign, "confirmed swing structure aligned"),
                (15, current_adx >= cfg.adx_min, f"ADX {current_adx:.1f} >= {cfg.adx_min:.1f}"),
                (10, extension <= cfg.maximum_extension_atr, f"extension {extension:.2f} ATR acceptable"),
                (10, separation >= cfg.ema_separation_min_atr, f"EMA separation {separation:.2f} ATR"),
            )
            passed = tuple(label for _, ok, label in tests if ok)
            failed = tuple(label for _, ok, label in tests if not ok)
            return sum(weight for weight, ok, _ in tests if ok), passed, failed

        bull, bull_passed, bull_failed = directional(1)
        bear, bear_passed, bear_failed = directional(-1)
        confidence = max(bull, bear)
        if bull >= cfg.regime_confidence_min and bull > bear:
            regime, reasons, failed = MarketRegime.BULL_TREND, bull_passed, bull_failed
        elif bear >= cfg.regime_confidence_min and bear > bull:
            regime, reasons, failed = MarketRegime.BEAR_TREND, bear_passed, bear_failed
        elif current_adx < cfg.adx_min or separation < cfg.ema_separation_min_atr:
            regime, reasons, failed = MarketRegime.RANGE, (), ("trend strength/separation too low",)
        else:
            regime, reasons, failed = MarketRegime.UNCERTAIN, (), ("directional evidence is conflicting",)
        return RegimeAssessment(regime, confidence, bull, bear, current_adx, atr_pct,
                                reasons=reasons, failed=failed)
