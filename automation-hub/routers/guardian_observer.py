"""Opt-in, credential-scoped read-only export of committed lab decisions."""
from __future__ import annotations

import hmac
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from config import settings
from services.guardian_execution_read_model import smc_execution_integrity_snapshot
from services.guardian_lab_execution_read_model import lab_paper_execution_snapshot
from services.guardian_lab_fill_read_model import lab_fill_page
from services.guardian_smc_journal_read_model import smc_journal_page
from services.guardian_smc_intent_read_model import smc_intent_event_page
from services.guardian_smc_fill_transition_read_model import smc_fill_transition_page
from services.guardian_smc_exit_fill_read_model import smc_exit_fill_page
from services.guardian_instance_read_model import instance_decision_page
from services.guardian_instance_ledger_read_model import instance_paper_ledger_snapshot
from services.guardian_lab_feed_read_model import lab_feed_snapshot
from services.guardian_read_model import lab_decision_page, lab_decision_snapshot, lab_lifecycle_page

router = APIRouter(tags=["guardian-observer"])


@router.get("/guardian/smc-exit-fills")
def guardian_smc_exit_fills(
        request: Request,
        after: int = Query(0, ge=0, le=2**63-1),
        anchor: str = Query("", max_length=64),
        x_guardian_observer_key: Optional[str] = Header(default=None)):
    _require_observer_key(x_guardian_observer_key)
    query = request.query_params
    if set(query) - {"after", "anchor"} or any(len(query.getlist(k)) != 1 for k in query):
        raise HTTPException(422, "Invalid or ambiguous exit evidence query")
    try:
        page = smc_exit_fill_page(settings.smc_paper_db, after=after, anchor=anchor)
    except (sqlite3.Error, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED", "code": "SMC_EXIT_EVIDENCE_UNAVAILABLE"}) from exc
    return JSONResponse({"schema_version": 1, "scope": "RETAINED_SMC_PAPER_EXIT_FILL_EVIDENCE",
                         "observed_at": datetime.now(timezone.utc).isoformat(),
                         "execution_integrity_verified": False, "page": page},
                        headers={"Cache-Control": "no-store"})


@router.get("/guardian/smc-fill-transitions")
def guardian_smc_fill_transitions(
        after: int = Query(0, ge=0, le=2**63 - 1),
        anchor: str = Query("", max_length=64),
        x_guardian_observer_key: Optional[str] = Header(default=None)):
    _require_observer_key(x_guardian_observer_key)
    try:
        page = smc_fill_transition_page(settings.smc_paper_db, after=after, anchor=anchor)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "SMC_FILL_TRANSITIONS_UNAVAILABLE"}) from exc
    return JSONResponse({"schema_version": 1, "scope": "RETAINED_SMC_PAPER_FILL_POSITION_TRANSITIONS",
                         "observed_at": datetime.now(timezone.utc).isoformat(),
                         "execution_integrity_verified": False, "page": page},
                        headers={"Cache-Control": "no-store"})


@router.get("/guardian/smc-intent-events")
def guardian_smc_intent_events(
        after: int = Query(0, ge=0, le=2**63 - 1),
        anchor: str = Query("", max_length=64),
        x_guardian_observer_key: Optional[str] = Header(default=None)):
    """Observe retained events, not the broker or the Agent's current verdict."""
    _require_observer_key(x_guardian_observer_key)
    try:
        page = smc_intent_event_page(settings.smc_agent_journal_db, after=after, anchor=anchor)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "SMC_INTENT_HISTORY_UNAVAILABLE"}) from exc
    return JSONResponse({"schema_version": 1, "scope": "RETAINED_SMC_AGENT_EXECUTION_INTENT_EVENTS",
                         "observed_at": datetime.now(timezone.utc).isoformat(),
                         "execution_integrity_verified": False, "page": page},
                        headers={"Cache-Control": "no-store"})


@router.get("/guardian/smc-journal")
def guardian_smc_journal(
        cycle: int = Query(0, ge=0, le=2**63 - 1),
        after: int = Query(0, ge=0, le=2**63 - 1),
        upper: int = Query(0, ge=0, le=2**63 - 1),
        origin: str = Query("", max_length=64),
        anchor: str = Query("", max_length=64),
        x_guardian_observer_key: Optional[str] = Header(default=None)):
    """Closed Agent journal evidence only; no runtime, migration or broker calls."""
    _require_observer_key(x_guardian_observer_key)
    try:
        page = smc_journal_page(settings.smc_agent_journal_db, cursor={
            "cycle": cycle, "after": after, "upper": upper, "origin": origin, "anchor": anchor})
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "SMC_JOURNAL_HISTORY_UNAVAILABLE"}) from exc
    return JSONResponse({"schema_version": 1, "scope": "CYCLIC_RETAINED_SMC_AGENT_CLOSED_TRADES",
                         "observed_at": datetime.now(timezone.utc).isoformat(),
                         "execution_integrity_verified": False, "page": page},
                        headers={"Cache-Control": "no-store"})


@router.get("/guardian/lab-fills")
def guardian_lab_fills(
        lab: str = Query(..., pattern="^(PRICE_ACTION|SMC)$"),
        after: int = Query(0, ge=0, le=2**63 - 1),
        anchor: str = Query("", max_length=64),
        x_guardian_observer_key: Optional[str] = Header(default=None)):
    _require_observer_key(x_guardian_observer_key)
    try:
        page = lab_fill_page(
            settings.price_action_paper_db if lab == "PRICE_ACTION" else settings.smc_paper_db,
            lab, after=after, anchor=anchor)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "LAB_FILL_HISTORY_UNAVAILABLE"}) from exc
    return JSONResponse({"schema_version": 1, "scope": "RETAINED_ISOLATED_PAPER_FILLS",
                         "observed_at": datetime.now(timezone.utc).isoformat(),
                         "execution_integrity_verified": False, "page": page},
                        headers={"Cache-Control": "no-store"})


@router.get("/guardian/lab-execution")
def guardian_lab_execution(
        lab: str = Query(..., pattern="^(PRICE_ACTION|SMC)$"),
        x_guardian_observer_key: Optional[str] = Header(default=None)):
    _require_observer_key(x_guardian_observer_key)
    try:
        snapshot = lab_paper_execution_snapshot(
            settings.price_action_paper_db if lab == "PRICE_ACTION" else settings.smc_paper_db, lab)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "LAB_EXECUTION_EVIDENCE_UNAVAILABLE"}) from exc
    return JSONResponse({"schema_version": 1, "scope": "ISOLATED_LAB_PAPER_BROKER",
                         "observed_at": datetime.now(timezone.utc).isoformat(),
                         "execution_integrity_verified": False, "snapshot": snapshot},
                        headers={"Cache-Control": "no-store"})


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


@router.get("/guardian/instance-decisions")
def guardian_instance_decisions(
        after: int = Query(0, ge=0),
        anchor: str = Query("", max_length=128),
        x_guardian_observer_key: Optional[str] = Header(default=None)):
    """Read committed decision/gate history, without invoking the worker."""
    _require_observer_key(x_guardian_observer_key)
    try:
        page = instance_decision_page(settings.decisions_db, after=after, anchor=anchor)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "INSTANCE_DECISION_EVIDENCE_UNAVAILABLE"}) from exc
    return JSONResponse({
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "POST_INSTALL_INSTANCE_DECISION_LIFECYCLE",
        "feed_health_verified": False,
        "execution_integrity_verified": False,
        "page": page,
    }, headers={"Cache-Control": "no-store"})


@router.get("/guardian/instance-ledger")
def guardian_instance_ledger(x_guardian_observer_key: Optional[str] = Header(default=None)):
    """Inspect the configured primary ledger; never use a fallback account."""
    _require_observer_key(x_guardian_observer_key)
    from webhook_api import ledger
    try:
        snapshot = instance_paper_ledger_snapshot(ledger)
    except Exception as exc:  # noqa: BLE001 -- remote primary failures must be structured
        raise HTTPException(503, {"state": "PERSISTENCE_BLOCKED",
                                  "code": "INSTANCE_LEDGER_EVIDENCE_UNAVAILABLE"}) from exc
    return JSONResponse({
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "scope": "INSTANCE_ATTRIBUTED_PAPER_LEDGER_ONLY",
        "feed_health_verified": False,
        "execution_integrity_verified": False,
        "snapshot": snapshot,
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
        "schema_version": 2,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "feed_health_verified": False,
        "execution_integrity_verified": False,
        **snapshot,
    }, headers={"Cache-Control": "no-store"})
