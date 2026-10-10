"""Owner-authorized, cached v2 intelligence reads, independent of execution.

The provider receives only server-resolved ownership/account scope. Unknown
query fields are rejected so a client cannot supply completeness assertions.
These routes never run a historical calculation or call an order method.
"""
from __future__ import annotations

import hmac
import importlib
import logging
from typing import Annotated, Callable

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from services.strategy_intelligence_v2 import SUPPORTED_GROUPINGS

CONTRACT_VERSION = "strategy_intelligence.v2"
logger = logging.getLogger(__name__)
# Internal calculators also support an empty total grouping. Public inspection
# requests select a named context grouping; advertise only these supported reads.
_GROUPINGS = tuple(group for group in SUPPORTED_GROUPINGS if group)


class CohortQuery(BaseModel):
    """Missing nullable identities mean explicitly unknown, never a wildcard."""
    model_config = ConfigDict(extra="forbid")
    instance_id: str = Field(min_length=1, max_length=120)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"\S")
    strategy_version: str | None = Field(default=None, min_length=1, max_length=120)
    config_fingerprint: str | None = Field(default=None, min_length=1, max_length=128)
    simulation_session_id: str | None = Field(default=None, min_length=1, max_length=120)
    lab_id: str | None = Field(default=None, min_length=1, max_length=120)
    execution_mode: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"\S")
    source_kind: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"\S")
    symbol: str | None = Field(default=None, min_length=1, max_length=40)


class PerformanceQuery(CohortQuery):
    group_by: str = Field(default="symbol", max_length=160)


class ContextQuery(CohortQuery):
    limit: int = Field(default=100, ge=1, le=500)


class DiscoveryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: str | None = Field(default=None, min_length=1, max_length=120)
    limit: int = Field(default=500, ge=1, le=500)


class DefinitionsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _runtime(name):
    return getattr(importlib.import_module("webhook_api"), name, None)


def _authenticated_owner(request: Request) -> str:
    """Use the existing verified identity, without an anonymous owner fallback."""
    app = importlib.import_module("app")
    from services.tenancy import OWNER_TENANT, multi_user_enabled
    try:
        user = app._user(request)
        if user:
            return str(user) if multi_user_enabled() else OWNER_TENANT
        supplied = request.headers.get("x-webhook-secret", "")
        expected = str(app.settings.admin_key or "")
        if supplied and expected and hmac.compare_digest(supplied, expected):
            return OWNER_TENANT
    except Exception as exc:
        logger.warning("intelligence_authorization_failed", extra={"error_type": type(exc).__name__})
        raise HTTPException(503, "Intelligence authorization unavailable") from None
    raise HTTPException(401, "Sign in required")


def _unknown(cache_status="PENDING"):
    return {"contract_version": CONTRACT_VERSION, "calculation_timestamp": None,
            "evidence_quality": "UNKNOWN", "cost_coverage": {
                "fees": "UNKNOWN", "funding": "UNKNOWN", "slippage": "UNKNOWN"},
            "sample_confidence": "INSUFFICIENT", "profitability_verified": False,
            "cache_status": cache_status, "rows": [], "groups": [], "contexts": [], "cohorts": [],
            "error_reason": "Intelligence cache unavailable" if cache_status == "ERROR" else None,
            "supported_groupings": [list(group) for group in _GROUPINGS]}


def create_router(*, service_provider: Callable | None = None,
                  manager_provider: Callable | None = None,
                  owner_resolver: Callable | None = None) -> APIRouter:
    """Inject cache/auth seams for isolated consumers; defaults use existing app."""
    router = APIRouter(prefix="/api/v2/strategy-intelligence", tags=["Strategy Intelligence v2"])
    provider = service_provider or (lambda: _runtime("strategy_intelligence"))
    manager_source = manager_provider or (lambda: _runtime("instance_manager"))
    resolve_owner = owner_resolver or _authenticated_owner

    def owner(request, response):
        response.headers["Cache-Control"] = "no-store"
        identity = resolve_owner(request)
        if not identity or not isinstance(identity, str) or not identity.strip():
            raise HTTPException(401, "Sign in required")
        # Repeated query values are ambiguous even when their first value is valid.
        if len(request.query_params) != len(request.query_params.multi_items()):
            raise HTTPException(422, "Repeated intelligence filters are not supported")
        return identity

    def manager():
        result = manager_source()
        if result is None or not result.store.available:
            raise HTTPException(503, "Intelligence ownership store unavailable")
        return result

    def instance_scope(instance_id, session_id, owner_id):
        authority = manager()
        try:
            instance = authority.instance_for(instance_id, owner_id)
        except KeyError:
            raise HTTPException(404, "Trading instance not found") from None
        session = session_id if session_id is not None else instance.simulation_session_id
        if not session:
            raise HTTPException(404, "Simulation session not found")
        if session != instance.simulation_session_id:
            try:
                known = authority.store.simulation_sessions(instance_id)
            except Exception:
                raise HTTPException(503, "Intelligence session authorization unavailable") from None
            if not any(row.get("id") == session and row.get("instance_id") == instance_id for row in known):
                raise HTTPException(404, "Simulation session not found")
        return {"owner_id": owner_id, "instance_id": instance_id,
                "simulation_session_id": session, "account_id": f"instance:{instance_id}:{session}"}

    def read(view, scope, filters):
        try:
            service = provider()
            if service is None:
                return _unknown()
            result = service.read(view, scope, filters)
            if not isinstance(result, dict) or result.get("contract_version") != CONTRACT_VERSION:
                logger.warning("intelligence_cached_contract_mismatch", extra={"view": view})
                return _unknown("ERROR")
            envelope = _unknown(result.get("cache_status", "READY"))
            envelope.update(result)
            envelope["calculation_timestamp"] = result.get("calculation_timestamp", result.get("calculated_at"))
            envelope["supported_groupings"] = [list(group) for group in _GROUPINGS]
            return envelope
        except Exception as exc:
            logger.warning("intelligence_cache_read_failed", extra={"view": view, "error_type": type(exc).__name__})
            return _unknown("ERROR")

    def exact_filters(query, strategy_id=None):
        if strategy_id is not None and query.strategy_id not in (None, strategy_id):
            raise HTTPException(422, "Strategy path and cohort filter disagree")
        strategy = strategy_id or query.strategy_id
        if not strategy or not query.execution_mode or not query.source_kind:
            raise HTTPException(422, "Exact strategy_id, execution_mode and source_kind are required")
        return {"strategy_id": strategy, "strategy_version": query.strategy_version,
                "config_fingerprint": query.config_fingerprint, "lab_id": query.lab_id,
                "execution_mode": query.execution_mode, "source_kind": query.source_kind, "symbol": query.symbol}

    def performance_result(request, response, query, view="performance", strategy_id=None):
        identity = owner(request, response)
        filters = exact_filters(query, strategy_id)
        grouping = tuple(query.group_by.split(","))
        if grouping not in _GROUPINGS:
            raise HTTPException(422, "Unsupported cached intelligence grouping")
        filters["group_by"] = list(grouping)
        return read(view, instance_scope(query.instance_id, query.simulation_session_id, identity), filters)

    def context_result(request, response, query, strategy_id=None):
        identity = owner(request, response)
        filters = exact_filters(query, strategy_id)
        filters["limit"] = query.limit
        return read("context", instance_scope(query.instance_id, query.simulation_session_id, identity), filters)

    @router.get("/cohorts")
    def cohorts(request: Request, response: Response, query: Annotated[DiscoveryQuery, Query()]):
        identity = owner(request, response)
        scope = {"owner_id": identity}
        if query.instance_id is not None:
            try:
                manager().instance_for(query.instance_id, identity)
            except KeyError:
                raise HTTPException(404, "Trading instance not found") from None
            scope["instance_id"] = query.instance_id
        result = read("cohorts", scope, {"limit": query.limit})
        authorized = []
        for candidate in result.get("cohorts", [])[:query.limit]:
            if not isinstance(candidate, dict) or candidate.get("owner_id") != identity:
                continue
            instance_id, session = candidate.get("instance_id"), candidate.get("simulation_session_id")
            if not instance_id or not session or (query.instance_id is not None and query.instance_id != instance_id):
                continue
            try:
                expected = instance_scope(instance_id, session, identity)
            except HTTPException as exc:
                if exc.status_code == 503:
                    raise
                continue
            if candidate.get("account_id") == expected["account_id"]:
                authorized.append(candidate)
        # Discovery must never echo other cached financial/context collections.
        result.update(cohorts=authorized, rows=[], groups=[], contexts=[])
        return result

    @router.get("/performance")
    def performance(request: Request, response: Response, query: Annotated[PerformanceQuery, Query()]):
        return performance_result(request, response, query)

    @router.get("/context")
    def context(request: Request, response: Response, query: Annotated[ContextQuery, Query()]):
        return context_result(request, response, query)

    @router.get("/strategy/{strategy_id}/breakdown")
    def breakdown(strategy_id: str, request: Request, response: Response, query: Annotated[PerformanceQuery, Query()]):
        return performance_result(request, response, query, strategy_id=strategy_id)

    @router.get("/strategy/{strategy_id}/confidence")
    def confidence(strategy_id: str, request: Request, response: Response, query: Annotated[PerformanceQuery, Query()]):
        return performance_result(request, response, query, view="confidence", strategy_id=strategy_id)

    @router.get("/strategy/{strategy_id}/context-history")
    def context_history(strategy_id: str, request: Request, response: Response, query: Annotated[ContextQuery, Query()]):
        return context_result(request, response, query, strategy_id=strategy_id)

    def definitions(request, response, view):
        return read(view, {"owner_id": owner(request, response)}, {})

    @router.get("/sessions")
    def sessions(request: Request, response: Response, query: Annotated[DefinitionsQuery, Query()]):
        return definitions(request, response, "sessions")

    @router.get("/regimes")
    def regimes(request: Request, response: Response, query: Annotated[DefinitionsQuery, Query()]):
        return definitions(request, response, "regimes")

    @router.get("/volatility")
    def volatility(request: Request, response: Response, query: Annotated[DefinitionsQuery, Query()]):
        return definitions(request, response, "volatility")

    return router
