"""Advisory selection uses qualified, exact and independently verified context evidence."""
from copy import deepcopy

import pytest

from services.strategy_selection import SELECTION_GROUPING, recommend_strategies


SCOPE = {"owner_id": "owner", "instance_id": "one", "account_id": "account-one",
         "simulation_session_id": "session"}
QUERY = {"symbol": "XRPUSDT", "session": "LONDON", "trend_regime": "BULL",
         "volatility_regime": "NORMAL", "entry_timeframe": "5m",
         "classifier_id": "market_context_classifier", "classifier_version": "1.0.0",
         "parameter_hash": "b" * 64, "lab_id": None}


def candidate(strategy="alpha", expectancy="0.4", **changes):
    cohort = {**SCOPE, "strategy_id": strategy, "strategy_version": "1.0.0",
              "config_fingerprint": "a" * 64, "symbol": "XRPUSDT", "lab_id": None,
              "execution_mode": "paper", "source_kind": "forward_paper"}
    ids = [strategy + str(i) for i in range(40)]
    period = {"first_opened_at": "2025-03-01T00:00:00Z", "last_closed_at": "2025-04-01T00:00:00Z"}
    group = {"cohort": cohort, "group": {key: QUERY[key] for key in SELECTION_GROUPING},
             "classifier": {key: QUERY[key] for key in ("classifier_id", "classifier_version", "parameter_hash")},
             "cost_model": {key + "_cost_model": key + ".v1" for key in ("fees", "funding", "slippage")},
             "metrics": {"completed_episode_count": 40, "r_sample_size": 40,
                         "expectancy_net_r": expectancy, "max_drawdown_r": "3",
                         "net_profit_factor": "1.5", "financial_evidence_complete": True},
             "episode_ids": ids, "profitability_verified": True,
             "context_quality": {"status": "VALID"}, "cost_coverage": {"status": "COMPLETE"},
             "evidence_quality": {"status": "COMPLETE", "binding_status": "MATCHED_SUBGROUP",
                                  "subgroup_episode_ids": ids, "source_watermark": "source-one"},
             "observation_period": period}
    result = {"cohort": cohort, "group": group,
              "qualification": {"qualification_status": "QUALIFIED", "identity": cohort,
                                "evidence_chain": {"status": "COMPLETE", "validation_run_ids": {"historical_backtest": "run-one"},
                                                   "forward_paper": {"source_watermark": "source-one", "episode_ids": ids}}},
              "performance": {"cohort": cohort, "group_by": list(SELECTION_GROUPING),
                              "cache_status": "READY", "run_id": strategy + "-cache",
                              "calculation_timestamp": "2026-10-18T00:00:00Z", "source_watermark": "source-one"}}
    result.update(changes)
    return result


class Cache:
    def __init__(self, candidates):
        self.candidates = candidates
        self.context = self

    def cached_cohorts(self, *, filters, limit):
        return deepcopy([c["cohort"] for c in self.candidates][:limit])

    def read(self, view, scope, filters):
        c = next(c for c in self.candidates if c["cohort"]["strategy_id"] == filters["strategy_id"])
        if view == "qualification":
            return deepcopy(c["qualification"])
        assert view == "performance" and filters["group_by"] == list(SELECTION_GROUPING)
        return deepcopy({**c["performance"], "groups": [c["group"]]})


def select(*candidates, query=None):
    return recommend_strategies(Cache(list(candidates)), SCOPE, query or QUERY)


def test_best_qualified_context_expectancy_wins_without_mutating_inputs():
    alpha, beta = candidate(), candidate("beta", "0.5")
    before = deepcopy([alpha, beta])
    result = select(alpha, beta)
    assert result["status"] == "RECOMMENDED"
    assert result["recommendation"]["cohort"]["strategy_id"] == "beta"
    assert result["advisory_only"] is True and result["orders_allowed"] is False
    assert result["policy"]["policy_version"] == "regime_selection.v1"
    assert [alpha, beta] == before


@pytest.mark.parametrize("field,value", [("symbol", "BTCUSDT"), ("session", "NEW_YORK"),
    ("trend_regime", "BEAR"), ("volatility_regime", "HIGH"), ("entry_timeframe", "15m")])
def test_context_dimensions_never_fall_back_to_parent_or_other_groups(field, value):
    c = candidate()
    c["group"]["group"][field] = value
    assert select(c)["recommendation"] is None


@pytest.mark.parametrize("change", ["unqualified", "stale", "incomplete_cost", "unknown_context",
    "unknown_quality", "unbound", "invalid_count", "small_sample", "missing_risk", "duplicate_episode",
    "forged_subgroup", "missing_metric", "nonfinite", "negative", "bad_pf", "missing_drawdown",
    "wrong_classifier", "changed_source", "foreign_group", "foreign_qualification", "incomplete_chain",
    "wrong_grouping", "missing_period", "foreign_episode"])
def test_incomplete_or_mismatched_evidence_cannot_be_recommended(change):
    c = candidate()
    if change == "unqualified": c["qualification"]["qualification_status"] = "NOT_QUALIFIED"
    if change == "stale": c["performance"]["cache_status"] = "STALE"
    if change == "incomplete_cost": c["group"]["cost_coverage"]["status"] = "PARTIAL"
    if change == "unknown_context": c["group"]["context_quality"]["status"] = "UNKNOWN"
    if change == "unknown_quality": c["group"]["evidence_quality"]["status"] = "UNKNOWN"
    if change == "unbound": c["group"]["evidence_quality"]["binding_status"] = "SUBGROUP_NOT_VERIFIED"
    if change == "invalid_count": c["group"]["metrics"]["completed_episode_count"] = True
    if change == "small_sample": c["group"]["metrics"]["completed_episode_count"] = 29
    if change == "missing_risk": c["group"]["metrics"]["r_sample_size"] = None
    if change == "duplicate_episode": c["group"]["episode_ids"][0] = c["group"]["episode_ids"][1]
    if change == "forged_subgroup": c["group"]["evidence_quality"]["subgroup_episode_ids"] = ["forged"]
    if change == "missing_metric": c["group"]["metrics"]["expectancy_net_r"] = None
    if change == "nonfinite": c["group"]["metrics"]["expectancy_net_r"] = "NaN"
    if change == "negative": c["group"]["metrics"]["expectancy_net_r"] = "-0.1"
    if change == "bad_pf": c["group"]["metrics"]["net_profit_factor"] = "1.1"
    if change == "missing_drawdown": c["group"]["metrics"]["max_drawdown_r"] = None
    if change == "wrong_classifier": c["group"]["classifier"]["parameter_hash"] = "c" * 64
    if change == "changed_source": c["performance"]["source_watermark"] = "source-two"
    if change == "foreign_group": c["group"]["cohort"] = {**c["cohort"], "account_id": "foreign"}
    if change == "foreign_qualification": c["qualification"]["identity"] = {**c["cohort"], "owner_id": "foreign"}
    if change == "incomplete_chain": c["qualification"]["evidence_chain"]["status"] = "INCOMPLETE"
    if change == "wrong_grouping": c["performance"]["group_by"] = ["symbol"]
    if change == "missing_period": c["group"].pop("observation_period")
    if change == "foreign_episode": c["qualification"]["evidence_chain"]["forward_paper"]["episode_ids"] = ["foreign"]
    result = select(c)
    assert result["status"] == "INSUFFICIENT_EVIDENCE" and result["recommendation"] is None
    assert result["candidates"][0]["blocking_reasons"]


def test_tie_abstains_and_order_is_deterministic():
    a, b = candidate(), candidate("beta")
    result = select(a, b)
    assert result["status"] == "TIED" and result["recommendation"] is None
    assert result == select(b, a)


def test_drawdown_then_sample_count_break_expectancy_ties():
    a, b = candidate(), candidate("beta")
    b["group"]["metrics"]["max_drawdown_r"] = "2"
    assert select(a, b)["recommendation"]["cohort"]["strategy_id"] == "beta"


@pytest.mark.parametrize("difference", ["cost_model", "nonoverlapping_period"])
def test_incompatible_evidence_is_not_ranked(difference):
    a, b = candidate(), candidate("beta", "0.5")
    if difference == "cost_model": b["group"]["cost_model"]["funding_cost_model"] = "different"
    else: b["group"]["observation_period"] = {"first_opened_at": "2025-05-01T00:00:00Z", "last_closed_at": "2025-06-01T00:00:00Z"}
    result = select(a, b)
    assert result["status"] == "NOT_COMPARABLE" and result["recommendation"] is None


def test_discovery_bound_abstains_instead_of_selecting_truncated_winner():
    assert select(*(candidate(str(i)) for i in range(101)))["status"] == "BOUND_EXCEEDED"


def test_foreign_discovery_never_leaks_or_reads_candidate():
    c = candidate()
    c["cohort"]["owner_id"] = "foreign"
    result = select(c)
    assert result["candidates"] == [] and result["recommendation"] is None


def test_unknown_request_context_is_rejected():
    with pytest.raises(ValueError):
        select(candidate(), query={**QUERY, "session": "UNKNOWN"})


def test_decimal_ranking_does_not_round_distinct_expectancies_to_a_tie():
    a, b = candidate(expectancy="0.40000000000000000000000000000000001"), candidate("beta", "0.4")
    assert select(a, b)["recommendation"]["cohort"]["strategy_id"] == "alpha"


def test_foreign_group_financials_and_validation_references_are_not_echoed():
    c = candidate()
    c["group"]["cohort"] = {**c["cohort"], "owner_id": "foreign"}
    c["group"]["metrics"]["expectancy_net_r"] = "123456.789"
    c["qualification"]["identity"] = {**c["cohort"], "owner_id": "foreign"}
    c["qualification"]["evidence_chain"]["validation_run_ids"] = {"historical_backtest": "foreign-private-run"}
    result = select(c)
    assert "123456.789" not in str(result) and "foreign-private-run" not in str(result)


def test_cached_context_calculator_partitions_timeframe_without_parent_count_inheritance():
    from dataclasses import replace
    from test_strategy_intelligence_v2 import COHORT, NOW, context, episode, report
    from services.strategy_evidence_completeness import metrics_episode_watermark
    from services.strategy_intelligence_v2 import calculate_context_performance
    cohort = replace(COHORT, execution_mode="paper", source_kind="forward_paper")
    rows = [episode(str(i), **{**cohort.as_dict(), "symbol": "XRPUSDT"}) for i in range(40)]
    contexts = [context(str(i), **{**cohort.as_dict(), "symbol": "XRPUSDT"},
                        entry_timeframe="5m" if i < 10 else "15m") for i in range(40)]
    completeness = {**report(rows), "cohort": cohort.as_dict(),
                    "metrics_evidence_watermark": metrics_episode_watermark(rows, cohort)}
    result = calculate_context_performance(rows, contexts, cohort=cohort, group_by=SELECTION_GROUPING,
        completeness_report=completeness, source_watermark="source-one", calculation_timestamp=NOW)
    groups = {g["group"]["entry_timeframe"]: g for g in result["groups"]}
    assert groups["5m"]["metrics"]["completed_episode_count"] == 10
    assert groups["15m"]["metrics"]["completed_episode_count"] == 30
    assert groups["5m"]["evidence_quality"]["subgroup_episode_ids"] == [str(i) for i in range(10)]
    assert all(g["profitability_verified"] is True for g in groups.values())


def test_real_qualification_chain_and_context_cache_drive_read_only_selection(tmp_path, monkeypatch):
    from test_historical_forward_evidence import setup_chain
    from services.strategy_intelligence_service import StrategyIntelligenceService
    journal, service, filters, parent = setup_chain(tmp_path, monkeypatch)
    c = candidate(filters["strategy_id"])
    cohort = {**SCOPE, **filters, "lab_id": None}
    c["cohort"] = cohort
    c["group"]["cohort"] = cohort
    ids = parent["groups"][0]["episode_ids"]
    c["group"]["episode_ids"] = ids
    c["group"]["evidence_quality"].update(subgroup_episode_ids=ids, source_watermark="paper-source-one")
    c["performance"].update(cohort=cohort, source_watermark="paper-source-one")
    before = journal.context.validation_runs(filters={"owner_id": "owner"})
    read = service.read
    monkeypatch.setattr(journal.context, "cached_cohorts", lambda **kw: [deepcopy(cohort)])
    monkeypatch.setattr(service, "read", lambda view, scope, query:
        deepcopy({**c["performance"], "groups": [c["group"]]}) if view == "performance" and query.get("group_by") == list(SELECTION_GROUPING)
        else read(view, scope, query))
    result = StrategyIntelligenceService.read(service, "selection", SCOPE, QUERY)
    assert result["status"] == "RECOMMENDED" and result["recommendation"]["cohort"] == cohort
    assert result["recommendation"]["evidence"]["validation_run_ids"]["historical_backtest"] == "historical_backtest"
    assert journal.context.validation_runs(filters={"owner_id": "owner"}) == before
    service.invalidate_scope(SCOPE)
    # Force an invalidation between the qualification and subgroup reads.
    original = service.read
    def changed(view, scope, query):
        result = original(view, scope, query)
        if view == "performance" and query.get("group_by") == list(SELECTION_GROUPING):
            service.invalidate_scope(scope)
        return result
    monkeypatch.setattr(service, "read", changed)
    result = StrategyIntelligenceService.read(service, "selection", SCOPE, QUERY)
    assert result["recommendation"] is None
    assert result["blocking_reasons"] == ["SCOPE_CHANGED_DURING_SELECTION"]


def test_actual_paper_episode_selection_preserves_authoritative_financial_snapshot(tmp_path):
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from test_strategy_evidence_runtime import entry, quote, runtime
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5, execution_id="selection-partial")
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="selection-final")
    service = StrategyIntelligenceService(store)
    service.refresh(ledger, scope=SCOPE)
    before = ledger.get_authoritative_evidence_snapshot()
    result = service.read("selection", SCOPE, QUERY)
    assert result["recommendation"] is None and result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["orders_allowed"] is False
    assert ledger.get_authoritative_evidence_snapshot() == before


def test_authenticated_api_reads_calculated_persisted_cache_and_real_qualification(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.strategy_intelligence import create_router
    from services.strategy_intelligence_metrics import EvidenceCohort
    from services.strategy_evidence_completeness import metrics_episode_watermark
    from services.strategy_intelligence_service import StrategyIntelligenceService
    from services.strategy_intelligence_v2 import calculate_context_performance
    from test_historical_forward_evidence import setup_chain
    from test_strategy_intelligence_v2 import context, episode, report
    scope = {**SCOPE, "account_id": "instance:one:session"}
    journal, service, filters, _ = setup_chain(tmp_path, monkeypatch, scope=scope)
    # Use the actual public reader for both persisted context projections.
    monkeypatch.setattr(service, "read", StrategyIntelligenceService.read.__get__(service))
    cohort = EvidenceCohort(**{**scope, **filters, "lab_id": None})
    now = datetime.now(timezone.utc).isoformat()
    rows = [episode(f"forward-{i}", **{**cohort.as_dict(),
        "opened_at": "2025-03-01T00:00:00Z", "closed_at": "2025-03-01T01:00:00Z",
        "net_pnl": "9" if i % 3 else "-9", "gross_pnl": "10" if i % 3 else "-8"}) for i in range(40)]
    contexts = [context(row["episode_id"], **{**cohort.as_dict(),
        **{key: QUERY[key] for key in (*SELECTION_GROUPING, "classifier_id", "classifier_version", "parameter_hash")},
        "entry_timestamp": row["opened_at"], "signal_timestamp": "2025-02-28T23:59:00Z"}) for row in rows]
    completeness = {**report(rows), "cohort": cohort.as_dict(), "calculated_at": now,
                    "metrics_evidence_watermark": metrics_episode_watermark(rows, cohort)}
    for grouping in (("symbol",), SELECTION_GROUPING):
        result = calculate_context_performance(rows, contexts, cohort=cohort, group_by=grouping,
            completeness_report=completeness, source_watermark="source-one", calculation_timestamp=now)
        result.update(run_id=str(len(grouping)), cache_key=service._cache_key(cohort, grouping),
            input_watermark="fixture-input", calculated_at=now, scope=scope)
        journal.context.record_intelligence_run(result)
    monkeypatch.setattr(journal.context, "cached_cohorts", lambda **kw: [cohort.as_dict()])
    service._ready_scopes.add(service._scope_key(scope))
    class Manager:
        store = SimpleNamespace(available=True)
        def instance_for(self, iid, owner):
            if iid != "one" or owner != "owner": raise KeyError(iid)
            return SimpleNamespace(simulation_session_id="session")
    app = FastAPI()
    app.include_router(create_router(service_provider=lambda: service,
        manager_provider=Manager, owner_resolver=lambda req: req.headers.get("x-test-owner")))
    client = TestClient(app)
    params = {"instance_id": "one", **{k: v for k, v in QUERY.items() if v is not None}}
    assert client.get("/api/v2/strategy-intelligence/selection", params=params).status_code == 401
    assert client.get("/api/v2/strategy-intelligence/selection", params=params,
                      headers={"x-test-owner": "foreign"}).status_code == 404
    response = client.get("/api/v2/strategy-intelligence/selection", params=params, headers={"x-test-owner": "owner"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "RECOMMENDED", [(c.get("qualification_status"), c.get("blocking_reasons")) for c in result.get("candidates", [])]
    assert result["recommendation"]["metrics"]["completed_episode_count"] == 40
    assert result["recommendation"]["evidence"]["source_watermark"] == "source-one"
    assert result["recommendation"]["evidence"]["cache_run_id"] == "5"
    service.invalidate_scope(scope)
    result = client.get("/api/v2/strategy-intelligence/selection", params=params,
                        headers={"x-test-owner": "owner"}).json()
    assert result["recommendation"] is None and result["status"] == "INSUFFICIENT_EVIDENCE"


def test_context_r_drawdown_uses_original_risk_and_actual_close_order():
    from test_strategy_intelligence_v2 import calculate, context, episode
    rows = [episode("a", net_pnl="-10", gross_pnl="-9", initial_risk="10", closed_at="2026-09-01T01:00:00Z"),
            episode("b", net_pnl="50", gross_pnl="51", initial_risk="100", closed_at="2026-09-01T02:00:00Z"),
            episode("c", net_pnl="-20", gross_pnl="-19", initial_risk="10", closed_at="2026-09-01T03:00:00Z")]
    result = calculate(list(reversed(rows)), [context(r["episode_id"]) for r in rows])
    metrics = result["groups"][0]["metrics"]
    assert metrics["max_drawdown_r"] == "2.5"
    assert metrics["max_drawdown_net_pnl"] == "20"
    rows[0]["initial_risk"] = None
    assert calculate(rows, [context(r["episode_id"]) for r in rows])["groups"][0]["metrics"]["max_drawdown_r"] is None
