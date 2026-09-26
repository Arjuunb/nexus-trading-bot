"""Custom (user-built) strategies — rule blocks + real-data simulator.

A custom strategy is a JSON spec: entry conditions combined with AND/OR (each rule
optionally NOT-negated), plus stop / take-profit / risk / session / max-trades.
``simulate()`` runs it over REAL historical bars and returns full results (trades
with reasons, metrics, equity curve) — for SIMULATION only, before paper trading.

Supported rule types (only what's actually implemented — no fakes):
  ema_cross   fast EMA above/below slow EMA
  rsi         RSI above/below a threshold
  sma_trend   price above/below SMA(n)  (trend filter)
  macd        MACD line above/below its signal
  breakout    close breaks above/below the prior n-bar high/low (price action)
  volume      volume above/below its n-period average
  atr_filter  ATR% above/below a threshold (volatility filter)
"""
from __future__ import annotations

from typing import Optional

from datetime import timedelta

from bot.data.indicators import atr, ema, rsi
from bot.tradecore.costs import cost_r as _cost_r
from bot.tradecore.rmath import net_r as _net_r

RULE_TYPES = ("ema_cross", "rsi", "sma_trend", "macd", "breakout", "volume", "atr_filter",
              "pullback", "support_bounce", "liquidity_sweep", "fair_value_gap",
              "vwap", "bollinger", "bos", "choch")
WARMUP = 210  # enough history for SMA200 etc.


class _RiskGate:
    """Strategy-agnostic risk manager applied in SIMULATION so the brain-tuning
    risk limits actually take effect (previously only honoured live). Enforces:
      * max_trades_per_day      — cap entries per UTC day
      * max_consecutive_losses  — pause new entries after N losses (resets daily)
      * cooldown_after_loss     — block entries for N minutes after a loss
    Per-day counters reset at the UTC day boundary; the cooldown is time-based
    off the losing trade's exit time.
    """

    def __init__(self, max_per_day=0, cooldown_min=0, max_consec=0,
                 max_daily_loss_r=0.0, max_drawdown_r=0.0):
        self.max_per_day = int(max_per_day or 0)
        self.cooldown_min = int(cooldown_min or 0)
        self.max_consec = int(max_consec or 0)
        self.day = None
        self.day_count = 0
        self.recent_losses = 0
        self.cooldown_until = None
        #: Exit time of the most recent losing trade, for the Decision Brain's
        #: 24-hour losing-streak pause (services/quality_gate.py).
        self.last_loss_at = None
        self.max_daily_loss_r = float(max_daily_loss_r or 0.0)
        self.max_drawdown_r = float(max_drawdown_r or 0.0)
        self.daily_net_r = 0.0
        self.equity_r = 0.0
        self.peak_r = 0.0
        self.halted = False

    def active(self) -> bool:
        return bool(self.max_per_day or self.max_consec or self.cooldown_min
                    or self.max_daily_loss_r or self.max_drawdown_r)

    def _roll(self, ts) -> None:
        d = ts.date()
        if d != self.day:
            # Only the daily entry counter resets at midnight. Production's
            # consecutive-loss guard is based on the latest closed trades and
            # therefore carries across UTC day boundaries until a win/manual
            # resume. Resetting the streak here let research take trades that
            # the deployed risk pipeline would have halted.
            self.day, self.day_count, self.daily_net_r = d, 0, 0.0

    def blocked_reason(self, ts):
        self._roll(ts)
        if self.halted:
            return "max drawdown or consecutive-loss auto-halt"
        if self.max_daily_loss_r and self.daily_net_r <= -self.max_daily_loss_r:
            return "daily loss limit reached"
        if self.max_per_day and self.day_count >= self.max_per_day:
            return "max trades/day reached"
        if self.max_consec and self.recent_losses >= self.max_consec:
            return "max consecutive losses — trading paused"
        if self.cooldown_until is not None and ts < self.cooldown_until:
            return "cooldown after loss"
        return None

    def on_entry(self, ts) -> None:
        self._roll(ts)
        self.day_count += 1

    def on_exit(self, exit_ts, r: float) -> None:
        self._roll(exit_ts)
        self.daily_net_r += r
        self.equity_r += r
        self.peak_r = max(self.peak_r, self.equity_r)
        if r > 0:
            self.recent_losses, self.cooldown_until = 0, None
        else:
            self.recent_losses += 1
            self.last_loss_at = exit_ts
            if self.cooldown_min:
                self.cooldown_until = exit_ts + timedelta(minutes=self.cooldown_min)
        if self.max_consec and self.recent_losses >= self.max_consec:
            self.halted = True
        if (self.max_drawdown_r
                and self.peak_r - self.equity_r >= self.max_drawdown_r):
            self.halted = True


def _brain_streak(gate, now) -> int:
    """The loss streak the Decision Brain sees in a simulation: the same
    24-hour losing-streak pause the live engine applies, on candle time."""
    from services.quality_gate import streak_for_gate
    return streak_for_gate(gate.recent_losses, gate.last_loss_at, now)


# ----------------------------------------------------------------- indicators
def _sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else (sum(values) / len(values) if values else 0.0)


def _macd(closes, fast, slow, signal):
    if len(closes) < slow + signal:
        return 0.0, 0.0
    ef, es = ema(closes, fast), ema(closes, slow)
    macd_series = [a - b for a, b in zip(ef, es)]
    sig = ema(macd_series, signal)
    return macd_series[-1], sig[-1]


# ---- extra indicators for the strategy builder (bounded-window, like the rest) ----
def _wilder_adx(bars, period: int = 14) -> float:
    """Average Directional Index (trend strength, 0..100). Compact Wilder calc
    over the trailing window supplied. Returns 0 when there is not enough data."""
    n = period * 2 + 2
    seg = bars[-n:] if len(bars) > n else bars
    if len(seg) < period + 2:
        return 0.0
    trs, plus_dm, minus_dm = [], [], []
    for k in range(1, len(seg)):
        up = seg[k].high - seg[k - 1].high
        down = seg[k - 1].low - seg[k].low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(seg[k].high - seg[k].low,
                       abs(seg[k].high - seg[k - 1].close),
                       abs(seg[k].low - seg[k - 1].close)))
    atr_s = sum(trs[-period:]) or 1e-9
    pdi = 100 * sum(plus_dm[-period:]) / atr_s
    mdi = 100 * sum(minus_dm[-period:]) / atr_s
    dx = 100 * abs(pdi - mdi) / ((pdi + mdi) or 1e-9)
    return round(dx, 2)


def _obv_rising(bars, lookback: int) -> bool:
    """True when On-Balance Volume is higher than ``lookback`` bars ago."""
    seg = bars[-(lookback + 1):]
    if len(seg) < 2:
        return False
    obv0 = obv = 0.0
    for k in range(1, len(seg)):
        if seg[k].close > seg[k - 1].close:
            obv += seg[k].volume
        elif seg[k].close < seg[k - 1].close:
            obv -= seg[k].volume
        if k == 1:
            obv0 = obv
    return obv > obv0


def _supertrend_bullish(bars, period: int, mult: float) -> bool:
    """Supertrend direction: True = uptrend (price above the ATR band)."""
    n = period * 3 + 2
    seg = bars[-n:] if len(bars) > n else bars
    if len(seg) < period + 2:
        return False
    st_up = True
    lower = upper = None
    for k in range(period, len(seg)):
        a = atr(seg[: k + 1], period)
        mid = (seg[k].high + seg[k].low) / 2
        up_band, low_band = mid + mult * a, mid - mult * a
        upper = up_band if upper is None else (min(up_band, upper) if seg[k - 1].close <= upper else up_band)
        lower = low_band if lower is None else (max(low_band, lower) if seg[k - 1].close >= lower else low_band)
        if seg[k].close > (upper or up_band):
            st_up = True
        elif seg[k].close < (lower or low_band):
            st_up = False
    return st_up


def _rsi_series(closes, period: int) -> list[float]:
    """RSI at each of the trailing points (Wilder), bounded window."""
    n = period * 3 + 2
    seg = closes[-n:] if len(closes) > n else closes
    out = []
    for k in range(period, len(seg) + 1):
        out.append(rsi(seg[:k], period))
    return out


def _swing_seq(bars, i, k, lookback, count):
    """The last ``count`` confirmed swing highs and lows (newest last)."""
    highs, lows = [], []
    for j in range(i - k, max(k, i - lookback) - 1, -1):
        if j - k < 0 or j + k >= len(bars):
            continue
        seg = bars[j - k:j + k + 1]
        mid = bars[j]
        if mid.high == max(b.high for b in seg):
            highs.append(mid.high)
        if mid.low == min(b.low for b in seg):
            lows.append(mid.low)
        if len(highs) >= count and len(lows) >= count:
            break
    return list(reversed(highs[:count])), list(reversed(lows[:count]))


# ----------------------------------------------------------------- rule eval
def _rule(rule: dict, bars, i: int) -> tuple[bool, str]:
    """Evaluate one rule at bar index i. Returns (passed, human reason)."""
    t = rule.get("type")
    window = bars[:i + 1]
    closes = [b.close for b in window]
    p = rule

    if t == "ema_cross":
        f, s = int(p.get("fast", 20)), int(p.get("slow", 50))
        if len(closes) < s + 1:
            return False, ""
        fv, sv = ema(closes, f)[-1], ema(closes, s)[-1]
        up = fv > sv
        ok = up if p.get("dir", "above") == "above" else (not up)
        return ok, f"EMA{f}{'>' if up else '<'}EMA{s}"

    if t == "rsi":
        per = int(p.get("period", 14)); val = float(p.get("value", 50))
        r = rsi(closes, per)
        ok = r > val if p.get("op", "above") == "above" else r < val
        return ok, f"RSI {r:.0f}{'>' if r > val else '<'}{val:.0f}"

    if t == "sma_trend":
        n = int(p.get("period", 200))
        if len(closes) < n:
            return False, ""
        sv = _sma(closes, n)
        above = closes[-1] > sv
        ok = above if p.get("dir", "above") == "above" else (not above)
        return ok, f"price{'>' if above else '<'}SMA{n}"

    if t == "macd":
        m, sig = _macd(closes, int(p.get("fast", 12)), int(p.get("slow", 26)), int(p.get("signal", 9)))
        above = m > sig
        ok = above if p.get("dir", "above") == "above" else (not above)
        return ok, f"MACD{'>' if above else '<'}signal"

    if t == "breakout":
        lb = int(p.get("lookback", 20))
        if i < lb:
            return False, ""
        up = p.get("dir", "up") == "up"
        prior = bars[i - lb:i]
        if up:
            ok = bars[i].close > max(b.high for b in prior)
            return ok, f"break {lb}-bar high"
        ok = bars[i].close < min(b.low for b in prior)
        return ok, f"break {lb}-bar low"

    if t == "volume":
        n = int(p.get("period", 20))
        if i < n:
            return False, ""
        vols = [b.volume for b in bars[i - n:i]]
        avg = sum(vols) / n if n else 0
        above = bars[i].volume > avg
        ok = above if p.get("op", "above") == "above" else (not above)
        return ok, "vol>avg" if above else "vol<avg"

    if t == "atr_filter":
        per = int(p.get("period", 14)); val = float(p.get("value_pct", 4)) / 100.0
        a = atr(window, per)
        pct = a / closes[-1] if closes and closes[-1] else 0
        below = pct < val
        ok = below if p.get("op", "below") == "below" else (not below)
        return ok, f"ATR {pct*100:.1f}%"

    # ---- price action ----
    if t == "pullback":  # bounce off a moving average in the trend's direction
        n = int(p.get("period", 20))
        if len(closes) < n + 1:
            return False, ""
        ev = ema(closes, n)[-1]
        if p.get("dir", "up") == "up":
            return (closes[-1] > ev and bars[i].low <= ev), f"pullback to EMA{n}"
        return (closes[-1] < ev and bars[i].high >= ev), f"pullback to EMA{n}"

    if t == "support_bounce":  # price reacting at a recent support/resistance level
        lb = int(p.get("lookback", 30)); tol = float(p.get("tolerance_pct", 0.5)) / 100.0
        if i < lb:
            return False, ""
        prior = bars[i - lb:i]
        if p.get("dir", "support") == "support":
            lvl = min(b.low for b in prior)
            return (lvl <= bars[i].close <= lvl * (1 + tol)), f"near {lb}-bar support"
        lvl = max(b.high for b in prior)
        return (lvl * (1 - tol) <= bars[i].close <= lvl), f"near {lb}-bar resistance"

    if t == "liquidity_sweep":  # wick beyond a level then reclaim (stop hunt)
        lb = int(p.get("lookback", 20))
        if i < lb:
            return False, ""
        prior = bars[i - lb:i]
        if p.get("dir", "down") == "down":  # sweep lows, reclaim -> bullish
            lo = min(b.low for b in prior)
            return (bars[i].low < lo and bars[i].close > lo), "swept lows + reclaim"
        hi = max(b.high for b in prior)
        return (bars[i].high > hi and bars[i].close < hi), "swept highs + reject"

    if t == "fair_value_gap":  # 3-candle imbalance
        if i < 2:
            return False, ""
        if p.get("dir", "up") == "up":
            return (bars[i - 2].high < bars[i].low), "bullish FVG"
        return (bars[i - 2].low > bars[i].high), "bearish FVG"

    if t == "vwap":  # rolling VWAP filter
        n = int(p.get("period", 20))
        if i < n:
            return False, ""
        seg = bars[i - n:i + 1]
        den = sum(b.volume for b in seg) or 1e-9
        vw = sum(((b.high + b.low + b.close) / 3) * b.volume for b in seg) / den
        above = closes[-1] > vw
        ok = above if p.get("dir", "above") == "above" else (not above)
        return ok, f"price{'>' if above else '<'}VWAP"

    if t == "bollinger":
        n = int(p.get("period", 20)); k = float(p.get("std", 2))
        if len(closes) < n:
            return False, ""
        seg = closes[-n:]
        mid = sum(seg) / n
        sd = (sum((x - mid) ** 2 for x in seg) / n) ** 0.5
        price = closes[-1]
        zone = p.get("zone", "below_lower")
        ok = {
            "below_lower": price < mid - k * sd, "above_upper": price > mid + k * sd,
            "above_mid": price > mid, "below_mid": price < mid,
        }.get(zone, False)
        return ok, f"BB {zone}"

    if t == "bos":  # break of structure (swing pivot)
        k = int(p.get("pivot", 3))
        sh, sl = _last_swings(bars, i, k)
        if p.get("dir", "up") == "up":
            return (sh is not None and closes[-1] > sh), "BOS up"
        return (sl is not None and closes[-1] < sl), "BOS down"

    if t == "choch":  # change of character (reversal break vs trend)
        k = int(p.get("pivot", 3))
        if len(closes) < 52:
            return False, ""
        sh, sl = _last_swings(bars, i, k)
        es = ema(closes, 50)[-1]
        if p.get("dir", "up") == "up":
            return (sh is not None and closes[-1] > sh and closes[-2] < es), "CHoCH up"
        return (sl is not None and closes[-1] < sl and closes[-2] > es), "CHoCH down"

    # ---- extra indicators ----
    if t == "adx":
        per = int(p.get("period", 14)); val = float(p.get("value", 25))
        a = _wilder_adx(window, per)
        strong = a >= val
        ok = strong if p.get("op", "above") == "above" else (not strong)
        return ok, f"ADX {a:.0f}{'≥' if strong else '<'}{val:.0f}"

    if t == "supertrend":
        per = int(p.get("period", 10)); mult = float(p.get("mult", 3))
        up = _supertrend_bullish(window, per, mult)
        ok = up if p.get("dir", "up") == "up" else (not up)
        return ok, f"Supertrend {'up' if up else 'down'}"

    if t == "obv":
        lb = int(p.get("lookback", 20))
        rising = _obv_rising(window, lb)
        ok = rising if p.get("dir", "up") == "up" else (not rising)
        return ok, f"OBV {'rising' if rising else 'falling'}"

    if t == "stoch_rsi":
        per = int(p.get("period", 14)); val = float(p.get("value", 20))
        series = _rsi_series(closes, per)
        if len(series) < per:
            return False, ""
        win = series[-per:]
        lo, hi = min(win), max(win)
        k = 100 * (series[-1] - lo) / ((hi - lo) or 1e-9)
        below = k < val
        ok = below if p.get("op", "below") == "below" else (k > (100 - val))
        return ok, f"StochRSI {k:.0f}"

    if t == "trend":  # market-structure direction from the last two swings
        k = int(p.get("pivot", 3))
        highs, lows = _swing_seq(bars, i, k, int(p.get("lookback", 60)), 2)
        if len(highs) < 2 or len(lows) < 2:
            return False, ""
        up = highs[-1] > highs[-2] and lows[-1] > lows[-2]      # HH + HL
        down = highs[-1] < highs[-2] and lows[-1] < lows[-2]    # LH + LL
        want = p.get("dir", "up")
        ok = up if want == "up" else down
        return ok, f"trend {'up (HH/HL)' if up else 'down (LH/LL)' if down else 'unclear'}"

    if t == "ichimoku":  # price relative to the Ichimoku cloud
        seg = window[-max(54, 54):] if len(window) >= 52 else window
        if len(seg) < 52:
            return False, ""

        def _mid(k):
            s = seg[-k:]
            return (max(b.high for b in s) + min(b.low for b in s)) / 2
        tenkan, kijun, senkou_b = _mid(9), _mid(26), _mid(52)
        senkou_a = (tenkan + kijun) / 2
        top, bot = max(senkou_a, senkou_b), min(senkou_a, senkou_b)
        price = closes[-1]
        above = price > top
        ok = above if p.get("dir", "above") == "above" else (price < bot)
        return ok, f"price {'above' if above else 'below/in'} Ichimoku cloud"

    if t == "order_block":  # retest of the last opposing candle before a structure break
        lb = int(p.get("lookback", 20))
        if i < lb + 2:
            return False, ""
        prior = bars[i - lb:i]
        up = p.get("dir", "up") == "up"
        broke = closes[-1] > max(b.high for b in prior) if up else closes[-1] < min(b.low for b in prior)
        # the order block is the last candle against the break direction
        ob = next((b for b in reversed(prior) if (b.close < b.open) == up), None)
        if ob is None:
            return False, ""
        retest = ob.low <= bars[i].low <= ob.high if up else ob.low <= bars[i].high <= ob.high
        respected = retest and (closes[-1] >= ob.low if up else closes[-1] <= ob.high)
        return (broke or respected) and retest, f"{'bullish' if up else 'bearish'} order block retest"

    if t == "supply_demand":  # base → impulse → return to the zone
        lb = int(p.get("lookback", 30))
        if i < lb + 3:
            return False, ""
        demand = p.get("dir", "demand") == "demand"
        prior = bars[i - lb:i]
        # zone = the recent extreme (base) the impulse left from
        if demand:
            base = min(b.low for b in prior)
            impulse = max(b.high for b in prior) - base > 2 * atr(window, 14)
            near = base <= bars[i].low <= base * 1.01
            return (impulse and near), f"demand zone @ {base:.4g}"
        base = max(b.high for b in prior)
        impulse = base - min(b.low for b in prior) > 2 * atr(window, 14)
        near = base * 0.99 <= bars[i].high <= base
        return (impulse and near), f"supply zone @ {base:.4g}"

    return False, ""


def _last_swings(bars, i, k=3, lookback=60):
    """Most recent swing-high and swing-low pivot values before bar i."""
    sh = sl = None
    stop = max(k, i - lookback)
    for j in range(i - k, stop - 1, -1):
        if j - k < 0:
            break
        seg = bars[j - k:j + k + 1]
        if sh is None and bars[j].high == max(b.high for b in seg):
            sh = bars[j].high
        if sl is None and bars[j].low == min(b.low for b in seg):
            sl = bars[j].low
        if sh is not None and sl is not None:
            break
    return sh, sl


def evaluate(tree: dict, bars, i: int) -> tuple[bool, list[str]]:
    """Evaluate a condition tree {op: AND|OR, rules: [...]}. A rule may carry
    'negate': true (NOT), and may itself be a nested group ({op, rules}) so the
    node-canvas builder can express things like (A AND B) OR C. Returns
    (matched, reasons-for-passing-rules)."""
    op = (tree.get("op") or "AND").upper()
    rules = tree.get("rules") or []
    if not rules:
        return False, []
    results, reasons = [], []
    for r in rules:
        if isinstance(r, dict) and r.get("rules") is not None and r.get("type") is None:
            passed, sub = evaluate(r, bars, i)          # nested group -> recurse
            why = f"({op_join(r)})" if passed else ""
            reasons.extend(sub)
        else:
            passed, why = _rule(r, bars, i)
        if r.get("negate"):
            passed = not passed
            why = f"NOT({why})" if why else why
        results.append(passed)
        if passed and why:
            reasons.append(why)
    matched = all(results) if op == "AND" else any(results)
    return matched, reasons


def op_join(tree: dict) -> str:
    return f" {(tree.get('op') or 'AND')} ".join(
        r.get("type", "group") for r in (tree.get("rules") or []))


# ----------------------------------------------------------------- simulator
def _stop_distance(cfg: dict, entry: float, bars, i: int) -> float:
    if (cfg.get("type") or "atr") == "pct":
        return entry * float(cfg.get("pct", 2)) / 100.0
    a = atr(bars[:i + 1], int(cfg.get("period", 14)))
    return float(cfg.get("mult", 1.5)) * a


def _target_distance(cfg: dict, risk_abs: float, entry: float) -> float:
    if (cfg.get("type") or "rr") == "pct":
        return entry * float(cfg.get("pct", 3)) / 100.0
    return float(cfg.get("rr", 1.5)) * risk_abs


def _in_session(ts, session: dict) -> bool:
    start, end = int(session.get("start", 0)), int(session.get("end", 24))
    return start <= ts.hour < end


def simulate(spec: dict, bars, *, fee: float = 0.0004, slippage: float = 0.0002,
             starting_balance: float = 10_000.0, brain=None, min_score: int = 0,
             mtf_lookup=None, mtf_tfs=None) -> dict:
    """Run a custom strategy spec over historical bars. SIMULATION ONLY.

    When a ``brain`` (TradeBrain) is supplied, each candidate entry is scored;
    setups that are blocked or below ``min_score`` are skipped and recorded in
    the returned ``blocked`` log. With no brain the behaviour is unchanged.

    ``spec["exit"]`` may enable improved exits (all optional, default off):
        breakeven_at_r  move the stop to entry once price is +N·risk in profit
        trail_atr       trail the stop by N·ATR once break-even is armed
        time_stop_bars  close at market after N bars if neither stop nor target hit
    """
    side = spec.get("side", "long")
    entry_tree = spec.get("entry") or {"op": "AND", "rules": []}
    stop_cfg = spec.get("stop") or {"type": "atr", "mult": 1.5, "period": 14}
    target_cfg = spec.get("target") or {"type": "rr", "rr": 1.5}
    risk_pct = float(spec.get("risk_per_trade_pct", 0.01))
    max_per_day = int(spec.get("max_trades_per_day", 0) or 0)
    session = spec.get("session")
    exit_cfg = spec.get("exit") or {}
    be_at = float(exit_cfg.get("breakeven_at_r", 0) or 0)
    trail_atr = float(exit_cfg.get("trail_atr", 0) or 0)
    time_stop = int(exit_cfg.get("time_stop_bars", 0) or 0)
    exit_rules = exit_cfg.get("rules") or []          # indicator-exit condition tree
    exit_op = (exit_cfg.get("op") or "OR")
    ai_exit = bool(exit_cfg.get("ai_exit"))           # exit on a reversal (CHoCH against the trade)
    atr_period = int(stop_cfg.get("period", 14))
    reversal = bool(spec.get("reversal")) if brain is None else None
    cost = fee + slippage

    pos = None
    trades: list[dict] = []
    blocked: list[dict] = []
    gate = _RiskGate(max_per_day, spec.get("cooldown_after_loss", 0),
                     spec.get("max_consecutive_losses", 0))

    for i in range(WARMUP, len(bars)):
        bar = bars[i]
        if pos is not None:
            # dynamic stop management (break-even, ATR trail) — opt-in
            if be_at or trail_atr:
                a = atr(bars[:i + 1], atr_period)
                if pos["side"] == "long":
                    fav = bar.high - pos["entry"]
                    if be_at and not pos["be"] and fav >= be_at * pos["risk"]:
                        pos["stop"] = max(pos["stop"], pos["entry"]); pos["be"] = True
                    if trail_atr and pos["be"] and a > 0:
                        pos["stop"] = max(pos["stop"], bar.high - trail_atr * a)
                else:
                    fav = pos["entry"] - bar.low
                    if be_at and not pos["be"] and fav >= be_at * pos["risk"]:
                        pos["stop"] = min(pos["stop"], pos["entry"]); pos["be"] = True
                    if trail_atr and pos["be"] and a > 0:
                        pos["stop"] = min(pos["stop"], bar.low + trail_atr * a)

            exit_px = exit_reason = None
            if pos["side"] == "long":
                if bar.low <= pos["stop"]:
                    exit_px, exit_reason = pos["stop"], ("breakeven" if pos["be"] and pos["stop"] >= pos["entry"] else "stop")
                elif bar.high >= pos["target"]:
                    exit_px, exit_reason = pos["target"], "target"
            else:
                if bar.high >= pos["stop"]:
                    exit_px, exit_reason = pos["stop"], ("breakeven" if pos["be"] and pos["stop"] <= pos["entry"] else "stop")
                elif bar.low <= pos["target"]:
                    exit_px, exit_reason = pos["target"], "target"
            if exit_px is None and time_stop and (i - pos["idx"]) >= time_stop:
                exit_px, exit_reason = bar.close, "time"
            # indicator exit: user-built exit conditions (same blocks/engine) —
            # close at this bar's close if the exit tree fires.
            if exit_px is None and exit_rules:
                hit, _ = evaluate({"op": exit_op, "rules": exit_rules}, bars, i)
                if hit:
                    exit_px, exit_reason = bar.close, "indicator"
            # AI exit suggestion: bail on a change-of-character against the trade.
            if exit_px is None and ai_exit:
                hit, _ = _rule({"type": "choch", "dir": "down" if pos["side"] == "long" else "up"}, bars, i)
                if hit:
                    exit_px, exit_reason = bar.close, "ai-exit"
            if exit_px is not None:
                r = _net_r(pos["entry"], exit_px, pos["risk"], pos["side"],
                           _cost_r(pos["entry"], pos["risk"], cost))
                rec = {
                    "side": pos["side"], "entry": round(pos["entry"], 6), "exit": round(exit_px, 6),
                    "stop": round(pos["stop"], 6), "target": round(pos["target"], 6),
                    "r": round(r, 3), "result": "win" if r > 0 else "loss",
                    "reason": pos["reason"], "exit_reason": exit_reason,
                    "entry_time": pos["time"].isoformat(), "exit_time": bar.timestamp.isoformat(),
                    "bars_held": i - pos["idx"],
                }
                if pos.get("brain"):
                    rec.update(pos["brain"])
                trades.append(rec)
                gate.on_exit(bar.timestamp, r)
                pos = None

        if pos is None:
            if session and not _in_session(bar.timestamp, session):
                continue
            rzn = gate.blocked_reason(bar.timestamp)
            if rzn:
                matched, _ = evaluate(entry_tree, bars, i)
                if matched:        # a setup existed but the risk manager vetoed it
                    blocked.append({"time": bar.timestamp.isoformat(), "side": side,
                                    "score": 0, "regime": "—", "htf_bias": rzn,
                                    "blocks": [rzn], "reason": rzn})
                continue
            matched, reasons = evaluate(entry_tree, bars, i)
            if matched:
                entry_px = bar.close
                risk_abs = _stop_distance(stop_cfg, entry_px, bars, i)
                if risk_abs <= 0:
                    continue
                tgt = _target_distance(target_cfg, risk_abs, entry_px)
                if side == "long":
                    stop, target = entry_px - risk_abs, entry_px + tgt
                else:
                    stop, target = entry_px + risk_abs, entry_px - tgt

                brain_tag = None
                if brain is not None:
                    is_rev = reversal if reversal is not None else _detect_reversal(spec)
                    v = brain.evaluate(bars, i, side=side, entry=entry_px, stop=stop,
                                       target=target, reversal=is_rev,
                                       recent_losses=_brain_streak(gate, bar.timestamp))
                    if not v.allowed or v.score < min_score:
                        blocked.append({
                            "time": bar.timestamp.isoformat(), "side": side,
                            "score": v.score, "regime": v.regime, "htf_bias": v.htf_bias,
                            "blocks": v.blocks or [f"score {v.score} < {min_score}"],
                            "reason": (v.blocks[0] if v.blocks else f"low quality score {v.score}"),
                        })
                        continue
                    brain_tag = {"score": v.score, "grade": v.grade, "regime": v.regime,
                                 "htf_bias": v.htf_bias, "setup_type": v.setup_type,
                                 "passed": v.passed, "failed": v.failed}

                if mtf_lookup is not None:
                    from services.mtf_engine import htf_consensus
                    mtf = htf_consensus(mtf_lookup(i), 1 if side == "long" else -1, mtf_tfs or ())
                    if not mtf["allowed"]:
                        blocked.append({"time": bar.timestamp.isoformat(), "side": side, "score": 0,
                                        "regime": "—", "htf_bias": mtf["reason"],
                                        "blocks": [mtf["reason"]], "reason": mtf["reason"]})
                        continue

                pos = {"side": side, "entry": entry_px, "stop": stop, "target": target,
                       "risk": risk_abs, "reason": "; ".join(reasons) or "entry conditions met",
                       "time": bar.timestamp, "idx": i, "be": False, "brain": brain_tag}
                gate.on_entry(bar.timestamp)

    return _results(trades, starting_balance, risk_pct, bars, blocked=blocked)


def _detect_reversal(spec: dict) -> bool:
    """Local reversal check (avoids a hard import of brain at module load)."""
    from strategies.brain import detect_reversal
    return detect_reversal(spec)


# The quality bar the LIVE engine applies to every entry (HUB_MIN_SCORE default).
# Research judges MUST simulate with this same gate, or they score a different
# system than the one that trades. See services/retune, services/context_brain.
LIVE_MIN_SCORE = 60


def gated_sim(strat, bars, **kw) -> dict:
    """Simulate a strategy through the SAME TradeBrain quality gate the live
    engine enforces — the canonical parity path for retune / context judges."""
    from strategies.brain import TradeBrain
    return simulate_strategy(strat, bars, brain=TradeBrain(),
                             min_score=LIVE_MIN_SCORE, **kw)


def simulate_strategy(strat, bars, *, fee: float = 0.0004, slippage: float = 0.0002,
                      starting_balance: float = 10_000.0, risk_pct: float = 0.01,
                      brain=None, min_score: int = 0, mtf_lookup=None, mtf_tfs=None,
                      max_trades_per_day: int = 0, cooldown_after_loss: int = 0,
                      max_consecutive_losses: int = 0, manage: bool = True,
                      max_daily_loss_pct: float = 0.0,
                      max_drawdown_pct: float = 0.0,
                      apply_confidence_sizing: bool = False,
                      apply_streak_sizing: bool = False,
                      enforce_brain: bool = True,
                      trade_start_at=None,
                      manager=None,
                      entry_mode: str = "market", limit_ttl_bars: int = 3,
                      entry_delay_bars: int = 0,
                      retain_all: bool = False) -> dict:
    """Run a built-in HubStrategy object over historical bars and return results
    in the SAME shape as ``simulate()`` (metrics, equity curve, trades).

    The strategy is fed every bar (to keep indicators warm); a new entry is only
    taken when flat. Entry at the signal bar's close; stop/target from the
    signal — no same-bar fill, no lookahead. When a ``brain`` is supplied, weak
    signals (blocked or below ``min_score``) are skipped and recorded in
    ``blocked`` — so the same quality filter applies to built-in strategies.
    ``manage`` applies the shared TradeManager (break-even / scale-out /
    trailing) — the same mid-trade behavior the live engine uses.
    ``entry_mode='limit'`` simulates maker entries: a limit order rests at the
    signal price for up to ``limit_ttl_bars`` bars; it fills only if price
    trades back through it (at the open when the next bar gaps through), pays
    no slippage on the entry side, and is cancelled unfilled when it expires —
    the honest cost of maker entries is the winners that run away unfilled.
    """
    from bot.types import SignalType
    from services.trade_manager import ManagedTrade, TradeManager
    cost = fee + slippage
    mgr = (manager or TradeManager()) if manage else None
    pos = None
    pending = None                      # resting limit entry (entry_mode='limit')
    missed = 0                          # limit entries that expired unfilled
    trades: list[dict] = []
    blocked: list[dict] = []
    gate = _RiskGate(
        max_trades_per_day, cooldown_after_loss, max_consecutive_losses,
        (max_daily_loss_pct / risk_pct if risk_pct > 0 else 0.0),
        (max_drawdown_pct / risk_pct if risk_pct > 0 else 0.0),
    )

    def _open(side, entry, stop, target, reason, bar, i, entry_cost, meta=None):
        return {"side": side, "entry": entry, "risk": abs(entry - stop),
                "reason": reason, "time": bar.timestamp, "idx": i, "partial": None,
                "entry_cost": entry_cost, "meta": dict(meta or {}),
                "mt": ManagedTrade(side=side, entry=entry, stop=stop,
                                   target=target, risk=abs(entry - stop))}

    def _record_close(position, exit_px, exit_reason, bar, i):
        mt = position["mt"]
        cost_r = _cost_r(position["entry"], position["risk"],
                         position.get("entry_cost", cost), cost)
        if mgr is not None:
            r = mgr.r_multiple(mt, exit_px, position.get("partial"), cost_r=cost_r)
        else:
            r = _net_r(position["entry"], exit_px, position["risk"],
                       position["side"], cost_r)
        sign = 1.0 if position["side"] == "long" else -1.0
        mfe_r = (mt.mfe - position["entry"]) * sign / position["risk"]
        mae_r = (position["entry"] - mt.mae) * sign / position["risk"]
        risk_weight = 1.0
        confidence = position.get("meta", {}).get("signal_confidence")
        if apply_confidence_sizing:
            confidence = float(confidence if confidence is not None else 1.0)
            risk_weight *= 0.5 + 0.5 * max(0.0, min(1.0, confidence))
        if apply_streak_sizing:
            risk_weight *= 0.25 if gate.recent_losses >= 4 else 0.5 if gate.recent_losses >= 2 else 1.0
        trade = {
            "side": position["side"], "entry": round(position["entry"], 6),
            "exit": round(exit_px, 6), "stop": round(mt.stop, 6),
            "target": round(mt.target, 6), "gross_r": round(r + cost_r, 4),
            "cost_r": round(cost_r, 4), "r": round(r, 3),
            "result": "win" if r > 0 else "loss", "reason": position["reason"],
            "exit_reason": exit_reason, "scaled": mt.scaled,
            "entry_time": position["time"].isoformat(),
            "exit_time": bar.timestamp.isoformat(), "bars_held": i - position["idx"],
            "mfe_r": round(mfe_r, 3), "mae_r": round(mae_r, 3),
            "risk_weight": round(risk_weight, 4),
            "weighted_r": round(r * risk_weight, 4),
            **position.get("meta", {}),
        }
        trades.append(trade)
        gate.on_exit(bar.timestamp, r)

    for i, bar in enumerate(bars):
        filled_this_bar = False
        if pending is not None and pos is None:
            if pending.get("delay", 0) > 0:
                pending["delay"] -= 1
                fill = None
                may_fill = False
            else:
                may_fill = True
            # resting maker order: fills only if price trades through it
            fill = None
            if may_fill and pending["side"] == "long":
                if bar.open <= pending["price"]:
                    fill = bar.open                    # gapped through: better fill
                elif bar.low <= pending["price"]:
                    fill = pending["price"]
            elif may_fill:
                if bar.open >= pending["price"]:
                    fill = bar.open
                elif bar.high >= pending["price"]:
                    fill = pending["price"]
            if fill is not None:
                sl, tp = pending["stop"], pending["target"]
                if abs(fill - sl) > 0:
                    pos = _open(pending["side"], fill, sl, tp, pending["reason"],
                                bar, i, entry_cost=fee, meta=pending.get("meta"))
                    gate.on_entry(bar.timestamp)
                    filled_this_bar = True
                pending = None
            elif may_fill:
                pending["ttl"] -= 1
                if pending["ttl"] <= 0:
                    pending = None
                    missed += 1
        # Production's conservative OHLC ordering permits only an adverse stop
        # on the candle where a resting limit fills. A favorable high/low may
        # have happened before the fill and must never be credited. The old
        # simulator skipped the whole candle, overstating results whenever a
        # maker entry and its stop were both inside the same OHLC range.
        if pos is not None and filled_this_bar:
            mt = pos["mt"]
            adverse = bar.low if mt.side == "long" else bar.high
            stop_hit = adverse <= mt.stop if mt.side == "long" else adverse >= mt.stop
            if stop_hit:
                mt.mae = min(mt.mae, adverse) if mt.side == "long" else max(mt.mae, adverse)
                gap = bar.open <= mt.stop if mt.side == "long" else bar.open >= mt.stop
                exit_px = bar.open if gap else mt.stop
                _record_close(pos, exit_px, "stop", bar, i)
                pos = None
        if pos is not None and pos["idx"] != i:   # never exit on the entry bar
            exit_px = exit_reason = None
            mt = pos["mt"]
            if mgr is not None:
                act = mgr.on_bar(mt, bar.high, bar.low, bar.close)
                if act.partial_price is not None:
                    pos["partial"] = act.partial_price
                if act.exit_price is not None:
                    exit_px, exit_reason = act.exit_price, act.exit_reason
                    if exit_reason == "stop":
                        gap = bar.open <= mt.stop if mt.side == "long" else bar.open >= mt.stop
                        if gap:
                            exit_px = bar.open
            elif pos["side"] == "long":
                if bar.low <= mt.stop:
                    exit_px = bar.open if bar.open <= mt.stop else mt.stop
                    exit_reason = "stop"
                elif bar.high >= mt.target:
                    exit_px, exit_reason = mt.target, "target"
            else:
                if bar.high >= mt.stop:
                    exit_px = bar.open if bar.open >= mt.stop else mt.stop
                    exit_reason = "stop"
                elif bar.low <= mt.target:
                    exit_px, exit_reason = mt.target, "target"
            if exit_px is not None:
                _record_close(pos, exit_px, exit_reason, bar, i)
                pos = None

        sig = strat.on_bar(bar)  # always feed: runtime strategies see every closed bar
        if pos is not None and sig is not None:
            desired = "long" if sig.type == SignalType.LONG else "short"
            if desired != pos["side"]:
                # Production treats an opposite strategy signal as a CLOSE,
                # not an instantaneous reversal. Preserve that lifecycle and
                # the taker exit cost; a future signal may open the new side.
                _record_close(pos, sig.entry, "opposite-signal", bar, i)
                pos = None
                sig = None
        if (pos is None and sig is not None
                and (trade_start_at is None or bar.timestamp >= trade_start_at)):
            entry, stop = sig.entry, sig.stop_loss
            risk = abs(entry - stop)
            if risk > 0:
                side = "long" if sig.type == SignalType.LONG else "short"
                rzn = gate.blocked_reason(bar.timestamp)
                if rzn:
                    blocked.append({"time": bar.timestamp.isoformat(), "side": side, "score": 0,
                                    "regime": "—", "htf_bias": rzn, "reason": rzn})
                    continue
                if brain is not None:
                    # The live engine scores the strategy's bounded causal
                    # window (normally 600 bars), not the entire process-age
                    # history. Using the full research array changed HTF bucket
                    # alignment and made the two decision gates diverge.
                    causal = list(getattr(strat, "bars", []) or bars[:i + 1])
                    v = brain.evaluate(causal, len(causal) - 1,
                                       side=side, entry=entry, stop=stop,
                                       target=sig.take_profit,
                                       recent_losses=_brain_streak(gate, bar.timestamp))
                    if enforce_brain and (not v.allowed or v.score < min_score):
                        blocked.append({"time": bar.timestamp.isoformat(), "side": side,
                                        "score": v.score, "regime": v.regime, "htf_bias": v.htf_bias,
                                        "reason": (v.blocks[0] if v.blocks else f"score {v.score} < {min_score}")})
                        continue
                if mtf_lookup is not None:
                    from services.mtf_engine import htf_consensus
                    mtf = htf_consensus(mtf_lookup(i), 1 if side == "long" else -1, mtf_tfs or ())
                    if not mtf["allowed"]:
                        blocked.append({"time": bar.timestamp.isoformat(), "side": side, "score": 0,
                                        "regime": "—", "htf_bias": mtf["reason"], "reason": mtf["reason"]})
                        continue
                reason = getattr(sig, "reason", "") or f"{side} entry"
                meta = {
                    "signal_confidence": getattr(sig, "confidence", None),
                    "signal_regime": getattr(sig, "regime", None),
                    "quality_score": (v.score if brain is not None else None),
                    "quality_regime": (v.regime if brain is not None else None),
                }
                if entry_mode == "limit":
                    pending = {"side": side, "price": entry, "stop": stop,
                               "target": sig.take_profit, "reason": reason,
                               "ttl": max(1, int(limit_ttl_bars)),
                               "delay": max(0, int(entry_delay_bars)), "meta": meta}
                else:
                    pos = _open(side, entry, stop, sig.take_profit, reason,
                                bar, i, entry_cost=cost, meta=meta)
                    gate.on_entry(bar.timestamp)

    out = _results(trades, starting_balance, risk_pct, bars, blocked=blocked,
                   retain_all=retain_all)
    if entry_mode == "limit":
        out["entry_mode"] = "limit"
        out["missed_entries"] = missed
    return out


def _results(trades: list, start: float, risk_pct: float, bars,
             blocked: list | None = None, *, retain_all: bool = False) -> dict:
    rs = [t["r"] for t in trades]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    gp, gl = sum(wins), -sum(losses)
    # equity (R) for drawdown + $ compounding
    eqR = peak = ddR = 0.0
    cw = cl = mcw = mcl = 0
    bal = start
    curve = [{"t": None, "equity": round(start, 2)}]
    for t in trades:
        eqR += t["r"]
        peak = max(peak, eqR)
        ddR = max(ddR, peak - eqR)
        if t["r"] > 0:
            cw += 1; cl = 0; mcw = max(mcw, cw)
        else:
            cl += 1; cw = 0; mcl = max(mcl, cl)
        bal *= (1 + risk_pct * t["r"])
        curve.append({"t": t["exit_time"], "equity": round(bal, 2)})
    # $ max drawdown %
    pe = pp = ddp = 0.0
    eq2 = start
    pp = start
    for c in curve[1:]:
        eq2 = c["equity"]; pp = max(pp, eq2)
        ddp = max(ddp, (pp - eq2) / pp if pp else 0)
    avg_win = (gp / len(wins)) if wins else 0.0
    avg_loss = (gl / len(losses)) if losses else 0.0
    span_days = ((bars[-1].timestamp - bars[WARMUP].timestamp).days) if len(bars) > WARMUP else 0
    # --- extra performance metrics (expectancy / Sharpe / recovery / hold) ---
    net_r = sum(rs)
    expectancy = (net_r / n) if n else 0.0          # average R per trade
    if n >= 2:
        m = net_r / n
        var = sum((r - m) ** 2 for r in rs) / (n - 1)
        sd = var ** 0.5
        sharpe = (m / sd * (n ** 0.5)) if sd > 0 else 0.0   # per-trade Sharpe, annualised by sqrt(N)
    else:
        sharpe = 0.0
    recovery = (net_r / ddR) if ddR > 0 else (net_r if net_r > 0 else 0.0)
    holds = [t.get("bars_held", 0) for t in trades if "bars_held" in t]
    avg_hold = round(sum(holds) / len(holds), 1) if holds else 0.0
    longs = [t["r"] for t in trades if t.get("side") == "long"]
    shorts = [t["r"] for t in trades if t.get("side") == "short"]
    return {
        "simulation": True,
        "data_points": len(bars),
        "total_trades": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "wins": len(wins), "losses": len(losses),
        "profit_factor": round(gp / gl, 2) if gl else (gp and 99.0 or 0.0),
        "net_r": round(net_r, 2),
        "net_pct": round(net_r * risk_pct * 100, 1),
        "max_drawdown_r": round(ddR, 1),
        "max_drawdown_pct": round(ddp * 100, 1),
        "avg_rr": round(avg_win / avg_loss, 2) if avg_loss else 0.0,
        "avg_win_r": round(avg_win, 2), "avg_loss_r": round(-avg_loss, 2),
        "best_r": round(max(rs), 2) if rs else 0.0,
        "worst_r": round(min(rs), 2) if rs else 0.0,
        "max_consecutive_wins": mcw, "max_consecutive_losses": mcl,
        "end_balance": round(bal, 2),
        "span_days": span_days,
        # --- new metrics ---
        "expectancy_r": round(expectancy, 3),
        "sharpe": round(sharpe, 2),
        "recovery_factor": round(recovery, 2),
        "avg_hold_bars": avg_hold,
        "long_trades": len(longs), "short_trades": len(shorts),
        "long_net_r": round(sum(longs), 2), "short_net_r": round(sum(shorts), 2),
        "blocked_count": len(blocked or []),
        "equity_curve": curve,
        "trades": trades if retain_all else trades[-200:],
        "blocked": (blocked or []) if retain_all else (blocked or [])[-200:],
    }


# ----------------------------------------------------------------- validation
def validate(spec: dict, results: dict) -> list[dict]:
    """Honest, simulation-based warnings (the doc's required checks)."""
    out = []

    def warn(level, msg):
        out.append({"level": level, "message": msg})

    risk = float(spec.get("risk_per_trade_pct", 0.01))
    if risk > 0.03:
        warn("danger", f"Risk per trade is {risk*100:.1f}% — above 3% is aggressive and can blow up the account.")
    n = results.get("total_trades", 0)
    if n < 30:
        warn("warning", f"Only {n} trades in simulation — too few to trust; results are not statistically significant.")
    if results.get("max_drawdown_pct", 0) > 30:
        warn("danger", f"Max drawdown is {results['max_drawdown_pct']:.0f}% — very large; most people can't sit through this.")
    if results.get("win_rate", 0) > 60 and results.get("profit_factor", 0) < 1.1:
        warn("warning", "High win rate but weak profit factor — small wins and big losses; one bad trade erases many.")
    if n < 50 and results.get("profit_factor", 0) > 2.5:
        warn("warning", "Very high profit factor on few trades — likely overfit. Validate on out-of-sample data before trusting it.")
    if results.get("profit_factor", 0) < 1 and n > 0:
        warn("danger", "Strategy is unprofitable in simulation (profit factor below 1).")
    if not out and n >= 30:
        warn("ok", "No major red flags in simulation. Validate further with paper trading before any live use.")
    return out


# ----------------------------------------------------------------- describe
def _phrase_rule(r: dict) -> str:
    t, neg = r.get("type"), r.get("negate")
    s = {
        "ema_cross": f"EMA{r.get('fast',20)} is {r.get('dir','above')} EMA{r.get('slow',50)}",
        "rsi": f"RSI({r.get('period',14)}) is {r.get('op','above')} {r.get('value',50)}",
        "sma_trend": f"price is {r.get('dir','above')} the {r.get('period',200)} SMA",
        "macd": f"MACD is {r.get('dir','above')} its signal line",
        "breakout": f"price breaks the {r.get('lookback',20)}-bar {'high' if r.get('dir','up')=='up' else 'low'}",
        "volume": f"volume is {r.get('op','above')} its {r.get('period',20)}-bar average",
        "atr_filter": f"volatility (ATR) is {r.get('op','below')} {r.get('value_pct',4)}%",
        "pullback": f"price pulls back to the {r.get('period',20)} EMA in the {r.get('dir','up')} direction",
        "support_bounce": f"price reacts at the {r.get('lookback',30)}-bar {r.get('dir','support')}",
        "liquidity_sweep": f"price sweeps the {r.get('lookback',20)}-bar {'lows' if r.get('dir','down')=='down' else 'highs'} and reverses",
        "fair_value_gap": f"a {'bullish' if r.get('dir','up')=='up' else 'bearish'} fair-value gap forms",
        "vwap": f"price is {r.get('dir','above')} VWAP({r.get('period',20)})",
        "bollinger": f"price is at the Bollinger {r.get('zone','below_lower').replace('_',' ')} ({r.get('period',20)},{r.get('std',2)})",
        "bos": f"a {r.get('dir','up')} break of structure occurs",
        "choch": f"a {r.get('dir','up')} change of character (reversal) occurs",
        "adx": f"ADX({r.get('period',14)}) is {r.get('op','above')} {r.get('value',25)} (trend strength)",
        "supertrend": f"Supertrend points {r.get('dir','up')}",
        "obv": f"On-Balance Volume is {'rising' if r.get('dir','up')=='up' else 'falling'}",
        "stoch_rsi": f"Stochastic RSI({r.get('period',14)}) is {r.get('op','below')} {r.get('value',20)}",
        "trend": f"market structure is trending {r.get('dir','up')} ({'HH/HL' if r.get('dir','up')=='up' else 'LH/LL'})",
        "ichimoku": f"price is {r.get('dir','above')} the Ichimoku cloud",
        "order_block": f"price retests a {'bullish' if r.get('dir','up')=='up' else 'bearish'} order block",
        "supply_demand": f"price returns to a {r.get('dir','demand')} zone",
    }.get(t, t or "a condition")
    return f"NOT ({s})" if neg else s


def describe(spec: dict) -> str:
    side = spec.get("side", "long")
    entry = spec.get("entry") or {}
    rules = entry.get("rules") or []
    joiner = f" {entry.get('op','AND')} "
    conds = joiner.join(_phrase_rule(r) for r in rules) if rules else "no entry conditions set"
    stop = spec.get("stop") or {"type": "atr", "mult": 1.5}
    target = spec.get("target") or {"type": "rr", "rr": 1.5}
    stop_txt = (f"a {stop.get('mult',1.5)}× ATR stop" if (stop.get('type','atr') == 'atr')
                else f"a {stop.get('pct',2)}% stop")
    tgt_txt = (f"a {target.get('rr',1.5)} risk:reward target" if (target.get('type','rr') == 'rr')
               else f"a {target.get('pct',3)}% target")
    return (f"This strategy enters {side} when {conds}. "
            f"It exits using {stop_txt} and {tgt_txt}, "
            f"risking {float(spec.get('risk_per_trade_pct',0.01))*100:.1f}% of equity per trade.")
