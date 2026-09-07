"""Base class for Automation Hub strategies.

Thin layer over the existing, tested ``bot.strategies.base.Strategy`` so we
reuse the engine's bar-feeding contract (``on_bar`` -> ``generate``). Adds:

- display metadata (``label``) for the UI,
- an ATR-based stop/target helper so every strategy sizes risk consistently.
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence

from bot.data.indicators import atr
from bot.strategies.base import Strategy
from bot.types import Bar, Signal, SignalType


class HubStrategy(Strategy):
    label: str = "Base Strategy"
    supported_regimes: tuple[str, ...] = ()   # P4: empty = trade in any regime

    def __init__(self, symbol: str, atr_period: int = 14, atr_mult: float = 1.5,
                 rr_target: float = 2.0, **params):
        super().__init__(symbol, atr_period=atr_period, atr_mult=atr_mult,
                         rr_target=rr_target, **params)
        self._native_mtf_context: dict[str, list[Bar]] = {}
        self._native_mtf_evidence: dict = {}

    def set_native_mtf_context(
        self, context: Mapping[str, Sequence[Bar]], evidence: Mapping,
    ) -> None:
        """Attach independently sourced native HTF candles to one decision.

        The forward engine has already removed forming candles at the exact
        decision boundary. Strategies must consume this context as-is and must
        never manufacture replacement HTF candles from their entry stream.
        """
        self._native_mtf_context = {key: list(rows) for key, rows in context.items()}
        self._native_mtf_evidence = dict(evidence)

    def _bracket(self, entry: float, direction: SignalType) -> Optional[tuple[float, float, float]]:
        """Return (stop, take_profit, risk) using ATR, or None if not enough data."""
        a = atr(self.bars, self.params["atr_period"])
        if a <= 0:
            return None
        risk = self.params["atr_mult"] * a
        rr = self.params["rr_target"]
        if direction == SignalType.LONG:
            return entry - risk, entry + rr * risk, risk
        return entry + risk, entry - rr * risk, risk

    def _signal(self, bar: Bar, direction: SignalType, reason: str) -> Optional[Signal]:
        br = self._bracket(bar.close, direction)
        if br is None:
            return None
        stop, tp, _ = br
        return Signal(
            timestamp=bar.timestamp, symbol=self.symbol, type=direction,
            entry=bar.close, stop_loss=stop, take_profit=tp, reason=reason,
        )
