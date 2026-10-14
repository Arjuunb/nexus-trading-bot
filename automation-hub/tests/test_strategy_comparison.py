from data.journal_store import JournalStore
from services.strategy_comparison import StrategyComparisonService


def _candidate(service, *, owner="owner", fingerprint="a", run_id="run-a", period="same", expectancy="1"):
    service.context.record_validation_run({
        "run_id": run_id, "strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
        "config_fingerprint": fingerprint * 64, "dataset_identity": "xrp:5m:dataset",
        "data_revision": "revision-1", "execution_mode": "backtest", "method": "historical_backtest",
        "status": "COMPLETE", "period_start": "2025-01-01", "period_end": period,
        "report": {"status": "COMPLETE", "method": "historical_backtest", "metrics": {
            "expectancy_r": expectancy, "max_drawdown_r": "2", "net_r": expectancy},
            "dataset": {"dataset_id": "xrp:5m:dataset"}}, "owner_id": owner})
    return service.create_candidate({
        "strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
        "config_fingerprint": fingerprint * 64, "frozen_config": {"ema": 20},
        "dataset_identity": "xrp:5m:dataset", "data_revision": "revision-1", "symbol": "XRPUSDT",
        "venue": "binance", "market_type": "spot", "timeframe": "5m", "execution_mode": "backtest",
        "cost_model_version": "cost.v1", "classifier_version": "regime.v1", "policy_version": "qualification.v1",
        "validation_run_ids": [run_id], "evidence_quality": "COMPLETE",
        "scope": {"period_start": "2025-01-01", "period_end": period, "venue": "binance", "market_type": "spot",
                  "symbol": "XRPUSDT", "timeframe": "5m", "execution_mode": "backtest", "cost_model_version": "cost.v1",
                  "funding_coverage": "COMPLETE", "slippage_model": "observed", "method": "historical_backtest",
                  "oos_status": "PASS", "classifier_version": "regime.v1"}}, owner_id=owner)


def test_candidate_fingerprint_and_version_identity_are_isolated(tmp_path):
    service = StrategyComparisonService(JournalStore(str(tmp_path / "journal.sqlite")).context)
    first = _candidate(service, fingerprint="a")
    second = _candidate(service, fingerprint="b", run_id="run-b")
    assert first["candidate_id"] != second["candidate_id"]
    assert service.candidate(first["candidate_id"], owner_id="owner")["config_fingerprint"] != service.candidate(second["candidate_id"], owner_id="owner")["config_fingerprint"]


def test_incompatible_period_is_not_comparable_with_reasons(tmp_path):
    service = StrategyComparisonService(JournalStore(str(tmp_path / "journal.sqlite")).context)
    champion = _candidate(service, fingerprint="a")
    challenger = _candidate(service, fingerprint="b", run_id="run-b", period="different")
    result = service.compare({"idempotency_key": "period-mismatch", "champion_candidate_id": champion["candidate_id"],
                              "challenger_candidate_id": challenger["candidate_id"]}, owner_id="owner")
    assert result["recommendation"] == "NOT_COMPARABLE"
    assert any("period_end mismatch" in reason for reason in result["blocking_reasons"])


def test_comparison_is_deterministic_and_idempotent(tmp_path):
    service = StrategyComparisonService(JournalStore(str(tmp_path / "journal.sqlite")).context)
    champion = _candidate(service, fingerprint="a", expectancy="1")
    challenger = _candidate(service, fingerprint="b", run_id="run-b", expectancy="2")
    request = {"idempotency_key": "same-comparison", "champion_candidate_id": champion["candidate_id"],
               "challenger_candidate_id": challenger["candidate_id"], "random_seed": 7}
    first = service.compare(request, owner_id="owner")
    second = service.compare(request, owner_id="owner")
    assert first["comparison_id"] == second["comparison_id"]
    assert first["recommendation"] == "CHALLENGER_PREFERRED"
    assert first["report"]["metrics"]["expectancy_difference_r"] == "1"


def test_champion_designation_is_explicit_and_owner_scoped(tmp_path):
    service = StrategyComparisonService(JournalStore(str(tmp_path / "journal.sqlite")).context)
    candidate = _candidate(service)
    assert service.context.research_champion(owner_id="owner", scope_key="xrp:5m") is None
    designated = service.context.designate_research_champion({
        "designation_id": "designation:1", "owner_id": "owner", "scope_key": "xrp:5m",
        "candidate_id": candidate["candidate_id"], "selection_policy_version": "challenger.v1",
        "reviewer_id": "owner", "review_status": "REVIEWED", "reason": "explicit research review"})
    assert designated["candidate_id"] == candidate["candidate_id"]
    assert service.context.research_champion(owner_id="other", scope_key="xrp:5m") is None


def test_degradation_requires_multiple_completed_results(tmp_path):
    service = StrategyComparisonService(JournalStore(str(tmp_path / "journal.sqlite")).context)
    candidate = _candidate(service)
    result = service.degradation(candidate["candidate_id"], owner_id="owner")
    assert result["status"] == "UNKNOWN"
