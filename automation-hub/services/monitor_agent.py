"""AI Monitoring Agent — is the live strategy still the strategy you tested?

A backtest is a claim about behaviour. Live trading is the experiment. This
module is the referee: it compares what a strategy is DOING against what the
same strategy DID in its own backtest, over the same engine, and reports the
gaps.

Design rules, in order of importance:

  * It never modifies a strategy. Every finding carries a ``recommendation``
    that a human performs. ``auto_modify`` is False and there is deliberately
    no code path here that writes a spec.
  * Every finding carries the two numbers that produced it (baseline vs live).
    There is no rule in this file that fires on a feeling.
  * With no baseline, or with too few live trades, it says so. A deviation
    verdict computed from four trades is worse than no verdict, so the
    trade-based checks stay silent until the sample can support them — while
    the checks that DON'T need a trade sample (volatility regime, execution
    slippage) still run, because those are observable immediately.

The baseline is not a stored number that can drift out of sync with the spec —
it is recomputed by running the strategy through the same ``simulate`` path
everything else uses (see ``baseline_from_spec``). That is what makes the
comparison meaningful: both sides came out of one engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

MIN_LIVE_TRADES = 15        # below this, trade-based deviation is noise


@dataclass(frozen=True)
class Thresholds:
    """Every threshold in one place, so the sensitivity of the agent is a
    reviewable object rather than a scatter of magic numbers."""
    win_rate_drop_pts: float = 15.0     # absolute percentage points below baseline
    pf_ratio: float = 0.6               # live PF below this fraction of baseline...
    pf_floor: float = 1.2               # ...and below this absolute value
    drawdown_ratio: float = 1.25        # live DD worse than baseline DD by this factor
    frequency_ratio: float = 0.4        # live trades/30d below this fraction of backtest
    slippage_ratio: float = 1.5         # measured slippage vs the model's assumption
    slippage_floor_bps: float = 0.5     # ...plus an absolute floor, so 0-ish never trips


DEFAULT = Thresholds()


# --------------------------------------------------------------- metric shapes

def _drawdown_r(rs: Sequence[float]) -> float:
    """Peak-to-trough of the cumulative R curve, as a positive magnitude —
    the same definition ``strategies.custom.simulate`` uses."""
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(dd, 2)


def live_metrics(trades: Sequence[dict]) -> dict:
    """Metrics from REAL closed trades, in the same shape a backtest reports.

    ``r`` is read from ``r`` or ``rr`` (paper history uses ``rr``). Trades with
    no R value are excluded rather than counted as zero — an unmeasurable trade
    is missing data, not a break-even."""
    rs = []
    for t in trades:
        v = t.get("r", t.get("rr"))
        if isinstance(v, (int, float)):
            rs.append(float(v))
    n = len(rs)
    if not n:
        return {"total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "net_r": 0.0, "expectancy_r": 0.0, "max_drawdown_r": 0.0}
    wins = [r for r in rs if r > 0]
    gp = sum(wins)
    gl = -sum(r for r in rs if r < 0)
    return {
        "total_trades": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "profit_factor": round(gp / gl, 2) if gl else (99.0 if gp else 0.0),
        "net_r": round(sum(rs), 2),
        "expectancy_r": round(sum(rs) / n, 3),
        "max_drawdown_r": _drawdown_r(rs),
    }


_TS_KEYS = ("closed_at", "opened_at", "exit_time", "entry_time", "ts")


def span_days(trades: Sequence[dict]) -> float:
    """Calendar days between the first and last timestamp in the trade record.

    Used to put live and backtest trade FREQUENCY on the same footing — 20
    trades means something different over a week than over a year. Returns 0.0
    when the record carries no parseable timestamps, and the frequency check
    then stays silent rather than guessing a span."""
    from datetime import datetime, timezone
    stamps: list[datetime] = []
    for t in trades:
        for k in _TS_KEYS:
            v = t.get(k)
            if not isinstance(v, str) or not v:
                continue
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                continue
            # The ledger writes naive stamps and some sources write aware ones;
            # normalise to naive UTC so a mixed record can still be subtracted.
            stamps.append(dt.astimezone(timezone.utc).replace(tzinfo=None)
                          if dt.tzinfo else dt)
            break
    if len(stamps) < 2:
        return 0.0
    delta = max(stamps) - min(stamps)
    return round(max(delta.total_seconds() / 86400.0, 0.0), 2)


def _f(d: Optional[dict], key: str, default: float = 0.0) -> float:
    try:
        v = (d or {}).get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _per_30d(trades: float, span_days: float) -> Optional[float]:
    if not span_days or span_days <= 0:
        return None
    return round(trades / span_days * 30.0, 2)


# ------------------------------------------------------------------ volatility

def atr_pct_band(bars: Sequence[Any], period: int = 14) -> Optional[dict]:
    """ATR as a percentage of price across a window -> {p10, median, p90}.

    This is what "the volatility the strategy was tested in" actually means:
    not one number, a distribution. Returns None when the window is too short
    to describe a distribution at all."""
    try:
        from bot.data.indicators import atr
    except Exception:  # noqa: BLE001 — indicator import is optional here
        return None
    vals: list[float] = []
    for i in range(period + 1, len(bars)):
        window = bars[: i + 1]
        close = float(getattr(window[-1], "close", 0) or 0)
        if close <= 0:
            continue
        try:
            a = atr(window, period)
        except Exception:  # noqa: BLE001
            continue
        if a:
            vals.append(a / close * 100.0)
    if len(vals) < 20:
        return None
    vals.sort()
    def _pct(p: float) -> float:
        return round(vals[min(len(vals) - 1, int(len(vals) * p))], 3)
    return {"p10": _pct(0.10), "median": _pct(0.50), "p90": _pct(0.90),
            "samples": len(vals)}


# --------------------------------------------------------------------- checks

def _performance(base: dict, live: dict, t: Thresholds) -> list[dict]:
    out: list[dict] = []
    b_wr, l_wr = _f(base, "win_rate"), _f(live, "win_rate")
    if b_wr - l_wr >= t.win_rate_drop_pts:
        out.append({
            "key": "win-rate-deviation", "severity": "warning",
            "title": "Win rate is below the backtested rate",
            "detail": (f"Live is winning {l_wr}% of trades against {b_wr}% in the "
                       f"backtest — {round(b_wr - l_wr, 1)} points lower."),
            "evidence": [{"stat": "win rate (backtest -> live)", "value": f"{b_wr}% -> {l_wr}%"},
                         {"stat": "live sample", "value": f"{int(_f(live, 'total_trades'))} closed trades"}],
            "recommendation": ("Check whether the market regime matches the backtest window "
                               "before changing anything — a win-rate gap this size is often "
                               "regime, not a broken rule."),
        })

    b_pf, l_pf = _f(base, "profit_factor"), _f(live, "profit_factor")
    if b_pf > 0 and l_pf < b_pf * t.pf_ratio and l_pf < t.pf_floor:
        out.append({
            "key": "profit-factor-deviation", "severity": "critical" if l_pf < 1 else "warning",
            "title": "Profit factor has deviated from historical behaviour",
            "detail": (f"Live profit factor is {l_pf} against {b_pf} in the backtest."
                       + (" Below 1.0 means live trading is currently losing money."
                          if l_pf < 1 else "")),
            "evidence": [{"stat": "profit factor (backtest -> live)", "value": f"{b_pf} -> {l_pf}"},
                         {"stat": "live net R", "value": f"{_f(live, 'net_r')}R"}],
            "recommendation": ("Review the losing trades in the journal against the entry rules. "
                               "Consider pausing the strategy while you review — do not re-tune "
                               "parameters on a live sample this small."),
        })

    b_ex, l_ex = _f(base, "expectancy_r"), _f(live, "expectancy_r")
    if b_ex > 0 and l_ex < 0:
        out.append({
            "key": "expectancy-negative", "severity": "critical",
            "title": "Live expectancy is negative where the backtest was positive",
            "detail": (f"Each live trade is averaging {l_ex}R; the backtest averaged "
                       f"{b_ex}R per trade."),
            "evidence": [{"stat": "expectancy R (backtest -> live)", "value": f"{b_ex} -> {l_ex}"}],
            "recommendation": "Pause and review before adding further risk.",
        })
    return out


def _drawdown(base: dict, live: dict, t: Thresholds) -> list[dict]:
    b_dd, l_dd = abs(_f(base, "max_drawdown_r")), abs(_f(live, "max_drawdown_r"))
    if b_dd > 0 and l_dd > b_dd * t.drawdown_ratio:
        return [{
            "key": "drawdown-exceeds-backtest", "severity": "critical",
            "title": "Drawdown is deeper than the backtest ever produced",
            "detail": (f"Live drawdown has reached {l_dd}R against a worst backtest "
                       f"drawdown of {b_dd}R. The strategy is outside the range the "
                       "historical test explored."),
            "evidence": [{"stat": "max drawdown R (backtest -> live)", "value": f"{b_dd} -> {l_dd}"},
                         {"stat": "exceeded by", "value": f"{round(l_dd - b_dd, 2)}R"}],
            "recommendation": ("Reduce size or pause. A drawdown beyond the tested range means "
                               "the backtest no longer bounds your downside."),
        }]
    return []


def _frequency(base: dict, live: dict, t: Thresholds) -> list[dict]:
    b_rate = _per_30d(_f(base, "total_trades"), _f(base, "span_days"))
    l_rate = _per_30d(_f(live, "total_trades"), _f(live, "span_days"))
    if b_rate and l_rate is not None and b_rate > 0 and l_rate < b_rate * t.frequency_ratio:
        return [{
            "key": "trade-frequency-low", "severity": "warning",
            "title": "The strategy is trading far less than it did in backtest",
            "detail": (f"Live is taking {l_rate} trades per 30 days against {b_rate} "
                       "in the backtest. Either the market stopped producing these "
                       "setups, or something upstream is filtering them out."),
            "evidence": [{"stat": "trades per 30d (backtest -> live)", "value": f"{b_rate} -> {l_rate}"}],
            "recommendation": ("Check the decision log for blocked setups — a quality filter or "
                               "session window is the usual cause before you suspect the rules."),
        }]
    return []


def _volatility(band: Optional[dict], current: Optional[float]) -> list[dict]:
    if not band or current is None:
        return []
    cur = round(float(current), 3)
    lo, hi = band.get("p10"), band.get("p90")
    if lo is None or hi is None:
        return []
    if cur > hi:
        where, sev = "above", "warning"
        detail = (f"Current volatility is {cur}% ATR against a backtest range of "
                  f"{lo}%–{hi}%. Stops sized for the tested range will be hit more often.")
    elif cur < lo:
        where, sev = "below", "info"
        detail = (f"Current volatility is {cur}% ATR against a backtest range of "
                  f"{lo}%–{hi}%. Targets sized for the tested range may not be reached.")
    else:
        return []
    return [{
        "key": "volatility-out-of-range", "severity": sev,
        "title": f"Current volatility is {where} the historical backtest range",
        "detail": detail,
        "evidence": [{"stat": "current ATR %", "value": f"{cur}%"},
                     {"stat": "backtest ATR % range (p10-p90)", "value": f"{lo}% - {hi}%"},
                     {"stat": "backtest median ATR %", "value": f"{band.get('median')}%"}],
        "recommendation": ("Results in this regime are not covered by the backtest. Treat live "
                           "performance as out-of-sample until volatility returns to range."),
    }]


def _execution(execution: Optional[dict], t: Thresholds) -> list[dict]:
    cal = (execution or {}).get("model_calibration")
    if not cal:
        return []
    measured, assumed = _f(cal, "measured_bps"), _f(cal, "assumed_bps")
    if measured <= assumed * t.slippage_ratio + t.slippage_floor_bps:
        return []
    return [{
        "key": "slippage-increased", "severity": "warning",
        "title": "Execution slippage is worse than the fill model assumes",
        "detail": (f"Measured taker slippage is {measured} bps against the model's "
                   f"{assumed} bps. Every backtest run with this model is therefore "
                   "optimistic by the difference."),
        "evidence": [{"stat": "measured taker slippage", "value": f"{measured} bps"},
                     {"stat": "fill model assumes", "value": f"{assumed} bps"},
                     {"stat": "taker fills measured", "value": str((execution or {}).get("taker", {}).get("fills", 0))}],
        "recommendation": ("Raise the fill model's slippage to the measured value and re-run the "
                           "backtest — the strategy's real edge is smaller than the current "
                           "numbers show."),
    }]


# ------------------------------------------------------------------ the agent

_SEV_RANK = {"critical": 0, "warning": 1, "info": 2}


def _attribution(att: Optional[dict]) -> list[dict]:
    """Say so when the monitor cannot see this strategy's trades.

    Comparing a strategy's backtest against the POOLED history of every
    strategy is not a weaker comparison, it is a different one: it reported
    a 36.75R live drawdown against a 4.3R backtest for a strategy whose own
    backtest took one trade in a year. The 36.75R belonged to 2,804 trades it
    never made.

    Scoping that comparison correctly is the fix, but silence is not an
    acceptable result of it. A monitor with nothing to look at looks exactly
    like a monitor reporting all-clear, so when trades exist and none of them
    carry this strategy's id, that is itself the finding.
    """
    if not att:
        return []
    scoped, pooled = int(_f(att, "scoped")), int(_f(att, "pooled"))
    if scoped or not pooled:
        return []
    return [{
        "key": "monitor-unattributed-trades", "severity": "warning",
        "title": "Monitoring is blind: no closed trade carries this strategy's id",
        "detail": (f"The ledger holds {pooled} closed paper trades and none of them "
                   f"are attributed to this strategy, so there is nothing to compare "
                   f"against its backtest. This is not an all-clear -- it is the "
                   f"monitor reporting that it cannot see."),
        "evidence": [{"stat": "closed trades in ledger", "value": str(pooled)},
                     {"stat": "attributed to this strategy", "value": str(scoped)},
                     {"stat": "strategy id", "value": str(att.get("strategy_id") or "(none)")}],
        "recommendation": ("Trades are written without a strategy id, so no per-strategy "
                           "statistic can be trusted until that is fixed. Until then, read "
                           "deviation monitoring as unavailable rather than passing."),
    }]


def evaluate(*, baseline: Optional[dict], live: Optional[dict],
             volatility_band: Optional[dict] = None,
             current_atr_pct: Optional[float] = None,
             execution: Optional[dict] = None,
             attribution: Optional[dict] = None,
             thresholds: Thresholds = DEFAULT) -> dict:
    """Compare live behaviour against the strategy's own backtest.

    Returns ``{available, status, findings, baseline, live, note, auto_modify}``.
    ``auto_modify`` is always False — this agent recommends, it never acts."""
    # Checks that need no trade sample run in every state.
    ambient = (_volatility(volatility_band, current_atr_pct)
               + _execution(execution, thresholds)
               + _attribution(attribution))

    if not baseline or not _f(baseline, "total_trades"):
        return {"available": False, "status": "no_baseline", "auto_modify": False,
                "findings": ambient, "baseline": baseline, "live": live,
                "note": ("No backtest baseline for this strategy, so live behaviour has "
                         "nothing to be compared against. Run a backtest to enable "
                         "deviation monitoring.")}

    live = live or {}
    n_live = int(_f(live, "total_trades"))
    if n_live < MIN_LIVE_TRADES:
        blind = any(f["key"] == "monitor-unattributed-trades" for f in ambient)
        return {"available": True, "status": "warming_up", "auto_modify": False,
                "findings": ambient, "baseline": baseline, "live": live,
                "note": (f"{n_live} of {MIN_LIVE_TRADES} closed live trades attributed to "
                         "this strategy. Performance deviation is not reported below that "
                         "sample — it would be noise. Volatility and execution checks are "
                         "active now."
                         + (" Trades exist in the ledger but none carry this strategy's id,"
                            " so this is a gap in attribution rather than a quiet strategy."
                            if blind else ""))}

    findings = (_performance(baseline, live, thresholds)
                + _drawdown(baseline, live, thresholds)
                + _frequency(baseline, live, thresholds)
                + ambient)
    findings.sort(key=lambda f: _SEV_RANK.get(f["severity"], 9))

    worst = findings[0]["severity"] if findings else None
    return {
        "available": True,
        "status": ("critical" if worst == "critical" else
                   "warning" if worst == "warning" else
                   "info" if worst == "info" else "in_line"),
        "auto_modify": False,
        "findings": findings,
        "baseline": baseline,
        "live": live,
        "note": (None if findings else
                 f"Live behaviour is consistent with the backtest across "
                 f"{n_live} closed trades."),
    }


# ------------------------------------------------------------------- baseline

_BASELINE_KEYS = ("total_trades", "win_rate", "profit_factor", "net_r",
                  "expectancy_r", "max_drawdown_r", "span_days", "avg_rr")


def baseline_from_results(results: Optional[dict]) -> Optional[dict]:
    """The comparable subset of a backtest result. Nothing is computed here —
    these are the engine's own numbers, carried across unchanged."""
    if not results or not results.get("total_trades"):
        return None
    return {k: results.get(k) for k in _BASELINE_KEYS if results.get(k) is not None}
