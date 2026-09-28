"""Guardian's read-only API (PRD "Nexus Guardian", Phase 1).

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
                    before: Optional[int] = None, limit: int = 200) -> dict:
    """The activity stream, newest first. ``before`` pages back by sequence."""
    if min_severity and min_severity not in SEVERITIES:
        raise HTTPException(400, f"min_severity must be one of {', '.join(SEVERITIES)}")
    if event_type and event_type not in EVENT_TYPES:
        raise HTTPException(400, "unknown event type")
    rows = _wa.guardian_store.events(
        limit=limit, before_seq=before, min_severity=min_severity,
        source_component=component, instance_id=instance_id, lab_id=lab_id, symbol=symbol,
        timeframe=timeframe, event_type=event_type, category=category)
    return {"events": rows, "next_before": rows[-1]["seq"] if len(rows) == max(1, min(limit, 1000)) else None}


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
