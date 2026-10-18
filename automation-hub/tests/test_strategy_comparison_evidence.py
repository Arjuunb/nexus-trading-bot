"""Task 5: comparisons must consume owned, bound, original financial samples."""
from copy import deepcopy

import pytest

from data.journal_store import JournalStore
from services.strategy_comparison import StrategyComparisonService
from services.historical_forward_evidence import historical_metrics
from services.strategy_identity import observed_strategy_identity
from strategies.adaptive_trend_pullback.strategy import AdaptiveTrendPullbackStrategy
from test_historical_forward_evidence import financial_trades
from test_strategy_comparison import _candidate


def complete_candidate(service, label, *, target_rr=2.5, gain="10", transform=None, attach_references=True):
    frozen = observed_strategy_identity(AdaptiveTrendPullbackStrategy("XRPUSDT", target_rr=target_rr),
        strategy_id="adaptive_trend_pullback", timeframe="5m")
    identity = {"strategy_id": frozen["strategy_id"], "strategy_version": frozen["strategy_version"],
                "config_fingerprint": frozen["strategy_config_hash"]}
    dataset = {"dataset_id": "dataset", "content_hash": "d"*64, "symbol": "XRPUSDT", "timeframe": "5m",
               "period_start": "2025-01-01T00:00:00+00:00", "period_end": "2025-02-01T00:00:00+00:00",
               "coverage": {"status": "VERIFIED", "missing_candles": 0}}
    def metrics(day):
        from datetime import datetime, timedelta
        from decimal import Decimal
        rows = financial_trades()
        for i, row in enumerate(rows):
            row["episode_id"] = f"{label}:{day}:{i}"
            if i % 3:
                row.update(net_pnl=gain, gross_pnl=str(Decimal(gain)+1))
            for key in ("entry_time", "exit_time"):
                row[key] = (datetime.fromisoformat(row[key])+timedelta(days=day-1)).isoformat()
        return historical_metrics({"trades": rows})
    refs = []
    for method in ("historical_backtest", "out_of_sample", "walk_forward", "robustness", "cost_stress"):
        base = metrics(1)
        report = {"status": "PASS", "result_status": "PASS", "identity_binding_status": "MATCHED",
            "identity": frozen, "dataset": dataset, "metrics": base, "base": base,
            "train": base, "validation": metrics(8), "holdout": metrics(15),
            "partitions": {"train_end": "2025-01-07T00:00:00+00:00", "validation_end": "2025-01-14T00:00:00+00:00",
                           "holdout_end": dataset["period_end"]},
            "windows": [{"development": base, "evaluation": metrics(15),
                "evaluation_start": "2025-01-15T00:00:00+00:00", "evaluation_end": dataset["period_end"]}],
            "concentration": {"concentration_status": "PASS"}, "monte_carlo": {"status": "PASS"},
            "stress": {"status": "PASS", "gross_r": base["net_r"], "net_r": base["net_r"],
                       "fee": "0", "slippage": "0", "funding": "0"}}
        row = {**identity, "run_id": f"{label}:{method}", "owner_id": "owner", "account_id": None,
            "instance_id": None, "simulation_session_id": None, "dataset_identity": dataset["dataset_id"],
            "data_revision": dataset["content_hash"], "execution_mode": "backtest", "method": method,
            "status": "COMPLETE", "period_start": dataset["period_start"], "period_end": dataset["period_end"],
            "created_at": "2025-02-02T00:00:00+00:00", "report": deepcopy(report)}
        if transform: transform(method, row)
        service.context.record_validation_run(row)
        refs.append(row["run_id"])
    return service.create_candidate({**identity, "frozen_config": frozen["configuration"],
        "dataset_identity": dataset["dataset_id"], "data_revision": dataset["content_hash"],
        "symbol": "XRPUSDT", "timeframe": "5m", "execution_mode": "backtest", "venue": "binance",
        "market_type": "spot", "cost_model_version": "cost.v1", "classifier_version": "regime.v1",
        "validation_run_ids": refs if attach_references else [], "evidence_quality": "COMPLETE" if attach_references else "UNKNOWN", "scope": {
            "period_start": dataset["period_start"], "period_end": dataset["period_end"],
            "funding_coverage": "COMPLETE", "slippage_model": "observed", "method": "out_of_sample",
            "oos_status": "PASS"}}, owner_id="owner")


def compare(service, champion, challenger, **changes):
    return service.compare({"idempotency_key": "evidence-comparison", "champion_candidate_id": champion["candidate_id"],
        "challenger_candidate_id": challenger["candidate_id"], "random_seed": 7, **changes}, owner_id="owner")


@pytest.fixture
def service(tmp_path):
    return StrategyComparisonService(JournalStore(tmp_path / "journal.sqlite").context)


def test_complete_bound_comparison_uses_holdout_finances_and_preserves_records(service):
    champion = complete_candidate(service, "champion")
    challenger = complete_candidate(service, "challenger", target_rr=3, gain="20")
    before = service.context.validation_runs(filters={"owner_id": "owner"})
    result = compare(service, champion, challenger)
    assert result["recommendation"] == "CHALLENGER_PREFERRED"
    assert result["report"]["uncertainty"]["status"] == "PASS"
    assert result["report"]["evidence_basis"] == "bound_out_of_sample_financial_samples"
    assert result["report"]["metrics"]["expectancy_difference_r"] is not None
    assert service.context.validation_runs(filters={"owner_id": "owner"}) == before
    assert compare(service, champion, challenger)["comparison_id"] == result["comparison_id"]


def test_legacy_summary_claims_cannot_prefer_a_challenger(service):
    a = _candidate(service)
    b = _candidate(service, fingerprint="b", run_id="run-b", expectancy="999")
    result = compare(service, a, b)
    assert result["recommendation"] == "INSUFFICIENT_EVIDENCE"
    assert result["report"]["metrics"]["expectancy_difference_r"] is None


@pytest.mark.parametrize("change", ["foreign_owner", "other_identity", "other_account", "partial_scope",
    "missing_drawdown", "missing_samples", "missing_cost", "missing_oos", "missing_wf", "reused_holdout",
    "short_holdout", "forged_summary", "source_hash", "future_development"])
def test_incomplete_or_foreign_evidence_never_grants_preference(service, change):
    a = complete_candidate(service, "champion")
    def corrupt(method, row):
        if change == "foreign_owner": row["owner_id"] = "foreign"
        if change == "other_identity": row["config_fingerprint"] = "f"*64
        if change == "other_account": row.update(account_id="foreign", instance_id="elsewhere", simulation_session_id="other")
        if change == "partial_scope": row["account_id"] = "partial"
        if change == "missing_oos" and method == "out_of_sample": row["status"] = "FAILED"
        if change == "missing_wf" and method == "walk_forward": row["status"] = "FAILED"
        if change == "reused_holdout" and method == "out_of_sample":
            service.context.record_validation_run({**deepcopy(row), "run_id": "earlier-holdout-access"})
        if change == "source_hash": row["report"]["identity"]["source_hash"] = None
        if method == "out_of_sample":
            holdout = row["report"]["holdout"]
            if change == "missing_drawdown": holdout.pop("max_drawdown_r")
            if change == "missing_samples": holdout.pop("financial_samples")
            if change == "missing_cost": holdout["financial_samples"][0]["funding"] = None
            if change == "forged_summary": holdout["expectancy_net_r"] = "888888"
            if change == "short_holdout": row["report"]["holdout"] = historical_metrics({"trades": holdout["financial_samples"][:2]})
        if change == "future_development" and method == "walk_forward":
            row["report"]["windows"][0]["development"]["last_closed_at"] = "2025-01-20T00:00:00+00:00"
    b = complete_candidate(service, "challenger", target_rr=3, gain="20", transform=corrupt)
    result = compare(service, a, b)
    assert result["recommendation"] == "INSUFFICIENT_EVIDENCE"
    if change == "foreign_owner":
        assert "888888" not in str(result)
        assert result["report"]["metrics"]["challenger_expectancy_r"] is None
    assert service.degradation(b["candidate_id"], owner_id="owner")["status"] == "UNKNOWN"


@pytest.mark.parametrize("policy", [{"minimum_episodes": 0}, {"minimum_episodes": True},
    {"minimum_expectancy_improvement_r": "NaN"}, {"maximum_drawdown_regression_r": "bad"},
    {"bootstrap_runs": "bad"}, {"confidence_level": "1"}, {"require_complete_costs": False},
    {"require_positive_oos": False}, {"require_walk_forward": False}, {"unexpected": True}])
def test_invalid_or_weakened_comparison_policy_rejected(service, policy):
    a = _candidate(service)
    b = _candidate(service, fingerprint="b", run_id="run-b")
    with pytest.raises(ValueError, match="policy"):
        compare(service, a, b, policy=policy)


def test_uncommitted_worker_override_cannot_bypass_evidence(service):
    a = _candidate(service)
    b = _candidate(service, fingerprint="b", run_id="run-b")
    overrides = {c["candidate_id"]: {"status": "COMPLETE", "metrics": {"expectancy_r": exp,
        "max_drawdown_r": "0"}} for c, exp in ((a, "1"), (b, "999"))}
    result = compare(service, a, b, validation_overrides=overrides)
    assert result["recommendation"] == "INSUFFICIENT_EVIDENCE"
    assert result["report"]["metrics"]["challenger_expectancy_r"] is None


def test_original_cost_models_must_be_comparable_even_when_candidate_claims_match(service):
    a = complete_candidate(service, "champion")
    def other_cost_model(method, row):
        report = row["report"]
        for metrics in [report[key] for key in ("metrics", "base", "train", "validation", "holdout")]:
            for sample in metrics["financial_samples"]: sample["fees_cost_model"] = "different-fee-schedule.v2"
        for window in report["windows"]:
            for metrics in (window["development"], window["evaluation"]):
                for sample in metrics["financial_samples"]: sample["fees_cost_model"] = "different-fee-schedule.v2"
    b = complete_candidate(service, "challenger", target_rr=3, gain="20", transform=other_cost_model)
    result = compare(service, a, b)
    assert result["recommendation"] == "NOT_COMPARABLE"
    assert any("original financial cost models" in reason for reason in result["blocking_reasons"])


def test_nonpositive_oos_does_not_recommend_retaining_unqualified_champion(service):
    a = complete_candidate(service, "champion", gain="1")
    b = complete_candidate(service, "challenger", target_rr=3, gain="0.5")
    assert compare(service, a, b)["recommendation"] == "INCONCLUSIVE"


def test_pre_registered_candidates_use_committed_owned_worker_references_instead_of_quality_claims(service):
    a = complete_candidate(service, "champion", attach_references=False)
    b = complete_candidate(service, "challenger", target_rr=3, gain="20", attach_references=False)
    refs = [row["run_id"] for row in service.context.validation_runs(filters={"owner_id": "owner"})]
    result = compare(service, a, b, validation_run_ids=refs)
    assert result["recommendation"] == "CHALLENGER_PREFERRED"
    assert result["orders_allowed"] is False and result["qualification_granted"] is False
    assert service.candidate(a["candidate_id"], owner_id="owner")["evidence_quality"] == "UNKNOWN"
