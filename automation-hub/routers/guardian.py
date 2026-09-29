"""Guardian's read-only API (PRD "Nexus Guardian", Phases 1-2).

Every route here is a GET. Guardian has no endpoint that starts, stops,
configures or trades anything, and none that edits its own evidence: what it
observed and what it did are append-only in its own store. Reads are open to a
signed-in session like the rest of the dashboard.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

import webhook_api as _wa
from services.guardian.schema import EVENT_TYPES, HEALTH_STATES, SEVERITIES

router = APIRouter()


@router.get("/guardian/status")
def guardian_status() -> dict:
    """The Command Center: platform health derived through dependencies,
    Guardian's own health, and the day's event counts."""
    return _wa.guardian.snapshot()


@router.get("/guardian/events")
def guardian_events(component: Optional[str] = None, instance_id: Optional[str] = None,
                    lab_id: Optional[str] = None, symbol: Optional[str] = None,
                    timeframe: Optional[str] = None, event_type: Optional[str] = None,
                    category: Optional[str] = None, min_severity: Optional[str] = None,
                    strategy_id: Optional[str] = None, decision: Optional[str] = None,
                    before: Optional[int] = None, limit: int = 200) -> dict:
    """The activity stream, newest first. ``before`` pages back by sequence."""
    if min_severity and min_severity not in SEVERITIES:
        raise HTTPException(400, f"min_severity must be one of {', '.join(SEVERITIES)}")
    if event_type and event_type not in EVENT_TYPES:
        raise HTTPException(400, "unknown event type")
    rows = _wa.guardian_store.events(
        limit=limit, before_seq=before, min_severity=min_severity,
        source_component=component, instance_id=instance_id, lab_id=lab_id, symbol=symbol,
        timeframe=timeframe, event_type=event_type, category=category,
        strategy_id=strategy_id, decision=decision)
    return {"events": rows, "next_before": rows[-1]["seq"] if len(rows) == max(1, min(limit, 1000)) else None}


@router.get("/guardian/events/{event_id}")
def guardian_event(event_id: str) -> dict:
    """One event with its full evidence -- for a strategy event, the whole
    decision trace (PRD §8)."""
    row = _wa.guardian_store.event(event_id)
    if row is None:
        raise HTTPException(404, "no such event")
    return row


@router.get("/guardian/strategies")
def guardian_strategies(days: int = 7) -> dict:
    """What each strategy evaluated, set up, entered and was refused, its top
    rejection reasons, its almost-trades and its closed-trade results
    (PRD §37). Observation only: nothing here changes a strategy."""
    return _wa.guardian.strategies(days=days)


@router.get("/guardian/almost-trades")
def guardian_almost_trades(strategy_id: Optional[str] = None, component: Optional[str] = None,
                           limit: int = 100) -> dict:
    """Near-valid setups (PRD §9). Each is a MISSED OPPORTUNITY CANDIDATE for
    research, never a verdict that a rule is wrong."""
    return {"almost_trades": _wa.guardian_store.almost_trades(
        limit=limit, strategy_id=strategy_id, source_component=component)}


@router.get("/guardian/incidents")
def guardian_incidents(state: Optional[str] = None, limit: int = 100) -> dict:
    """Incidents, open first (PRD §29, §36). ``state``: active, OPEN,
    RECOVERED or CLOSED. Closed incidents are kept as history."""
    if state and state not in ("active", "OPEN", "RECOVERED", "CLOSED"):
        raise HTTPException(400, "state must be active, OPEN, RECOVERED or CLOSED")
    return {"incidents": _wa.guardian.incidents.list(state=state, limit=limit),
            "counts": _wa.guardian.incidents.counts()}


@router.get("/guardian/incidents/{incident_id}")
def guardian_incident(incident_id: int) -> dict:
    """One incident: its diagnosis with confidence, what it affected, its
    append-only log and the reconstructed event timeline (PRD §10, §11)."""
    row = _wa.guardian.incidents.get(incident_id)
    if row is None:
        raise HTTPException(404, "no such incident")
    return row


@router.get("/guardian/anomalies")
def guardian_anomalies() -> dict:
    """Deviations from each stream's own normal (PRD §21). Not failures."""
    return {"active": _wa.guardian.anomalies.active(),
            "recent": _wa.guardian_store.events(event_type="anomaly_detected", limit=50)}


@router.get("/guardian/integrity")
def guardian_integrity() -> dict:
    """Execution and journal reconciliation findings, and open exposure
    across every account with paper and live kept apart (PRD §23-25).
    Reading it changes nothing."""
    import time
    monitor = _wa.guardian.integrity
    if monitor is None:
        raise HTTPException(503, "integrity monitoring is not configured")
    return monitor.last or monitor.run(now=time.time())


@router.get("/guardian/actions")
def guardian_actions(limit: int = 100) -> dict:
    """Everything Guardian itself has done, append-only (PRD §39)."""
    return {"actions": _wa.guardian_store.actions(limit)}


@router.get("/guardian/catalogue")
def guardian_catalogue() -> dict:
    """The vocabulary: event types by category, severities and health states."""
    categories: dict[str, list[str]] = {}
    for event_type, category in EVENT_TYPES.items():
        categories.setdefault(category, []).append(event_type)
    return {"categories": categories, "severities": list(SEVERITIES),
            "health_states": list(HEALTH_STATES)}
