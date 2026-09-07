"""Smart Money Concepts (SMC) strategy — a Python port of the KYROS/Tradexa
SMC PRO confluence model, for simulation and paper trading.

Mirrors the core of the Pine v6 strategy's entry logic on a single bar stream:

    long  = HTF bias bullish
            + recent liquidity sweep of lows (wick below, close back inside)
            + recent bullish structure shift (CHoCH/BOS)
            + recent bullish fair-value gap
            + (optional) bullish rejection candle
    short = the mirror image

Higher-timeframe bias comes from the provider-native primary HTF selected by
the shared MTF policy. Stop = signal-bar extreme ± ATR; target = R:R.

Pure/stdlib; no indicators beyond the engine's own ``atr``/``ema``.
"""
from __future__ import annotations

from typing import Optional

from bot.data.indicators import atr
from bot.types import Bar, Signal, SignalType
from strategies.base_strategy import HubStrategy

_NEG = -10 ** 9


class SMCStrategy(HubStrategy):
    name = "smc"
    label = "SMC (Smart Money)"
    supported_regimes = ()  # the confluence model gates itself

    def __init__(self, symbol: str, *, pivot_len: int = 5, sweep_lookback: int = 10,
                 choch_lookback: int = 8, fvg_lookback: int = 5, use_rejection: bool = False,
                 wick_mult: float = 2.0, warmup: int = 120, atr_period: int = 14,
                 atr_mult: float = 1.5, rr_target: float = 2.5,
                 allow_legacy_htf_resample: bool = False, **params):
        super().__init__(symbol, atr_period=atr_period, atr_mult=atr_mult, rr_target=rr_target,
                         pivot_len=pivot_len, sweep_lookback=sweep_lookback,
                         choch_lookback=choch_lookback, fvg_lookback=fvg_lookback,
                         use_rejection=use_rejection, wick_mult=wick_mult, warmup=warmup, **params)
        # rolling structure state
        self._bias = 0
        self._swing_high: Optional[float] = None
        self._swing_low: Optional[float] = None
        self._sh_crossed = False
        self._sl_crossed = False
        # bar index of the most recent event (for "recent X" windows)
        self._last_bull_struct = _NEG
        self._last_bear_struct = _NEG
        self._last_sweep_low = _NEG
        self._last_sweep_high = _NEG
        self._last_bull_fvg = _NEG
        self._last_bear_fvg = _NEG
        self._cfg = None  # lazy BrainConfig for HTF bias
        self.allow_legacy_htf_resample = bool(allow_legacy_htf_resample)

    def generate(self, bar: Bar) -> Optional[Signal]:
        from strategies.brain import BrainConfig, htf_bias
        if self._cfg is None:
            self._cfg = BrainConfig()
        bars = self.bars
        i = len(bars) - 1
        p = self.params
        if i < p["warmup"]:
            return None

        self._update_pivots(bars, i, p["pivot_len"])
        self._update_structure(bar, i)
        self._update_sweep(bars, bar, i, p["sweep_lookback"])
        self._update_fvg(bars, i)

        primary = self._native_mtf_evidence.get("primary") or {}
        primary_tf = primary.get("htf_timeframe")
        native_rows = self._native_mtf_context.get(primary_tf, ()) if primary_tf else ()
        bias_name, strength = htf_bias(
            bars, self._cfg, native_bars=native_rows if native_rows else None,
            allow_legacy_resample=self.allow_legacy_htf_resample,
        )
        htf = 1 if bias_name == "bullish" else -1 if bias_name == "bearish" else 0

        bull_pin, bear_pin = self._rejection(bar, p["wick_mult"])
        rej_long = bull_pin if p["use_rejection"] else True
        rej_short = bear_pin if p["use_rejection"] else True

        sweep_lb, choch_lb, fvg_lb = p["sweep_lookback"], p["choch_lookback"], p["fvg_lookback"]
        recent_sweep_low = (i - self._last_sweep_low) <= sweep_lb
        recent_sweep_high = (i - self._last_sweep_high) <= sweep_lb
        recent_bull_struct = (i - self._last_bull_struct) <= choch_lb
        recent_bear_struct = (i - self._last_bear_struct) <= choch_lb
        recent_bull_fvg = (i - self._last_bull_fvg) <= fvg_lb
        recent_bear_fvg = (i - self._last_bear_fvg) <= fvg_lb

        a = atr(bars, p["atr_period"])
        if a <= 0:
            return None

        long_ok = htf == 1 and recent_sweep_low and recent_bull_struct and recent_bull_fvg and rej_long
        short_ok = htf == -1 and recent_sweep_high and recent_bear_struct and recent_bear_fvg and rej_short

        if long_ok:
            stop = bar.low - a * p["atr_mult"]
            risk = bar.close - stop
            if risk <= 0:
                return None
            sig = Signal(timestamp=bar.timestamp, symbol=self.symbol, type=SignalType.LONG,
                         entry=bar.close, stop_loss=stop, take_profit=bar.close + risk * p["rr_target"],
                         reason=f"SMC long — sweep+CHoCH+FVG, HTF {bias_name}")
            sig.confidence = self._confidence(strength)
            sig.snapshot = {"mtf_evidence": dict(self._native_mtf_evidence)}
            return sig

        if short_ok:
            stop = bar.high + a * p["atr_mult"]
            risk = stop - bar.close
            if risk <= 0:
                return None
            sig = Signal(timestamp=bar.timestamp, symbol=self.symbol, type=SignalType.SHORT,
                         entry=bar.close, stop_loss=stop, take_profit=bar.close - risk * p["rr_target"],
                         reason=f"SMC short — sweep+CHoCH+FVG, HTF {bias_name}")
            sig.confidence = self._confidence(strength)
            sig.snapshot = {"mtf_evidence": dict(self._native_mtf_evidence)}
            return sig
        return None

    # ----------------------------------------------------------- internals
    def _update_pivots(self, bars, i, L):
        """Confirm a swing pivot L bars back (needs L bars on each side)."""
        pi = i - L
        if pi - L < 0:
            return
        seg = bars[pi - L:pi + L + 1]
        if bars[pi].high == max(b.high for b in seg):
            self._swing_high = bars[pi].high
            self._sh_crossed = False
        if bars[pi].low == min(b.low for b in seg):
            self._swing_low = bars[pi].low
            self._sl_crossed = False

    def _update_structure(self, bar, i):
        """Break of the last swing high/low on close = BOS/CHoCH; flips bias."""
        c = bar.close
        if self._swing_high is not None and not self._sh_crossed and c > self._swing_high:
            self._sh_crossed = True
            self._bias = 1
            self._last_bull_struct = i
        if self._swing_low is not None and not self._sl_crossed and c < self._swing_low:
            self._sl_crossed = True
            self._bias = -1
            self._last_bear_struct = i

    def _update_sweep(self, bars, bar, i, lb):
        """Liquidity sweep: wick beyond the prior lb-bar extreme, close back inside."""
        if i - lb < 0:
            return
        prior = bars[i - lb:i]
        liq_high = max(b.high for b in prior)
        liq_low = min(b.low for b in prior)
        if bar.high > liq_high and bar.close < liq_high:
            self._last_sweep_high = i
        if bar.low < liq_low and bar.close > liq_low:
            self._last_sweep_low = i

    def _update_fvg(self, bars, i):
        """3-candle imbalance (fair-value gap)."""
        if i < 2:
            return
        if bars[i].low > bars[i - 2].high:
            self._last_bull_fvg = i
        if bars[i].high < bars[i - 2].low:
            self._last_bear_fvg = i

    @staticmethod
    def _rejection(bar, wick_mult):
        body = abs(bar.close - bar.open)
        lower = min(bar.close, bar.open) - bar.low
        upper = bar.high - max(bar.close, bar.open)
        bull_pin = lower >= body * wick_mult and upper <= body
        bear_pin = upper >= body * wick_mult and lower <= body
        return bull_pin, bear_pin

    @staticmethod
    def _confidence(strength: float) -> float:
        # full confluence already required; scale a little by HTF trend strength
        return max(0.0, min(1.0, 0.7 + 0.3 * min(1.0, strength / 0.45)))
