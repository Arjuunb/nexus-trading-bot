"""Research never skips evidence/approval or gains production trading authority."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.research import GuardianResearch, STAGES
from tradexa.guardian.store import GuardianStore


@pytest.fixture
def research(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    store.append(GuardianEvent(source_service="smc_lab", source_component="evaluation",
                              event_type="condition_failed", event_id="research_evidence_001"))
    return GuardianResearch(store)


def proposal(**changes):
    return {"strategy_id": "SMC_PRODUCTION_LOCKED", "strategy_version": "1.0",
            "code_commit": "a" * 40, "config_hash": "b" * 64,
            "candidate_artifact_sha256": "c" * 64,
            "hypothesis": "Test whether rejection distribution differs out of sample; unproven.",
            "evidence_ids": ["research_evidence_001"], **changes}


def period(start, end):
    return {"start": f"2020-{start}T00:00:00Z", "end": f"2020-{end}T00:00:00Z"}


def stage_result(stage, digest, **changes):
    periods = {
        "HISTORICAL_BACKTEST": period("01-01", "02-01"),
        "OUT_OF_SAMPLE": period("02-02", "03-01"),
        "WALK_FORWARD": period("01-01", "05-01"),
        "STRESS_TEST": period("06-01", "07-01"),
        "FORWARD_PAPER": period("08-01", "09-01"),
        "STATISTICAL_COMPARISON": period("09-01", "10-01"),
        "RECOMMENDATION": period("09-01", "10-01"),
    }
    return {"stage": stage, "expected_digest": digest,
            "candidate_artifact_sha256": "c" * 64,
            "result_artifact_sha256": f"{STAGES.index(stage) + 1:064x}",
            "dataset_sha256": f"{STAGES.index(stage) + 101:064x}",
            "passed": True, "period": periods[stage],
            "metrics": {"sample_count": 31, "wins": 14, "losses": 17, "expectancy_r": 0.1},
            "folds": [{"train": period("01-01", "02-01"), "test": period("02-02", "03-01")}]
                if stage == "WALK_FORWARD" else [], "paper_only": True, **changes}


def approve(research, hypothesis):
    return research.review(hypothesis["hypothesis_id"], decision="SEND_TO_BACKTEST",
                           expected_digest=hypothesis["evidence_digest"])


def test_hypothesis_replay_memory_is_persistent_without_duplicate_audit(research):
    first = research.create(proposal())
    assert first["status"] == "HYPOTHESIS_UNPROVEN"
    for _ in range(100):
        assert research.create(proposal())["hypothesis_id"] == first["hypothesis_id"]
    restarted = GuardianResearch(GuardianStore(research.store.path))
    assert restarted.get(first["hypothesis_id"]) == first
    assert len(research.list()) == 1 and research.store.count() == 2
    assert first["methods_verified"] is False and first["production_changed"] is False


@pytest.mark.parametrize("changes", [
    {"evidence_ids": ["unknown_evidence_001"]}, {"code_commit": "main"},
    {"config_hash": "missing"}, {"candidate_artifact_sha256": "/etc/passwd"},
    {"hypothesis": "Bearer exposed-value"}, {"command": "deploy"},
])
def test_unknown_evidence_secrets_commands_and_unpinned_artifacts_rejected(research, changes):
    with pytest.raises(ValueError):
        research.create(proposal(**changes))
    assert research.list() == [] and research.store.count() == 1


def test_approval_required_no_stage_skipping_and_stale_digest_rejected(research):
    hypothesis = research.create(proposal())
    key, digest = hypothesis["hypothesis_id"], hypothesis["evidence_digest"]
    with pytest.raises(ValueError, match="unapproved"):
        research.record_result(key, stage_result("HISTORICAL_BACKTEST", digest))
    hypothesis = approve(research, hypothesis)
    with pytest.raises(ValueError, match="skip"):
        research.record_result(key, stage_result("OUT_OF_SAMPLE", digest))
    with pytest.raises(ValueError, match="all research stages"):
        research.review(key, decision="APPROVE_FOR_DEVELOPMENT", expected_digest=digest)
    hypothesis = research.record_result(key, stage_result("HISTORICAL_BACKTEST", digest))
    with pytest.raises(ValueError, match="evidence changed"):
        research.record_result(key, stage_result("OUT_OF_SAMPLE", digest))
    assert hypothesis["status"] == "OUT_OF_SAMPLE_PENDING"


def test_complete_attested_pipeline_owner_review_never_executes_or_certifies(research):
    hypothesis = approve(research, research.create(proposal()))
    key = hypothesis["hypothesis_id"]
    for stage in STAGES:
        payload = stage_result(stage, hypothesis["evidence_digest"])
        hypothesis = research.record_result(key, payload)
        before = research.store.count()
        for _ in range(3):
            assert research.record_result(key, payload) == hypothesis
        assert research.store.count() == before
    assert hypothesis["status"] == "OWNER_REVIEW_REQUIRED"
    assert all(result["source_reported_result_only"] for result in hypothesis["results"])
    assert not any(result["method_verified"] for result in hypothesis["results"])
    review_digest = hypothesis["evidence_digest"]
    hypothesis = research.review(key, decision="APPROVE_FOR_DEVELOPMENT", expected_digest=review_digest)
    assert hypothesis["status"] == "APPROVED_FOR_DEVELOPMENT_NO_DEPLOYMENT"
    assert hypothesis["methods_verified"] is False
    assert hypothesis["live_routing_enabled"] is False and hypothesis["production_changed"] is False
    assert len(hypothesis["results"]) == 7 and len(hypothesis["reviews"]) == 2
    assert research.review(key, decision="APPROVE_FOR_DEVELOPMENT", expected_digest=review_digest) == hypothesis
    assert research.store.count() == 11  # original + hypothesis + 7 results + 2 reviews


@pytest.mark.parametrize("changes", [
    {"candidate_artifact_sha256": "d" * 64}, {"paper_only": False},
    {"metrics": {"sample_count": 0}}, {"metrics": {"sample_count": 2, "wins": 3}},
    {"metrics": {"sample_count": 2, "wins": 2, "losses": 1}},
    {"metrics": {"sample_count": 2, "expectancy_r": float("inf")}},
    {"period": {"start": "2020-01-01", "end": "2020-02-01"}},
    {"period": period("02-01", "01-01")}, {"folds": [{"train": period("01-01", "02-01")}]},
])
def test_candidate_change_live_results_invalid_periods_and_metrics_fail_closed(research, changes):
    hypothesis = approve(research, research.create(proposal()))
    with pytest.raises(ValueError):
        research.record_result(hypothesis["hypothesis_id"], stage_result(
            "HISTORICAL_BACKTEST", hypothesis["evidence_digest"], **changes))
    assert research.get(hypothesis["hypothesis_id"])["results"] == []


def test_no_oos_training_overlap_or_walk_forward_future_overlap(research):
    hypothesis = approve(research, research.create(proposal()))
    key = hypothesis["hypothesis_id"]
    hypothesis = research.record_result(key, stage_result("HISTORICAL_BACKTEST", hypothesis["evidence_digest"]))
    for changes in ({"period": period("01-31", "03-01")}, {"dataset_sha256": f"{101:064x}"}):
        with pytest.raises(ValueError, match="out-of-sample"):
            research.record_result(key, stage_result("OUT_OF_SAMPLE", hypothesis["evidence_digest"], **changes))
    hypothesis = research.record_result(key, stage_result("OUT_OF_SAMPLE", hypothesis["evidence_digest"]))
    for folds in (
        [{"train": period("01-01", "02-15"), "test": period("02-02", "03-01")}],
        [{"train": period("01-01", "02-01"), "test": period("02-02", "06-01")}],
        [{"train": period("01-01", "02-01"), "test": period("02-02", "03-01")},
         {"train": period("01-01", "02-01"), "test": period("02-15", "04-01")}],
    ):
        with pytest.raises(ValueError):
            research.record_result(key, stage_result("WALK_FORWARD", hypothesis["evidence_digest"], folds=folds))


def test_failed_or_rejected_hypothesis_cannot_resume_or_overwrite_result(research):
    hypothesis = approve(research, research.create(proposal()))
    key = hypothesis["hypothesis_id"]
    payload = stage_result("HISTORICAL_BACKTEST", hypothesis["evidence_digest"], passed=False)
    hypothesis = research.record_result(key, payload)
    assert hypothesis["status"] == "RESEARCH_FAILED"
    with pytest.raises(ValueError, match="conflicts"):
        research.record_result(key, {**payload, "passed": True})
    with pytest.raises(ValueError, match="terminal"):
        research.record_result(key, stage_result("OUT_OF_SAMPLE", hypothesis["evidence_digest"]))
    other = research.create(proposal(candidate_artifact_sha256="d" * 64))
    other = research.review(other["hypothesis_id"], decision="REJECT", expected_digest=other["evidence_digest"])
    assert other["status"] == "REJECTED"
    with pytest.raises(ValueError, match="terminal"):
        approve(research, other)


def test_registry_write_and_audit_rollback_together_then_retry_concurrently(research):
    with sqlite3.connect(research.store.path) as conn:
        conn.execute("CREATE TRIGGER fail_research_audit BEFORE INSERT ON events "
                     "WHEN NEW.source_service='guardian_research' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        research.create(proposal())
    assert research.list() == [] and research.store.count() == 1
    with sqlite3.connect(research.store.path) as conn:
        conn.execute("DROP TRIGGER fail_research_audit")
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(lambda _: research.create(proposal()), range(3)))
    assert len({row["hypothesis_id"] for row in rows}) == 1 and research.store.count() == 2
    with sqlite3.connect(research.store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE guardian_research_hypotheses SET payload_json='{}'")


@pytest.mark.parametrize("operation", ["research_owner_reviewed", "research_result_recorded"])
def test_result_and_owner_review_failures_do_not_advance_state_without_audit(research, operation):
    hypothesis = research.create(proposal())
    if operation == "research_result_recorded":
        hypothesis = approve(research, hypothesis)
    prior_count = research.store.count()
    with sqlite3.connect(research.store.path) as conn:
        conn.execute(f"CREATE TRIGGER fail_research_step BEFORE INSERT ON events "
                     f"WHEN NEW.event_type='{operation}' BEGIN SELECT RAISE(ABORT,'injected'); END")
    key, digest = hypothesis["hypothesis_id"], hypothesis["evidence_digest"]
    def action():
        if operation == "research_result_recorded":
            return research.record_result(key, stage_result("HISTORICAL_BACKTEST", digest))
        return research.review(key, decision="SEND_TO_BACKTEST", expected_digest=digest)
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        action()
    assert research.get(key) == hypothesis and research.store.count() == prior_count
    with sqlite3.connect(research.store.path) as conn:
        conn.execute("DROP TRIGGER fail_research_step")
    assert action()["status"] != hypothesis["status"]
    assert research.store.count() == prior_count + 1
