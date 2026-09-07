"""Deterministic orchestration for adaptive multi-timeframe trend pullbacks."""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

from bot.data.indicators import atr
from bot.types import Bar, Signal, SignalType
from strategies.base_strategy import HubStrategy

from .config import AdaptiveTrendPullbackConfig
from .confirmation_engine import EntryConfirmationEngine
from .models import MarketRegime, SetupState, StrategyDecision
from .pullback_detector import PullbackDetector
from .quality_scorer import QualityScorer
from .regime_engine import MarketRegimeEngine
from .trend_engine import HigherTimeframeTrendEngine
from .indicators import confirmed_swings


class AdaptiveTrendPullbackStrategy(HubStrategy):
    name = "adaptive_trend_pullback"
    label = "Adaptive MTF Trend Pullback"
    supported_regimes = ()  # its own stricter directional regime engine is authoritative
    # 1h is the shared policy's primary gate for a 5m entry. 4h arrives via
    # set_native_mtf_context as secondary bias and is never a separate gate.
    required_timeframes = ("1h", "15m", "5m")
    decision_timeframe = "5m"

    def __init__(self, symbol: str, *, config: Optional[AdaptiveTrendPullbackConfig] = None,
                 allow_legacy_mtf_resample: bool = False, **params):
        self.config = config or AdaptiveTrendPullbackConfig(**params)
        super().__init__(symbol, atr_period=self.config.atr_period,
                         rr_target=self.config.target_rr)
        self._context: dict[str, list[Bar]] = {timeframe: [] for timeframe in self.required_timeframes}
        self._external_context = False
        self.allow_legacy_mtf_resample = bool(allow_legacy_mtf_resample)
        self.regime_engine = MarketRegimeEngine(self.config)
        self.trend_engine = HigherTimeframeTrendEngine(self.config)
        self.pullback_detector = PullbackDetector(self.config)
        self.confirmation_engine = EntryConfirmationEngine(self.config)
        self.quality_scorer = QualityScorer()
        self.lifecycle_state = SetupState.SCANNING
        self.last_decision = StrategyDecision(SetupState.SCANNING, "WAIT", None, "Awaiting multi-timeframe context")

    def set_timeframe_context(self, context: Mapping[str, Sequence[Bar]]) -> None:
        """Supply independently completed candles; forming bars are forbidden upstream."""
        self._context = {timeframe: list(context.get(timeframe, ())) for timeframe in self.required_timeframes}
        # Secondary policy evidence is retained for identity/telemetry only;
        # it is deliberately absent from the gate list above.
        if "4h" in context:
            self._context["4h"] = list(context.get("4h", ()))
        self._external_context = True
        # The base engine still calls on_bar on each new 5M close. Keep the
        # canonical entry stream identical to that engine-owned sequence.
        self.bars = list(self._context[self.decision_timeframe])

    def on_bar(self, bar: Bar) -> Optional[Signal]:
        # A context-aware engine has already inserted this closed 5M candle.
        # Direct backtest callers retain the ordinary Strategy contract.
        entry = self._context.get(self.decision_timeframe) or []
        if not entry or entry[-1].timestamp != bar.timestamp:
            entry = [*entry, bar]
            self._context[self.decision_timeframe] = entry
        if not self._external_context and self.allow_legacy_mtf_resample:
            # Historical/walk-forward callers may stream genuine 5M candles.
            # Aggregate only complete aligned buckets; never interpolate a
            # higher timeframe or expose the in-progress bucket.
            from bot.data.resample import resample
            for timeframe in self.required_timeframes:
                if timeframe != self.decision_timeframe:
                    self._context[timeframe] = resample(
                        entry, timeframe, source_tf=self.decision_timeframe)
        self.bars = entry
        return self.generate(bar)

    def generate(self, bar: Bar) -> Optional[Signal]:
        missing = [timeframe for timeframe in self.required_timeframes
                   if len(self._context.get(timeframe, ())) < self.config.minimum_bars[timeframe]]
        if missing:
            self._block(f"Insufficient completed candles: {', '.join(missing)}")
            return None

        regime = self.regime_engine.assess(self._context["1h"])
        if regime.regime not in (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND):
            self._block(f"1H primary regime {regime.regime.value} blocks trend entries", regime=regime)
            return None
        direction = "LONG" if regime.regime == MarketRegime.BULL_TREND else "SHORT"
        trend = self.trend_engine.assess(self._context["1h"], direction)
        if not trend.valid:
            self._block("1H trend confirmation failed", direction, regime, trend)
            return None
        self.lifecycle_state = SetupState.SETUP_FOUND
        self.lifecycle_state = SetupState.WAITING_FOR_PULLBACK
        pullback = self.pullback_detector.assess(self._context["15m"], direction)
        if not pullback.valid:
            self.last_decision = StrategyDecision(
                SetupState.WAITING_FOR_PULLBACK, "WAIT", direction,
                "15M pullback is not at a valid corrective location",
                regime=regime, trend=trend, pullback=pullback)
            return None
        self.lifecycle_state = SetupState.WAITING_FOR_CONFIRMATION
        confirmation = self.confirmation_engine.assess(self._context["5m"], direction)
        if not confirmation.valid:
            self.last_decision = StrategyDecision(
                SetupState.WAITING_FOR_CONFIRMATION, "REJECT", direction,
                "No completed 5M candle plus structure-break confirmation",
                regime=regime, trend=trend, pullback=pullback, confirmation=confirmation)
            return None

        entry = bar.close
        entry_atr = atr(self._context["5m"], self.config.atr_period)
        if entry_atr <= 0:
            self._block("5M ATR is unavailable", direction, regime, trend)
            return None
        stop = ((pullback.swing_low or bar.low) - entry_atr * self.config.stop_atr_buffer
                if direction == "LONG" else
                (pullback.swing_high or bar.high) + entry_atr * self.config.stop_atr_buffer)
        risk = abs(entry - stop)
        if risk <= 0 or (direction == "LONG" and stop >= entry) or (direction == "SHORT" and stop <= entry):
            self._block("Structure stop is invalid relative to entry", direction, regime, trend)
            return None
        sign = 1 if direction == "LONG" else -1
        target = entry + sign * risk * self.config.target_rr
        if self.config.target_method == "structure":
            highs, lows = confirmed_swings(self._context["1h"], self.config.structure_lookback)
            candidates = ([level for level in highs if level > entry] if direction == "LONG"
                          else [level for level in lows if level < entry])
            if not candidates:
                self._block("No valid next 1H structure target", direction, regime, trend)
                return None
            target = min(candidates) if direction == "LONG" else max(candidates)
        rr = abs(target - entry) / risk
        quality, components = self.quality_scorer.score(
            regime=regime, trend=trend, pullback=pullback,
            confirmation=confirmation, rr=rr, minimum_rr=self.config.minimum_rr)
        if rr < self.config.minimum_rr or quality < self.config.quality_minimum:
            self.last_decision = StrategyDecision(
                SetupState.BLOCKED, "REJECT", direction,
                f"Quality {quality:.1f} or R:R {rr:.2f} below configured minimum",
                quality, regime, trend, pullback, confirmation, components,
                entry, stop, target, rr)
            self.lifecycle_state = SetupState.BLOCKED
            return None

        self.lifecycle_state = SetupState.ORDER_PENDING
        secondary = (self._native_mtf_evidence.get("secondary") or {})
        reason = (f"{direction} | 1H primary {regime.regime.value} {regime.confidence:.0f}% | "
                  f"4H bias {secondary.get('htf_bias', 'NEUTRAL')} | "
                  f"15M {pullback.location} | "
                  f"5M confirmation | quality {quality:.0f}/100 | RR {rr:.2f}")
        signal = Signal(
            timestamp=bar.timestamp, symbol=self.symbol,
            type=SignalType.LONG if direction == "LONG" else SignalType.SHORT,
            entry=entry, stop_loss=stop, take_profit=target,
            reason=reason, confidence=min(1.0, quality / 100),
        )
        signal.regime = regime.regime.value
        signal.brain_score = round(quality, 2)
        signal.snapshot = {
            "market_data_mode": "multi_timeframe_closed_candles",
            "regime": regime.regime.value, "regime_confidence": regime.confidence,
            "trend_1h": trend.label, "pullback_15m": pullback.label,
            "pullback_location": pullback.location,
            "confirmation_5m": confirmation.label,
            "quality_score": round(quality, 2), "planned_rr": round(rr, 2),
            "atr_5m": round(entry_atr, 8),
            "timeframe_closes": self._timeframe_closes(),
            "mtf_evidence": dict(self._native_mtf_evidence),
        }
        signal.checklist = self._checklist(regime, trend, pullback, confirmation, quality, rr)
        self.last_decision = StrategyDecision(
            SetupState.ORDER_PENDING, f"ENTER {direction}", direction, reason,
            quality, regime, trend, pullback, confirmation, components,
            entry, stop, target, rr)
        return signal

    def _timeframe_closes(self) -> dict[str, str]:
        """Return actual close instants; provider candle timestamps are opens."""
        from datetime import timedelta
        from bot.data.resample import TF_SECONDS
        return {
            timeframe: (
                self._context[timeframe][-1].timestamp
                + timedelta(seconds=TF_SECONDS[timeframe])
            ).isoformat()
            for timeframe in self._context
        }

    def _block(self, reason: str, direction=None, regime=None, trend=None) -> None:
        self.lifecycle_state = SetupState.BLOCKED
        self.last_decision = StrategyDecision(SetupState.BLOCKED, "NO TRADE", direction,
                                              reason, regime=regime, trend=trend)

    @staticmethod
    def _checklist(regime, trend, pullback, confirmation, quality, rr) -> list[dict]:
        return [
            {"name": "1H primary regime", "status": "Passed", "detail": f"{regime.regime.value} {regime.confidence:.0f}%"},
            {"name": "1H primary trend confirmation", "status": "Passed" if trend.valid else "Failed", "detail": trend.label},
            {"name": "4H secondary bias", "status": "Neutral", "detail": "context only; never an entry gate"},
            {"name": "15M corrective pullback", "status": "Passed" if pullback.valid else "Failed", "detail": pullback.location or "no valid location"},
            {"name": "5M candle confirmation", "status": "Passed" if confirmation.valid else "Failed", "detail": confirmation.label},
            {"name": "Volume confirmation", "status": "Passed" if confirmation.volume_confirmed else "Neutral", "detail": "5M vs 20-bar average"},
            {"name": "Quality threshold", "status": "Passed", "detail": f"{quality:.0f}/100"},
            {"name": "Structure stop and R:R", "status": "Passed", "detail": f"{rr:.2f}R"},
        ]

    def decision_report(self) -> dict:
        return self.last_decision.public()

    def mark_position_open(self, reason: str = "Paper order filled") -> None:
        """Synchronise the strategy state with the authoritative paper broker."""
        self.lifecycle_state = SetupState.POSITION_OPEN
        self.last_decision.state = SetupState.POSITION_OPEN
        self.last_decision.decision = "POSITION OPEN"
        self.last_decision.reason = reason

    def mark_position_managing(self, reason: str = "Managing open paper position") -> None:
        self.lifecycle_state = SetupState.MANAGING_POSITION
        self.last_decision.state = SetupState.MANAGING_POSITION
        self.last_decision.decision = "MANAGE"
        self.last_decision.reason = reason

    def mark_position_closed(self, reason: str = "Paper position closed") -> None:
        self.lifecycle_state = SetupState.POSITION_CLOSED
        self.last_decision.state = SetupState.POSITION_CLOSED
        self.last_decision.decision = "POSITION CLOSED"
        self.last_decision.reason = reason
