from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.native_smc import SMCConfig, SMCMarketStructureEngine, VisualReview
from services import native_smc_visual_attestation as attestation
from services.native_smc_visual_verification import frozen_evidence_hash


DATA = Path(__file__).resolve().parents[1] / "data"


def test_checked_in_bulk_attestation_is_complete_append_only_and_non_executable():
    ledger_path = DATA / "native_smc_visual_attestation.jsonl"
    records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    manifest = json.loads((DATA / "native_smc_visual_verification_final.json").read_text(encoding="utf-8"))

    assert len(records) == 83
    parent, classifications = records[0], records[1:]
    assert parent["record_type"] == "BULK_HUMAN_ATTESTATION"
    assert parent["review_method"] == "HUMAN_RETROSPECTIVE_BULK_ATTESTATION"
    assert parent["reviewer_assertion"] == "USER_MANUALLY_REVIEWED_ALL_82_AND_CONFIRMS_CORRECT"
    assert parent["classification_summary"] == {"CORRECT": 82, "INCORRECT": 0, "AMBIGUOUS": 0}
    assert len(classifications) == 82
    assert len({row["review_item_id"] for row in classifications}) == 82
    assert len({row["native_object_id"] for row in classifications}) == 82
    assert {row["classification"] for row in classifications} == {"CORRECT"}
    assert {row["parent_attestation_id"] for row in classifications} == {parent["attestation_id"]}
    assert {row["evidence_timestamp"] for row in classifications} == {parent["evidence_timestamp"]}
    assert all(row["execution_allowed"] is False for row in records)
    assert all("notes" not in row and "screenshot_timestamp" not in row for row in classifications)

    assert manifest["reviewed"] == manifest["sample_size"] == 82
    assert manifest["remaining"] == 0
    assert manifest["classification_summary"] == {"CORRECT": 82, "INCORRECT": 0, "AMBIGUOUS": 0}
    assert manifest["visual_evidence_hash"] == attestation.attestation_evidence_hash(records)
    assert manifest["status"] == "VISUAL_STATE_VERIFICATION_PASSED"
    assert manifest["native_engine_freeze"] == "SMC_NATIVE_V1_VISUALLY_VERIFIED_FROZEN"
    assert manifest["execution_allowed"] is False
    assert manifest["next_authorized_stage"] == "TRAIN_UNIVERSE_APPROVAL"


def test_engine_freeze_fingerprints_match_the_current_exact_engine():
    frozen = json.loads((DATA / "native_smc_engine_freeze_manifest.json").read_text(encoding="utf-8"))
    current = attestation.engine_fingerprints()
    assert frozen["source_hash"] == current["source_hash"] == attestation.EXPECTED_ENGINE_SOURCE_HASH
    assert frozen["config_hash"] == current["config_hash"]
    assert frozen["domain_schema_hash"] == current["domain_schema_hash"]
    assert frozen["state_machine_hash"] == current["state_machine_hash"]
    assert frozen["engine_fingerprint"] == current["engine_fingerprint"]
    assert frozen["visual_evidence_hash"] == json.loads(
        (DATA / "native_smc_visual_verification_final.json").read_text(encoding="utf-8")
    )["visual_evidence_hash"]
    assert frozen["execution_allowed"] is False
    # Paper simulation was authorised by the repository owner as an approved
    # non-alpha delta; the manifest records who authorised it and the evidence.
    # The three permissions below it did NOT move, and this is the test that
    # says so -- paper trading is not a foothold for live routing.
    assert frozen["paper_trading_enabled"] is True
    assert frozen["paper_execution_authorised_by"]
    assert frozen["forward_paper_enabled"] is False
    assert frozen["live_trading_enabled"] is False


def test_bulk_attestation_rejects_existing_incorrect_or_ambiguous_review():
    created_at = datetime(2025, 3, 1, tzinfo=timezone.utc)
    frozen = {"sample": [{"object_id": "pivot-1", "category": "swing_pivot", "timestamp": created_at}]}
    review = VisualReview(
        "review-1", "SMC_NATIVE_V1_RESEARCH", "BTCUSDT", "5m", "pivot-1", "swing_pivot",
        "INCORRECT", None, None, None, None, created_at,
    )
    with pytest.raises(attestation.BulkAttestationBlocked, match="BULK_ATTESTATION_CONFLICT"):
        attestation.materialize_bulk_attestation(frozen, existing_reviews=[review], evidence_timestamp=created_at)


def test_changed_sample_or_engine_fails_closed(monkeypatch):
    engine = SMCMarketStructureEngine(SMCConfig("BTCUSDT"))
    evidence = {"review_sample": []}
    evidence["evidence_content_sha256"] = frozen_evidence_hash(evidence)
    monkeypatch.setattr(attestation, "EXPECTED_SAMPLE_SIZE", 0)
    monkeypatch.setattr(attestation, "EXPECTED_FROZEN_EVIDENCE_HASH", evidence["evidence_content_sha256"])
    attestation.validate_frozen_sample(engine, evidence)

    changed = {**evidence, "review_sample": [{"object_id": "different"}]}
    with pytest.raises(attestation.BulkAttestationBlocked, match="BULK_ATTESTATION_BLOCKED_BY_SAMPLE_OR_ENGINE_CHANGE"):
        attestation.validate_frozen_sample(engine, changed)

    monkeypatch.setattr(attestation, "EXPECTED_ENGINE_SOURCE_HASH", "0" * 64)
    with pytest.raises(attestation.BulkAttestationBlocked, match="BULK_ATTESTATION_BLOCKED_BY_SAMPLE_OR_ENGINE_CHANGE"):
        attestation.validate_frozen_sample(engine, evidence)
