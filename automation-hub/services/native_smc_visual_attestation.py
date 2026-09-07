"""Append-only evidence finalisation for the frozen Native SMC visual review.

This module records a retrospective human bulk attestation without pretending
that 82 separate UI submissions occurred.  It fails closed unless the exact
frozen sample can be reproduced by the exact reviewed engine and dataset.
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import MISSING, asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from services.native_smc import (
    ChartObject,
    DealingRange,
    FairValueGap,
    LiquiditySweep,
    OrderBlock,
    PivotPoint,
    PriceAction,
    ProposedTrade,
    SMCConfig,
    SMCMarketSnapshot,
    SMCMarketStructureEngine,
    SMCSetup,
    SetupPhase,
    SetupTransition,
    StructureEvent,
    VisualReview,
)
from services.native_smc_visual_verification import (
    deterministic_review_sample,
    frozen_evidence_hash,
)


RESEARCH_ID = "SMC_NATIVE_V1_RESEARCH"
REVIEW_METHOD = "HUMAN_RETROSPECTIVE_BULK_ATTESTATION"
REVIEWER_ASSERTION = "USER_MANUALLY_REVIEWED_ALL_82_AND_CONFIRMS_CORRECT"
EXPECTED_SAMPLE_SIZE = 82
EXPECTED_FROZEN_EVIDENCE_HASH = "c8641eda709e673718b6902a6eb84595280f24fe62802049f32281c0e4bf5ceb"
# The visually attested legacy output remains unchanged. This source hash also
# binds the approved, non-alpha native-MTF input adapter added after review.
EXPECTED_ENGINE_SOURCE_HASH = "fa7114e598f5bef20231c7708d04e3439ff6f14bb7a4c7a9d10de5ec09bdfe23"
NATIVE_ENGINE_FREEZE = "SMC_NATIVE_V1_VISUALLY_VERIFIED_FROZEN"
VISUAL_STATE_VERIFICATION = "VISUAL_STATE_VERIFICATION_PASSED"

_NATIVE_SOURCE_PATH = Path(__file__).with_name("native_smc.py")
_DATASET_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "data" / "native_smc_visual_verification_manifest_v2.json"
_DOMAIN_TYPES = (
    PivotPoint,
    StructureEvent,
    LiquiditySweep,
    FairValueGap,
    OrderBlock,
    DealingRange,
    PriceAction,
    SetupTransition,
    SMCSetup,
    ProposedTrade,
    ChartObject,
    SMCMarketSnapshot,
)
_STATE_METHODS = ("process_closed_bar", "_transition", "_advance_setups", "_propose")


class BulkAttestationBlocked(ValueError):
    """The user attestation cannot safely be bound to the available evidence."""


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"Unsupported evidence value: {type(value)!r}")


def canonical_json(value: object) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def frozen_sample_hash(sample: list[dict]) -> str:
    return canonical_hash(sample)


def frozen_sample_id(sample_hash: str) -> str:
    return f"smc-frozen-sample-{sample_hash[:20]}"


def native_engine_source_hash() -> str:
    return hashlib.sha256(_NATIVE_SOURCE_PATH.read_bytes()).hexdigest()


def configuration_hash() -> str:
    return canonical_hash(asdict(SMCConfig("BTCUSDT", timeframe="5m")))


def _field_schema(model: type) -> list[dict[str, str | None]]:
    schema = []
    for item in fields(model):
        if item.default is not MISSING:
            default = repr(item.default)
        elif item.default_factory is not MISSING:  # type: ignore[comparison-overlap]
            default = "DEFAULT_FACTORY"
        else:
            default = None
        schema.append({"name": item.name, "type": str(item.type), "default": default})
    return schema


def domain_schema_hash() -> str:
    return canonical_hash({model.__name__: _field_schema(model) for model in _DOMAIN_TYPES})


def state_machine_hash() -> str:
    source = _NATIVE_SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    selected = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SetupPhase":
            selected.append(node)
        if isinstance(node, ast.ClassDef) and node.name == "SMCMarketStructureEngine":
            selected.extend(
                child for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name in _STATE_METHODS
            )
    # Python 3.12 added empty ``type_params`` fields to ClassDef/FunctionDef
    # AST dumps. They are interpreter metadata, not SMC state-machine logic.
    # Remove only that empty field so the frozen Python 3.11 fingerprint is
    # stable when verification runs under the supported 3.12 developer VM.
    canonical = "\n".join(ast.dump(node, include_attributes=False) for node in selected)
    canonical = canonical.replace(", type_params=[]", "")
    return hashlib.sha256(canonical.encode()).hexdigest()


def dataset_provenance_hash() -> str:
    payload = json.loads(_DATASET_MANIFEST_PATH.read_text(encoding="utf-8"))
    return str(payload["evidence_content_sha256"])


def engine_fingerprints() -> dict[str, str]:
    parts = {
        "source_hash": native_engine_source_hash(),
        "config_hash": configuration_hash(),
        "domain_schema_hash": domain_schema_hash(),
        "state_machine_hash": state_machine_hash(),
    }
    return {"engine_fingerprint": canonical_hash(parts), **parts}


def validate_frozen_sample(engine: SMCMarketStructureEngine, evidence: dict) -> dict:
    """Verify that the attestation is being bound to the exact frozen run."""
    evidence_hash = str(evidence.get("evidence_content_sha256") or "")
    if frozen_evidence_hash(evidence) != evidence_hash or evidence_hash != EXPECTED_FROZEN_EVIDENCE_HASH:
        raise BulkAttestationBlocked("BULK_ATTESTATION_BLOCKED_BY_SAMPLE_OR_ENGINE_CHANGE")
    if native_engine_source_hash() != EXPECTED_ENGINE_SOURCE_HASH:
        raise BulkAttestationBlocked("BULK_ATTESTATION_BLOCKED_BY_SAMPLE_OR_ENGINE_CHANGE")

    sample = evidence.get("review_sample")
    if not isinstance(sample, list) or len(sample) != EXPECTED_SAMPLE_SIZE:
        raise BulkAttestationBlocked("BULK_ATTESTATION_BLOCKED_BY_SAMPLE_OR_ENGINE_CHANGE")
    regenerated = [asdict(item) for item in deterministic_review_sample(engine)]
    if canonical_json(sample) != canonical_json(regenerated):
        raise BulkAttestationBlocked("BULK_ATTESTATION_BLOCKED_BY_SAMPLE_OR_ENGINE_CHANGE")

    object_ids = [str(item.get("object_id") or "") for item in sample]
    if len(set(object_ids)) != EXPECTED_SAMPLE_SIZE or not set(object_ids).issubset(engine.known_object_ids()):
        raise BulkAttestationBlocked("BULK_ATTESTATION_BLOCKED_BY_SAMPLE_OR_ENGINE_CHANGE")

    sample_hash = frozen_sample_hash(sample)
    return {
        "frozen_sample_id": frozen_sample_id(sample_hash),
        "sample_hash": sample_hash,
        "sample_size": len(sample),
        "frozen_evidence_hash": evidence_hash,
        "sample": sample,
        **engine_fingerprints(),
    }


def assert_no_conflicting_reviews(sample: list[dict], reviews: Iterable[VisualReview]) -> None:
    sampled_ids = {str(item["object_id"]) for item in sample}
    conflicts = sorted(
        review.object_id for review in reviews
        if review.object_id in sampled_ids and review.classification in {"INCORRECT", "AMBIGUOUS"}
    )
    if conflicts:
        raise BulkAttestationBlocked(
            "BULK_ATTESTATION_CONFLICT_EXISTING_HUMAN_CLASSIFICATION: " + ", ".join(conflicts)
        )


def materialize_bulk_attestation(
    frozen: dict,
    *,
    existing_reviews: Iterable[VisualReview] = (),
    evidence_timestamp: datetime | None = None,
) -> list[dict]:
    """Create one parent and one append-only classification per frozen item."""
    sample = frozen["sample"]
    assert_no_conflicting_reviews(sample, existing_reviews)
    recorded_at = (evidence_timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    parent_identity = {
        "research_id": RESEARCH_ID,
        "review_method": REVIEW_METHOD,
        "frozen_sample_id": frozen["frozen_sample_id"],
        "sample_hash": frozen["sample_hash"],
        "engine_fingerprint": frozen["engine_fingerprint"],
        "evidence_timestamp": recorded_at,
    }
    attestation_id = f"smc-attestation-{canonical_hash(parent_identity)[:20]}"
    parent = {
        "record_type": "BULK_HUMAN_ATTESTATION",
        "attestation_id": attestation_id,
        "research_id": RESEARCH_ID,
        "review_method": REVIEW_METHOD,
        "reviewer_assertion": REVIEWER_ASSERTION,
        "frozen_sample_id": frozen["frozen_sample_id"],
        "sample_hash": frozen["sample_hash"],
        "sample_size": EXPECTED_SAMPLE_SIZE,
        "classification_summary": {"CORRECT": EXPECTED_SAMPLE_SIZE, "INCORRECT": 0, "AMBIGUOUS": 0},
        "engine_fingerprint": frozen["engine_fingerprint"],
        "configuration_fingerprint": frozen["config_hash"],
        "evidence_timestamp": recorded_at,
        "execution_allowed": False,
    }
    records = [parent]
    for item in sample:
        record_identity = {
            "frozen_sample_id": frozen["frozen_sample_id"],
            "object_id": item["object_id"],
            "object_type": item["category"],
        }
        records.append({
            "record_type": "VISUAL_CLASSIFICATION",
            "review_item_id": f"smc-review-item-{canonical_hash(record_identity)[:20]}",
            "native_object_id": item["object_id"],
            "object_type": item["category"],
            "object_timestamp": item["timestamp"],
            "classification": "CORRECT",
            "review_method": REVIEW_METHOD,
            "parent_attestation_id": attestation_id,
            "frozen_sample_id": frozen["frozen_sample_id"],
            "sample_hash": frozen["sample_hash"],
            "engine_fingerprint": frozen["engine_fingerprint"],
            "evidence_timestamp": recorded_at,
            "execution_allowed": False,
        })
    return records


def attestation_evidence_hash(records: list[dict]) -> str:
    payload = "".join(canonical_json(record) + "\n" for record in records).encode()
    return hashlib.sha256(payload).hexdigest()


def verification_manifest(frozen: dict, records: list[dict], technical_gates: dict[str, str]) -> dict:
    classifications = records[1:]
    correct = sum(record["classification"] == "CORRECT" for record in classifications)
    incorrect = sum(record["classification"] == "INCORRECT" for record in classifications)
    ambiguous = sum(record["classification"] == "AMBIGUOUS" for record in classifications)
    if len(classifications) != EXPECTED_SAMPLE_SIZE or (correct, incorrect, ambiguous) != (82, 0, 0):
        raise BulkAttestationBlocked("BULK_ATTESTATION_BLOCKED_BY_SAMPLE_OR_ENGINE_CHANGE")
    if not technical_gates or any(status != "PASSED" for status in technical_gates.values()):
        raise BulkAttestationBlocked("VISUAL_STATE_VERIFICATION_TECHNICAL_GATE_FAILED")
    return {
        "research_id": RESEARCH_ID,
        "record_type": "NATIVE_SMC_VISUAL_VERIFICATION_FINAL",
        "status": VISUAL_STATE_VERIFICATION,
        "review_method": REVIEW_METHOD,
        "parent_attestation_id": records[0]["attestation_id"],
        "frozen_sample_id": frozen["frozen_sample_id"],
        "sample_hash": frozen["sample_hash"],
        "frozen_evidence_hash": frozen["frozen_evidence_hash"],
        "sample_size": EXPECTED_SAMPLE_SIZE,
        "reviewed": EXPECTED_SAMPLE_SIZE,
        "remaining": 0,
        "classification_summary": {"CORRECT": correct, "INCORRECT": incorrect, "AMBIGUOUS": ambiguous},
        "technical_gates": technical_gates,
        "technical_gate_status": "PASSED",
        "native_engine_freeze": NATIVE_ENGINE_FREEZE,
        "engine_fingerprint": frozen["engine_fingerprint"],
        "source_hash": frozen["source_hash"],
        "config_hash": frozen["config_hash"],
        "domain_schema_hash": frozen["domain_schema_hash"],
        "state_machine_hash": frozen["state_machine_hash"],
        "dataset_provenance_hash": dataset_provenance_hash(),
        "visual_evidence_hash": attestation_evidence_hash(records),
        "evidence_timestamp": records[0]["evidence_timestamp"],
        "execution_allowed": False,
        "next_authorized_stage": "TRAIN_UNIVERSE_APPROVAL",
    }


def render_jsonl(records: list[dict]) -> str:
    return "".join(canonical_json(record) + "\n" for record in records)


def load_legacy_reviews(path: str | Path | None) -> list[VisualReview]:
    if path is None:
        return []
    review_path = Path(path)
    if not review_path.exists():
        return []
    rows = json.loads(review_path.read_text(encoding="utf-8"))
    return [VisualReview(**{**row, "created_at": datetime.fromisoformat(row["created_at"])}) for row in rows]
