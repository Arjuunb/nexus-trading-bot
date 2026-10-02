"""Latest observed instance gate state; no inference about actual broker fills."""
from __future__ import annotations

from .store import GuardianStore


def instance_decision_traces(store: GuardianStore, *, limit: int = 50,
                             instance_id: str | None = None) -> dict:
    if (type(limit) is not int or not 1 <= limit <= 100 or
            (instance_id is not None and
             (not isinstance(instance_id, str) or not 1 <= len(instance_id) <= 128))):
        raise ValueError("invalid instance trace request")
    scanned = store.recent_instance_decision_evidence()
    selected: dict[tuple[str, str], dict] = {}
    for event in scanned:
        owner = event.get("instance_id")
        evidence = event.get("evidence") or {}
        if not isinstance(owner, str) or not owner or \
                (instance_id and owner != instance_id) or not isinstance(evidence, dict):
            continue
        decision_identity = evidence.get("decision_identity")
        decision_id = evidence.get("decision_id")
        source_sequence = evidence.get("source_sequence")
        if (not isinstance(decision_identity, str) or
                type(decision_id) is not int or type(source_sequence) is not int):
            continue
        identity = (owner, decision_identity or f"source-row:{decision_id}")
        prior = selected.get(identity)
        if prior and source_sequence <= prior["source_sequence"]:
            continue
        selected[identity] = {
            "instance_id": owner,
            "decision_id": decision_id,
            "decision_identity": decision_identity,
            "source_sequence": source_sequence,
            "event_id": event["event_id"],
            "decision_time": evidence.get("decision_time"),
            "received_at": event["received_at"],
            "strategy_id": event.get("strategy_id"),
            "symbol": event.get("symbol"), "timeframe": event.get("timeframe"),
            "side": evidence.get("side"),
            "strategy_verdict": evidence.get("strategy_verdict"),
            "final_state": event.get("decision"),
            "gate_stage": evidence.get("gate_stage"),
            "blocker": evidence.get("blocker"),
            "reason": event.get("reason"),
            "executed_in_decision_store": evidence.get("executed") is True,
            "passed_rules": evidence.get("passed_rules")
                if isinstance(evidence.get("passed_rules"), list) else [],
            "failed_rules": evidence.get("failed_rules")
                if isinstance(evidence.get("failed_rules"), list) else [],
            "broker_fill_verified": False,
        }
    traces = sorted(selected.values(), key=lambda row: row["source_sequence"],
                    reverse=True)[:limit]
    return {
        "coverage": "BOUNDED_POST_INSTALL_PERSISTED_DECISIONS",
        "scanned_event_limit": 2000,
        "scanned_events": len(scanned),
        "scan_may_be_truncated": len(scanned) == 2000,
        "all_evaluations_proven": False,
        "broker_fills_verified": False,
        "traces": traces,
    }
