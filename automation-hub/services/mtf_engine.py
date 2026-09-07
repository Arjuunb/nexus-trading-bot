"""Multi-Timeframe Decision Engine.

Combines five timeframes into ONE explainable trade decision — the bot must
agree across timeframes before it will take a trade:

    Weekly  -> macro bias          (direction of the big trend)
    Daily   -> trend confirmation  (agrees with / against the macro)
    4H      -> market structure     (HH/HL = bull, LH/LL = bear)
    15M     -> setup identification (pullback / sweep in the trend direction)
    5M      -> entry trigger        (momentum confirmation)

Rules:
- The higher timeframes (weekly / daily / 4H) must AGREE on a direction. If they
  conflict, no trade — the conflict is reported as a blocker.
- A trade is only "Entry confirmed" when the HTF stack aligns AND a 15M setup AND
  a 5M trigger are present in that direction.
- Every layer returns a plain-English reason, so the decision is explainable.

Pure (`analyze_layers`) so it is fully testable with crafted bars; `analyze`
loads each timeframe real-first via ``get_bars`` and degrades gracefully to
``n/a`` when a timeframe lacks enough history (e.g. weekly before a data sync).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from bot.data.indicators import ema
from bot.types import Bar

TIMEFRAMES = ("1w", "1d", "4h", "15m", "5m")
_DIR = {1: "Bullish", -1: "Bearish", 0: "Neutral", None: "n/a"}


def _ema_bias(closes: Sequence[float], fast: int = 8, slow: int = 21):
    """Return (dir, strength). dir in {1,-1,0,None}; None = not enough data."""
    if len(closes) < slow + 1:
        return None, 0.0
    ef, es = ema(closes, fast)[-1], ema(closes, slow)[-1]
    seg = closes[-(slow + 1):]
    net = abs(seg[-1] - seg[0])
    path = sum(abs(seg[i] - seg[i - 1]) for i in range(1, len(seg))) or 1e-9
    strength = round(net / path, 3)            # 0..1 efficiency ratio
    if ef > es:
        return 1, strength
    if ef < es:
        return -1, strength
    return 0, strength


def _swings(bars: Sequence[Bar], pivot: int = 3):
    """Most recent two swing highs and lows (values), oldest->newest."""
    highs, lows = [], []
    for i in range(pivot, len(bars) - pivot):
        seg = bars[i - pivot:i + pivot + 1]
        if bars[i].high == max(b.high for b in seg):
            highs.append(bars[i].high)
        if bars[i].low == min(b.low for b in seg):
            lows.append(bars[i].low)
    return highs[-2:], lows[-2:]


def _structure_bias(bars: Sequence[Bar], pivot: int = 3):
    """4H structure: higher-high + higher-low = bull; lower-low + lower-high = bear."""
    if len(bars) < 2 * pivot + 5:
        return None, "insufficient 4H data"
    highs, lows = _swings(bars, pivot)
    if len(highs) < 2 or len(lows) < 2:
        return 0, "no clear structure"
    hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
    ll, lh = lows[-1] < lows[-2], highs[-1] < highs[-2]
    if hh and hl:
        return 1, "higher highs + higher lows (bullish structure)"
    if ll and lh:
        return -1, "lower lows + lower highs (bearish structure)"
    return 0, "mixed structure (no clean trend)"


def _setup(bars: Sequence[Bar], side: int, ema_period: int = 20):
    """15M setup: a pullback into the trend (price near the EMA on the trend side)."""
    closes = [b.close for b in bars]
    if len(closes) < ema_period + 2 or side == 0:
        return False, "no setup"
    ev = ema(closes, ema_period)[-1]
    last = bars[-1]
    if side > 0:
        # bullish: price above EMA but recently dipped to/below it (pullback)
        if last.close > ev and last.low <= ev * 1.003:
            return True, "pullback to the 15M EMA in an uptrend"
    else:
        if last.close < ev and last.high >= ev * 0.997:
            return True, "pullback to the 15M EMA in a downtrend"
    return False, "no pullback setup yet"


def _trigger(bars: Sequence[Bar], side: int):
    """5M trigger: a momentum candle in the trade direction (close beyond prior)."""
    if len(bars) < 3 or side == 0:
        return False, "no trigger"
    a, b = bars[-2], bars[-1]
    if side > 0 and b.close > b.open and b.close > a.high:
        return True, "5M bullish momentum candle (close above prior high)"
    if side < 0 and b.close < b.open and b.close < a.low:
        return True, "5M bearish momentum candle (close below prior low)"
    return False, "waiting for a 5M trigger"


@dataclass
class MTFDecision:
    allowed: bool
    side: Optional[str]            # "long" | "short" | None
    trigger_state: str             # Blocked | Waiting for setup | Setup found … | Entry confirmed
    score: int
    layers: dict = field(default_factory=dict)     # per-timeframe verdict + reason
    reasons: list = field(default_factory=list)
    blockers: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "side": self.side, "trigger_state": self.trigger_state,
                "score": self.score, "layers": self.layers,
                "reasons": self.reasons, "blockers": self.blockers}


def analyze_layers(weekly, daily, h4, m15, m5) -> MTFDecision:
    """Combine the five timeframes into one explainable decision. Pure."""
    wk_dir, wk_str = _ema_bias([b.close for b in weekly]) if weekly else (None, 0.0)
    d_dir, d_str = _ema_bias([b.close for b in daily]) if daily else (None, 0.0)
    s_dir, s_reason = _structure_bias(h4) if h4 else (None, "no 4H data")

    layers = {
        "Weekly": {"role": "macro bias", "dir": _DIR[wk_dir], "strength": wk_str},
        "Daily": {"role": "trend confirmation", "dir": _DIR[d_dir], "strength": d_str},
        "4H": {"role": "market structure", "dir": _DIR[s_dir], "reason": s_reason},
        "15M": {"role": "setup", "state": "pending"},
        "5M": {"role": "trigger", "state": "pending"},
    }
    reasons, blockers = [], []

    # higher-timeframe consensus (ignore neutral / n/a)
    htf = [(name, d) for name, d in (("Weekly", wk_dir), ("Daily", d_dir), ("4H", s_dir)) if d in (1, -1)]
    if not htf:
        blockers.append("No higher-timeframe direction (weekly/daily/4H all neutral or unavailable).")
        return MTFDecision(False, None, "Blocked", 0, layers, reasons, blockers)
    dirs = {d for _, d in htf}
    if len(dirs) > 1:
        disagree = ", ".join(f"{n} {_DIR[d]}" for n, d in htf)
        blockers.append(f"Higher-timeframe conflict ({disagree}) — stand aside.")
        return MTFDecision(False, None, "Blocked", 0, layers, reasons, blockers)

    side_int = dirs.pop()
    side = "long" if side_int > 0 else "short"
    for name, d in htf:
        reasons.append(f"{name} {_DIR[d]}")

    score = 0
    if wk_dir == side_int:
        score += 25
    if d_dir == side_int:
        score += 25
    if s_dir == side_int:
        score += 20

    has_setup, setup_reason = _setup(m15, side_int) if m15 else (False, "no 15M data")
    layers["15M"] = {"role": "setup", "state": "found" if has_setup else "waiting", "reason": setup_reason}
    if has_setup:
        score += 15
        reasons.append(setup_reason)

    has_trigger, trig_reason = _trigger(m5, side_int) if m5 else (False, "no 5M data")
    layers["5M"] = {"role": "trigger", "state": "confirmed" if has_trigger else "waiting", "reason": trig_reason}
    if has_trigger:
        score += 15
        reasons.append(trig_reason)

    if not has_setup:
        state = "Waiting for setup"
        allowed = False
    elif not has_trigger:
        state = "Setup found — waiting for 5M trigger"
        allowed = False
    else:
        state = "Entry confirmed"
        allowed = True

    return MTFDecision(allowed, side, state, min(score, 100), layers, reasons, blockers)


_TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}
_HTF_LABEL = {"4h": "4H", "1d": "Daily", "1w": "Weekly"}


def _resample_closes(bars, factor):
    """Closes of complete higher-tf candles (start-aligned). Causal."""
    closes, n, k = [], len(bars), 0
    while (k + 1) * factor <= n:
        closes.append(bars[(k + 1) * factor - 1].close)
        k += 1
    return closes


def _trend_array(closes, fast=5, slow=12):
    """Per-candle trend code (1/-1/0) over a close series. One pass."""
    if len(closes) < slow:
        return [0] * len(closes)
    ef, es = ema(closes, fast), ema(closes, slow)
    return [1 if ef[k] > es[k] else -1 if ef[k] < es[k] else 0 for k in range(len(closes))]


def make_trend_lookup(bars, exec_tf: str, htf_list, *,
                      native_context=None, allow_legacy_resample: bool = False):
    """Precompute higher-timeframe trends so each execution bar can read its
    'last closed higher-tf candle' trend in O(1). Causal (no lookahead).

    Returns ``lookup(i) -> {tf_label: Bullish/Bearish/Neutral/n/a}`` for the
    timeframes in ``htf_list`` that are strictly higher than ``exec_tf``.
    """
    em = _TF_MIN.get(exec_tf)
    series: dict = {}
    if native_context:
        for tf in htf_list:
            rows = list(native_context.get(tf, ()))
            if rows:
                series[tf] = (_trend_array([bar.close for bar in rows]), 1)
    elif em and allow_legacy_resample:
        for tf in htf_list:
            hm = _TF_MIN.get(tf)
            if not hm or hm <= em:
                continue
            factor = hm // em
            series[tf] = (_trend_array(_resample_closes(bars, factor)), factor)

    def lookup(i: int) -> dict:
        out = {}
        for tf, (arr, factor) in series.items():
            closed = ((i + 1) // factor) - 1        # last CLOSED higher-tf candle
            out[tf] = _DIR[arr[closed]] if 0 <= closed < len(arr) else "n/a"
        return out

    lookup.timeframes = list(series)                # the tfs that had enough data
    return lookup


def trends_from_stream(bars, exec_tf: str, *, allow_legacy_resample: bool = False) -> dict:
    """Derive {4H/Daily/Weekly: Bullish/Bearish/Neutral/n/a} from a single
    execution-timeframe bar stream by resampling upward. Used by the live/paper
    adapter so paper trading applies the same higher-timeframe gate as replay."""
    em = _TF_MIN.get(exec_tf)
    out: dict = {}
    if not em or not allow_legacy_resample:
        return out
    for tf in ("4h", "1d", "1w"):
        hm = _TF_MIN[tf]
        if hm <= em:
            continue                      # only timeframes strictly higher than exec
        factor = hm // em
        d, _ = _ema_bias(_resample_closes(bars, factor), 5, 12)
        out[_HTF_LABEL[tf]] = _DIR[d]
    return out


def htf_consensus(trends: dict, side: int, tfs=("Weekly", "Daily", "4H")) -> dict:
    """Gate a candidate trade against the higher-timeframe trends.

    ``trends`` maps timeframe label -> "Bullish"/"Bearish"/"Neutral"/"n/a".
    A trade is blocked only when a directional higher timeframe in ``tfs``
    OPPOSES it — i.e. the bot never trades against the higher-timeframe trend.
    ``tfs`` lets the control center pick which macro/confirmation timeframes gate.
    """
    want = "Bullish" if side > 0 else "Bearish"
    opp = "Bearish" if side > 0 else "Bullish"
    aligned, opposing = [], []
    for tf in tfs:
        d = trends.get(tf)
        if d == want:
            aligned.append(tf)
        elif d == opp:
            opposing.append(tf)
    allowed = not opposing
    side_txt = "long" if side > 0 else "short"
    if opposing:
        reason = f"MTF conflict — {', '.join(f'{t} {opp}' for t in opposing)} oppose {side_txt}"
    elif aligned:
        reason = f"higher-timeframe aligned ({', '.join(f'{t} {want}' for t in aligned)})"
    else:
        reason = "no opposing higher-timeframe trend"
    return {"allowed": allowed, "reason": reason, "aligned": aligned, "opposing": opposing}


def analyze(symbol: str) -> dict:
    """Load each timeframe (real-first via get_bars) and analyse them together."""
    from data.market_data import get_bars
    bars: dict = {}
    sources: dict = {}
    n = {"1w": 120, "1d": 300, "4h": 400, "15m": 300, "5m": 200}
    for tf in TIMEFRAMES:
        rows, src = get_bars(symbol, n=n[tf], timeframe=tf)
        bars[tf] = rows
        sources[tf] = src
    decision = analyze_layers(bars["1w"], bars["1d"], bars["4h"], bars["15m"], bars["5m"])
    out = decision.to_dict()
    out["symbol"] = symbol
    out["data_sources"] = sources
    out["data_available"] = {tf: len(bars[tf]) > 0 for tf in TIMEFRAMES}
    return out
