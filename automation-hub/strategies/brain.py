"""TradeBrain — pre-trade decision quality engine.

Pure, stdlib-only and fully testable (no FastAPI, no I/O). Given the bars up to
a candidate entry, it answers one question well: *is this setup actually worth
taking?* It returns a 0–100 quality score broken down by component, the market
regime, the higher-timeframe bias, the rule pass/fail checklist, and any hard
block reasons.

It deliberately reuses the engine's existing `RegimeDetector` and the same
indicator library the strategies use, so the simulator and the live pipeline
reason the same way. The brain does NOT generate signals — the strategy's entry
rules still decide *where*; the brain decides *whether the location is good*.

Scoring (components sum to 100, before the losing-streak penalty):

    htf_alignment   22   higher-timeframe trend agrees with the trade side
    regime_fit      18   regime suits the setup (trend vs reversal)
    rr_quality      14   reward:risk is realistic (>=2 great, <1 blocked)
    stop_safety     10   stop is neither absurdly tight nor wide
    volatility      10   ATR% sits in a tradeable band
    momentum        12   RSI supports the side without being exhausted
    structure        8   price is on the right side of the structural EMA
    volume           6   volume confirms participation

Hard blocks (taken regardless of score, each with a reason):
    * reward:risk below 1.0
    * stop too tight / too wide (capital-protection thresholds)
    * volatility far too low to trade
    * strong higher-timeframe trend against a non-reversal trade
    * choppy / unclear regime for a non-reversal trade
    * losing-streak cooldown
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from bot.data.indicators import atr, ema, rsi
from bot.types import Bar
from services.regime import RegimeDetector

# Rule types that express a mean-reversion / reversal intent. For these the
# brain relaxes trend and regime gating (they are *meant* to fade a move).
REVERSAL_RULES = {"liquidity_sweep", "support_bounce", "choch", "fair_value_gap"}


@dataclass
class BrainConfig:
    min_rr: float = 1.0              # hard block below this reward:risk
    good_rr: float = 2.0            # full RR score at/above this
    min_stop_pct: float = 0.0005    # 0.05% — tighter = oversize risk (block)
    max_stop_pct: float = 0.25      # 25%  — wider = likely bad data (block)
    min_atr_pct: float = 0.0008     # below this the market is too dead (block)
    htf_factor: int = 4            # bars per higher-timeframe candle
    htf_fast: int = 8
    htf_slow: int = 21
    structure_ema: int = 50
    sr_lookback: int = 20          # bars for nearest support/resistance
    streak_penalty_at: int = 3      # start penalising after N losses in a row
    streak_block_at: int = 5        # block entirely at N losses in a row
    strong_trend_er: float = 0.45   # efficiency ratio that counts as "strong"


@dataclass
class BrainVerdict:
    allowed: bool
    score: int
    regime: str
    htf_bias: str                  # "bullish" | "bearish" | "neutral"
    setup_type: str                # "trend" | "reversal"
    components: dict = field(default_factory=dict)
    passed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    blocks: list = field(default_factory=list)

    @property
    def grade(self) -> str:
        if not self.allowed:
            return "blocked"
        return "high" if self.score >= 80 else "acceptable" if self.score >= 60 else "weak"

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed, "score": self.score, "grade": self.grade,
            "regime": self.regime, "htf_bias": self.htf_bias, "setup_type": self.setup_type,
            "components": self.components, "passed": self.passed, "failed": self.failed,
            "blocks": self.blocks,
        }


# ----------------------------------------------------------------- HTF helper
def aggregate_htf(bars: Sequence[Bar], factor: int) -> list[Bar]:
    """Legacy research helper; never used by REAL_PAPER votes.

    Groups are aligned to the END of the series so the latest HTF candle always
    closes on the latest base bar — a real multi-timeframe view, no lookahead.
    """
    if factor <= 1 or len(bars) < factor:
        return list(bars)
    out: list[Bar] = []
    n = len(bars)
    start = n % factor  # drop the oldest partial group, keep groups aligned to the end
    for j in range(start, n, factor):
        grp = bars[j:j + factor]
        if len(grp) < factor:
            break
        out.append(Bar(
            timestamp=grp[-1].timestamp, open=grp[0].open,
            high=max(b.high for b in grp), low=min(b.low for b in grp),
            close=grp[-1].close, volume=sum(b.volume for b in grp),
        ))
    return out


def htf_bias(bars: Sequence[Bar], cfg: BrainConfig, *,
             native_bars: Sequence[Bar] | None = None,
             allow_legacy_resample: bool = False) -> tuple[str, float]:
    """Higher-timeframe trend from an independently sourced native series.

    ``allow_legacy_resample`` is an explicit test/research escape hatch. The
    REAL_PAPER path never enables it and therefore cannot derive HTF from LTF.
    """
    if native_bars is not None:
        htf = list(native_bars)
    elif allow_legacy_resample:
        htf = aggregate_htf(bars, cfg.htf_factor)
    else:
        return "neutral", 0.0
    closes = [b.close for b in htf]
    if len(closes) < cfg.htf_slow + 1:
        return "neutral", 0.0
    fast = ema(closes, cfg.htf_fast)[-1]
    slow = ema(closes, cfg.htf_slow)[-1]
    seg = closes[-(cfg.htf_slow + 1):]
    net = abs(seg[-1] - seg[0])
    path = sum(abs(seg[k] - seg[k - 1]) for k in range(1, len(seg))) or 1e-9
    strength = net / path
    if fast > slow:
        return "bullish", strength
    if fast < slow:
        return "bearish", strength
    return "neutral", strength


# ----------------------------------------------------------------- the brain
class TradeBrain:
    def __init__(self, config: BrainConfig | None = None):
        self.cfg = config or BrainConfig()
        self.detector = RegimeDetector()

    def evaluate(self, bars: Sequence[Bar], i: int, *, side: str,
                 entry: float, stop: float, target: float,
                 reversal: bool = False, recent_losses: int = 0,
                 native_htf_bars: Sequence[Bar] | None = None,
                 require_native_htf: bool = False,
                 allow_legacy_htf_resample: bool = True) -> BrainVerdict:
        cfg = self.cfg
        window = list(bars[:i + 1])
        closes = [b.close for b in window]
        risk_abs = abs(entry - stop)
        reward_abs = abs(target - entry)
        rr = reward_abs / risk_abs if risk_abs > 0 else 0.0
        setup_type = "reversal" if reversal else "trend"

        regime = self.detector.detect(window)
        bias, strength = htf_bias(
            window, cfg, native_bars=native_htf_bars,
            allow_legacy_resample=(allow_legacy_htf_resample and not require_native_htf),
        )

        comp: dict = {}
        passed: list = []
        failed: list = []
        blocks: list = []

        if require_native_htf and not native_htf_bars:
            blocks.append("native primary HTF context unavailable")

        # ---- hard blocks (capital protection / nonsensical setups) ----
        if rr < cfg.min_rr:
            blocks.append(f"reward:risk {rr:.2f} below {cfg.min_rr:.1f}")
        stop_pct = (risk_abs / entry) if entry else 0.0
        if stop_pct < cfg.min_stop_pct:
            blocks.append(f"stop too tight ({stop_pct*100:.3f}%) — oversize risk")
        elif stop_pct > cfg.max_stop_pct:
            blocks.append(f"stop too wide ({stop_pct*100:.1f}%) — likely bad setup")
        if regime.atr_pct < cfg.min_atr_pct:
            blocks.append(f"volatility too low (ATR {regime.atr_pct*100:.2f}%)")
        if recent_losses >= cfg.streak_block_at:
            blocks.append(f"losing-streak cooldown ({recent_losses} in a row)")

        against_htf = (side == "long" and bias == "bearish") or (side == "short" and bias == "bullish")
        if against_htf and strength >= cfg.strong_trend_er and not reversal:
            blocks.append(f"against strong higher-timeframe {bias} trend")
        if regime.name in ("Ranging", "Extreme Volatility") and not reversal:
            blocks.append(f"{regime.name.lower()} / unclear regime for a trend setup")

        # ---- component scores (0..weight) ----
        # higher-timeframe alignment (22)
        with_htf = (side == "long" and bias == "bullish") or (side == "short" and bias == "bearish")
        if reversal:
            htf_pts = 22 if not with_htf else 14  # reversals prefer to fade the HTF
        elif with_htf:
            htf_pts = 12 + 10 * min(1.0, strength / cfg.strong_trend_er)
        elif bias == "neutral":
            htf_pts = 11
        else:
            htf_pts = 4 * (1 - min(1.0, strength / cfg.strong_trend_er))
        comp["htf_alignment"] = round(htf_pts, 1)
        (passed if htf_pts >= 11 else failed).append(
            f"HTF {bias} ({strength:.2f}) vs {side}")

        # regime fit (18)
        regime_pts = {
            "Trending": 18 if not reversal else 8,
            "High Volatility": 12,
            "Ranging": 16 if reversal else 4,
            "Low Volatility": 7,
            "Extreme Volatility": 5,
        }.get(regime.name, 8)
        comp["regime_fit"] = float(regime_pts)
        (passed if regime_pts >= 10 else failed).append(f"regime {regime.name}")

        # reward:risk quality (14)
        rr_pts = 14 * min(1.0, rr / cfg.good_rr) if rr >= cfg.min_rr else 0.0
        comp["rr_quality"] = round(rr_pts, 1)
        (passed if rr >= 1.5 else failed).append(f"reward:risk {rr:.2f}")

        # stop safety (10) — best in a sane mid-band
        if cfg.min_stop_pct <= stop_pct <= cfg.max_stop_pct:
            safe = 1.0 - min(1.0, abs(stop_pct - 0.012) / 0.05)  # sweet spot ~1.2%
            stop_pts = 5 + 5 * max(0.0, safe)
        else:
            stop_pts = 0.0
        comp["stop_safety"] = round(stop_pts, 1)
        (passed if stop_pts >= 5 else failed).append(f"stop {stop_pct*100:.2f}%")

        # volatility band (10)
        ap = regime.atr_pct
        if 0.004 <= ap <= 0.03:
            vol_pts = 10.0
        elif ap < 0.004:
            vol_pts = 10 * max(0.0, ap / 0.004)
        else:
            vol_pts = 10 * max(0.0, 1 - (ap - 0.03) / 0.03)
        comp["volatility"] = round(vol_pts, 1)
        (passed if vol_pts >= 5 else failed).append(f"ATR {ap*100:.2f}%")

        # momentum (12) — RSI supports the side, not exhausted
        r = rsi(closes, 14)
        if side == "long":
            mom = 12 * _band(r, lo=50, hi=70, hard_lo=40, hard_hi=82)
        else:
            mom = 12 * _band(100 - r, lo=50, hi=70, hard_lo=40, hard_hi=82)
        comp["momentum"] = round(mom, 1)
        (passed if mom >= 6 else failed).append(f"RSI {r:.0f}")

        # structure (8) — price on the right side of the structural EMA
        struct_ema = ema(closes, cfg.structure_ema)[-1] if len(closes) >= cfg.structure_ema else closes[-1]
        on_side = (side == "long" and entry >= struct_ema) or (side == "short" and entry <= struct_ema)
        if reversal:
            on_side = not on_side  # reversals enter against the structural EMA
        struct_pts = 8.0 if on_side else 2.0
        comp["structure"] = struct_pts
        (passed if on_side else failed).append(f"price vs EMA{cfg.structure_ema}")

        # volume confirmation (6)
        vol_pts2 = 0.0
        if i >= 20:
            avg = sum(b.volume for b in bars[i - 20:i]) / 20
            if avg > 0 and bars[i].volume >= avg:
                vol_pts2 = 6.0
            elif avg > 0:
                vol_pts2 = 6 * min(1.0, bars[i].volume / avg)
        comp["volume"] = round(vol_pts2, 1)
        (passed if vol_pts2 >= 3 else failed).append("volume vs 20-bar avg")

        # distance to nearest support/resistance — folded into structure note,
        # and penalises entering right under resistance (long) / above support.
        sr_penalty = self._sr_penalty(bars, i, side, entry, risk_abs, reversal)
        comp["sr_distance"] = -round(sr_penalty, 1)
        if sr_penalty > 0:
            failed.append("too close to opposing S/R")
        else:
            passed.append("clear of opposing S/R")

        raw = sum(v for k, v in comp.items() if k != "sr_distance") - sr_penalty

        # losing-streak penalty (not a weight; trims the final score)
        streak_pen = 0.0
        if recent_losses >= cfg.streak_penalty_at:
            streak_pen = min(15.0, 5.0 * (recent_losses - cfg.streak_penalty_at + 1))
            failed.append(f"losing streak {recent_losses}")
        comp["streak_penalty"] = -round(streak_pen, 1)

        score = int(max(0, min(100, round(raw - streak_pen))))
        allowed = not blocks
        return BrainVerdict(allowed=allowed, score=score, regime=regime.name,
                            htf_bias=bias, setup_type=setup_type, components=comp,
                            passed=passed, failed=failed, blocks=blocks)

    def _sr_penalty(self, bars, i, side, entry, risk_abs, reversal) -> float:
        """Penalty (0..8) for entering with little room before the opposing
        support/resistance — e.g. buying right under resistance."""
        lb = self.cfg.sr_lookback
        if i < lb or risk_abs <= 0 or reversal:
            return 0.0
        prior = bars[i - lb:i]
        if side == "long":
            resistance = max(b.high for b in prior)
            room_r = (resistance - entry) / risk_abs
        else:
            support = min(b.low for b in prior)
            room_r = (entry - support) / risk_abs
        if room_r >= 1.5:
            return 0.0
        if room_r <= 0:
            return 8.0
        return round(8.0 * (1 - room_r / 1.5), 2)


def _band(x: float, *, lo: float, hi: float, hard_lo: float, hard_hi: float) -> float:
    """1.0 inside [lo,hi], ramping to 0 at the hard bounds, 0 outside."""
    if lo <= x <= hi:
        return 1.0
    if x < lo:
        return max(0.0, (x - hard_lo) / (lo - hard_lo)) if lo > hard_lo else 0.0
    return max(0.0, (hard_hi - x) / (hard_hi - hi)) if hard_hi > hi else 0.0


def detect_reversal(spec: dict) -> bool:
    """Infer whether a custom spec expresses reversal/mean-reversion intent."""
    rules = (spec.get("entry") or {}).get("rules") or []
    for r in rules:
        if r.get("type") in REVERSAL_RULES:
            return True
        if r.get("type") == "rsi" and r.get("op") == "below" and float(r.get("value", 50)) <= 35:
            return True
        if r.get("type") == "bollinger" and str(r.get("zone", "")).startswith("below"):
            return True
    return bool(spec.get("reversal"))
