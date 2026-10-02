"""Opt-in, credential-scoped read-only export of committed lab decisions."""
from __future__ import annotations

import hmac
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from config import settings
from services.guardian_execution_read_model import smc_execution_integrity_snapshot
from services.guardian_lab_feed_read_model import lab_feed_snapshot
from services.guardian_read_model import lab_decision_page, lab_decision_snapshot, lab_lifecycle_page

router = APIRouter(tags=["guardian-observer"])


def _require_observer_key(presented: Optional[str]) -> None:
    if not settings.guardian_observer_key:
        raise HTTPException(404, "Observer is not configured")
    if not presented or not hmac.compare_digest(presented, settings.guardian_observer_key):
        raise HTTPException(401, "Observer credential required")


@router.get("/guardian/observations")
def guardian_observations(x_guardian_observer_key: Optional[str] = Header(default=None)):
    """No runtime, broker, strategy, or market call is made by this route."""
    _require_observer_key(x_guardian_observer_key)
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


@router.get("/guardian/lab-feeds")
def guardian_lab_feeds(x_guardian_observer_key: Optional[str] = Header(default=None)):
    """Read saved session identity and in-memory stream status only.

    This does not hydrate a chart or call a market provider, strategy, risk
    gate, broker, account state, or journal. Feed health is not lab execution
    health, so the latter remains explicitly unverified.
    """
    _require_observer_key(x_guardian_observer_key)
    import webhook_api as runtime
    try:
        pa_runtime = getattr(runtime, "price_action_runtime", None)
        smc_runtime = getattr(runtime, "smc_runtime", None)
        feeds = [
            lab_feed_snapshot(
                settings.price_action_paper_db, "PRICE_ACTION",
                getattr(pa_runtime, "stream", None)),
            lab_feed_snapshot(
                settings.smc_paper_db, "SMC", getattr(smc_runtime, "stream", None),
                reconciled=dict(getattr(smc_runtime, "last_market_health", {}) or {})),
        ]
    except sqlite3.Error as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "SOURCE_FEED_EVIDENCE_UNAVAILABLE"}) from exc
    except (ValueError, TypeError, RuntimeError) as exc:
        raise HTTPException(503, {"state": "BLOCKED",
                                  "code": "SOURCE_FEED_STATUS_UNAVAILABLE"}) from exc
    return JSONResponse({
        "schema_version": 1, "scope": "CURRENT_LAB_FEED_STATUS",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "execution_health_verified": False, "feeds": feeds,
    }, headers={"Cache-Control": "no-store"})


@router.get("/guardian/evaluations")
def guardian_evaluations(
        lab: str = Query(..., pattern="^(PRICE_ACTION|SMC)$"),
        after: int = Query(0, ge=0),
        anchor: str = Query("", max_length=128),
        x_guardian_observer_key: Optional[str] = Header(default=None)):
    """Read-only, keyset-paged backfill of retained decision identities."""
    _require_observer_key(x_guardian_observer_key)
    try:
        page = lab_decision_page(
            settings.price_action_paper_db if lab == "PRICE_ACTION" else settings.smc_paper_db,
            lab, after=after, anchor=anchor)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "SOURCE_EVIDENCE_UNAVAILABLE"}) from exc
    return JSONResponse({
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "RETAINED_LAB_DECISION_IDENTITIES",
        "feed_health_verified": False,
        "execution_integrity_verified": False,
        "page": page,
    }, headers={"Cache-Control": "no-store"})


@router.get("/guardian/lifecycle")
def guardian_lifecycle(
        lab: str = Query(..., pattern="^(PRICE_ACTION|SMC)$"),
        after: int = Query(0, ge=0),
        anchor: str = Query("", max_length=128),
        x_guardian_observer_key: Optional[str] = Header(default=None)):
    """Export committed lifecycle evidence; never invoke a trading runtime."""
    _require_observer_key(x_guardian_observer_key)
    try:
        page = lab_lifecycle_page(
            settings.price_action_paper_db if lab == "PRICE_ACTION" else settings.smc_paper_db,
            lab, after=after, anchor=anchor)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "SOURCE_LIFECYCLE_UNAVAILABLE"}) from exc
    return JSONResponse({
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "POST_INSTALL_MATERIAL_LIFECYCLE",
        "feed_health_verified": False,
        "execution_integrity_verified": False,
        "page": page,
    }, headers={"Cache-Control": "no-store"})


@router.get("/guardian/smc-execution")
def guardian_smc_execution(x_guardian_observer_key: Optional[str] = Header(default=None)):
    """Observe durable SMC Agent paper records without invoking its runtime."""
    _require_observer_key(x_guardian_observer_key)
    try:
        snapshot = smc_execution_integrity_snapshot(
            settings.smc_agent_journal_db, settings.smc_paper_db)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "EXECUTION_EVIDENCE_UNAVAILABLE"}) from exc
    return JSONResponse({
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "feed_health_verified": False,
        "execution_integrity_verified": False,
        **snapshot,
    }, headers={"Cache-Control": "no-store"})
