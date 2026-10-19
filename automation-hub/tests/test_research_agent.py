import pytest

from data.journal_store import JournalStore
from services.research_agent import ResearchAgentService
from services.strategy_comparison import StrategyComparisonService


def _candidate(context, *, owner="owner", config=None):
    context.record_validation_run({"run_id": "validation:baseline", "strategy_id": "adaptive_trend_pullback",
        "strategy_version": "1.0.0", "config_fingerprint": "a" * 64, "dataset_identity": "dataset-1",
        "data_revision": "revision-1", "execution_mode": "backtest", "method": "historical_backtest", "status": "COMPLETED"})
    return StrategyComparisonService(context).create_candidate({
        "strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
        "frozen_config": config or {"min_score": 60, "rr": 2.0},
        "dataset_identity": "dataset-1", "data_revision": "revision-1", "symbol": "XRPUSDT",
        "venue": "binance", "market_type": "futures", "timeframe": "5m", "execution_mode": "backtest",
        "validation_run_ids": ["validation:baseline"], "evidence_quality": "COMPLETE",
        "scope": {"dataset_identity": "dataset-1", "data_revision": "revision-1", "period_start": "2025-01-01T00:00:00+00:00", "period_end": "2025-02-01T00:00:00+00:00", "symbol": "XRPUSDT", "timeframe": "5m", "venue": "binance", "market_type": "futures"}}, owner_id=owner)


def test_agent_rejects_fixed_and_unknown_parameters(tmp_path):
    service = ResearchAgentService(JournalStore(str(tmp_path / "agent.sqlite")).context)
    result = service.validate_variation(strategy_id="adaptive_trend_pullback", baseline={"rr": 2.0}, proposed={"rr": 2.5, "leverage": 10})
    assert result["ok"] is False
    assert any("leverage" in error for error in result["errors"])
    with pytest.raises(PermissionError): service.forbidden_action("PLACE_ORDER")
    with pytest.raises(PermissionError): service.forbidden_action("EXECUTE_CODE")


def test_hypothesis_requires_evidence_and_is_versioned(tmp_path):
    context = JournalStore(str(tmp_path / "agent.sqlite")).context
    candidate = _candidate(context)
    service = ResearchAgentService(context)
    hypothesis = service.generate_hypothesis({"candidate_id": candidate["candidate_id"], "proposed_config": {"min_score": 65, "rr": 2.0},
        "dataset_scope": candidate["scope"], "cost_assumptions": {"version": "cost.v1", "funding": "observed"},
        "lookback_verified": True, "lookback_spec": {"reviewed": True, "version": "fixture-lookback.v1"}}, owner_id="owner")
    assert hypothesis["baseline_fingerprint"] != hypothesis["proposed_fingerprint"]
    assert hypothesis["evidence_refs"] == ["validation:baseline"]
    assert service.context.get_agent_hypothesis(hypothesis["hypothesis_id"], owner_id="other") is None


def test_material_plan_change_invalidates_approval(tmp_path):
    context = JournalStore(str(tmp_path / "agent.sqlite")).context
    candidate = _candidate(context)
    service = ResearchAgentService(context)
    hypothesis = service.generate_hypothesis({"candidate_id": candidate["candidate_id"], "proposed_config": {"min_score": 65, "rr": 2.0},
        "dataset_scope": candidate["scope"], "cost_assumptions": {"version": "cost.v1", "funding": "observed"}, "lookback_verified": True, "lookback_spec": {"reviewed": True, "version": "fixture-lookback.v1"}}, owner_id="owner")
    approval = service.approve(hypothesis["hypothesis_id"], owner_id="owner", reviewer_id="reviewer")
    assert approval["decision"] == "APPROVED"
    context.update_agent_hypothesis(hypothesis["hypothesis_id"], owner_id="owner", plan={**hypothesis["plan"], "proposed_config": {"min_score": 70, "rr": 2.0}})
    with pytest.raises(PermissionError): service.submit_approved(hypothesis["hypothesis_id"], owner_id="owner")


def test_unknown_lookback_and_funding_block_approval(tmp_path):
    context = JournalStore(str(tmp_path / "agent.sqlite")).context
    candidate = _candidate(context)
    service = ResearchAgentService(context)
    hypothesis = service.generate_hypothesis({"candidate_id": candidate["candidate_id"], "proposed_config": {"min_score": 65, "rr": 2.0},
        "dataset_scope": candidate["scope"], "cost_assumptions": {}, "lookback_verified": False}, owner_id="owner")
    with pytest.raises(ValueError, match="LOOKBACK_UNVERIFIED"):
        service.approve(hypothesis["hypothesis_id"], owner_id="owner", reviewer_id="reviewer")
