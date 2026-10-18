"""Owner-authorized, cached v2 intelligence reads, independent of execution.

The provider receives only server-resolved ownership/account scope. Unknown
query fields are rejected so a client cannot supply completeness assertions.
These routes never run a historical calculation or call an order method.
"""
from __future__ import annotations

import hmac
import importlib
import logging
from datetime import date
from typing import Annotated, Callable, Literal

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


class OverviewQuery(CohortQuery):
    strategy_ids: str | None = Field(default=None, max_length=600)


class EdgeMapQuery(CohortQuery):
    x_dimension: str = Field(default="session", max_length=40)
    y_dimension: str = Field(default="symbol", max_length=40)
    metric: str = Field(default="expectancy_r", max_length=40)


class ComparisonQuery(CohortQuery):
    strategy_ids: str = Field(max_length=500)


class EpisodesQuery(CohortQuery):
    episode_id: str | None = Field(default=None, max_length=160)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100000)


class QualificationQuery(CohortQuery):
    pass


class EdgeScoreQuery(CohortQuery):
    strategy_id: str = Field(min_length=1, max_length=120, pattern=r"\S")
    strategy_version: str = Field(min_length=1, max_length=120)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str = Field(min_length=1, max_length=40, pattern=r"\S")
    execution_mode: Literal["paper"] = "paper"
    source_kind: Literal["forward_paper"] = "forward_paper"
    classifier_id: str | None = Field(default=None, min_length=1, max_length=120)
    classifier_version: str | None = Field(default=None, min_length=1, max_length=120)
    parameter_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    score_policy_version: Literal["edge_score.v1"] = "edge_score.v1"


class SelectionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: str = Field(min_length=1, max_length=120)
    simulation_session_id: str | None = Field(default=None, min_length=1, max_length=120)
    lab_id: str | None = Field(default=None, min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=40, pattern=r"\S")
    session: Literal["ASIA", "LONDON", "NEW_YORK", "LONDON_NEW_YORK_OVERLAP", "OFF_SESSION"]
    trend_regime: Literal["BULL", "BEAR", "STRONG_BULL", "STRONG_BEAR", "RANGE"]
    volatility_regime: Literal["LOW", "NORMAL", "HIGH", "EXTREME"]
    entry_timeframe: str = Field(min_length=2, max_length=12, pattern=r"^[1-9][0-9]*[mhdw]$")
    classifier_id: str | None = Field(default=None, min_length=1, max_length=120)
    classifier_version: str | None = Field(default=None, min_length=1, max_length=120)
    parameter_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class MonitoringQuery(CohortQuery):
    strategy_id: str = Field(min_length=1,max_length=120,pattern=r"\S")
    strategy_version: str = Field(min_length=1,max_length=120)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str = Field(min_length=1,max_length=40,pattern=r"\S")
    execution_mode: Literal["paper"] = "paper"
    source_kind: Literal["forward_paper"] = "forward_paper"
    classifier_id: str | None = Field(default=None,min_length=1,max_length=120)
    classifier_version: str | None = Field(default=None,min_length=1,max_length=120)
    parameter_hash: str | None = Field(default=None,pattern=r"^[0-9a-f]{64}$")
    monitor_policy_version: Literal["edge_decay.v1"] = "edge_decay.v1"


class WeeklyResearchQuery(MonitoringQuery):
    week_start: date | None = None
    limit: int = Field(default=50,ge=1,le=100)


class ValidationHistoryQuery(CohortQuery):
    method: str | None = Field(default=None, max_length=40)
    status: str | None = Field(default=None, max_length=40)
    limit: int = Field(default=100, ge=1, le=1000)


class ResearchJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=160)
    job_type: str = Field(max_length=40)
    strategy_id: str = Field(min_length=1, max_length=120)
    strategy_version: str | None = Field(default=None, max_length=80)
    config_fingerprint: str | None = Field(default=None, max_length=128)
    frozen_config: dict = Field(default_factory=dict)
    symbol: str = Field(min_length=1, max_length=40)
    timeframe: str = Field(min_length=2, max_length=12)
    venue: str = Field(default="unknown", max_length=80)
    market_type: str = Field(default="unknown", max_length=80)
    bars: int = Field(default=4000, ge=600, le=10000)
    train_fraction: float = Field(default=.6, gt=0, lt=1)
    validation_fraction: float = Field(default=.2, gt=0, lt=1)
    holdout_fraction: float = Field(default=.2, gt=0, lt=1)
    development_size: int | None = Field(default=None, ge=1, le=10000)
    evaluation_size: int | None = Field(default=None, ge=1, le=10000)
    warmup: int = Field(default=0, ge=0, le=5000)
    fee: str = Field(default="0", max_length=40)
    slippage: str = Field(default="0", max_length=40)
    funding: str | None = Field(default=None, max_length=40)
    runs: int = Field(default=1000, ge=100, le=5000)
    seed: int = Field(default=1, ge=0, le=2**31 - 1)
    policy_version: str | None = Field(default="qualification.v1", max_length=80)
    cost_model_version: str | None = Field(default=None, max_length=80)
    classifier_version: str | None = Field(default=None, max_length=80)
    software_revision: str | None = Field(default=None, max_length=160)
    instance_id: str | None = Field(default=None, max_length=120)
    simulation_session_id: str | None = Field(default=None, max_length=120)
    period_start: str | None = Field(default=None, max_length=80)
    period_end: str | None = Field(default=None, max_length=80)
    dataset_identity: str | None = Field(default=None, max_length=240)
    data_revision: str | None = Field(default=None, max_length=240)


class ResearchCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(min_length=1, max_length=120)
    strategy_version: str | None = Field(default=None, max_length=80)
    config_fingerprint: str | None = Field(default=None, max_length=128)
    frozen_config: dict = Field(default_factory=dict)
    dataset_identity: str = Field(min_length=1, max_length=240)
    data_revision: str | None = Field(default=None, max_length=240)
    symbol: str | None = Field(default=None, max_length=40)
    asset_portfolio: list[str] | None = None
    venue: str | None = Field(default=None, max_length=80)
    market_type: str | None = Field(default=None, max_length=80)
    timeframe: str | None = Field(default=None, max_length=20)
    execution_mode: str = Field(min_length=1, max_length=80)
    cost_model_version: str | None = Field(default=None, max_length=80)
    classifier_version: str | None = Field(default=None, max_length=80)
    software_revision: str | None = Field(default=None, max_length=160)
    policy_version: str | None = Field(default=None, max_length=80)
    validation_run_ids: list[str] = Field(default_factory=list, max_length=50)
    evidence_quality: str = Field(default="UNKNOWN", max_length=40)
    scope: dict = Field(default_factory=dict)


class ShadowRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=240)
    idempotency_key: str = Field(min_length=8, max_length=160)
    authorize: bool = False
    authorization_reason: str = Field(min_length=1, max_length=1000)
    lookback_spec: dict = Field(default_factory=dict)
    reviewed_source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ShadowActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


class ShadowComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    registration_id: str = Field(min_length=1,max_length=240)
    active_source_id: str = Field(min_length=1,max_length=240)
    start: str = Field(min_length=1,max_length=50)
    end: str = Field(min_length=1,max_length=50)
    idempotency_key: str = Field(min_length=8,max_length=160)


class ChampionDesignationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_key: str = Field(min_length=1, max_length=240)
    candidate_id: str = Field(min_length=1, max_length=240)
    selection_policy_version: str = Field(default="challenger.v1", min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=1000)
    evidence_reference: str | None = Field(default=None, max_length=240)


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=160)
    champion_candidate_id: str = Field(min_length=1, max_length=240)
    challenger_candidate_id: str = Field(min_length=1, max_length=240)
    policy: dict = Field(default_factory=dict)
    scope: dict = Field(default_factory=dict)
    random_seed: int = Field(default=1, ge=0, le=2**31 - 1)


class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=160)
    strategy_family: str | None = Field(default=None, max_length=120)
    candidate_ids: list[str] = Field(default_factory=list, max_length=9)
    selection_criteria: dict = Field(default_factory=dict)
    policy_versions: dict = Field(default_factory=dict)


class MatchedExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=160)
    champion_candidate_id: str = Field(min_length=1, max_length=240)
    challenger_candidate_ids: list[str] = Field(min_length=1, max_length=8)
    campaign_id: str | None = Field(default=None, max_length=240)
    method: str = Field(default="OUT_OF_SAMPLE_VALIDATION", max_length=40)
    scope: dict = Field(default_factory=dict)
    bars: int = Field(default=4000, ge=600, le=10000)
    train_fraction: float = Field(default=.6, gt=0, lt=1)
    validation_fraction: float = Field(default=.2, gt=0, lt=1)
    holdout_fraction: float = Field(default=.2, gt=0, lt=1)
    warmup: int = Field(default=0, ge=0, le=5000)
    fee: str = Field(default="0", max_length=40)
    slippage: str = Field(default="0", max_length=40)
    funding: str | None = Field(default=None, max_length=40)
    runs: int = Field(default=1000, ge=100, le=5000)
    random_seed: int = Field(default=1, ge=0, le=2**31 - 1)
    policy_version: str | None = Field(default="qualification.v1", max_length=80)
    comparison_policy_version: str | None = Field(default="challenger.v1", max_length=80)
    cost_model_version: str | None = Field(default=None, max_length=80)
    classifier_version: str | None = Field(default=None, max_length=80)
    software_revision: str | None = Field(default=None, max_length=160)


class AgentHypothesisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=240)
    campaign_id: str | None = Field(default=None, max_length=240)
    question: str | None = Field(default=None, max_length=1000)
    expected_effect: str | None = Field(default=None, max_length=500)
    proposed_config: dict
    dataset_scope: dict = Field(default_factory=dict)
    methodology: dict = Field(default_factory=lambda: {"method": "OUT_OF_SAMPLE_VALIDATION"})
    cost_assumptions: dict = Field(default_factory=dict)
    uncertainties: list[str] = Field(default_factory=list, max_length=20)
    lookback_verified: bool = False
    lookback_spec: dict | None = None


class AgentApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(default="APPROVED", max_length=20)
    max_jobs: int = Field(default=2, ge=1, le=2)


class ForwardReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: Literal["FORWARD_SHADOW_DECISION", "ACTIVE_DECISION_OBSERVATION"]
    source_id: str = Field(min_length=1, max_length=240)
    start: str = Field(min_length=1, max_length=50)
    end: str = Field(min_length=1, max_length=50)
    policy_version: str = Field(default="forward-qualification.v1", min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=160)
    active_source_id: str | None = Field(default=None, min_length=1, max_length=240)
    paper_instance_id: str | None = Field(default=None, min_length=1, max_length=120)
    paper_session_id: str | None = Field(default=None, min_length=1, max_length=120)
    supersedes_report_id: str | None = Field(default=None, min_length=1, max_length=240)


class PaperReconciliationRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source_id:str=Field(min_length=1,max_length=240)
    start:str=Field(min_length=10,max_length=80)
    end:str=Field(min_length=10,max_length=80)
    idempotency_key:str=Field(min_length=8,max_length=160)
    supersedes_report_id:str|None=Field(default=None,max_length=240)


class ForwardReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=160)
    decision: Literal["ACKNOWLEDGED", "REJECTED", "REQUEST_MORE_EVIDENCE", "ARCHIVED"]
    reason: str = Field(min_length=3, max_length=1000)


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
                  owner_resolver: Callable | None = None,
                  shadow_provider: Callable | None = None,
                  operator_authorizer: Callable | None = None) -> APIRouter:
    """Inject cache/auth seams for isolated consumers; defaults use existing app."""
    router = APIRouter(prefix="/api/v2/strategy-intelligence", tags=["Strategy Intelligence v2"])
    provider = service_provider or (lambda: _runtime("strategy_intelligence"))
    manager_source = manager_provider or (lambda: _runtime("instance_manager"))
    resolve_owner = owner_resolver or _authenticated_owner

    def shadow():
        result = shadow_provider() if shadow_provider else _runtime("shadow_router")
        if result is None: raise HTTPException(503, "Shadow observation unavailable")
        return result

    def shadow_call(operation):
        try: return operation()
        except KeyError: raise HTTPException(404, "Shadow registration or candidate not found") from None
        except PermissionError: raise HTTPException(403, "Forbidden shadow capability") from None
        except ValueError as exc: raise HTTPException(409, str(exc)) from None

    @router.get("/shadow/policy")
    def shadow_policy(request: Request, response: Response):
        owner(request, response)
        return {"policy": shadow().policy, "execution_mode": "shadow_observation", "orders_allowed": False}

    @router.get("/shadow/requirements/{candidate_id}")
    def shadow_requirements(candidate_id: str, request: Request, response: Response):
        identity = owner(request, response)
        return shadow_call(lambda: shadow().requirements(candidate_id, owner_id=identity))

    @router.post("/shadow/registrations", status_code=201)
    def shadow_register(request: Request, response: Response, body: ShadowRegistrationRequest):
        identity = owner(request, response)
        result = shadow_call(lambda: shadow().register(body.model_dump(), owner_id=identity))
        return {key: value for key, value in result.items() if key != "lease_token"}

    @router.get("/shadow/registrations")
    def shadow_list(request: Request, response: Response, limit: int = Query(100, ge=1, le=1000)):
        identity = owner(request, response)
        return {"rows": [{key: value for key, value in row.items() if key != "lease_token"} for row in shadow().store.registrations(owner_id=identity, limit=limit)], "limit": limit}

    @router.get("/shadow/registrations/{rid}")
    def shadow_status(rid: str, request: Request, response: Response):
        identity = owner(request, response)
        result = shadow().store.get(rid, owner_id=identity)
        if not result: raise HTTPException(404, "Shadow registration not found")
        # Lease ownership tokens are an internal commit capability, not UI data.
        return {key: value for key, value in result.items() if key != "lease_token"}

    @router.post("/shadow/registrations/{rid}/{action}")
    def shadow_action(rid: str, action: str, request: Request, response: Response, body: ShadowActionRequest):
        identity = owner(request, response)
        result = shadow_call(lambda: shadow().action(rid, action.upper(), owner_id=identity, reason=body.reason))
        return {key: value for key, value in result.items() if key != "lease_token"}

    @router.get("/shadow/registrations/{rid}/decisions")
    def shadow_decisions(rid: str, request: Request, response: Response, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0, le=100000)):
        identity = owner(request, response)
        return shadow_call(lambda: {"rows": shadow().store.decisions(rid, owner_id=identity, limit=limit, offset=offset), "limit": limit, "offset": offset})

    @router.get("/shadow/registrations/{rid}/coverage")
    def shadow_coverage(rid: str, request: Request, response: Response):
        identity = owner(request, response)
        return shadow_call(lambda: shadow().store.analytics(rid, owner_id=identity))

    @router.get("/shadow/registrations/{rid}/comparison")
    def shadow_comparison(rid: str, request: Request, response: Response, limit: int = Query(1000, ge=1, le=1000)):
        identity = owner(request, response)
        return shadow_call(lambda: shadow().comparison(rid, owner_id=identity, limit=limit))

    @router.get("/shadow/registrations/{rid}/audit")
    def shadow_audit(rid: str, request: Request, response: Response, limit: int = Query(100, ge=1, le=1000)):
        identity = owner(request, response)
        return shadow_call(lambda: {"rows": shadow().store.events(rid, owner_id=identity, limit=limit), "limit": limit})

    def active_sources():
        result=getattr(shadow(),"active_sources",None)
        if result is None: raise HTTPException(503,"Read-only active evidence adapter unavailable")
        return result

    def shadow_comparisons():
        result=getattr(shadow(),"comparison_service",None)
        if result is None: raise HTTPException(503,"Versioned shadow comparison unavailable")
        return result

    @router.get("/shadow/active-sources")
    def shadow_active_sources(request: Request,response: Response,limit: int=Query(100,ge=1,le=100)):
        identity=owner(request,response)
        return {"rows":active_sources().sources(owner_id=identity,limit=limit)}

    def active_window(start, end):
        from services.shadow_router import utc
        from datetime import timedelta,datetime,timezone
        a,b=utc(start),utc(end)
        if b<=a or b-a>timedelta(days=7) or b>datetime.now(timezone.utc):
            raise ValueError("INVALID_OBSERVATION_WINDOW")
        return a,b

    @router.get("/shadow/active-sources/{source_id}/health")
    def active_evidence_health(source_id: str,request: Request,response: Response,start: str=Query(max_length=50),end: str=Query(max_length=50)):
        identity=owner(request,response)
        def health():
            a,b=active_window(start,end)
            return active_sources().health(source_id,owner_id=identity,start=a,end=b)
        return shadow_call(health)

    @router.get("/shadow/active-sources/{source_id}/decisions")
    def active_evidence_timeline(source_id: str,request: Request,response: Response,start: str=Query(max_length=50),end: str=Query(max_length=50),limit: int=Query(100,ge=1,le=200),offset: int=Query(0,ge=0,le=2016)):
        identity=owner(request,response)
        def timeline():
            a,b=active_window(start,end)
            source,rows=active_sources().read(source_id,owner_id=identity,start=a,end=b,limit=2016)
            return {"source":source,"rows":rows[offset:offset+limit],"total":len(rows),"limit":limit,"offset":offset,
                    "evidence_kind":"ACTIVE_DECISION_OBSERVATION","profitability":"NOT_ASSESSED"}
        return shadow_call(timeline)

    @router.get("/shadow/active-sources/{source_id}/coverage")
    def active_source_coverage(source_id: str,request: Request,response: Response,start: str=Query(max_length=50),end: str=Query(max_length=50)):
        from services.shadow_router import utc
        identity=owner(request,response)
        def read_source():
            a,b=utc(start),utc(end)
            from datetime import timedelta,datetime,timezone
            if b<=a or b-a>timedelta(days=7) or b>datetime.now(timezone.utc): raise ValueError("INVALID_OBSERVATION_WINDOW")
            source,rows=active_sources().read(source_id,owner_id=identity,start=a,end=b,limit=2016)
            return {"source":source,"start":a.isoformat(),"end":b.isoformat(),"record_count":len(rows),"profitability":"NOT_ASSESSED","basis":"PERSISTED_RECORDS; NOT COMPLETE ELIGIBLE OPPORTUNITIES"}
        return shadow_call(read_source)

    @router.post("/shadow/comparisons",status_code=201)
    def create_shadow_comparison(request: Request,response: Response,body: ShadowComparisonRequest):
        identity=owner(request,response)
        return shadow_call(lambda:shadow_comparisons().create(body.model_dump(),owner_id=identity))

    @router.get("/shadow/comparisons")
    def list_shadow_comparisons(request: Request,response: Response,limit: int=Query(50,ge=1,le=100),offset: int=Query(0,ge=0,le=10000)):
        identity=owner(request,response)
        return {"rows":shadow_comparisons().list(owner_id=identity,limit=limit,offset=offset),"limit":limit,"offset":offset}

    @router.get("/shadow/comparisons/{comparison_id}")
    def get_shadow_comparison(comparison_id: str,request: Request,response: Response):
        identity=owner(request,response)
        result=shadow_comparisons().get(comparison_id,owner_id=identity)
        if result is None: raise HTTPException(404,"Shadow comparison not found")
        return result

    @router.get("/shadow/comparisons/{comparison_id}/timeline")
    def shadow_comparison_timeline(comparison_id: str,request: Request,response: Response,limit: int=Query(100,ge=1,le=1000),offset: int=Query(0,ge=0,le=2016)):
        result=get_shadow_comparison(comparison_id,request,response)
        return {"rows":result["timeline"][offset:offset+limit],"total":len(result["timeline"]),"limit":limit,"offset":offset}

    def forward_service():
        runtime=shadow()
        existing=getattr(runtime,"forward_qualification_service",None)
        if existing is not None: return existing
        from services.forward_qualification import ForwardQualificationService
        from services.forward_qualification_store import ForwardQualificationStore
        from services.forward_paper_evidence_reader import read_paper_references, read_holdout_provenance
        def paper_reader(body,identity,tenant,start,end):
            scope=instance_scope(body["paper_instance_id"],body.get("paper_session_id"),tenant)
            service=provider()
            if service is None: raise HTTPException(503,"Paper evidence journal unavailable")
            return read_paper_references(service.journal.path,scope=scope,identity=identity,start=start,end=end)
        def holdout_reader(source,identity,tenant):
            service=provider()
            from services.worker_backends import research_context
            return read_holdout_provenance(research_context(service.context) if service else None,source,identity,owner_id=tenant)
        def operational_reader(source_id,tenant,start,end,timeframe):
            return operational_store().report(source_id,owner_id=tenant,start=start,end=end,timeframe=timeframe)
        result=ForwardQualificationService(runtime,ForwardQualificationStore(runtime.store),paper_reader=paper_reader,holdout_reader=holdout_reader,operational_reader=operational_reader)
        runtime.forward_qualification_service=result
        return result

    def forward_call(operation):
        import sqlite3
        try: return shadow_call(operation)
        except sqlite3.Error as exc:
            logger.warning("forward_evidence_store_unavailable",extra={"error_category":type(exc).__name__})
            raise HTTPException(503,"Forward evidence persistence unavailable") from None
        except Exception as exc:
            if type(exc).__module__.startswith('psycopg'):
                logger.warning('forward_evidence_store_unavailable',extra={'error_category':type(exc).__name__})
                raise HTTPException(503,'Forward evidence persistence unavailable') from None
            raise

    def operational_store():
        runtime=shadow()
        cached=getattr(runtime,'operational_evidence_store',None)
        if cached is None:
            from services.operational_evidence import OperationalEvidenceStore
            cached=OperationalEvidenceStore(runtime.store,clock=runtime.clock)
            runtime.operational_evidence_store=cached
        return cached

    def reconciliation_service():
        runtime=shadow()
        cached=getattr(runtime,'paper_reconciliation_service',None)
        if cached is None:
            from services.paper_reconciliation_store import PaperReconciliationService
            from services.paper_evidence_reader import PaperEvidenceReader
            def instance_snapshot(source,tenant):
                scope=instance_scope(source['instance_id'],source.get('session_id'),tenant)
                getter=_runtime('ledger')
                if getter is None or not hasattr(getter,'get_authoritative_evidence_snapshot'):
                    return {'limitations':['AUTHORITATIVE_LEDGER_UNAVAILABLE']}
                original=getter.get_authoritative_evidence_snapshot(source['instance_id'],scope.get('simulation_session_id') or '')
                return {**original,'snapshot_semantics':'ORIGINAL_AUTHORITATIVE_LEDGER_SNAPSHOT; ACCOUNT_EQUITY_AUTHORITY_NOT_ATTESTED'}
            reader=PaperEvidenceReader(runtime.active_sources,instance_snapshot=instance_snapshot)
            cached=PaperReconciliationService(runtime.store,reader,clock=runtime.clock)
            runtime.paper_reconciliation_service=cached
        return cached

    @router.get('/operational-reliability/sources')
    def operational_sources(request:Request,response:Response,limit:int=Query(50,ge=1,le=100)):
        identity=owner(request,response)
        return forward_call(lambda:{'rows':operational_store().sources(owner_id=identity,limit=limit)})

    @router.get('/operational-resilience/health')
    def resilience_health(request:Request,response:Response):
        identity=owner(request,response)
        def read():
            import os
            from pathlib import Path
            op=operational_store();raw=op.shadow._store
            with raw._lock:
                rows=raw._db.execute('SELECT COUNT(*),COALESCE(SUM(payload_bytes),0) FROM operational_events WHERE owner_id=?',(identity,)).fetchone()
            spool={'status':'UNKNOWN','reason':'DURABLE_SPOOL_NOT_CONFIGURED_OR_NOT_RETAINED'}
            selected=os.environ.get('NEXUS_OPERATIONAL_SPOOL_PATH')
            if not selected and getattr(raw,'backend',None)!='postgresql':selected=str(Path(raw.path).with_suffix('.operational-spool.db'))
            if selected and Path(selected).is_file():
                from services.operational_spool import DurableOperationalSpool
                spool=DurableOperationalSpool(selected).health(owner_id=identity)
            return {'scope':'OWNED_METADATA_EVIDENCE; NOT_CONTINUOUS_UPTIME_CERTIFICATION','spool':spool,
                'raw_retention':{'owner_events':rows[0],'owner_payload_bytes':rows[1],'owner_event_limit':op.max_owner_events,
                    'owner_byte_limit':op.max_owner_bytes,'reporting_retention_seconds':op.retention_seconds,
                    'raw_reclamation':'BLOCKED_UNTIL_REVIEWED_REPORT_PIN_POLICY'},
                'feed_soak':{'status':'BLOCKED','reason':'NO_AUTHORIZED_SUSTAINED_LIVE_FEED_SOAK_EVIDENCE'},
                'worker_supervision':'CONSULT_IMMUTABLE_LOCAL_READINESS_ASSESSMENTS','execution_authorization':False}
        return forward_call(read)

    def local_readiness_store():
        from services.local_readiness import LocalReadinessStore
        return LocalReadinessStore(shadow().store)

    @router.get('/local-readiness')
    def local_readiness_history(request:Request,response:Response,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=10000)):
        identity=owner(request,response)
        return forward_call(lambda:{'rows':local_readiness_store().history(owner_id=identity,limit=limit,offset=offset),
            'production_certification':False,'execution_authorization':False})

    @router.get('/remote-staging-readiness')
    def remote_staging_health(request:Request,response:Response):
        identity=owner(request,response)
        if operator_authorizer:
            if not operator_authorizer(request):raise HTTPException(403,'Operator role required')
        else:
            importlib.import_module('app')._require_role(request,'operator')
        import os
        from services.remote_staging import remote_readiness,read_operator_diagnostics
        return remote_readiness(owner_id=identity,diagnostics=read_operator_diagnostics(os.environ.get('NEXUS_STAGING_DIAGNOSTICS_FILE')))

    @router.get('/deployment-readiness')
    def deployment_health(request:Request,response:Response):
        identity=owner(request,response)
        if operator_authorizer:
            if not operator_authorizer(request):raise HTTPException(403,'Operator role required')
        else:
            importlib.import_module('app')._require_role(request,'operator')
        import os
        from services.deployment_readiness import deployment_readiness
        def metadata():
            context=research_service().context
            raw=shadow().store._store
            with context.journal._lock:context.journal._c.execute('SELECT 1')
            with raw._lock:raw._db.execute('SELECT 1')
            return {'backend':getattr(context,'backend','sqlite'), 'available':True,
                'research_authority':getattr(context.journal,'authority_id',None),
                'shadow_authority':getattr(raw,'authority_id',None)}
        return deployment_readiness(owner_id=identity,auth_verified=True,metadata_probe=metadata,
            worker_paths={'research':os.environ.get('NEXUS_RESEARCH_HEALTH_FILE'),
                          'shadow':os.environ.get('NEXUS_SHADOW_HEALTH_FILE')},
            shadow_enabled=os.environ.get('NEXUS_SHADOW_SUPERVISED')=='1',
            spool_path=os.environ.get('NEXUS_OPERATIONAL_SPOOL_PATH'))

    @router.get('/local-readiness/{assessment_id}')
    def local_readiness_detail(assessment_id:str,request:Request,response:Response):
        identity=owner(request,response)
        result=forward_call(lambda:local_readiness_store().get(assessment_id,owner_id=identity))
        if result is None:raise HTTPException(404,'Local readiness assessment not found')
        return result

    @router.get('/operational-reliability/sources/{source_id}')
    def operational_report(source_id:str,request:Request,response:Response,start:str=Query(...,max_length=80),end:str=Query(...,max_length=80)):
        identity=owner(request,response)
        scope=forward_call(lambda:operational_store().source(source_id,owner_id=identity))
        if scope is None:raise HTTPException(404,'Operational evidence source not found')
        return forward_call(lambda:operational_store().report(source_id,owner_id=identity,start=start,end=end,timeframe=scope.get('timeframe','5m')))

    @router.get('/operational-reliability/sources/{source_id}/events')
    def operational_events(source_id:str,request:Request,response:Response,start:str=Query(...,max_length=80),end:str=Query(...,max_length=80),limit:int=Query(100,ge=1,le=200),offset:int=Query(0,ge=0,le=100000)):
        identity=owner(request,response)
        scope=forward_call(lambda:operational_store().source(source_id,owner_id=identity))
        if scope is None:raise HTTPException(404,'Operational evidence source not found')
        return forward_call(lambda:operational_store().event_page(source_id,owner_id=identity,start=start,end=end,limit=limit,offset=offset))

    @router.get('/paper-reconciliation/sources')
    def paper_reconciliation_sources(request:Request,response:Response,limit:int=Query(50,ge=1,le=100)):
        identity=owner(request,response)
        return forward_call(lambda:{'rows':reconciliation_service().reader.sources(owner_id=identity,limit=limit)})

    @router.get('/paper-reconciliation/sources/{source_id}/availability')
    def paper_reconciliation_availability(source_id:str,request:Request,response:Response):
        identity=owner(request,response)
        return forward_call(lambda:reconciliation_service().reader.availability(source_id,owner_id=identity))

    @router.get('/paper-reconciliation/sources/{source_id}/lineage')
    def native_paper_lineage(source_id:str,request:Request,response:Response,start:str=Query(...,max_length=80),end:str=Query(...,max_length=80),limit:int=Query(100,ge=1,le=200),offset:int=Query(0,ge=0,le=10000)):
        identity=owner(request,response)
        def read():
            from services.shadow_router import utc
            if utc(end)<=utc(start) or (utc(end)-utc(start)).total_seconds()>31*86400:raise ValueError('PAPER_LINEAGE_WINDOW_LIMIT')
            bundle=reconciliation_service().reader.read(source_id,owner_id=identity,start=start,end=end)
            episodes=bundle.get('episodes') or []
            return {'schema_version':'paper-lineage.v1','scope':bundle.get('scope'),
                'configuration_evidence':bundle.get('configuration_evidence'),'capture_health':bundle.get('capture_health'),
                'episodes':episodes[offset:offset+limit],'total_episodes':len(episodes),
                'funding_coverage':[{'episode_id':e['episode_id'],'status':e.get('funding_coverage','UNKNOWN'),
                    'original_exposure_reference':e.get('funding_exposure_reference')} for e in episodes[offset:offset+limit]],
                'cash_journal_complete':bundle.get('journal_complete',False),'cash_journal_count':len(bundle.get('journal') or []),
                'equity_status':(bundle.get('equity_series') or {}).get('status','UNKNOWN'),
                'limitations':bundle.get('limitations',[]),'execution_authorization':False}
        return forward_call(read)

    @router.post('/paper-reconciliation/preview')
    def preview_paper_reconciliation(request:Request,response:Response,body:PaperReconciliationRequest):
        identity=owner(request,response)
        return forward_call(lambda:reconciliation_service().assess(body.model_dump(exclude_none=True),owner_id=identity))

    @router.post('/paper-reconciliation/reports',status_code=201)
    def create_paper_reconciliation_report(request:Request,response:Response,body:PaperReconciliationRequest):
        identity=owner(request,response)
        return forward_call(lambda:reconciliation_service().assess(body.model_dump(exclude_none=True),owner_id=identity,commit=True))

    @router.get('/paper-reconciliation/reports')
    def paper_reconciliation_history(request:Request,response:Response,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=10000)):
        identity=owner(request,response)
        return forward_call(lambda:{'rows':reconciliation_service().history(owner_id=identity,limit=limit,offset=offset)})

    @router.get('/paper-reconciliation/reports/{report_id}')
    def paper_reconciliation_detail(report_id:str,request:Request,response:Response):
        identity=owner(request,response)
        result=forward_call(lambda:reconciliation_service().get(report_id,owner_id=identity,include_snapshot=True))
        if result is None:raise HTTPException(404,'Paper reconciliation report not found')
        return result

    @router.post('/paper-reconciliation/reports/{report_id}/review')
    def review_paper_evidence(report_id:str,request:Request,response:Response,body:ForwardReviewRequest):
        identity=owner(request,response)
        from services.paper_evidence_review import PaperEvidenceReview
        return forward_call(lambda:PaperEvidenceReview(reconciliation_service()).review(report_id,body.model_dump(),owner_id=identity))

    @router.get("/forward-qualification/policies")
    def forward_policies(request: Request,response: Response):
        owner(request,response)
        from services.forward_qualification import EVIDENCE_TAXONOMY
        return forward_call(lambda:{"rows":[{**p.as_dict(),"fingerprint":p.fingerprint} for p in forward_service().policies.values()],
            "evidence_taxonomy":EVIDENCE_TAXONOMY,"execution_authorization":False})

    @router.get("/forward-qualification/sources")
    def forward_sources(request: Request,response: Response,limit: int=Query(50,ge=1,le=100)):
        identity=owner(request,response)
        def sources():
            runtime=shadow()
            rows=[{"source_id":r["registration_id"],"source_kind":"FORWARD_SHADOW_DECISION",
                **{k:r.get(k) for k in ("strategy_id","strategy_version","config_fingerprint","symbol","timeframe","status")}}
                for r in runtime.store.registrations(owner_id=identity,limit=limit)]
            adapter=getattr(runtime,"active_sources",None)
            if adapter:
                rows.extend({**r,"source_kind":"ACTIVE_DECISION_OBSERVATION"} for r in adapter.sources(owner_id=identity,limit=limit))
            return {"rows":rows[:limit],"limit":limit,"eligibility":"REQUIRES_WINDOW_ASSESSMENT"}
        return forward_call(sources)

    @router.post("/forward-qualification/eligibility")
    def forward_eligibility(request: Request,response: Response,body: ForwardReportRequest):
        identity=owner(request,response)
        return forward_call(lambda:forward_service().preview(body.model_dump(exclude_none=True),owner_id=identity))

    @router.post("/forward-qualification/reports",status_code=201)
    def forward_create_report(request: Request,response: Response,body: ForwardReportRequest):
        identity=owner(request,response)
        return forward_call(lambda:forward_service().create(body.model_dump(exclude_none=True),owner_id=identity))

    @router.get("/forward-qualification/reports")
    @router.get("/forward-qualification/history")
    def forward_history(request: Request,response: Response,source_id: str|None=Query(default=None,max_length=240),limit: int=Query(50,ge=1,le=100),offset: int=Query(0,ge=0,le=10000)):
        identity=owner(request,response)
        return forward_call(lambda:forward_service().history(owner_id=identity,source_id=source_id,limit=limit,offset=offset))

    @router.get("/forward-qualification/reports/{report_id}")
    def forward_report(report_id: str,request: Request,response: Response):
        identity=owner(request,response)
        result=forward_call(lambda:forward_service().get(report_id,owner_id=identity))
        if result is None: raise HTTPException(404,"Forward qualification report not found")
        return result

    @router.post("/forward-qualification/reports/{report_id}/review",status_code=201)
    def forward_review(report_id: str,request: Request,response: Response,body: ForwardReviewRequest):
        identity=owner(request,response)
        return forward_call(lambda:forward_service().review(report_id,body.model_dump(),owner_id=identity))

    @router.get("/forward-qualification/windows/{window_id}")
    def forward_window(window_id: str,request: Request,response: Response,limit: int=Query(100,ge=1,le=200),offset: int=Query(0,ge=0,le=2016)):
        identity=owner(request,response)
        result=forward_call(lambda:forward_service().store.window(window_id,owner_id=identity))
        if result is None: raise HTTPException(404,"Forward observation window not found")
        # Bound evidence pages, retaining original hashes and counts. No raw
        # filesystem paths or provider credentials are accepted or returned.
        inputs=result["inputs"]
        return {**{k:v for k,v in result.items() if k!="inputs"},"inputs":{
            "decisions":inputs["decisions"][offset:offset+limit],"active":inputs["active"][offset:offset+limit]},
            "total_decisions":len(inputs["decisions"]),"total_active":len(inputs["active"]),"limit":limit,"offset":offset}

    @router.get("/forward-qualification/operations")
    def forward_operations(request: Request,response: Response):
        owner(request,response)
        from services.forward_paper_evidence_reader import operational_diagnostics
        return forward_call(lambda:operational_diagnostics(shadow()))

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

    def scoped_read(request, response, query, view, extra=None, *, require_exact=True):
        identity = owner(request, response)
        if require_exact:
            filters = exact_filters(query)
        else:
            filters = {key: getattr(query, key) for key in (
                "strategy_id", "strategy_version", "config_fingerprint", "lab_id",
                "execution_mode", "source_kind", "symbol") if getattr(query, key, None) is not None}
        filters.update(extra or {})
        return read(view, instance_scope(query.instance_id, query.simulation_session_id, identity), filters)

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

    @router.get("/overview")
    def overview(request: Request, response: Response, query: Annotated[OverviewQuery, Query()]):
        strategy_ids = [item.strip() for item in (query.strategy_ids or "").split(",") if item.strip()]
        return scoped_read(request, response, query, "overview", {"strategy_ids": strategy_ids}, require_exact=False)

    @router.get("/edge-map")
    def edge_map(request: Request, response: Response, query: Annotated[EdgeMapQuery, Query()]):
        allowed = {"symbol", "session", "trend_regime", "volatility_regime", "direction", "entry_timeframe"}
        if query.x_dimension not in allowed or query.y_dimension not in allowed or query.x_dimension == query.y_dimension:
            raise HTTPException(422, "Unsupported edge map dimensions")
        grouping = {query.x_dimension, query.y_dimension}
        supported = [set(item) for item in _GROUPINGS]
        if grouping not in supported:
            raise HTTPException(422, "Edge map dimension combination is not cached")
        return scoped_read(request, response, query, "edge_map",
                           {"x_dimension": query.x_dimension, "y_dimension": query.y_dimension, "metric": query.metric})

    @router.get("/comparison")
    def comparison(request: Request, response: Response, query: Annotated[ComparisonQuery, Query()]):
        strategy_ids = [item.strip() for item in query.strategy_ids.split(",") if item.strip()]
        if len(strategy_ids) < 2 or len(strategy_ids) > 4 or len(set(strategy_ids)) != len(strategy_ids):
            raise HTTPException(422, "Comparison requires two to four distinct strategy IDs")
        return scoped_read(request, response, query, "comparison", {"strategy_ids": strategy_ids})

    @router.get("/episodes")
    def episodes(request: Request, response: Response, query: Annotated[EpisodesQuery, Query()]):
        return scoped_read(request, response, query, "episodes", {
            "episode_id": query.episode_id, "limit": query.limit, "offset": query.offset})

    @router.get("/qualification")
    def qualification(request: Request, response: Response, query: Annotated[QualificationQuery, Query()]):
        return scoped_read(request, response, query, "qualification")

    @router.get("/edge-score/policies")
    def edge_score_policies(request: Request, response: Response, query: Annotated[DefinitionsQuery, Query()]):
        return read("edge_score_policies", {"owner_id": owner(request, response)}, {})

    @router.get("/monitoring/policies")
    def monitoring_policies(request:Request,response:Response,query:Annotated[DefinitionsQuery,Query()]):
        return read("monitoring_policies",{"owner_id":owner(request,response)},{})

    def monitoring_request(request,response,query):
        identity=owner(request,response)
        classifier={key:getattr(query,key) for key in ("classifier_id","classifier_version","parameter_hash")}
        if any(classifier.values()) and not all(classifier.values()):
            raise HTTPException(422,"Complete classifier identity is required")
        filters={**exact_filters(query),**classifier,"monitor_policy_version":query.monitor_policy_version}
        if isinstance(query,WeeklyResearchQuery):
            from services.weekly_strategy_research import review_week
            from datetime import datetime,timezone
            try: filters["week_start"]=review_week(query.week_start.isoformat() if query.week_start else None,datetime.now(timezone.utc).isoformat())
            except ValueError as exc: raise HTTPException(422,str(exc)) from None
            filters["limit"]=query.limit
        return instance_scope(query.instance_id,query.simulation_session_id,identity),filters

    @router.get("/decay")
    def decay(request:Request,response:Response,query:Annotated[MonitoringQuery,Query()]):
        scope,filters=monitoring_request(request,response,query)
        return {"status":"UNKNOWN","windows":[],**read("decay",scope,filters),
                "advisory_only":True,"orders_allowed":False,"strategy_changes_allowed":False}

    @router.get("/weekly-research")
    def weekly_research(request:Request,response:Response,query:Annotated[WeeklyResearchQuery,Query()]):
        scope,filters=monitoring_request(request,response,query)
        return {**read("weekly_research",scope,filters),"advisory_only":True,
                "orders_allowed":False,"strategy_changes_allowed":False}

    @router.post("/weekly-research",status_code=201)
    def generate_weekly_research(request:Request,response:Response,body:WeeklyResearchQuery):
        scope,filters=monitoring_request(request,response,body)
        try:
            service=provider()
            if service is None: raise HTTPException(503,"Research evidence unavailable")
            return service.generate_weekly_review(scope,filters)
        except ValueError as exc: raise HTTPException(409,str(exc)) from None
        except HTTPException: raise
        except Exception:
            logger.exception("weekly_research_generation_failed")
            raise HTTPException(503,"Research evidence unavailable") from None

    @router.get("/edge-score")
    def edge_score(request: Request, response: Response, query: Annotated[EdgeScoreQuery, Query()]):
        identity = owner(request, response)
        classifier = {key: getattr(query, key) for key in ("classifier_id", "classifier_version", "parameter_hash")}
        if any(classifier.values()) and not all(classifier.values()):
            raise HTTPException(422, "Complete classifier identity is required")
        filters = {**exact_filters(query), **classifier, "score_policy_version": query.score_policy_version}
        result = read("edge_score", instance_scope(query.instance_id, query.simulation_session_id, identity), filters)
        return {"status": "UNSCORED", "score": None, "band": None, **result,
                "advisory_only": True, "orders_allowed": False, "recommendation_eligible": False}

    @router.get("/selection")
    def selection(request: Request, response: Response, query: Annotated[SelectionQuery, Query()]):
        identity = owner(request, response)
        values = query.model_dump(exclude={"instance_id", "simulation_session_id"})
        classifier = [values.get(key) for key in ("classifier_id", "classifier_version", "parameter_hash")]
        if any(classifier) and not all(classifier):
            raise HTTPException(422, "Complete classifier identity is required")
        result = read("selection", instance_scope(query.instance_id, query.simulation_session_id, identity), values)
        return {"status": "UNAVAILABLE", "recommendation": None, **result,
                "advisory_only": True, "orders_allowed": False}

    @router.get("/oos")
    def oos(request: Request, response: Response, query: Annotated[QualificationQuery, Query()]):
        return scoped_read(request, response, query, "oos")

    @router.get("/walk-forward")
    def walk_forward(request: Request, response: Response, query: Annotated[QualificationQuery, Query()]):
        return scoped_read(request, response, query, "walk_forward")

    @router.get("/robustness")
    def robustness(request: Request, response: Response, query: Annotated[QualificationQuery, Query()]):
        return scoped_read(request, response, query, "robustness")

    @router.get("/validation-history")
    def validation_history(request: Request, response: Response, query: Annotated[ValidationHistoryQuery, Query()]):
        return scoped_read(request, response, query, "validation_history", {
            "method": query.method, "status": query.status, "limit": query.limit}, require_exact=False)

    def research_service():
        from services.research_jobs import ResearchJobService
        from services.worker_backends import research_context
        service = provider()
        if service is None:
            raise HTTPException(503, "Research service unavailable")
        return ResearchJobService(research_context(service.context))

    def research_worker(service):
        from services.research_jobs import ResearchJobWorker
        # ResearchJobService is intentionally lightweight and reconstructed per
        # request; keep the daemon on the shared context store so requests
        # and shutdown share one worker instance.
        worker = getattr(service.context, "_research_worker", None)
        if worker is None:
            worker = ResearchJobWorker(service.context, service=service)
            setattr(service.context, "_research_worker", worker)
        return worker

    def comparison_service():
        from services.strategy_comparison import StrategyComparisonService
        service = provider()
        if service is None:
            raise HTTPException(503, "Research service unavailable")
        context=research_service().context
        comparison = getattr(context, "_strategy_comparison_service", None)
        if comparison is None:
            comparison = StrategyComparisonService(context)
            setattr(context, "_strategy_comparison_service", comparison)
        return comparison

    def matched_service():
        from services.matched_experiments import MatchedExperimentService
        service = provider()
        if service is None:
            raise HTTPException(503, "Research service unavailable")
        context=research_service().context
        matched = getattr(context, "_matched_experiment_service", None)
        if matched is None:
            matched = MatchedExperimentService(context)
            setattr(context, "_matched_experiment_service", matched)
        return matched

    def agent_service():
        from services.research_agent import ResearchAgentService
        service = provider()
        if service is None:
            raise HTTPException(503, "Research service unavailable")
        context=research_service().context
        agent = getattr(context, "_research_agent_service", None)
        if agent is None:
            agent = ResearchAgentService(context)
            setattr(context, "_research_agent_service", agent)
        return agent

    @router.get("/research-agent/policy")
    def research_agent_policy(request: Request, response: Response):
        owner(request, response)
        agent = agent_service()
        return {"policy": agent.policy().as_dict(), "capabilities": agent.capabilities(),
                "forbidden_capabilities": sorted(__import__("services.research_agent", fromlist=["FORBIDDEN_CAPABILITIES"]).FORBIDDEN_CAPABILITIES)}

    @router.get("/research-agent/hypotheses")
    def list_agent_hypotheses(request: Request, response: Response, limit: int = Query(100, ge=1, le=1000)):
        identity = owner(request, response)
        return {"rows": agent_service().context.agent_hypotheses(owner_id=identity, limit=limit), "limit": limit}

    @router.post("/research-agent/hypotheses", status_code=201)
    def create_agent_hypothesis(request: Request, response: Response, body: AgentHypothesisRequest):
        identity = owner(request, response)
        try:
            return agent_service().generate_hypothesis(body.model_dump(), owner_id=identity)
        except KeyError:
            raise HTTPException(404, "Research candidate not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/research-agent/hypotheses/{hypothesis_id}")
    def get_agent_hypothesis(hypothesis_id: str, request: Request, response: Response):
        identity = owner(request, response)
        result = agent_service().context.get_agent_hypothesis(hypothesis_id, owner_id=identity)
        if result is None:
            raise HTTPException(404, "Research hypothesis not found")
        return result

    @router.post("/research-agent/hypotheses/{hypothesis_id}/validate")
    def validate_agent_hypothesis(hypothesis_id: str, request: Request, response: Response):
        identity = owner(request, response)
        try:
            return agent_service().validate_plan(hypothesis_id, owner_id=identity)
        except KeyError:
            raise HTTPException(404, "Research hypothesis not found") from None

    @router.post("/research-agent/hypotheses/{hypothesis_id}/approve")
    def approve_agent_hypothesis(hypothesis_id: str, request: Request, response: Response, body: AgentApprovalRequest):
        identity = owner(request, response)
        try:
            return agent_service().approve(hypothesis_id, owner_id=identity, reviewer_id=identity,
                                           decision=body.decision, max_jobs=body.max_jobs)
        except KeyError:
            raise HTTPException(404, "Research hypothesis not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post("/research-agent/hypotheses/{hypothesis_id}/submit", status_code=202)
    def submit_agent_hypothesis(hypothesis_id: str, request: Request, response: Response):
        identity = owner(request, response)
        try:
            result = agent_service().submit_approved(hypothesis_id, owner_id=identity)
            service = research_service(); research_worker(service).start()
            return {**result, "accepted": True, "execution": "asynchronous"}
        except KeyError:
            raise HTTPException(404, "Research hypothesis not found") from None
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.get("/research-agent/hypotheses/{hypothesis_id}/review")
    def review_agent_hypothesis(hypothesis_id: str, request: Request, response: Response):
        identity = owner(request, response)
        try:
            return agent_service().review(hypothesis_id, owner_id=identity)
        except KeyError:
            raise HTTPException(404, "Research hypothesis not found") from None

    @router.get("/research-agent/events")
    def list_agent_events(request: Request, response: Response, hypothesis_id: str | None = None,
                          limit: int = Query(100, ge=1, le=1000)):
        identity = owner(request, response)
        return {"rows": agent_service().context.agent_events(owner_id=identity, hypothesis_id=hypothesis_id, limit=limit), "limit": limit}

    @router.post("/research-jobs", status_code=202)
    def create_research_job(request: Request, response: Response, body: ResearchJobRequest):
        identity = owner(request, response)
        # Research is never permitted to select a live account.  Instance
        # binding is optional for isolated backtests and, when supplied, is
        # resolved through the same owner/session authority as all reads.
        instance_id = getattr(body, "instance_id", None)
        if instance_id:
            scope = instance_scope(instance_id, body.simulation_session_id, identity)
        else:
            scope = {"owner_id": identity}
        try:
            return research_service().create(body.model_dump(), owner_id=identity, scope=scope)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/research-jobs")
    def list_research_jobs(request: Request, response: Response, instance_id: str | None = None,
                           limit: int = Query(100, ge=1, le=1000)):
        identity = owner(request, response)
        scope = {"instance_id": instance_id} if instance_id else {}
        return {"rows": research_service().list(owner_id=identity, scope=scope, limit=limit), "limit": limit}

    @router.get("/research-jobs/{job_id}/results")
    def research_job_results(job_id: str, request: Request, response: Response):
        identity = owner(request, response)
        result = research_service().get(job_id, owner_id=identity)
        if result is None:
            raise HTTPException(404, "Research job not found")
        if result.get("status") != "COMPLETED" or not result.get("result"):
            raise HTTPException(409, "Research result is not durably committed")
        return {"job_id": job_id, "result": result["result"], "result_reference": result.get("result_reference")}

    @router.get("/research-jobs/{job_id}")
    def get_research_job(job_id: str, request: Request, response: Response):
        identity = owner(request, response)
        result = research_service().get(job_id, owner_id=identity)
        if result is None:
            raise HTTPException(404, "Research job not found")
        return result

    @router.post("/research-jobs/{job_id}/run", status_code=202)
    def run_research_job(job_id: str, request: Request, response: Response):
        identity = owner(request, response)
        try:
            service = research_service()
            job = service.get(job_id, owner_id=identity)
            if job is None:
                raise KeyError(job_id)
            research_worker(service).start()
            return {**job, "accepted": True, "execution": "asynchronous"}
        except KeyError:
            raise HTTPException(404, "Research job not found") from None

    @router.post("/research-jobs/{job_id}/cancel")
    def cancel_research_job(job_id: str, request: Request, response: Response):
        identity = owner(request, response)
        result = research_service().cancel(job_id, owner_id=identity)
        if result is None:
            raise HTTPException(404, "Research job not found")
        return result

    @router.get("/research-jobs/{job_id}/dataset")
    def research_dataset(job_id: str,request: Request,response: Response):
        from services.research_dataset_retention import ResearchDatasetRetention
        identity=owner(request,response)
        service=research_service()
        if service.get(job_id,owner_id=identity) is None: raise HTTPException(404,"Research job not found")
        try:
            retained=ResearchDatasetRetention(service.context).load(job_id,owner_id=identity)
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from None
        if retained is None: return {"retention_status":"UNKNOWN","reconstruction_status":"UNKNOWN","reason":"No retained exact input for this job; hash-only historical records are not reconstructability proof"}
        return {k:v for k,v in retained.items() if k!="bars"}

    @router.post("/research-candidates", status_code=201)
    def create_research_candidate(request: Request, response: Response, body: ResearchCandidateRequest):
        identity = owner(request, response)
        try:
            return comparison_service().create_candidate(body.model_dump(), owner_id=identity)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/research-candidates")
    def list_research_candidates(request: Request, response: Response, limit: int = Query(100, ge=1, le=1000)):
        identity = owner(request, response)
        return {"rows": comparison_service().candidates(owner_id=identity, limit=limit), "limit": limit}

    @router.get("/research-candidates/{candidate_id}/degradation")
    def candidate_degradation(candidate_id: str, request: Request, response: Response):
        identity = owner(request, response)
        try:
            return comparison_service().degradation(candidate_id, owner_id=identity)
        except KeyError:
            raise HTTPException(404, "Research candidate not found") from None

    @router.post("/research-campaigns", status_code=201)
    def create_research_campaign(request: Request, response: Response, body: CampaignRequest):
        identity = owner(request, response)
        try:
            return matched_service().create_campaign(body.model_dump(), owner_id=identity)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/research-campaigns")
    def list_research_campaigns(request: Request, response: Response, limit: int = Query(100, ge=1, le=1000)):
        identity = owner(request, response)
        return {"rows": matched_service().campaigns(owner_id=identity, limit=limit), "limit": limit}

    @router.get("/research-campaigns/{campaign_id}")
    def get_research_campaign(campaign_id: str, request: Request, response: Response):
        identity = owner(request, response)
        result = matched_service().campaign(campaign_id, owner_id=identity)
        if result is None: raise HTTPException(404, "Research campaign not found")
        return result

    @router.post("/matched-experiments", status_code=201)
    def create_matched_experiment(request: Request, response: Response, body: MatchedExperimentRequest):
        identity = owner(request, response)
        try:
            return matched_service().create_experiment(body.model_dump(), owner_id=identity)
        except KeyError:
            raise HTTPException(404, "Research candidate not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/matched-experiments")
    def list_matched_experiments(request: Request, response: Response, limit: int = Query(100, ge=1, le=1000)):
        identity = owner(request, response)
        return {"rows": matched_service().experiments(owner_id=identity, limit=limit), "limit": limit}

    @router.get("/matched-experiments/{experiment_id}/lineage")
    def matched_experiment_lineage(experiment_id: str, request: Request, response: Response):
        identity = owner(request, response)
        result = matched_service().experiment(experiment_id, owner_id=identity)
        if result is None: raise HTTPException(404, "Matched experiment not found")
        return {"experiment_id": experiment_id, "campaign_id": result.get("campaign_id"), "candidate_jobs": result.get("candidate_job_ids"),
                "validation_runs": result.get("validation_run_ids"), "candidates": result.get("candidates"), "scope": result.get("scope")}

    @router.get("/matched-experiments/{experiment_id}/holdout")
    def matched_experiment_holdout(experiment_id: str, request: Request, response: Response):
        identity = owner(request, response)
        result = matched_service().experiment(experiment_id, owner_id=identity)
        if result is None: raise HTTPException(404, "Matched experiment not found")
        scope = result.get("scope") or {}
        exposures = matched_service().context.overlapping_holdout_exposures(owner_id=identity, dataset_identity=scope.get("dataset_identity", ""), period_start=scope.get("holdout_start", scope.get("period_start")), period_end=scope.get("holdout_end", scope.get("period_end")))
        return {"status": result.get("holdout_status", "UNKNOWN"), "exposures": exposures,
                "warning": "Repeated exposure is not an untouched holdout." if len(exposures) > 1 else None}

    @router.post("/matched-experiments/{experiment_id}/submit", status_code=202)
    def submit_matched_experiment(experiment_id: str, request: Request, response: Response):
        identity = owner(request, response)
        try:
            result = matched_service().submit(experiment_id, owner_id=identity)
            # Reuse the existing process-local Sprint 4.6 worker; no new queue.
            service = research_service(); research_worker(service).start()
            return result
        except KeyError:
            raise HTTPException(404, "Matched experiment not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post("/matched-experiments/{experiment_id}/reconcile")
    def reconcile_matched_experiment(experiment_id: str, request: Request, response: Response):
        identity = owner(request, response)
        try:
            return matched_service().reconcile(experiment_id, owner_id=identity)
        except KeyError:
            raise HTTPException(404, "Matched experiment not found") from None

    @router.post("/matched-experiments/{experiment_id}/cancel")
    def cancel_matched_experiment(experiment_id: str, request: Request, response: Response):
        identity = owner(request, response)
        try:
            return matched_service().cancel(experiment_id, owner_id=identity)
        except KeyError:
            raise HTTPException(404, "Matched experiment not found") from None

    @router.get("/matched-experiments/{experiment_id}")
    def get_matched_experiment(experiment_id: str, request: Request, response: Response):
        identity = owner(request, response)
        result = matched_service().experiment(experiment_id, owner_id=identity)
        if result is None: raise HTTPException(404, "Matched experiment not found")
        return result

    @router.get("/research-candidates/{candidate_id}")
    def get_research_candidate(candidate_id: str, request: Request, response: Response):
        identity = owner(request, response)
        result = comparison_service().candidate(candidate_id, owner_id=identity)
        if result is None:
            raise HTTPException(404, "Research candidate not found")
        return result

    @router.post("/research-champions", status_code=201)
    def designate_research_champion(request: Request, response: Response, body: ChampionDesignationRequest):
        identity = owner(request, response)
        try:
            return comparison_service().context.designate_research_champion({**body.model_dump(),
                "designation_id": "designation:" + __import__("uuid").uuid4().hex,
                "owner_id": identity, "reviewer_id": identity, "review_status": "REVIEWED"})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/research-champions")
    def get_research_champion(request: Request, response: Response, scope_key: str = Query(..., min_length=1, max_length=240)):
        identity = owner(request, response)
        result = comparison_service().context.research_champion(owner_id=identity, scope_key=scope_key)
        return result or {"status": "NO_RESEARCH_CHAMPION_SELECTED", "scope_key": scope_key}

    @router.post("/comparisons", status_code=201)
    def create_strategy_comparison(request: Request, response: Response, body: ComparisonRequest):
        identity = owner(request, response)
        try:
            return comparison_service().compare(body.model_dump(), owner_id=identity)
        except KeyError:
            raise HTTPException(404, "Research candidate not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/comparisons")
    def list_strategy_comparisons(request: Request, response: Response, limit: int = Query(100, ge=1, le=1000)):
        identity = owner(request, response)
        return {"rows": comparison_service().comparisons(owner_id=identity, limit=limit), "limit": limit}

    @router.get("/comparisons/{comparison_id}")
    def get_strategy_comparison(comparison_id: str, request: Request, response: Response):
        identity = owner(request, response)
        result = comparison_service().context.get_strategy_comparison(comparison_id, owner_id=identity)
        if result is None:
            raise HTTPException(404, "Comparison not found")
        return result


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
