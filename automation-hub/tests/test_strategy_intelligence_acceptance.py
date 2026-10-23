"""Task 5 API → persisted research → actual paper episode acceptance.

Historical inputs are synthetic financial fixtures, not claims of real edge.
Paper fills use the real execution engine and authoritative local test ledger.
Its missing cost coverage must remain a blocker throughout the workflow.
"""
from copy import deepcopy
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.strategy_intelligence import create_router
from services.strategy_comparison import StrategyComparisonService
from services.strategy_intelligence_service import StrategyIntelligenceService
from test_strategy_comparison_evidence import complete_candidate
from test_strategy_evidence_runtime import runtime, entry, quote

BASE = "/api/v2/strategy-intelligence"
SCOPE = {"owner_id": "owner", "instance_id": "one", "account_id": "instance:one:session",
         "simulation_session_id": "session"}


def acceptance_runtime(tmp_path):
    ledger, journal, decisions, paper, pipeline, capture = runtime(tmp_path)
    pipeline.journal_context["account_id"] = SCOPE["account_id"]
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5, execution_id="acceptance-partial")
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="acceptance-final")
    comparison = StrategyComparisonService(journal.context)
    champion = complete_candidate(comparison, "champion")
    challenger = complete_candidate(comparison, "challenger", target_rr=3, gain="20")
    service = StrategyIntelligenceService(journal)
    service.refresh(ledger, scope=SCOPE)
    return ledger, journal, service, champion, challenger


def acceptance_app(service):
    class Manager:
        store = SimpleNamespace(available=True)
        def instance_for(self, instance_id, owner_id):
            if instance_id != "one" or owner_id != "owner": raise KeyError(instance_id)
            return SimpleNamespace(simulation_session_id="session")
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: service, manager_provider=Manager,
        owner_resolver=lambda request: request.cookies.get("fixture_owner")))
    return app


def test_full_api_workflow_preserves_frozen_identity_finances_and_abstains_on_actual_cost_gaps(tmp_path):
    ledger, journal, service, champion, challenger = acceptance_runtime(tmp_path)
    client = TestClient(acceptance_app(service))
    before = deepcopy(ledger.get_authoritative_evidence_snapshot())
    events = journal.evidence_events()
    histories = journal.context.validation_runs(filters={"owner_id": "owner"})
    assert client.get(BASE+"/cohorts").status_code == 401
    client.cookies.set("fixture_owner", "owner")
    cohorts = client.get(BASE+"/cohorts").json()["cohorts"]
    assert len(cohorts) == 1
    cohort = cohorts[0]
    # This is the strategy identity actually captured at the fill, not an
    # identity supplied as a query assertion by this API client.
    assert cohort["config_fingerprint"] == champion["config_fingerprint"]
    params = {key: value for key, value in {**cohort, "symbol": "XRPUSDT"}.items()
              if value is not None and key not in {"owner_id", "account_id"}}
    qualification = client.get(BASE+"/qualification", params=params)
    assert qualification.status_code == 200, qualification.text
    q = qualification.json()
    assert q["status"] == "NOT_QUALIFIED"
    assert q["evidence_chain"]["historical"]["metrics"]["completed_episode_count"] == 120
    assert q["evidence_chain"]["forward_paper"]["completed_episodes"] == 1
    assert q["evidence_chain"]["forward_paper"]["cost_coverage_status"] != "COMPLETE"
    assert len(q["evidence_chain"]["forward_paper"]["episode_ids"]) == 1
    for endpoint in ("oos", "walk-forward", "robustness"):
        response = client.get(BASE+"/"+endpoint, params=params)
        assert response.status_code == 200, response.text
        assert response.json()["binding_status"] == "BOUND"
        assert response.json()["status"] == "PASS"
    score = client.get(BASE+"/edge-score", params=params)
    assert score.status_code == 200, score.text
    assert score.json()["score"] is None and score.json()["status"] == "UNSCORED"
    selection_query = {"instance_id": "one", "simulation_session_id": "session", "symbol": "XRPUSDT",
        "session": "LONDON", "trend_regime": "BULL", "volatility_regime": "NORMAL", "entry_timeframe": "5m"}
    selection = client.get(BASE+"/selection", params=selection_query)
    assert selection.status_code == 200, selection.text
    assert selection.json()["recommendation"] is None
    assert selection.json()["status"] == "INSUFFICIENT_EVIDENCE"
    decay = client.get(BASE+"/decay", params=params)
    assert decay.status_code == 200, decay.text
    assert decay.json()["status"] == "UNKNOWN" and decay.json()["windows"] == []
    weekly = client.post(BASE+"/weekly-research", json=params)
    assert weekly.status_code == 409
    assert client.get(BASE+"/weekly-research", params=params).json()["rows"] == []
    comparison = client.post(BASE+"/comparisons", json={"idempotency_key": "task5-api-comparison",
        "champion_candidate_id": champion["candidate_id"], "challenger_candidate_id": challenger["candidate_id"]})
    assert comparison.status_code == 201, comparison.text
    assert comparison.json()["recommendation"] == "CHALLENGER_PREFERRED"
    # This is a historical research comparison. It grants no qualification,
    # selection or permission to trade on the actual incomplete paper evidence.
    assert client.get(BASE+"/qualification", params=params).json()["status"] == "NOT_QUALIFIED"
    for endpoint in ("qualification", "edge-score", "decay", "weekly-research"):
        assert client.get(BASE+"/"+endpoint, params={**params, "owner_id": "foreign"}).status_code == 422
    client.cookies.set("fixture_owner", "foreign")
    for endpoint in ("qualification", "edge-score", "decay", "weekly-research"):
        assert client.get(BASE+"/"+endpoint, params=params).status_code == 404
    assert client.get(BASE+"/comparisons/"+comparison.json()["comparison_id"]).status_code == 404
    assert client.get(BASE+"/research-candidates").json()["rows"] == []
    assert ledger.get_authoritative_evidence_snapshot() == before
    assert journal.evidence_events() == events
    assert journal.context.validation_runs(filters={"owner_id": "owner"}) == histories


def test_restart_requires_reconciliation_and_cannot_reuse_old_paper_eligibility(tmp_path):
    ledger, journal, service, champion, _ = acceptance_runtime(tmp_path)
    before = ledger.get_authoritative_evidence_snapshot()
    restarted = StrategyIntelligenceService(journal)
    client = TestClient(acceptance_app(restarted))
    client.cookies.set("fixture_owner", "owner")
    query = {"instance_id": "one", "simulation_session_id": "session", "strategy_id": champion["strategy_id"],
        "strategy_version": champion["strategy_version"], "config_fingerprint": champion["config_fingerprint"],
        "symbol": "XRPUSDT", "execution_mode": "paper", "source_kind": "forward_paper"}
    for endpoint in ("edge-score", "decay", "qualification"):
        result = client.get(BASE+"/"+endpoint, params=query)
        assert result.status_code == 200
        assert result.json().get("score") is None
        assert result.json()["status"] in {"UNSCORED", "UNKNOWN", "NOT_QUALIFIED"}
    assert ledger.get_authoritative_evidence_snapshot() == before
