"""Deterministic trade statistics over canonical records.

Every number the Journal, the weekly reviews and the memory show is computed
here, in code, from structured fields -- never by an agent and never from
notes. A statistic with nothing to compute from is None ("insufficient
data"), not zero. Sample sizes travel with every figure so the reader can
judge them.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable, Optional

#: Below this many completed trades a figure is a description, not evidence.
MIN_SAMPLE = 30
COMPLETED = ("WIN", "LOSS", "BREAKEVEN")


def completed(records: Iterable[dict]) -> list[dict]:
    return [r for r in records if r.get("status") == "CLOSED" and r.get("outcome") in COMPLETED]


def _sum(values) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    return round(sum(vals), 10) if vals else None


def _mean(values) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _close_key(r: dict) -> str:
    return r.get("position_closed_at") or r.get("exit_filled_at") or r.get("created_at") or ""


def drawdown(values: list[Optional[float]]) -> Optional[float]:
    """Largest peak-to-trough fall of the running sum (positive number)."""
    known = [v for v in values if v is not None]
    if not known:
        return None
    peak = equity = 0.0
    worst = 0.0
    for v in known:
        equity += v
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 10)


def summarize(records: Iterable[dict], *, reviews: Optional[dict] = None) -> dict:
    """The full metric set for one group of records."""
    rows = sorted(completed(records), key=_close_key)
    n = len(rows)
    wins = [r for r in rows if r["outcome"] == "WIN"]
    losses = [r for r in rows if r["outcome"] == "LOSS"]
    breakevens = [r for r in rows if r["outcome"] == "BREAKEVEN"]
    nets = [r.get("net_pnl") for r in rows]
    rs = [r.get("realized_r") for r in rows]
    gains = sum(float(v) for v in nets if v is not None and v > 0)
    pains = -sum(float(v) for v in nets if v is not None and v < 0)
    reviewed = [(reviews or {}).get(r["journal_record_id"]) for r in rows]
    reviewed = [v for v in reviewed if v]
    # A review that could not assess compliance (UNKNOWN) is neither
    # compliant nor a violation; it stays out of the rate.
    assessable = [v for v in reviewed
                  if v.get("strategy_compliance") in ("COMPLIANT", "VIOLATION")
                  and v.get("risk_compliance") in (None, "COMPLIANT", "VIOLATION")]
    compliant = [v for v in assessable if v.get("strategy_compliance") == "COMPLIANT"
                 and v.get("risk_compliance") in (None, "COMPLIANT")]
    return {
        "trades": n,
        "wins": len(wins), "losses": len(losses), "breakevens": len(breakevens),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "gross_pnl": _sum(r.get("gross_pnl") for r in rows),
        "fees": _sum(r.get("fees") for r in rows),
        "net_pnl": _sum(nets),
        "total_r": _sum(rs),
        "average_r": _mean(rs),
        "r_known": sum(1 for v in rs if v is not None),
        "profit_factor": (round(gains / pains, 4) if pains > 0 else None),
        "profit_factor_note": ("no losing trades" if n and pains == 0 else None),
        "average_planned_rr": _mean(r.get("planned_rr") for r in rows),
        "average_achieved_r": _mean(r.get("achieved_rr") for r in rows),
        "max_drawdown": drawdown(nets),
        "max_drawdown_r": drawdown(rs),
        "rule_compliance": (round(len(compliant) / len(assessable), 4) if assessable else None),
        "reviewed": len(reviewed), "compliance_assessed": len(assessable),
        "sample_warning": ("INSUFFICIENT_SAMPLE" if n < MIN_SAMPLE else None),
        "journal_record_ids": [r["journal_record_id"] for r in rows],
        "period": {"start": _close_key(rows[0]) if rows else None,
                   "end": _close_key(rows[-1]) if rows else None},
    }


def breakdown(records: Iterable[dict], key: Callable[[dict], Optional[str]], *,
              reviews: Optional[dict] = None) -> dict:
    groups: dict = defaultdict(list)
    for r in completed(records):
        groups[key(r) or "UNKNOWN"].append(r)
    return {name: summarize(rows, reviews=reviews) for name, rows in sorted(groups.items())}


def full_report(records: list[dict], *, reviews: Optional[dict] = None) -> dict:
    return {
        "overall": summarize(records, reviews=reviews),
        "long": summarize([r for r in records if r.get("side") == "long"], reviews=reviews),
        "short": summarize([r for r in records if r.get("side") == "short"], reviews=reviews),
        "by_symbol": breakdown(records, lambda r: r.get("symbol"), reviews=reviews),
        "by_timeframe": breakdown(records, lambda r: r.get("timeframe"), reviews=reviews),
        "by_session": breakdown(records, lambda r: r.get("trading_session"), reviews=reviews),
        "by_setup": breakdown(records, lambda r: r.get("setup_type") or r.get("strategy_name"),
                              reviews=reviews),
        "by_exit_reason": breakdown(records, lambda r: r.get("exit_reason"), reviews=reviews),
        "by_strategy": breakdown(records, lambda r: r.get("strategy_name"), reviews=reviews),
    }


def kpis(records: list[dict], *, reviews: Optional[dict] = None) -> dict:
    s = summarize(records, reviews=reviews)
    return {k: s[k] for k in ("trades", "net_pnl", "total_r", "profit_factor",
                              "profit_factor_note", "win_rate", "rule_compliance",
                              "reviewed", "sample_warning", "wins", "losses", "breakevens")}
