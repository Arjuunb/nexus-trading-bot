"""Conservative, bounded read model for saved PA/SMC decision evidence.

This does not infer trade outcomes or recommend changes to strategy rules.
"""
from __future__ import annotations

from .store import GuardianStore


def _near_valid(event: dict) -> bool:
    evidence = event.get("evidence") or {}
    conditions = evidence.get("conditions")
    missing = evidence.get("missing_conditions")
    if (event.get("decision") != "WATCHING" or
            evidence.get("condition_trace_available") is not True or
            not isinstance(conditions, list) or len(conditions) < 2 or
            not isinstance(missing, list) or len(missing) != 1):
        return False
    statuses = [row.get("status") if isinstance(row, dict) else None
                for row in conditions]
    return statuses.count("MISSING") == 1 and all(
        status in ("PASS", "MISSING") for status in statuses)


def decision_traces(store: GuardianStore, *, limit: int = 50,
                    lab: str | None = None) -> dict:
    """Return latest *observed* state per decision in a bounded evidence scan.

    Backfill and recent polling may describe the same decision. They are not
    separate evaluations. The newest received snapshot wins; missing lifecycle
    transitions cannot be recreated from a saved row.
    """
    if type(limit) is not int or not 1 <= limit <= 100 or lab not in (
            None, "PRICE_ACTION", "SMC"):
        raise ValueError("invalid decision trace request")
    scanned = store.recent_lab_evidence()
    selected: dict[tuple[str, str, str], dict] = {}
    for event in scanned:
        event_lab = event.get("lab_id")
        session = event.get("session_id")
        correlation = event.get("correlation_id")
        if event_lab not in ("PRICE_ACTION", "SMC") or (lab and event_lab != lab) or \
                not isinstance(session, str) or not session or \
                not isinstance(correlation, str) or not correlation:
            continue
        identity = (event_lab, session, correlation)
        if identity in selected:
            continue
        evidence = event.get("evidence") or {}
        conditions = evidence.get("conditions")
        selected[identity] = {
            "lab": event_lab, "session_id": session,
            "correlation_id": correlation, "event_id": event["event_id"],
            "source_service": event["source_service"],
            "received_at": event["received_at"], "candle_time": event["timestamp"],
            "strategy_id": event.get("strategy_id"),
            "strategy_version": event.get("strategy_version"),
            "symbol": event.get("symbol"), "timeframe": event.get("timeframe"),
            "decision": event.get("decision"), "reason": event.get("reason"),
            "conditions": conditions if isinstance(conditions, list) else [],
            "missing_conditions": evidence.get("missing_conditions")
                if isinstance(evidence.get("missing_conditions"), list) else [],
            "condition_trace_available": evidence.get("condition_trace_available") is True,
            "near_valid_candidate": _near_valid(event),
            "outcome_verified": False,
            "feed_health_verified": False,
            "execution_integrity_verified": False,
        }
    traces = list(selected.values())[:limit]
    return {
        "coverage": "BOUNDED_RECEIVED_SNAPSHOTS",
        "scanned_event_limit": 2000,
        "scanned_events": len(scanned),
        "scan_may_be_truncated": len(scanned) == 2000,
        "lifecycle_history_complete": False,
        "strategy_performance_verified": False,
        "near_valid_definition": "WATCHING_WITH_ONE_MISSING_CONDITION_AND_ALL_OTHERS_PASS",
        "traces": traces,
    }
