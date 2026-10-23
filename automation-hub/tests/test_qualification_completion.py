from copy import deepcopy

import pytest

from services.strategy_qualification import evaluate_qualification


def evidence():
    return dict(
        identity={"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
                  "config_fingerprint": "a" * 64},
        metrics={"completed_episode_count": 120, "net_profit_factor": "1.5",
                 "expectancy_net_r": "0.2", "max_drawdown_r": "4",
                 "cost_coverage": {"status": "COMPLETE"}},
        oos={"status": "PASS"}, walk_forward={"status": "PASS"},
        robustness={"concentration": {"concentration_status": "PASS"},
                    "monte_carlo": {"status": "PASS"}},
        cost_stress={"stress": {"status": "PASS", "net_r": "5"}},
        forward_paper={"status": "PASS", "completed_episodes": 40,
                       "expectancy_net_r": "0.15", "evidence_quality": "COMPLETE",
                       "cost_coverage_status": "COMPLETE",
                       "independence_status": "VERIFIED_INDEPENDENT"},
        evidence_quality="COMPLETE")


def test_complete_chain_qualifies_without_mutating_evidence():
    inputs = evidence()
    original = deepcopy(inputs)
    result = evaluate_qualification(**inputs)
    assert result["status"] == "QUALIFIED"
    assert result["policy"]["policy_version"] == "qualification.v2"
    assert inputs == original


@pytest.mark.parametrize("field", ["strategy_id", "strategy_version", "config_fingerprint"])
def test_unknown_frozen_identity_cannot_qualify(field):
    inputs = evidence()
    inputs["identity"].pop(field)
    result = evaluate_qualification(**inputs)
    assert result["status"] == "NOT_QUALIFIED"
    assert "frozen_strategy_identity" in result["failed_requirements"]


@pytest.mark.parametrize("changes", [
    {"expectancy_net_r": "-0.1"}, {"expectancy_net_r": "0"},
    {"expectancy_net_r": "NaN"}, {"expectancy_net_r": None},
    {"status": "FAIL"}, {"evidence_quality": "PARTIAL"},
    {"cost_coverage_status": "UNKNOWN"}, {"independence_status": "REUSED"},
])
def test_forward_count_alone_cannot_qualify(changes):
    inputs = evidence()
    inputs["forward_paper"].update(changes)
    result = evaluate_qualification(**inputs)
    assert result["status"] == "NOT_QUALIFIED"
    assert "forward_paper_profitability" in result["failed_requirements"]


@pytest.mark.parametrize("count", [True, "NaN", "Infinity", "120.5", -1, None, "invalid"])
def test_malformed_sample_fails_closed(count):
    inputs = evidence()
    inputs["metrics"]["completed_episode_count"] = count
    result = evaluate_qualification(**inputs)
    assert result["status"] == "NOT_QUALIFIED"
    assert "minimum_historical_episodes" in result["failed_requirements"]


@pytest.mark.parametrize("stage,check", [("cost_stress", "cost_stress"), ("robustness", "monte_carlo")])
def test_missing_stress_analysis_blocks_qualification(stage, check):
    inputs = evidence()
    inputs[stage] = None
    result = evaluate_qualification(**inputs)
    assert result["status"] == "NOT_QUALIFIED"
    assert check in result["unknown_requirements"]


def test_completed_cost_calculation_with_a_loss_is_not_a_passing_stress_test():
    from services.strategy_qualification import stress_costs
    inputs = evidence()
    inputs["cost_stress"] = {"stress": stress_costs(["1", "-2"], funding="0")}
    assert inputs["cost_stress"]["stress"]["status"] == "PASS"
    result = evaluate_qualification(**inputs)
    assert result["status"] == "NOT_QUALIFIED"
    assert "cost_stress" in result["failed_requirements"]


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING", "FAILED", "CANCELLED", "INTERRUPTED", "FAIL"])
def test_qualification_projection_ignores_uncommitted_results(tmp_path, monkeypatch, status):
    from data.journal_store import JournalStore
    from services.strategy_intelligence_service import StrategyIntelligenceService
    journal = JournalStore(tmp_path / "journal.sqlite")
    service = StrategyIntelligenceService(journal)
    scope = {"owner_id": "owner", "instance_id": "one", "account_id": "account",
             "simulation_session_id": "session"}
    filters = {"strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
               "config_fingerprint": "a" * 64, "execution_mode": "paper", "source_kind": "forward_paper"}
    journal.context.record_validation_run({
        **scope, **filters, "execution_mode": "backtest", "run_id": "older-passing-oos", "dataset_identity": "dataset",
        "created_at": "2026-01-01T00:00:00+00:00",
        "method": "out_of_sample", "status": "PASS", "report": {"status": "PASS"}})
    journal.context.record_validation_run({
        **scope, **filters, "execution_mode": "backtest", "run_id": "uncommitted-oos", "dataset_identity": "dataset",
        "method": "out_of_sample", "status": status, "report": {"status": "PASS"}})
    group = {"metrics": evidence()["metrics"], "cost_coverage": {"status": "COMPLETE"},
             "evidence_quality": {"status": "COMPLETE"}}
    snapshot = deepcopy(group)
    monkeypatch.setattr(service, "read", lambda *_a, **_kw: {"groups": [group]})
    result = service._read_qualification(scope, filters, {})
    assert "out_of_sample" in result["failed_requirements" if status == "FAIL" else "unknown_requirements"]
    assert group == snapshot
    assert len(result["validation_history"]) == 2


def test_qualification_does_not_pick_an_arbitrary_asset_or_classifier(tmp_path, monkeypatch):
    from data.journal_store import JournalStore
    from services.strategy_intelligence_service import StrategyIntelligenceService
    service = StrategyIntelligenceService(JournalStore(tmp_path / "journal.sqlite"))
    group = {"metrics": evidence()["metrics"], "cost_coverage": {"status": "COMPLETE"},
             "evidence_quality": {"status": "COMPLETE"}}
    monkeypatch.setattr(service, "read", lambda *_a, **_kw: {"groups": [group, deepcopy(group)]})
    result = service._read_qualification({"owner_id": "owner"}, {
        "strategy_id": "adaptive_trend_pullback", "execution_mode": "paper", "source_kind": "forward_paper"}, {})
    assert result["status"] == "NOT_QUALIFIED"
    assert "minimum_historical_episodes" in result["failed_requirements"]
    assert "evidence_quality" in result["failed_requirements"]
