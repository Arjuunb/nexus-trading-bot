"""Read-only, versioned performance over explicitly scoped completed episodes.

One statistical trade is one position episode from its first entry until its
final closure. Entries, reductions, remainder rows, and funding receipts are
constituents of that episode, not independent trades. A reversal closes the
old episode and opens another, as determined by the execution evidence.

This contract does not replace legacy dashboard calculations. Financial
amounts and ratios are JSON-safe decimal strings. Source REAL/float precision
cannot be recovered by using Decimal; it is disclosed separately. Funding is
a signed cost (positive paid, negative received). Persisted net P&L is already
net of the source's booked costs and is never charged a second time here.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
from typing import Iterable, Mapping


CALCULATION_VERSION = "strategy_intelligence.v1"
_KNOWN_COST_COVERAGE = {"BOOKED", "MODELED", "VERIFIED_ZERO"}
_NONEXECUTED_SOURCES = {"counterfactual", "counterfactual_research", "shadow", "research",
                        "rejected", "rejected_decision", "hypothetical", "cancelled", "expired"}
_MODE_PARTITIONS = {
    "forward_paper": "EXECUTED_FORWARD_PAPER",
    "backtest": "HISTORICAL_BACKTEST",
    "historical_backtest": "HISTORICAL_BACKTEST",
    "replay": "SIMULATED_REPLAY",
    "simulation": "SIMULATED_REPLAY",
    "simulated": "SIMULATED_REPLAY",
    "paper_replay": "SIMULATED_REPLAY",
}


@dataclass(frozen=True)
class EvidenceCohort:
    """Every scope is explicit; None matches only explicitly unknown scope.

    The caller must supply server-resolved owner/account scope before exposing
    results. This internal calculator does not perform access authorization.
    Legacy unknown version/hash may be inspected but never become verified.
    A None symbol explicitly aggregates symbols within this exact cohort;
    all ownership, configuration, instance, lab, session and source dimensions
    always match exactly, including None.
    """

    strategy_id: str
    strategy_version: str | None
    config_fingerprint: str | None
    instance_id: str | None
    lab_id: str | None
    simulation_session_id: str | None
    execution_mode: str
    source_kind: str
    owner_id: str | None
    account_id: str | None
    symbol: str | None = None

    def __post_init__(self):
        for name, value in self.as_dict().items():
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"cohort {name} must be a nonempty string or explicit None")
        for name in ("strategy_id", "execution_mode", "source_kind"):
            if getattr(self, name) is None:
                raise ValueError(f"cohort {name} is required")

    def as_dict(self) -> dict:
        return asdict(self)


def _decimal(value, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite decimal or None") from exc


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f") if value else "0"


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _profit_factor(values: list[Decimal]) -> tuple[str | None, str]:
    if not values:
        return None, "NO_TRADES"
    wins = sum((v for v in values if v > 0), Decimal(0))
    losses = -sum((v for v in values if v < 0), Decimal(0))
    if losses:
        return _text(wins / losses), "FINITE"
    return None, "NO_LOSSES" if wins else "ALL_BREAKEVEN"


def _normalised_scope(row: Mapping) -> dict:
    """Accept the journal's canonical hash name without guessing any identity."""
    result = dict(row)
    if "config_fingerprint" not in result and "strategy_config_hash" in result:
        result["config_fingerprint"] = result["strategy_config_hash"]
    if "initial_risk" not in result and "initial_risk_amount" in result:
        result["initial_risk"] = result["initial_risk_amount"]
    return result


def calculate_metrics(
    episodes: Iterable[Mapping], *, cohort: EvidenceCohort,
    history_complete: bool = False, source_watermark: str | None = None,
    completeness_report: Mapping | None = None, calculation_timestamp: str | None = None,
) -> dict:
    """Calculate only executed CLOSED episodes in one explicit cohort.

    Missing scope keys are excluded rather than turned into wildcard matches.
    Missing amounts remain unknown. Totals/PF/win rate/DD are unavailable when
    their necessary episode facts are missing; separately named known totals
    disclose partial financial coverage. R uses captured episode entry risk,
    including explicitly observed scale-ins, never a current/trailing stop.
    """
    material = [dict(row) for row in episodes]
    calculated_at = calculation_timestamp or datetime.now(timezone.utc).isoformat()
    if _timestamp(calculated_at) is None:
        raise ValueError("calculation_timestamp must be an aware timestamp")
    with localcontext() as context:
        context.prec = 50
        result = _calculate(material, cohort=cohort, history_complete=False,
                            source_watermark=source_watermark)
    status, binding = _completeness_binding(
        material, cohort, source_watermark, completeness_report, calculated_at)
    verified = bool(status == "COMPLETE" and result["trade_count"] and
                    result["identity_verified"] and result["financial_evidence_complete"] and
                    result["cohort_evidence_complete"] and
                    result["coverage"]["valid_close_time_episodes"] == result["trade_count"] and
                    result["evidence_partition"] not in
                    ("UNVERIFIED_EXECUTION_MODE", "NONEXECUTED_RESEARCH_OR_DECISION"))
    result.update(calculation_timestamp=calculated_at, calculated_at=calculated_at,
                  evidence_completeness_status=status, completeness_binding_status=binding,
                  profitability_verified=verified, ready_for_evidence_review=verified,
                  history_complete=status == "COMPLETE", history_complete_claimed=bool(history_complete),
                  last_successful_reconciliation=(completeness_report or {}).get("last_successful_reconciliation"))
    return result


def _completeness_binding(episodes, cohort, source_watermark, report, calculated_at):
    """A caller's complete-history flag cannot substitute for reconciliation.

    The pure calculator cannot read a ledger. Its producer must supply a freshly
    assessed source watermark; the read-only two-store projection does so. Cached
    reports are valid only for their exact episode input and at most five minutes.
    """
    if not isinstance(report, Mapping):
        return "UNKNOWN", "NO_COMPLETENESS_REPORT"
    from services.strategy_evidence_completeness import REPORT_VERSION, metrics_episode_watermark
    checks = (
        (report.get("report_version") == REPORT_VERSION, "UNSUPPORTED_REPORT_VERSION"),
        (report.get("cohort") == cohort.as_dict(), "COHORT_MISMATCH"),
        (bool(source_watermark) and source_watermark == report.get("source_watermark"), "SOURCE_WATERMARK_MISMATCH"),
        (report.get("metrics_evidence_watermark") == metrics_episode_watermark(episodes, cohort), "EPISODE_WATERMARK_MISMATCH"),
    )
    for passed, reason in checks:
        if not passed:
            return "UNKNOWN", reason
    report_time = _timestamp(report.get("calculated_at"))
    if report_time is None:
        return "UNKNOWN", "INVALID_REPORT_TIMESTAMP"
    age = (_timestamp(calculated_at) - report_time).total_seconds()
    if age < 0:
        return "UNKNOWN", "FUTURE_REPORT_TIMESTAMP"
    if age > 300:
        return "UNKNOWN", "STALE_COMPLETENESS_REPORT"
    status = report.get("status")
    if status not in {"COMPLETE", "PARTIAL", "UNKNOWN", "CONFLICTED", "RECOVERING"}:
        return "UNKNOWN", "INVALID_REPORT_STATUS"
    if status == "COMPLETE" and report.get("history_complete") is not True:
        return "UNKNOWN", "INCONSISTENT_COMPLETE_REPORT"
    return status, "MATCHED"


def _calculate(episodes, *, cohort, history_complete, source_watermark):
    scope = cohort.as_dict()
    required_scope = {key: value for key, value in scope.items() if key != "symbol"}
    excluded = Counter({"missing_scope": 0, "other_cohort": 0, "nonexecuted": 0,
                        "not_completed": 0, "missing_episode_id": 0})
    selected = {}
    duplicates = 0
    for original in episodes:
        row = _normalised_scope(original)
        if any(key not in row for key in required_scope):
            excluded["missing_scope"] += 1
            continue
        if any(row[key] != value for key, value in required_scope.items()) or (
                cohort.symbol is not None and row.get("symbol") != cohort.symbol):
            excluded["other_cohort"] += 1
            continue
        if (str(row.get("evidence_kind", "")).lower() != "executed"
                or str(row.get("source_kind", "")).lower() in _NONEXECUTED_SOURCES):
            excluded["nonexecuted"] += 1
            continue
        if str(row.get("status", "")).lower() != "closed":
            excluded["not_completed"] += 1
            continue
        episode_id = row.get("episode_id")
        if not isinstance(episode_id, str) or not episode_id:
            excluded["missing_episode_id"] += 1
            continue
        if episode_id in selected:
            if row != selected[episode_id]:
                raise ValueError(f"conflicting episode evidence: {episode_id}")
            duplicates += 1
            continue
        selected[episode_id] = row

    rows = []
    float_fields = set()
    fee_statuses, funding_statuses = Counter(), Counter()
    for episode_id, row in sorted(selected.items()):
        amounts = {}
        for field in ("net_pnl", "gross_pnl", "fees", "funding", "initial_risk"):
            amounts[field] = _decimal(row.get(field), field)
            if isinstance(row.get(field), float):
                float_fields.add(field)
        fee_status = str(row.get("fees_coverage") or "UNKNOWN").upper()
        funding_status = str(row.get("funding_coverage") or "UNKNOWN").upper()
        fee_statuses[fee_status] += 1
        funding_statuses[funding_status] += 1
        fees_known = amounts["fees"] is not None and fee_status in _KNOWN_COST_COVERAGE
        funding_known = amounts["funding"] is not None and funding_status in _KNOWN_COST_COVERAGE
        if fees_known and amounts["fees"] < 0:
            raise ValueError("fees must be a nonnegative cost")
        gross_derived = False
        if amounts["gross_pnl"] is None and amounts["net_pnl"] is not None and fees_known and funding_known:
            amounts["gross_pnl"] = amounts["net_pnl"] + amounts["fees"] + amounts["funding"]
            gross_derived = True
        reconciliation = None
        if all(amounts[key] is not None for key in ("net_pnl", "gross_pnl")) and fees_known and funding_known:
            delta = amounts["gross_pnl"] - amounts["fees"] - amounts["funding"] - amounts["net_pnl"]
            # Existing ledgers use REAL. This tolerance only classifies source
            # reconciliation; it never rewrites any supplied financial value.
            if abs(delta) > Decimal("0.0000000001"):
                raise ValueError(f"financial reconciliation failed for episode {episode_id}")
            reconciliation = "EXACT" if not delta else "WITHIN_SOURCE_REAL_TOLERANCE"
        net_r = None
        if amounts["net_pnl"] is not None and amounts["initial_risk"] is not None and amounts["initial_risk"] > 0:
            net_r = amounts["net_pnl"] / amounts["initial_risk"]
        opened_at, closed_at = _timestamp(row.get("opened_at")), _timestamp(row.get("closed_at"))
        rows.append({**amounts, "episode_id": episode_id, "closed_at": closed_at,
                     "duration_s": Decimal(str((closed_at - opened_at).total_seconds()))
                     if opened_at and closed_at and closed_at >= opened_at else None,
                     "net_r": net_r, "fees_known": fees_known, "funding_known": funding_known,
                     "gross_derived": gross_derived, "reconciliation": reconciliation})

    count = len(rows)
    net = [r["net_pnl"] for r in rows if r["net_pnl"] is not None]
    gross = [r["gross_pnl"] for r in rows if r["gross_pnl"] is not None]
    rs = [r["net_r"] for r in rows if r["net_r"] is not None]
    all_net, all_gross = len(net) == count, len(gross) == count
    all_fees = all(r["fees_known"] for r in rows)
    all_funding = all(r["funding_known"] for r in rows)
    all_times = all(r["closed_at"] is not None for r in rows)
    wins, losses = sum(v > 0 for v in net), sum(v < 0 for v in net)
    net_pf, net_pf_state = _profit_factor(net) if all_net else (None, "INCOMPLETE_NET_PNL")
    gross_pf, gross_pf_state = _profit_factor(gross) if all_gross else (None, "INCOMPLETE_GROSS_PNL")
    drawdown = None
    longest_win = longest_loss = None
    first = last = None
    if all_times and rows:
        ordered = sorted(rows, key=lambda r: (r["closed_at"], r["episode_id"]))
        first, last = ordered[0]["closed_at"].isoformat(), ordered[-1]["closed_at"].isoformat()
        if all_net:
            equity = peak = drawdown = Decimal(0)
            longest_win = longest_loss = streak_win = streak_loss = 0
            for row in ordered:
                value = row["net_pnl"]
                equity += value
                peak = max(peak, equity)
                drawdown = max(drawdown, peak - equity)
                streak_win = streak_win + 1 if value > 0 else 0
                streak_loss = streak_loss + 1 if value < 0 else 0
                longest_win, longest_loss = max(longest_win, streak_win), max(longest_loss, streak_loss)
    identity_verified = bool(cohort.strategy_version and cohort.config_fingerprint and
                             re.fullmatch(r"[0-9a-f]{64}", cohort.config_fingerprint) and
                             cohort.owner_id and cohort.account_id and
                             (cohort.instance_id or cohort.lab_id))
    if any(row.get("identity_status") not in (None, "observed", "verified")
           for row in selected.values()):
        identity_verified = False
    partition = _MODE_PARTITIONS.get(cohort.execution_mode.lower(), "UNVERIFIED_EXECUTION_MODE")
    if cohort.execution_mode.lower() == "paper" and cohort.source_kind.lower() == "forward_paper":
        partition = "EXECUTED_FORWARD_PAPER"
    if cohort.source_kind.lower() in _NONEXECUTED_SOURCES:
        partition = "NONEXECUTED_RESEARCH_OR_DECISION"
    financial_complete = bool(count and all_net and all_gross and all_fees and all_funding)
    scope_complete = not (excluded["missing_scope"] or excluded["missing_episode_id"])
    durations = [r["duration_s"] for r in rows if r["duration_s"] is not None]
    total = lambda key, condition=True: _text(sum((r[key] for r in rows if r[key] is not None), Decimal(0))) if condition else None
    return {
        "calculation_version": CALCULATION_VERSION, "cohort": scope,
        "evidence_partition": partition, "source_watermark": source_watermark,
        "history_complete": bool(history_complete), "identity_verified": identity_verified,
        "cohort_evidence_complete": scope_complete,
        "financial_evidence_complete": financial_complete,
        "ready_for_evidence_review": bool(count and history_complete and identity_verified and
                                          financial_complete and scope_complete and all_times and
                                          partition != "UNVERIFIED_EXECUTION_MODE"),
        "trade_definition": "completed_position_episode",
        "trade_count": count, "completed_episode_count": count,
        "wins": wins if all_net else None, "losses": losses if all_net else None,
        "breakevens": len(net) - wins - losses if all_net else None,
        "win_rate_pct": _text(Decimal(wins) * 100 / count) if count and all_net else None,
        "gross_pnl": total("gross_pnl", all_gross), "net_pnl": total("net_pnl", all_net),
        "known_gross_pnl": total("gross_pnl"), "known_net_pnl": total("net_pnl"),
        "fees": total("fees", all_fees), "funding": total("funding", all_funding),
        "net_profit_factor": net_pf, "net_profit_factor_state": net_pf_state,
        "gross_profit_factor": gross_pf, "gross_profit_factor_state": gross_pf_state,
        "r_sample_size": len(rs), "expectancy_net_r": _text(sum(rs, Decimal(0)) / len(rs)) if rs else None,
        "net_r": _text(sum(rs, Decimal(0))) if rs else None,
        "max_drawdown_net_pnl": _text(drawdown),
        "drawdown_basis": "closed_episode_realised_net_pnl_ordered_by_actual_close_time",
        "longest_win_streak": longest_win, "longest_loss_streak": longest_loss,
        "mean_duration_s": _text(sum(durations, Decimal(0)) / len(durations)) if durations else None,
        "first_close_time": first, "last_close_time": last,
        "financial_precision": "decimal_from_source_text",
        "source_precision_notes": "source_float_precision_not_recoverable" if float_fields else None,
        "coverage": {
            "known_net_episodes": len(net), "known_gross_episodes": len(gross),
            "unknown_or_nonpositive_risk": count - len(rs),
            "valid_close_time_episodes": sum(r["closed_at"] is not None for r in rows),
            "duration_sample_size": len(durations), "fees_complete": bool(count and all_fees),
            "funding_complete": bool(count and all_funding),
            "fees_statuses": dict(sorted(fee_statuses.items())),
            "funding_statuses": dict(sorted(funding_statuses.items())),
            "gross_derived_from_confirmed_costs": sum(r["gross_derived"] for r in rows),
            "exact_cost_reconciliations": sum(r["reconciliation"] == "EXACT" for r in rows),
            "within_source_real_tolerance": sum(r["reconciliation"] == "WITHIN_SOURCE_REAL_TOLERANCE" for r in rows),
        },
        "excluded": dict(excluded), "duplicate_episode_rows": duplicates,
    }
