"""DecisionBrain — a multi-signal decision engine (not a single indicator).

Instead of one crossover, the brain weighs several independent reads of the same
data and only acts when they agree enough to clear a conviction threshold:

    trend      — fast vs slow EMA (direction of the medium-term trend)
    trend filter — price vs a long EMA (don't fight the bigger trend)
    momentum   — slope of the fast EMA (is the move accelerating?)
    RSI        — momentum oscillator (confirm strength, fade extremes)
    regime     — Trending / Ranging / Volatile (size down or stand aside)

Each produces a vote in [-1, +1]; a weighted sum gives a score in [-1, +1].
``confidence = |score|`` (after regime adjustment). Below the threshold the brain
returns ``None`` — i.e. it *decides not to trade*. Above it, it emits a Signal
whose ``confidence`` scales the risk taken and whose ``reason`` lists exactly
which reads agreed, so every decision is explainable.
"""
from __future__ import annotations

from typing import Optional, Sequence

import pandas as pd

from bot.data.indicators import atr, ema, rsi
from bot.types import Bar, Signal, SignalType
from services.regime import RegimeDetector
from strategies.base_strategy import HubStrategy

# Regime -> conviction multiplier (capital protection: stand aside in chaos).
# Out-of-sample testing across trend/range/chop regimes showed the brain earns
# in clean trends but bleeds in ranging / high-volatility noise, so those
# regimes are damped hard — the engine would rather skip a trade than take a
# coin-flip in chop.
_REGIME_FACTOR = {
    "Trending": 1.0,
    "Low Volatility": 0.85,
    "Ranging": 0.45,
    "High Volatility": 0.35,
    "Extreme Volatility": 0.0,   # do not trade
}


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _htf_trend_vote(bars: Sequence[Bar], mult: int) -> Optional[float]:
    """Legacy test-only LTF aggregation path.

    Buckets are left-closed/right-open and incomplete buckets are excluded.
    Appending another base candle therefore cannot repaint a previously closed
    HTF candle or move the timeframe origin.

    Returns +1 (HTF up), -1 (HTF down), or None when there isn't enough
    history for an honest read (needs ≥ 24 HTF buckets)."""
    if mult <= 1 or len(bars) < mult * 24 or len(bars) < 2:
        return None
    index = pd.DatetimeIndex([pd.Timestamp(bar.timestamp) for bar in bars])
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    if not index.is_monotonic_increasing or not index.is_unique:
        raise ValueError("HTF input candles must be strictly increasing and unique")
    deltas = index.to_series().diff().dropna()
    source_delta = deltas.median()
    if source_delta <= pd.Timedelta(0) or not (deltas == source_delta).all():
        raise ValueError("HTF input candles are missing or have an irregular interval")

    htf_rule = source_delta * mult
    frame = pd.DataFrame(
        {"close": [float(bar.close) for bar in bars]}, index=index,
    )
    aggregated = frame.resample(
        htf_rule, origin="epoch", closed="left", label="left",
    ).agg(close=("close", "last"), source_count=("close", "size"))

    # The newest source candle is already closed when this function is called,
    # so decision time is its close (open timestamp + one source interval).
    # The target bucket containing that instant is still forming and must never
    # vote. This is the explicit anti-lookahead boundary.
    current_time = index[-1] + source_delta
    forming_bucket = current_time.floor(htf_rule)
    aggregated = aggregated.loc[aggregated.index < forming_bucket]
    htf = aggregated.loc[aggregated["source_count"] == mult, "close"].tolist()
    if len(htf) < 24:
        return None
    hf = ema(htf, 10)[-1]
    hs = ema(htf, 21)[-1]
    if hf == hs:
        return None
    return 1.0 if hf > hs else -1.0


def _native_htf_trend_vote(bars: Sequence[Bar]) -> Optional[float]:
    """Trend vote from provider-native candles selected by the MTF policy."""
    closes = [float(bar.close) for bar in bars]
    if len(closes) < 24:
        return None
    fast = ema(closes, 10)[-1]
    slow = ema(closes, 21)[-1]
    if fast == slow:
        return None
    return 1.0 if fast > slow else -1.0


class DecisionBrain(HubStrategy):
    name = "brain"
    label = "Decision Brain"
    supported_regimes = ()  # handles regime internally

    # weights for the four reads (sum to 1.0)
    W_TREND, W_FILTER, W_SLOPE, W_RSI = 0.30, 0.25, 0.20, 0.25

    def __init__(self, symbol: str, *, fast: int = 12, slow: int = 26,
                 trend: int = 50, rsi_period: int = 14,
                 conviction_threshold: float = 0.56, max_history: int = 600,
                 er_mode: str = "off", volume_conf: bool = False,
                 htf_mode: str = "damp", htf_mult: int = 12,
                 htf_damp: float = 0.55,
                 allow_legacy_htf_resample: bool = False, **params):
        params.setdefault("rr_target", 3.0)  # validated reward:risk (out-of-sample sweep)
        super().__init__(symbol, fast=fast, slow=slow, trend=trend,
                         rsi_period=rsi_period, conviction_threshold=conviction_threshold,
                         **params)
        self.max_history = max_history
        self._regime = RegimeDetector()
        # experimental reads (measured via the eval harness before defaulting):
        # er_mode: "off" | "replace" (ER replaces the slope vote) | "add" (5th vote)
        # volume_conf: volume-surge conviction multiplier in [0.9, 1.1]
        self.er_mode = er_mode
        self.volume_conf = bool(volume_conf)
        # Production confirmation consumes the provider-native primary HTF
        # selected by services.mtf_policy. ``htf_mult`` is retained only for
        # explicit legacy research tests. Damp conviction ×``htf_damp`` when
        # the native primary trend disagrees. Measured on the seeded synthetic
        # regime grid (drift × vol × seeds, pessimistic fills, tune + holdout)
        # before defaulting ON:
        #     tune:    off +211.1R (559 trades) -> damp +240.1R (494)
        #     holdout: off +268.3R (585 trades) -> damp +282.6R (494)
        # The counter-HTF subset under "off" was net-negative in BOTH suites
        # (-29.0R / -11.8R) — damping removes exactly those entries. Synthetic
        # data, stated honestly; re-measure on real history when available.
        self.htf_mode = htf_mode
        self.htf_mult = int(htf_mult)
        self.htf_damp = float(htf_damp)
        self.allow_legacy_htf_resample = bool(allow_legacy_htf_resample)

    def generate(self, bar: Bar) -> Optional[Signal]:
        p = self.params
        # Keep memory bounded for a 24/7 engine (rolling window of bars).
        if len(self.bars) > self.max_history:
            del self.bars[:-self.max_history]

        closes = [b.close for b in self.bars]
        need = max(p["slow"], p["trend"], p["rsi_period"]) + 4
        if len(closes) < need:
            return None

        ef_series = ema(closes, p["fast"])
        es = ema(closes, p["slow"])[-1]
        et = ema(closes, p["trend"])[-1]
        ef = ef_series[-1]
        ef_prev = ef_series[-4]
        r = rsi(closes, p["rsi_period"])
        price = closes[-1]
        regime = self._regime.detect(self.bars)

        # --- four independent reads, each a vote in [-1, +1] ---
        v_trend = _clip((ef - es) / (es * 0.004)) if es else 0.0
        v_filter = 1.0 if price > et else -1.0
        v_slope = _clip((ef - ef_prev) / (ef_prev * 0.003)) if ef_prev else 0.0
        v_rsi = _clip((r - 50.0) / 20.0)
        # fade overbought/oversold extremes (mean-reversion guard)
        if r > 78:
            v_rsi = min(v_rsi, 0.2)
        elif r < 22:
            v_rsi = max(v_rsi, -0.2)

        if self.er_mode != "off" and len(closes) >= 22:
            # Kaufman efficiency ratio: |net move| / path length over 20 bars —
            # a direct trendiness read (1 = clean trend, 0 = pure chop)
            net = closes[-1] - closes[-21]
            path = sum(abs(closes[j] - closes[j - 1]) for j in range(-20, 0))
            v_er = _clip((net / path) * 2.5) if path else 0.0
        else:
            v_er = None

        if v_er is not None and self.er_mode == "replace":
            score = (self.W_TREND * v_trend + self.W_FILTER * v_filter
                     + self.W_SLOPE * v_er + self.W_RSI * v_rsi)
        elif v_er is not None and self.er_mode == "add":
            score = (0.26 * v_trend + 0.22 * v_filter + 0.16 * v_slope
                     + 0.20 * v_rsi + 0.16 * v_er)
        else:
            score = (self.W_TREND * v_trend + self.W_FILTER * v_filter
                     + self.W_SLOPE * v_slope + self.W_RSI * v_rsi)

        factor = _REGIME_FACTOR.get(regime.name, 0.45)
        conviction = abs(score) * factor
        if self.volume_conf and len(self.bars) >= 21:
            vols = [b.volume for b in self.bars[-21:-1]]
            avg = sum(vols) / len(vols) if vols else 0.0
            ratio = (self.bars[-1].volume / avg) if avg > 0 else 1.0
            conviction *= max(0.9, min(1.1, 0.9 + 0.2 * (ratio - 0.5)))

        side = 1.0 if score > 0 else -1.0

        # True multi-timeframe confirmation: production consumes only the
        # provider-native primary HTF chosen by services.mtf_policy. The old
        # relative ``htf_mult`` path exists solely for explicit legacy tests.
        # disagrees with the trade direction, damp conviction so only
        # exceptionally strong counter-HTF setups survive the threshold.
        primary = (self._native_mtf_evidence.get("primary") or {})
        primary_tf = primary.get("htf_timeframe")
        native_rows = self._native_mtf_context.get(primary_tf, ()) if primary_tf else ()
        if self.htf_mode == "off":
            v_htf = None
        elif native_rows:
            v_htf = _native_htf_trend_vote(native_rows)
        elif self.allow_legacy_htf_resample:
            v_htf = _htf_trend_vote(self.bars, self.htf_mult)
        else:
            v_htf = None
        if v_htf is not None and v_htf * side < 0:
            conviction *= self.htf_damp

        if factor == 0.0 or conviction < p["conviction_threshold"]:
            return None  # the brain decides NOT to trade

        # Alignment gate: the medium-term trend (fast vs slow EMA) and the
        # long-trend filter (price vs long EMA) must AGREE with the trade
        # direction, and momentum must not be pushing the other way. Testing
        # showed most losers came from counter-trend entries where a hot RSI
        # vote outshouted a disagreeing trend — this removes that failure mode.
        if v_trend * side <= 0 or v_filter * side <= 0:
            return None  # never trade against the trend reads
        if v_slope * side < -0.25:
            return None  # momentum actively disagrees — wait

        direction = SignalType.LONG if score > 0 else SignalType.SHORT
        reason = self._explain(direction, ef, es, et, price, r, regime, conviction)
        signal = self._signal(bar, direction, reason)
        if signal is not None:
            signal.confidence = round(_clip(conviction, 0.0, 1.0), 3)
            signal.regime = regime.name   # lets the learning loop study losses per regime
            signal.brain_score = round(score, 3)
            # REAL decision context for the trade journal — the exact reads and
            # market values this decision was made on (never fabricated).
            a = atr(self.bars, 14)
            avg_vol = (sum(b.volume for b in self.bars[-21:-1]) / 20
                       if len(self.bars) >= 21 else 0.0)
            signal.snapshot = {
                "price": round(price, 6), "ema_fast": round(ef, 6), "ema_slow": round(es, 6),
                "ema_trend": round(et, 6), "rsi": round(r, 1), "atr": round(a, 6),
                "atr_pct": round(a / price * 100, 3) if price else 0.0,
                "regime": regime.name, "trend_direction": "up" if price > et else "down",
                "volume": round(self.bars[-1].volume, 2), "avg_volume_20": round(avg_vol, 2),
                "volatility": getattr(regime, "volatility", None),
                "htf_trend": ("up" if v_htf == 1.0 else "down" if v_htf == -1.0 else None),
                "mtf_evidence": dict(self._native_mtf_evidence),
            }
            sd = 1.0 if score > 0 else -1.0
            def _st(agree, weak=False):
                return "Passed" if agree else ("Neutral" if weak else "Failed")
            signal.checklist = [
                {"name": "HTF trend filter (price vs EMA50)", "status": _st(v_filter * sd > 0),
                 "detail": f"price {'>' if price > et else '<'} EMA50"},
                {"name": "EMA trend (fast vs slow)", "status": _st(v_trend * sd > 0),
                 "detail": f"EMA{p['fast']} {'>' if ef > es else '<'} EMA{p['slow']}"},
                {"name": "Momentum (EMA slope)", "status": _st(v_slope * sd >= 0, v_slope * sd >= -0.25),
                 "detail": f"slope vote {v_slope:+.2f}"},
                {"name": "RSI confirmation", "status": _st(v_rsi * sd > 0, True),
                 "detail": f"RSI {r:.0f}"},
                {"name": "Regime tradeable", "status": _st(factor > 0),
                 "detail": f"{regime.name} (size ×{factor:.2f})"},
                {"name": "Volume confirmation",
                 "status": ("Passed" if (self.volume_conf and self.bars[-1].volume > avg_vol)
                            else "Neutral" if self.volume_conf else "Not checked"),
                 "detail": "vs 20-bar avg" if self.volume_conf else "not part of this config"},
                {"name": "Efficiency ratio",
                 "status": ("Passed" if self.er_mode != "off" else "Not checked"),
                 "detail": "Kaufman ER" if self.er_mode != "off" else "not part of this config"},
                {"name": f"Native HTF trend ({primary_tf or 'unavailable'})",
                 "status": ("Not checked" if self.htf_mode == "off"
                            else "Neutral" if v_htf is None
                            else _st(v_htf * sd > 0)),
                 "detail": ("not part of this config" if self.htf_mode == "off"
                            else "native closed-candle context unavailable" if v_htf is None
                            else f"HTF {'up' if v_htf > 0 else 'down'} vs "
                                 f"{'long' if sd > 0 else 'short'}"
                                 + ("" if v_htf * sd > 0 else f" — conviction ×{self.htf_damp}"))},
            ]
        return signal

    def _explain(self, direction, ef, es, et, price, r, regime, conviction) -> str:
        d = "LONG" if direction == SignalType.LONG else "SHORT"
        reads = [
            f"EMA{self.params['fast']}{'>' if ef > es else '<'}EMA{self.params['slow']}",
            f"price{'>' if price > et else '<'}EMA{self.params['trend']}",
            f"RSI {r:.0f}",
            f"regime {regime.name}",
        ]
        return f"{d} conviction {conviction:.0%} — " + "; ".join(reads)
