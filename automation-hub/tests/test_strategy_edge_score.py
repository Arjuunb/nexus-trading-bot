"""Versioned Edge Score: complete evidence, visible arithmetic, no promotion rights."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.strategy_edge_score import EdgeScorePolicy, calculate_edge_score
from services.strategy_intelligence_metrics import EvidenceCohort
from services.strategy_intelligence_v2 import calculate_context_performance
from test_historical_forward_evidence import SCOPE, financial_trades, setup_chain
from test_strategy_intelligence_v2 import context, episode, report


CLASSIFIER = {"classifier_id": "market_context_classifier", "classifier_version": "1.0.0", "parameter_hash": "b" * 64}


def score_inputs(tmp_path, monkeypatch, *, windows=3, scope=None):
    from services.historical_forward_evidence import historical_metrics
    scope = scope or SCOPE
    def at(day):
        trades = financial_trades()
        for row in trades:
            row["episode_id"] = f"day-{day}:" + row["episode_id"]
            for key in ("entry_time", "exit_time"):
                row[key] = (datetime.fromisoformat(row[key]) + timedelta(days=day-1)).isoformat()
        return historical_metrics({"trades": trades})
    def transform(method, r):
        if method == "walk_forward":
            r["windows"] = [{"development": at(1), "evaluation": at(day),
                "evaluation_start": f"2025-01-{day:02d}T00:00:00+00:00",
                "evaluation_end": f"2025-01-{day+5:02d}T00:00:00+00:00"} for day in (8, 15, 22)[:windows]]
    journal, service, filters, parent = setup_chain(tmp_path, monkeypatch, report_transform=transform, scope=scope)
    cohort = EvidenceCohort(**{**scope, **filters, "lab_id": None})
    now = datetime.now(timezone.utc).isoformat()
    rows = [episode(f"forward-{i}", **{**cohort.as_dict(), "opened_at": "2025-03-01T00:00:00Z",
        "closed_at": "2025-04-01T00:00:00Z", "net_pnl": "9" if i % 3 else "-9",
        "gross_pnl": "10" if i % 3 else "-8"}) for i in range(80)]
    contexts = [context(row["episode_id"], **{**cohort.as_dict(), **CLASSIFIER,
        "trend_regime": "BULL" if i < 40 else "BEAR", "entry_timeframe": "5m",
        "entry_timestamp": row["opened_at"], "signal_timestamp": "2025-02-28T23:59:00Z"}) for i, row in enumerate(rows)]
    from services.strategy_evidence_completeness import metrics_episode_watermark
    completeness = {**report(rows), "cohort": cohort.as_dict(), "calculated_at": now,
        "metrics_evidence_watermark": metrics_episode_watermark(rows, cohort)}
    def cached(grouping):
        return {**calculate_context_performance(rows, contexts, cohort=cohort, group_by=grouping,
            completeness_report=completeness, source_watermark="source-one", calculation_timestamp=now),
            "cache_status": "READY", "run_id": ":".join(grouping)}
    parent.clear(); parent.update(cached(("symbol",)))
    qualification = service.read("qualification", scope, filters)
    assert qualification["qualification_status"] == "QUALIFIED"
    return journal, service, filters, qualification, parent, cached(("trend_regime",))


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    return score_inputs(tmp_path, monkeypatch)


def score(evidence):
    _, _, filters, qualification, paper, regimes = evidence
    return calculate_edge_score({**SCOPE, **filters, "lab_id": None}, qualification, paper, regimes,
                                classifier=CLASSIFIER)


def test_score_has_nine_components_and_exact_visible_weighted_sum(evidence):
    before = deepcopy(evidence[3:])
    result = score(evidence)
    assert result["status"] == "SCORED", result["blocking_reasons"]
    # Independent fixture arithmetic: 10 + 10 + 8.474576 + 4 + 10 + 10
    # + 8.775 + 10 + 9.5; forward drawdown is 1.8R in actual close order.
    assert result["score"] == "80.749576"
    assert len(result["components"]) == 9
    assert Decimal(result["score"]) == sum(Decimal(c["weighted_points"]) for c in result["components"])
    assert 0 <= Decimal(result["score"]) <= 100
    assert sum(Decimal(c["weight"]) for c in result["components"]) == 100
    assert len(result["policy"]["fingerprint"]) == 64
    assert result["advisory_only"] is True and result["orders_allowed"] is False
    assert result["statistical_proof_of_profitability"] is False
    assert evidence[3:] == before


def test_score_identity_reproducible_and_ignores_read_time(evidence):
    a = score(evidence)
    evidence[3]["calculated_at"] = "2030-01-01T00:00:00Z"
    b = score(evidence)
    assert a == b


def test_policy_changes_require_a_new_version_and_keep_previous_policy(evidence):
    policy = EdgeScorePolicy()
    weights = dict(policy.weights)
    weights["expectancy"] = "10"; weights["forward_performance"] = "20"
    with pytest.raises(ValueError, match="version"):
        replace(policy, weights=tuple(weights.items()))
    changed = replace(policy, policy_version="edge_score.research.v2", weights=tuple(weights.items()))
    _, _, f, q, p, r = evidence
    result = calculate_edge_score({**SCOPE, **f, "lab_id": None}, q, p, r, classifier=CLASSIFIER, policy=changed)
    assert result["policy"]["fingerprint"] != score(evidence)["policy"]["fingerprint"]
    assert EdgeScorePolicy().as_dict() == policy.as_dict()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", True])
def test_invalid_policy_weights_rejected(value):
    weights = dict(EdgeScorePolicy().weights); weights["expectancy"] = value
    with pytest.raises(ValueError):
        EdgeScorePolicy(policy_version="edge_score.research.v2", weights=tuple(weights.items()))


@pytest.mark.parametrize("change", ["incomplete_chain", "stale_paper", "stale_regimes", "changed_source",
    "foreign_identity", "foreign_group", "foreign_run", "missing_financial_sample", "forged_summary",
    "duplicate_paper_episode", "incomplete_cost", "missing_original_risk", "ambiguous_parent", "wrong_classifier"])
def test_missing_conflicting_and_foreign_evidence_never_produces_score(evidence, change):
    _, _, _, q, p, r = evidence
    if change == "incomplete_chain": q["evidence_chain"]["status"] = "INCOMPLETE"
    if change == "stale_paper": p["cache_status"] = "STALE"
    if change == "stale_regimes": r["cache_status"] = "STALE"
    if change == "changed_source": r["source_watermark"] = "different-source"
    if change == "foreign_identity": q["identity"]["owner_id"] = "foreign"
    if change == "foreign_group": r["groups"][0]["cohort"]["owner_id"] = "foreign"
    if change == "foreign_run": q["validation_history"][0]["owner_id"] = "foreign"
    if change == "missing_financial_sample":
        next(row for row in q["validation_history"] if row["method"] == "historical_backtest")["report"]["metrics"]["financial_samples"][0]["funding"] = None
    if change == "forged_summary": q["evidence_chain"]["historical"]["metrics"]["expectancy_net_r"] = "999"
    if change == "duplicate_paper_episode": p["groups"][0]["episode_ids"][0] = p["groups"][0]["episode_ids"][1]
    if change == "incomplete_cost": p["groups"][0]["cost_coverage"]["status"] = "PARTIAL"
    if change == "missing_original_risk": p["groups"][0]["metrics"]["r_sample_size"] = None
    if change == "ambiguous_parent": p["groups"].append(deepcopy(p["groups"][0]))
    if change == "wrong_classifier": p["groups"][0]["classifier"]["parameter_hash"] = "c" * 64
    result = score(evidence)
    assert result["score"] is None and result["status"] == "UNSCORED"
    assert result["blocking_reasons"]


def test_one_window_does_not_claim_consistency_and_missing_weight_is_not_redistributed(tmp_path, monkeypatch):
    result = score(score_inputs(tmp_path, monkeypatch, windows=1))
    consistency = next(c for c in result["components"] if c["name"] == "consistency")
    assert consistency["normalized_score"] is None and consistency["weighted_points"] is None
    assert result["score"] is None
    assert consistency["weight"] == "10"


@pytest.mark.parametrize("change", ["single_regime", "small_regime", "unknown_regime", "uncovered_episode", "duplicate_membership"])
def test_regime_evidence_must_cover_every_paper_episode_without_cherry_picking(evidence, change):
    regimes = evidence[5]["groups"]
    if change == "single_regime": regimes.pop()
    if change == "small_regime": regimes[0]["metrics"]["completed_episode_count"] = 2
    if change == "unknown_regime": regimes[0]["group"]["trend_regime"] = "UNKNOWN"
    if change == "uncovered_episode": regimes[0]["episode_ids"].pop()
    if change == "duplicate_membership": regimes[0]["episode_ids"][0] = regimes[1]["episode_ids"][0]
    assert score(evidence)["score"] is None


@pytest.mark.parametrize("value", [None, True, "NaN", "Infinity"])
def test_unknown_nonfinite_or_boolean_financial_values_never_become_zero(evidence, value):
    evidence[4]["groups"][0]["metrics"]["expectancy_net_r"] = value
    assert score(evidence)["score"] is None


def test_adverse_cost_stress_is_required_even_when_calculation_status_passes(evidence):
    stress = next(row for row in evidence[3]["validation_history"] if row["method"] == "cost_stress")["report"]["stress"]
    stress.update(fee="0", slippage="0", funding="0", net_r=stress["gross_r"])
    result = score(evidence)
    assert result["score"] is None
    assert next(c for c in result["components"] if c["name"] == "cost_resilience")["status"] == "UNKNOWN"


def test_valid_negative_forward_economics_score_zero_instead_of_becoming_missing(evidence):
    evidence[4]["groups"][0]["metrics"]["expectancy_net_r"] = "-0.2"
    evidence[3]["evidence_chain"]["forward_paper"]["expectancy_net_r"] = "-0.2"
    evidence[3]["qualification_status"] = "NOT_QUALIFIED"
    result = score(evidence)
    assert result["status"] == "SCORED"
    assert next(c for c in result["components"] if c["name"] == "forward_performance")["normalized_score"] == "0"
    assert result["recommendation_eligible"] is False


def test_service_reads_persisted_caches_and_bound_reports_without_mutations(evidence, monkeypatch):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    journal, service, filters, q, paper, regimes = evidence
    for index, value in enumerate((paper, regimes)):
        cohort = EvidenceCohort(**value["cohort"])
        value = {**value, "run_id": "edge-cache-" + str(index),
            "calculated_at": value["calculation_timestamp"], "scope": SCOPE,
            "cache_key": service._cache_key(cohort, value["group_by"]), "input_watermark": "fixture-source"}
        journal.context.record_intelligence_run(value)
    monkeypatch.setattr(service, "read", StrategyIntelligenceService.read.__get__(service))
    service._ready_scopes.add(service._scope_key(SCOPE))
    before = journal.context.validation_runs(filters={"owner_id": "owner"})
    result = service.read("edge_score", SCOPE, {**filters, **CLASSIFIER})
    assert result["status"] == "SCORED", result["blocking_reasons"]
    assert result["evidence"]["paper_cache_run_id"] == "edge-cache-0"
    assert result["evidence"]["regime_cache_run_id"] == "edge-cache-1"
    assert journal.context.validation_runs(filters={"owner_id": "owner"}) == before
    again = service.read("edge_score", SCOPE, {**filters, **CLASSIFIER})
    assert again["score_id"] == result["score_id"]
    service.invalidate_scope(SCOPE)
    assert service.read("edge_score", SCOPE, {**filters, **CLASSIFIER})["score"] is None


@pytest.mark.parametrize("invalidation", [SCOPE, {"instance_id": "one"}])
def test_source_invalidation_during_scoring_revokes_total_and_component_values(evidence, monkeypatch, invalidation):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    _, service, filters, _, paper, regimes = evidence
    read = service.read
    def changed(view, scope, query):
        if view == "performance" and query.get("group_by") == ["trend_regime"]:
            service.invalidate_scope(invalidation)
            return deepcopy(regimes)
        return read(view, scope, query)
    monkeypatch.setattr(service, "read", changed)
    result = StrategyIntelligenceService.read(service, "edge_score", SCOPE, {**filters, **CLASSIFIER})
    assert result["score"] is None and result["band"] is None
    assert result["blocking_reasons"] == ["SCOPE_CHANGED_DURING_SCORING"]
    assert all(c["weighted_points"] is None for c in result["components"])


def test_authenticated_api_scores_calculated_persisted_evidence_and_policy(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from routers.strategy_intelligence import create_router
    from services.strategy_intelligence_service import StrategyIntelligenceService
    scope = {**SCOPE, "account_id": "instance:one:session"}
    journal, service, filters, q, paper, regimes = score_inputs(tmp_path, monkeypatch, scope=scope)
    for i, value in enumerate((paper, regimes)):
        cohort = EvidenceCohort(**value["cohort"])
        journal.context.record_intelligence_run({**value, "run_id": "api-edge-" + str(i),
            "calculated_at": value["calculation_timestamp"], "scope": scope,
            "cache_key": service._cache_key(cohort, value["group_by"]), "input_watermark": "fixture-source"})
    class Manager:
        store = SimpleNamespace(available=True)
        def instance_for(self, iid, owner):
            if iid not in {"one", "two"} or owner != "owner": raise KeyError(iid)
            return SimpleNamespace(simulation_session_id="session")
    # A route delegates to the full scorer with server-resolved scope, and a
    # different account cannot reuse the complete local evidence.
    monkeypatch.setattr(service, "read", StrategyIntelligenceService.read.__get__(service))
    service._ready_scopes.add(service._scope_key(scope))
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: service, manager_provider=Manager,
        owner_resolver=lambda req: req.headers.get("x-test-owner")))
    client = TestClient(app)
    params = {"instance_id": "one", **filters, **CLASSIFIER}
    assert client.get("/api/v2/strategy-intelligence/edge-score", params=params).status_code == 401
    assert client.get("/api/v2/strategy-intelligence/edge-score", params=params, headers={"x-test-owner": "foreign"}).status_code == 404
    response = client.get("/api/v2/strategy-intelligence/edge-score", params=params, headers={"x-test-owner": "owner"})
    assert response.status_code == 200 and response.json()["status"] == "SCORED", response.text
    assert response.json()["score"] is not None
    other = client.get("/api/v2/strategy-intelligence/edge-score", params={**params, "instance_id": "two"},
                       headers={"x-test-owner": "owner"})
    assert other.json()["score"] is None and other.json()["evidence"] == {}
    policies = client.get("/api/v2/strategy-intelligence/edge-score/policies", headers={"x-test-owner": "owner"})
    assert policies.json()["policies"][0] == EdgeScorePolicy().as_dict()


def test_policy_cannot_be_changed_through_a_mutable_weight_list():
    weights = [list(item) for item in EdgeScorePolicy().weights]
    policy = EdgeScorePolicy(weights=weights)
    original = policy.as_dict()
    weights[0][1] = "90"
    assert policy.as_dict() == original


def test_published_v1_policy_fingerprint_is_frozen():
    assert EdgeScorePolicy().as_dict()["fingerprint"] == "503fa1c6e14181bd00b8fca86b695330f4bfa02144376a89b929176706b1bd88"


@pytest.mark.parametrize("expectancy,normalized", [("-0.5", "0"), ("0", "0"), ("0.25", "50"), ("0.5", "100"), ("2", "100")])
def test_forward_normalization_is_monotonic_and_capped(evidence, expectancy, normalized):
    evidence[4]["groups"][0]["metrics"]["expectancy_net_r"] = expectancy
    evidence[3]["evidence_chain"]["forward_paper"]["expectancy_net_r"] = expectancy
    result = score(evidence)
    assert next(c for c in result["components"] if c["name"] == "forward_performance")["normalized_score"] == normalized


@pytest.mark.parametrize("drawdown,normalized", [("10", "50"), ("30", "25"), (None, None), ("-1", None)])
def test_worse_or_missing_drawdown_never_improves_the_score(evidence, drawdown, normalized):
    evidence[4]["groups"][0]["metrics"]["max_drawdown_r"] = drawdown
    result = score(evidence)
    assert next(c for c in result["components"] if c["name"] == "drawdown")["normalized_score"] == normalized
    if normalized is None: assert result["score"] is None


@pytest.mark.parametrize("count", [99, 100])
def test_minimum_historical_volume_is_a_gate_not_an_assumed_confidence(evidence, count):
    from services.historical_forward_evidence import historical_metrics
    metrics = historical_metrics({"trades": financial_trades(count)})
    q = evidence[3]
    q["evidence_chain"]["historical"]["metrics"] = metrics
    next(row for row in q["validation_history"] if row["method"] == "historical_backtest")["report"]["metrics"] = metrics
    result = score(evidence)
    assert (result["score"] is not None) == (count >= 100)
    if count < 100: assert "MINIMUM_FINANCIAL_SAMPLE_NOT_MET" in result["blocking_reasons"]


@pytest.mark.parametrize("net_pnl,expected,state", [("10", "100", "NO_LOSSES_OBSERVED"), ("0", "0", "ALL_BREAKEVEN")])
def test_explicit_no_loss_or_breakeven_financial_samples_are_not_an_invented_infinite_ratio(evidence, net_pnl, expected, state):
    from services.historical_forward_evidence import historical_metrics
    samples = financial_trades()
    for sample in samples:
        sample.update(net_pnl=net_pnl, gross_pnl=str(Decimal(net_pnl) + 1))
    metrics = historical_metrics({"trades": samples})
    q = evidence[3]
    q["evidence_chain"]["historical"]["metrics"] = metrics
    next(row for row in q["validation_history"] if row["method"] == "historical_backtest")["report"]["metrics"] = metrics
    q["qualification_status"] = "NOT_QUALIFIED"
    result = score(evidence)
    c = next(c for c in result["components"] if c["name"] == "profit_factor")
    assert c["normalized_score"] == expected and c["inputs"]["ratio_state"] == state
    assert c["inputs"]["historical_monetary_profit_factor"] is None
    assert result["recommendation_eligible"] is False


def negative_metrics(metrics):
    from services.historical_forward_evidence import historical_metrics
    samples = deepcopy(metrics["financial_samples"])
    for row in samples:
        row.update(net_pnl="-10", gross_pnl="-9")
    return historical_metrics({"trades": samples})


def test_two_negative_expectancies_do_not_create_positive_holdout_retention(evidence):
    oos = next(row for row in evidence[3]["validation_history"] if row["method"] == "out_of_sample")["report"]
    for key in ("train", "holdout"):
        oos[key] = negative_metrics(oos[key])
    evidence[3]["qualification_status"] = "NOT_QUALIFIED"
    result = score(evidence)
    assert next(c for c in result["components"] if c["name"] == "out_of_sample")["normalized_score"] == "0"


def test_consistency_counts_losing_windows_instead_of_using_only_successful_windows(evidence):
    wf = next(row for row in evidence[3]["validation_history"] if row["method"] == "walk_forward")["report"]
    wf["windows"][0]["evaluation"] = negative_metrics(wf["windows"][0]["evaluation"])
    result = score(evidence)
    assert next(c for c in result["components"] if c["name"] == "consistency")["normalized_score"] == "66.666667"


def test_losing_base_and_losing_stress_never_create_positive_resilience(evidence):
    cost = next(row for row in evidence[3]["validation_history"] if row["method"] == "cost_stress")["report"]
    cost["base"] = negative_metrics(cost["base"])
    cost["stress"].update(gross_r=cost["base"]["net_r"], net_r=str(Decimal(cost["base"]["net_r"]) - 2))
    result = score(evidence)
    assert next(c for c in result["components"] if c["name"] == "cost_resilience")["normalized_score"] == "0"


def test_foreign_provenance_cannot_leak_qualification_or_financial_values(evidence):
    evidence[3]["identity"]["owner_id"] = "foreign"
    evidence[3]["qualification_status"] = "PRIVATE_FOREIGN_STATE"
    result = score(evidence)
    assert "PRIVATE_FOREIGN_STATE" not in str(result)
    assert result["evidence"] == {} and all(c["inputs"] == {} for c in result["components"])


def test_unrelated_validation_assertions_do_not_replace_actual_paper_outcomes(evidence):
    original = score(evidence)
    q = evidence[3]
    q["evidence_chain"]["validation_run_ids"]["forward_paper"] = "untrusted-extra"
    q["validation_history"].append({"run_id": "untrusted-extra", "method": "forward_paper",
        "owner_id": "foreign", "status": "PASS", "report": {"net_pnl": "999999999"}})
    assert score(evidence) == original
    evidence[4]["groups"] = []
    assert score(evidence)["score"] is None


def test_actual_partial_and_final_paper_exits_remain_one_episode_and_financial_records_unchanged(tmp_path):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from test_strategy_evidence_runtime import entry, quote, runtime
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5, execution_id="edge-partial")
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="edge-final")
    service = StrategyIntelligenceService(store)
    service.refresh(ledger, scope=SCOPE)
    cohort = service.read("cohorts", SCOPE, {})["cohorts"][0]
    before = ledger.get_authoritative_evidence_snapshot()
    result = service.read("edge_score", SCOPE, {**cohort, "symbol": "XRPUSDT"})
    assert result["score"] is None and result["status"] == "UNSCORED"
    assert ledger.get_authoritative_evidence_snapshot() == before
    metrics = service.read("performance", SCOPE, {**cohort, "symbol": "XRPUSDT", "group_by": ["symbol"]})["groups"][0]["metrics"]
    assert metrics["completed_episode_count"] == 1


def test_other_tenant_invalidation_does_not_change_this_owners_score(evidence, monkeypatch):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    _, service, filters, _, _, regimes = evidence
    read = service.read
    def other_tenant_changed(view, scope, query):
        if view == "performance" and query.get("group_by") == ["trend_regime"]:
            service.invalidate_scope({"owner_id": "foreign"})
            return deepcopy(regimes)
        return read(view, scope, query)
    monkeypatch.setattr(service, "read", other_tenant_changed)
    result = StrategyIntelligenceService.read(service, "edge_score", SCOPE, {**filters, **CLASSIFIER})
    assert result["status"] == "SCORED" and result["score"] is not None


def test_stale_regime_view_cannot_be_advertised_as_ready(evidence, monkeypatch):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    _, service, filters, _, _, regimes = evidence
    regimes["cache_status"] = "STALE"
    read = service.read
    monkeypatch.setattr(service, "read", lambda view, scope, query:
        deepcopy(regimes) if view == "performance" and query.get("group_by") == ["trend_regime"]
        else read(view, scope, query))
    result = StrategyIntelligenceService.read(service, "edge_score", SCOPE, {**filters, **CLASSIFIER})
    assert result["score"] is None and result["cache_status"] == "STALE"
