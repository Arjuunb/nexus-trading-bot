"""Opt-in, credential-scoped read-only export of committed lab decisions."""
from __future__ import annotations

import hmac
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from config import settings
from services.guardian_read_model import lab_decision_snapshot

router = APIRouter(tags=["guardian-observer"])


@router.get("/guardian/observations")
def guardian_observations(x_guardian_observer_key: Optional[str] = Header(default=None)):
    """No runtime, broker, strategy, or market call is made by this route."""
    if not settings.guardian_observer_key:
        raise HTTPException(404, "Observer is not configured")
    if not x_guardian_observer_key or not hmac.compare_digest(
            x_guardian_observer_key, settings.guardian_observer_key):
        raise HTTPException(401, "Observer credential required")
    try:
        labs = [
            lab_decision_snapshot(settings.price_action_paper_db, "PRICE_ACTION"),
            lab_decision_snapshot(settings.smc_paper_db, "SMC"),
        ]
    except (sqlite3.Error, ValueError, TypeError) as exc:
        # Fail closed and avoid leaking a filesystem path or arbitrary saved
        # exception text through the observer surface.
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "SOURCE_EVIDENCE_UNAVAILABLE"}) from exc
    return JSONResponse({
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "BOUNDED_SAVED_LAB_DECISIONS",
        "feed_health_verified": False,
        "execution_integrity_verified": False,
        "labs": labs,
    }, headers={"Cache-Control": "no-store"})
