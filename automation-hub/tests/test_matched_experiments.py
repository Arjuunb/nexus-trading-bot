from data.journal_store import JournalStore
from services.matched_experiments import MatchedExperimentService
from services.strategy_comparison import StrategyComparisonService


def _candidate(service, fingerprint, *, period="same"):
    return service.create_candidate({
        "strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
        "config_fingerprint": fingerprint * 64, "frozen_config": {"ema": 20},
        "dataset_identity": "xrp:5m:dataset", "data_revision": "revision-1", "symbol": "XRPUSDT",
        "venue": "binance", "market_type": "spot", "timeframe": "5m", "execution_mode": "backtest",
        "cost_model_version": "cost.v1", "evidence_quality": "UNKNOWN",
        "scope": {"period_start": "2025-01-01", "period_end": period, "venue": "binance", "market_type": "spot",
                  "symbol": "XRPUSDT", "timeframe": "5m", "execution_mode": "backtest", "cost_model_version": "cost.v1",
                  "funding_coverage": "COMPLETE", "slippage_model": "observed", "method": "out_of_sample",
                  "oos_status": "PASS", "classifier_version": "regime.v1", "dataset_identity": "xrp:5m:dataset",
                  "data_revision": "revision-1", "holdout_start": "2025-02-01", "holdout_end": "2025-03-01"}}, owner_id="owner")


def test_matched_experiment_freezes_candidates_and_submits_idempotent_jobs(tmp_path):
    context = JournalStore(str(tmp_path / "journal.sqlite")).context
    candidates = StrategyComparisonService(context)
    champion = _candidate(candidates, "a")
    challenger = _candidate(candidates, "b")
    service = MatchedExperimentService(context)
    experiment = service.create_experiment({"idempotency_key": "experiment-1", "champion_candidate_id": champion["candidate_id"],
                                             "challenger_candidate_ids": [challenger["candidate_id"]], "scope": champion["scope"],
                                             "method": "OUT_OF_SAMPLE_VALIDATION"}, owner_id="owner")
    assert experiment["status"] == "DRAFT"
    submitted = service.submit(experiment["experiment_id"], owner_id="owner")
    assert submitted["status"] == "QUEUED"
    assert len(submitted["candidate_job_ids"]) == 2
    again = service.submit(experiment["experiment_id"], owner_id="owner")
    assert again["candidate_job_ids"] == submitted["candidate_job_ids"]


def test_mismatched_period_blocks_experiment_without_jobs(tmp_path):
    context = JournalStore(str(tmp_path / "journal.sqlite")).context
    candidates = StrategyComparisonService(context)
    champion = _candidate(candidates, "a")
    challenger = _candidate(candidates, "b", period="different")
    service = MatchedExperimentService(context)
    experiment = service.create_experiment({"idempotency_key": "experiment-2", "champion_candidate_id": champion["candidate_id"],
                                             "challenger_candidate_ids": [challenger["candidate_id"]], "scope": champion["scope"]}, owner_id="owner")
    assert experiment["status"] == "BLOCKED"
    assert service.submit(experiment["experiment_id"], owner_id="owner")["candidate_job_ids"] == {}


def test_holdout_exposure_is_reused_across_experiments(tmp_path):
    context = JournalStore(str(tmp_path / "journal.sqlite")).context
    first = context.record_holdout_exposure({"exposure_id": "exposure:1", "owner_id": "owner", "experiment_id": "e1",
        "dataset_identity": "dataset", "period_start": "a", "period_end": "b", "exposure_kind": "MATCHED_RESEARCH", "selection_influence": "PENDING"})
    second = context.record_holdout_exposure({"exposure_id": "exposure:2", "owner_id": "owner", "experiment_id": "e2",
        "dataset_identity": "dataset", "period_start": "a", "period_end": "b", "exposure_kind": "MATCHED_RESEARCH", "selection_influence": "PENDING"})
    assert first["holdout_status"] == "UNTOUCHED"
    assert second["holdout_status"] == "EXPOSED"
    assert len(context.holdout_exposures(owner_id="owner", dataset_identity="dataset", period_start="a", period_end="b")) == 2


def test_overlapping_holdout_periods_are_not_treated_as_independent(tmp_path):
    from data.journal_store import JournalStore
    context = JournalStore(str(tmp_path / "journal.sqlite")).context
    first = context.record_holdout_exposure({"exposure_id": "overlap:1", "owner_id": "owner", "experiment_id": "e1",
        "dataset_identity": "dataset", "period_start": "2025-01-01", "period_end": "2025-02-01",
        "exposure_kind": "MATCHED_RESEARCH", "selection_influence": "PENDING"})
    second = context.record_holdout_exposure({"exposure_id": "overlap:2", "owner_id": "owner", "experiment_id": "e2",
        "dataset_identity": "dataset", "period_start": "2025-01-15", "period_end": "2025-03-01",
        "exposure_kind": "MATCHED_RESEARCH", "selection_influence": "PENDING"})
    assert first["holdout_status"] == "UNTOUCHED"
    assert second["holdout_status"] == "EXPOSED"
    assert len(context.overlapping_holdout_exposures(owner_id="owner", dataset_identity="dataset",
                                                     period_start="2025-02-15", period_end="2025-02-20")) == 1


def test_experiment_is_tenant_scoped(tmp_path):
    context = JournalStore(str(tmp_path / "journal.sqlite")).context
    candidates = StrategyComparisonService(context)
    champion = _candidate(candidates, "a")
    challenger = _candidate(candidates, "b")
    service = MatchedExperimentService(context)
    experiment = service.create_experiment({"idempotency_key": "experiment-3", "champion_candidate_id": champion["candidate_id"],
                                             "challenger_candidate_ids": [challenger["candidate_id"]]}, owner_id="owner")
    assert service.experiment(experiment["experiment_id"], owner_id="other") is None
