"""Read-only, bounded incident reconstruction with explicitly unproven causality."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone

from .anomalies import bounded_events, utc
from .dependencies import DEPENDENCIES
from .incidents import classify_incident
from .store import GuardianStore

_DIRECT_LIMIT = 200
_CONTEXT_LIMIT = 500
_BYTE_LIMIT = 2 * 1024 * 1024
_FEED_FAILURES = {"websocket_disconnected", "stale_candle", "stale_htf_candle", "candle_missing", "sequence_gap"}
_FAILURES = _FEED_FAILURES | {"worker_crashed", "journal_failed", "execution_uncertain", "execution_integrity_observed"}


def _clock_valid(event: dict, moment: datetime) -> bool:
    source, received = (utc(datetime.fromisoformat(event[key])) for key in ("timestamp", "received_at"))
    return source <= moment and received <= moment and source <= received + timedelta(seconds=5)


def _component(event: dict) -> str:
    lab = event.get("lab_id")
    source = event.get("source_service")
    if lab == "SMC" or source == "smc_lab":
        if event["event_type"] in _FEED_FAILURES | {"websocket_reconnected", "feed_synchronized"}:
            return "smc_feed"
        if event["event_type"].startswith("journal_"):
            return "smc_agent_journal" if event["source_component"] == "agent_journal" else "smc_paper_journal"
        if event["event_type"].startswith("paper_order_"):
            return "smc_paper_execution"
        return "smc_agent" if event["source_component"] == "agent" else "smc_lab"
    if lab == "PRICE_ACTION" or source == "pa_lab":
        if event["event_type"] in _FEED_FAILURES | {"websocket_reconnected", "feed_synchronized"}:
            return "pa_feed"
        if event["event_type"].startswith("journal_"):
            return "pa_paper_journal"
        return "pa_paper_execution" if event["event_type"].startswith("paper_order_") else "pa_lab"
    if event.get("instance_id"):
        return "instance_market_data" if event["event_type"] in _FEED_FAILURES else "trading_instances"
    return event["source_component"]


def _owner_matches(a: dict, b: dict) -> bool:
    owner_a = (a.get("lab_id"), a.get("instance_id"))
    owner_b = (b.get("lab_id"), b.get("instance_id"))
    if owner_a != owner_b:
        return False
    if not any(owner_a) and (a["source_service"], a["source_component"]) != (
            b["source_service"], b["source_component"]):
        return False
    # Missing session/market context cannot stand in for an explicit identity.
    return (all(a.get(key) == b.get(key) for key in ("session_id", "symbol", "timeframe")) and
            all((a.get("metadata") or {}).get(key) == (b.get("metadata") or {}).get(key)
                for key in ("venue", "scope")))


def _relationship(event: dict, anchors: list[dict]) -> str | None:
    for anchor in anchors:
        if _owner_matches(event, anchor):
            for key, label in (("execution_id", "SAME_EXECUTION_ID"),
                               ("correlation_id", "SAME_DECISION_ID")):
                if event.get(key) and event[key] == anchor.get(key):
                    return label
        # Cross-lab correlation requires an explicitly named shared dependency,
        # not just a venue, coincident timestamps or the generic word "shared".
        left, right = event.get("metadata") or {}, anchor.get("metadata") or {}
        dependency = left.get("dependency_id")
        if (isinstance(dependency, str) and dependency and dependency == right.get("dependency_id")
                and isinstance(left.get("venue"), str) and left["venue"] == right.get("venue")
                and isinstance(left.get("scope"), str) and left["scope"] == right.get("scope")
                and (event["event_type"] in _FEED_FAILURES or anchor["event_type"] in _FEED_FAILURES)):
            return "EXPLICIT_SHARED_DEPENDENCY"
    return None


def incident_investigation(store: GuardianStore, incident_id: str, *,
                           now: datetime | None = None) -> dict | None:
    moment = utc(now or datetime.now(timezone.utc))
    with closing(store._connect()) as conn:
        conn.execute("BEGIN")
        incident = conn.execute("SELECT * FROM guardian_incidents WHERE incident_id=?",
                                (incident_id,)).fetchone()
        if incident is None:
            conn.commit()
            return None
        direct, direct_truncated = bounded_events(conn.execute(
            "SELECT e.sequence,e.received_at,e.payload_json FROM guardian_incident_updates u "
            "JOIN events e ON e.event_id=u.event_id WHERE u.incident_id=? "
            "ORDER BY u.sequence DESC LIMIT ?", (incident_id, _DIRECT_LIMIT + 1)),
            limit=_DIRECT_LIMIT, byte_limit=_BYTE_LIMIT)
        # Include the opening anchor even for a long/repeated incident; a
        # bounded recent page must not rewrite the origin as a recent symptom.
        opening = conn.execute(
            "SELECT e.sequence,e.received_at,e.payload_json FROM guardian_incident_updates u "
            "JOIN events e ON e.event_id=u.event_id WHERE u.incident_id=? "
            "ORDER BY u.sequence LIMIT 1", (incident_id,)).fetchone()
        if opening:
            first, _ = bounded_events([opening], limit=1, byte_limit=_BYTE_LIMIT)
            if first and first[0]["event_id"] not in {item["event_id"] for item in direct}:
                direct.append(first[0])
        valid = [event for event in direct if _clock_valid(event, moment)]
        context, context_truncated = [], False
        start = min((utc(datetime.fromisoformat(event["timestamp"])) for event in valid), default=moment)
        end = min(moment, start + timedelta(hours=1))
        if valid:
            context, context_truncated = bounded_events(conn.execute(
                "SELECT sequence,received_at,payload_json FROM events WHERE timestamp>=? "
                "AND timestamp<=? AND received_at<=? ORDER BY timestamp,sequence LIMIT ?",
                ((start - timedelta(minutes=5)).isoformat(), end.isoformat(),
                 moment.isoformat(), _CONTEXT_LIMIT + 1)), limit=_CONTEXT_LIMIT, byte_limit=_BYTE_LIMIT)
        conn.commit()
    direct_ids = {event["event_id"] for event in direct}
    selected = [(event, "DIRECT_INCIDENT_EVIDENCE") for event in direct]
    for event in context:
        if event["event_id"] in direct_ids:
            continue
        relationship = _relationship(event, valid)
        if relationship:
            selected.append((event, relationship))
    selected.sort(key=lambda item: (item[0]["timestamp"], item[0]["_sequence"]))
    timeline, candidates = [], []
    for event, relationship in selected:
        clock_valid = _clock_valid(event, moment)
        timeline.append({"event_id": event["event_id"], "source_time": event["timestamp"],
                         "received_at": event["received_at"], "component": _component(event),
                         "event_type": event["event_type"], "reason": event.get("reason"),
                         "decision": event.get("decision"), "relationship": relationship,
                         "clock_valid": clock_valid, "execution_id": event.get("execution_id"),
                         "session_id": event.get("session_id"), "lab_id": event.get("lab_id"),
                         "instance_id": event.get("instance_id")})
        signal = classify_incident(event)
        if (clock_valid and event["event_type"] in _FAILURES and signal and
                signal.transition == "OPEN" and len(candidates) < 20):
            candidates.append({"component": _component(event), "summary": signal.root_cause,
                               "evidence_ids": [event["event_id"]],
                               "failure_fact_confidence": signal.confidence,
                               "causal_confidence": "UNKNOWN", "causal_link_verified": False})
    chains = []
    # Source-time precedence is necessary, never sufficient for causation.
    # Late-delivered evidence is sorted by source time; receipt remains visible.
    for index, (root, _) in enumerate(selected):
        if root["event_type"] != "websocket_disconnected" or not _clock_valid(root, moment):
            continue
        downstream = [event for event, _ in selected[index + 1:]
                      if event["event_type"] in {"stale_candle", "stale_htf_candle", "candle_missing", "sequence_gap"}
                      and _clock_valid(event, moment)
                      and event["timestamp"] > root["timestamp"]
                      and _relationship(event, [root]) == "EXPLICIT_SHARED_DEPENDENCY"]
        if downstream:
            chains.append({"upstream_event_id": root["event_id"],
                           "downstream_event_ids": [item["event_id"] for item in downstream[:20]],
                           "confidence": "POSSIBLE", "basis": "NAMED_DEPENDENCY_AND_SOURCE_TIME_PRECEDENCE",
                           "causal_link_verified": False})
            for candidate in candidates:
                if candidate["evidence_ids"] == [root["event_id"]]:
                    candidate["causal_confidence"] = "POSSIBLE"
    components = sorted({_component(event) for event, _ in selected})
    checks = ["Verify source clocks and capture the missing authoritative lifecycle evidence."]
    if any(event["event_type"] in _FEED_FAILURES for event, _ in selected):
        checks.append("Inspect public transport, required native closed candles and candle continuity; do not retune the strategy.")
    if any(event["execution_id"] for event in timeline) or incident["root_component"] in {"journal", "instance_ledger"}:
        checks.append("Reconcile the original execution identity with broker orders, fills, positions and journal in source-authoritative snapshots.")
    return {"schema_version": 1, "incident": dict(incident), "timeline": timeline,
            "root_cause_candidates": candidates, "possible_chains": chains[:20],
            "affected_components": components,
            "declared_edges": [{"upstream": parent, "consumer": component,
                                "basis": "DECLARED_NOT_RUNTIME_VERIFIED"}
                               for component in components for parent in DEPENDENCIES.get(component, ())],
            "coverage": {"direct_evidence_truncated": direct_truncated,
                         "context_scan_truncated": context_truncated,
                         "context_window_start": (start - timedelta(minutes=5)).isoformat(),
                         "context_window_end": end.isoformat(), "direct_limit": _DIRECT_LIMIT,
                         "context_limit": _CONTEXT_LIMIT, "byte_limit_per_scan": _BYTE_LIMIT,
                         "clock_invalid_events": sum(not item["clock_valid"] for item in timeline),
                         "all_lifecycle_evidence_verified": False},
            "next_checks": checks, "automatic_action_allowed": False,
            "causal_chain_verified": False, "strategy_defect_verified": False,
            "limitations": ["Observed failures and chronological correlations are distinct from proven root causes.",
                            "Unlinked accounts/sessions/venues are excluded from contextual correlation.",
                            "Ordinary NO_SETUP/condition failures are not root-cause evidence."]}
