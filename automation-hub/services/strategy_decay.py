"""Descriptive edge decay over a frozen historical baseline and actual episodes.

This module has no execution interface. Lifecycle states are review proposals,
never changes to a champion, a worker, a risk setting or a financial record.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib

from services.historical_forward_evidence import build_evidence_chain, historical_metrics, number, timestamp
from services.strategy_evidence import evidence_json
from services.strategy_intelligence_metrics import _normalised_scope
from services.strategy_intelligence_v2 import _contexts_for, _context_quality
from services.strategy_edge_score import COHORT_FIELDS, SCOPE_FIELDS, METHODS, CLASSIFIER_FIELDS


def digest(value):
    return hashlib.sha256(evidence_json(value).encode()).hexdigest()


def text(value):
    return format(value.normalize(), "f") if value else "0"


def decay_policy():
    policy = {"version": "edge_decay.v1", "calibration": "UNCALIBRATED_RESEARCH_POLICY",
        "windows": [20, 50, 100], "minimum_historical_episodes": 100,
        "expectancy_retention_floor": "0.5", "profit_factor_floor": "1",
        "drawdown_multiple": "2", "historical_drawdown_floor_r": "1",
        "persistence": "two_consecutive_disjoint_20_episode_windows",
        "window_order": "actual_close_time_utc_then_episode_id",
        "recovery": "recompute_from_evidence_no_automatic_resume",
        "rules": "any expectancy, monetary PF or R drawdown deterioration flags a complete window"}
    return {**policy, "fingerprint": digest(policy)}


def financial_summary(rows):
    """Revalidate original amounts, costs and risk; never charge slippage twice."""
    rows = sorted(rows, key=lambda r: (timestamp(r.get("closed_at")), r["episode_id"]))
    samples = [{**r, "entry_time": r.get("opened_at"), "exit_time": r.get("closed_at")} for r in rows]
    metrics = historical_metrics({"trades": samples})
    keys = ("completed_episode_count", "net_pnl", "net_r", "expectancy_net_r", "net_profit_factor", "max_drawdown_r")
    result = {key: metrics[key] for key in keys}
    if not rows:
        return {**result, "status": "NO_EPISODES", "wins": 0, "losses": 0, "breakevens": 0,
                "win_rate_pct": None, "gross_pnl": None, "fees": None, "funding": None, "slippage": None}
    if metrics["financial_evidence_complete"] is not True:
        raise ValueError("ORIGINAL_FINANCIAL_EVIDENCE_INCOMPLETE")
    net = [number(r["net_pnl"]) for r in rows]
    equity = peak = dd = Decimal(0)
    for r in rows:
        equity += number(r["net_pnl"]) / number(r["initial_risk"])
        peak = max(peak, equity); dd = max(dd, peak-equity)
    result.update(status="COMPLETE", wins=sum(v > 0 for v in net), losses=sum(v < 0 for v in net),
        breakevens=sum(v == 0 for v in net), win_rate_pct=text(Decimal(sum(v > 0 for v in net))*100/len(rows)),
        max_drawdown_r=text(dd), profit_factor_state="FINITE" if metrics["net_profit_factor"] is not None
        else "NO_LOSSES_OBSERVED" if any(v > 0 for v in net) else "ALL_BREAKEVEN")
    for key in ("gross_pnl", "fees", "funding", "slippage"):
        result[key] = text(sum((number(r[key]) for r in rows), Decimal(0)))
    return result


def bind_monitoring_evidence(identity, qualification, performance, episodes, contexts, *, classifier, frozen):
    """Validate a current owner-authorized Task 1 chain against original episodes."""
    chain = qualification.get("evidence_chain") or {}
    if (any(key not in identity for key in COHORT_FIELDS)
            or any((qualification.get("identity") or {}).get(k) != identity[k]
                   or (chain.get("identity") or {}).get(k) != identity[k] for k in COHORT_FIELDS)
            or chain.get("status") != "COMPLETE" or performance.get("cache_status") != "READY"
            or performance.get("cohort") != identity or performance.get("group_by") != ["symbol"]
            or not all(isinstance(classifier.get(k), str) and classifier[k] for k in CLASSIFIER_FIELDS)):
        raise ValueError("CURRENT_BOUND_HISTORICAL_AND_PAPER_EVIDENCE_REQUIRED")
    refs = {m: (chain.get("validation_run_ids") or {}).get(m) for m in METHODS}
    records = [r for r in qualification.get("validation_history", []) if r.get("run_id") in refs.values()]
    if (len(records) != len(METHODS) or len(set(refs.values())) != len(METHODS)
            or any(not refs[m] for m in METHODS)):
        raise ValueError("VALIDATION_LINEAGE_INCOMPLETE")
    rebuilt = build_evidence_chain(scope={k:identity[k] for k in SCOPE_FIELDS}, identity=identity,
        frozen=frozen, records=records, performance=performance)
    if (rebuilt["status"] != "COMPLETE" or rebuilt["historical"] != chain.get("historical")
            or rebuilt["forward_paper"] != chain.get("forward_paper") or rebuilt["validation_run_ids"] != refs):
        raise ValueError("HISTORICAL_OR_PAPER_PROVENANCE_MISMATCH")
    groups = performance.get("groups") or []
    if len(groups) != 1:
        raise ValueError("AMBIGUOUS_PAPER_COHORT")
    parent = groups[0]
    if parent.get("classifier") != classifier:
        raise ValueError("PAPER_CLASSIFIER_IDENTITY_MISMATCH")
    rows = [_normalised_scope(r) for r in episodes]
    ids = [r.get("episode_id") for r in rows]
    if (len(ids) != len(set(ids)) or set(ids) != set(parent.get("episode_ids", []))
            or any(any(r.get(k) != identity[k] for k in COHORT_FIELDS) or r.get("status") != "closed"
                or r.get("evidence_kind") != "executed" for r in rows)):
        raise ValueError("ACTUAL_EPISODE_MEMBERSHIP_MISMATCH")
    if any(timestamp(r.get("closed_at")) is None or timestamp(r.get("opened_at")) is None for r in rows):
        raise ValueError("ACTUAL_EPISODE_TIMING_INCOMPLETE")
    with localcontext() as precision:
        precision.prec = 50
        summary = financial_summary(rows)
    if any(number(summary[k]) != number(parent.get("metrics", {}).get(k)) for k in
            ("completed_episode_count", "net_pnl", "net_r", "expectancy_net_r", "net_profit_factor", "max_drawdown_r")):
        raise ValueError("PAPER_FINANCIAL_SUMMARY_MISMATCH")
    period = {"first_opened_at": min(timestamp(r["opened_at"]) for r in rows).isoformat(),
              "last_closed_at": max(timestamp(r["closed_at"]) for r in rows).isoformat()}
    if any(timestamp(parent.get("observation_period", {}).get(k)) != timestamp(v) for k,v in period.items()):
        raise ValueError("PAPER_OBSERVATION_PERIOD_MISMATCH")
    context_by_id, _, _, _ = _contexts_for(rows, contexts)
    selected = {}
    for row in rows:
        candidates = [c for c in context_by_id.get(row["episode_id"], []) if all(c.get(k)==classifier[k] for k in CLASSIFIER_FIELDS)]
        selected[row["episode_id"]] = candidates[0] if len(candidates)==1 and _context_quality(row,candidates[0])=="VALID" else None
    return {"identity": deepcopy(identity), "episodes": deepcopy(rows), "contexts": deepcopy(selected),
        "classifier": dict(classifier), "baseline": deepcopy(rebuilt["historical"]["metrics"]),
        "lineage": {"validation_run_ids": refs, "paper_cache_run_id": performance.get("run_id"),
                    "source_watermark": performance["source_watermark"], "classifier": dict(classifier),
                    "historical_dataset": deepcopy(rebuilt["reports"]["historical_backtest"]["dataset"]),
                    "cost_model": deepcopy(parent.get("cost_model"))}}


def calculate_decay(bound, *, as_of):
    with localcontext() as precision:
        precision.prec = 50
        return _calculate_decay(bound, as_of)


def _calculate_decay(bound, as_of):
    at = timestamp(as_of)
    if at is None or any(timestamp(r["closed_at"]) > at for r in bound["episodes"]):
        raise ValueError("AS_OF_MUST_COVER_OBSERVED_EPISODES")
    policy = decay_policy()
    baseline = bound["baseline"]
    be, bdd, bpf = (number(baseline.get(k)) for k in ("expectancy_net_r", "max_drawdown_r", "net_profit_factor"))
    baseline_ok = (baseline.get("financial_evidence_complete") is True
        and (number(baseline.get("completed_episode_count")) or 0) >= policy["minimum_historical_episodes"]
        and be is not None and be > 0 and bdd is not None and bdd >= 0 and bpf is not None and bpf > 1)
    rows = sorted(bound["episodes"], key=lambda r:(timestamp(r["closed_at"]),r["episode_id"]))
    def window(size, end):
        subset = rows[max(0,end-size):end]
        result = {"window_size":size, "episode_ids":[r["episode_id"] for r in subset],
                  "status":"INSUFFICIENT_EVIDENCE", "metrics":None, "reasons":[]}
        if len(subset) != size or not baseline_ok:
            return result
        metrics = financial_summary(subset)
        reasons = []
        if number(metrics["expectancy_net_r"]) < be * Decimal(policy["expectancy_retention_floor"]):
            reasons.append("EXPECTANCY_BELOW_BASELINE_RETENTION")
        if number(metrics["net_profit_factor"]) is not None and number(metrics["net_profit_factor"]) < 1:
            reasons.append("MONETARY_PROFIT_FACTOR_BELOW_ONE")
        if metrics["profit_factor_state"] == "ALL_BREAKEVEN":
            reasons.append("NO_POSITIVE_NET_OUTCOMES")
        if number(metrics["max_drawdown_r"]) > max(bdd,Decimal(policy["historical_drawdown_floor_r"])) * Decimal(policy["drawdown_multiple"]):
            reasons.append("DRAWDOWN_EXCEEDS_HISTORICAL_MULTIPLE")
        return {**result, "status":"DEGRADING" if reasons else "STABLE", "metrics":metrics, "reasons":reasons,
                "expectancy_change_r":text(number(metrics["expectancy_net_r"])-be),
                "win_rate_comparison":"DESCRIPTIVE_ONLY", "independence_claim":False}
    windows = [window(size,len(rows)) for size in policy["windows"]]
    persistence = [window(20,max(0,len(rows)-20)),window(20,len(rows))]
    degrading = any(w["status"]=="DEGRADING" for w in windows)
    continued = all(w["status"]=="DEGRADING" for w in persistence)
    status = "PAUSE_REVIEW" if continued else "EDGE_DEGRADING" if degrading else "STABLE" if windows[0]["status"]=="STABLE" else "INSUFFICIENT_EVIDENCE"
    result = {"contract_version":"edge_decay.v1", "identity":bound["identity"], "policy":policy,
        "status":status, "windows":windows, "persistence_windows":persistence,
        "baseline":{k:baseline.get(k) for k in ("completed_episode_count","expectancy_net_r","net_profit_factor","max_drawdown_r")},
        "suggested_lifecycle_state":"PAUSED" if continued else "DEGRADED" if degrading else None,
        "action_status":"PENDING_APPROVAL" if degrading else "NO_ACTION", "lineage":deepcopy(bound["lineage"]),
        "advisory_only":True, "orders_allowed":False, "strategy_changes_allowed":False,
        "limitations":["Descriptive uncalibrated thresholds; no statistical proof of edge decay.",
            "20/50/100 windows overlap; persistence uses two disjoint 20-episode blocks.",
            "Historical and forward drawdown have different observation horizons.",
            "Suggestions do not change lifecycle state or execution; recovery never resumes trading automatically."]}
    result["monitor_id"] = "decay:"+digest(result)
    return result
