"""Source provenance is bounded, verifiable as a snapshot, never an attestation."""
import copy
import json
import ast
import inspect

import pytest

from tradexa.guardian.provenance import (project_saved_provenance, provenance_metadata,
                                         unknown_provenance)
from tradexa.guardian import provenance


def record():
    return {"correlation_id": "decision", "session_id": "session", "strategy_id": "SMC_SOURCE_V1",
            "strategy_version": "1", "code_commit": "a" * 40,
            "capture_method": "CONNECTION_LOCAL_INSERT_TRIGGER",
            "saved_config_scope": "SMC_SAVED_SESSION_SETTINGS",
            "saved_config_json": json.dumps({"symbol": "BTCUSDT", "timeframe": "5m",
                "model_id": "SMC_M1_SWEEP_REVERSAL", "operating_mode": "automatic", "risk_pct": .5})}


def exported_row():
    r = record()
    return {key: r[key] for key in ("correlation_id", "session_id", "strategy_id", "strategy_version")} | {
        "decision_provenance": project_saved_provenance(r)}


def test_canonical_hash_is_order_independent_and_settings_change_changes_hash():
    a = record()
    p = project_saved_provenance(a)
    parsed = json.loads(a["saved_config_json"])
    a["saved_config_json"] = json.dumps(dict(reversed(list(parsed.items()))), indent=2)
    assert project_saved_provenance(a) == p
    parsed["risk_pct"] = .75
    a["saved_config_json"] = json.dumps(parsed)
    q = project_saved_provenance(a)
    assert p["saved_config_hash"] != q["saved_config_hash"]
    assert p["full_strategy_config_verified"] is False


@pytest.mark.parametrize("key,value", [("saved_config_scope", "FULL_CONFIG"),
    ("capture_method", "CURRENT_ENV_BACKFILL"), ("code_commit", "a" * 7),
    ("saved_config_json", "[1,2]"), ("saved_config_json", "{}" + " " * 2049),
    ("session_id", ""), ("strategy_version", ""), ("correlation_id", "x" * 129)])
def test_invalid_source_record_is_rejected(key, value):
    r = record()
    r[key] = value
    with pytest.raises(ValueError):
        project_saved_provenance(r)


@pytest.mark.parametrize("value", [True, "0.5", 0, -1, float("nan"), float("inf"), 2**2000])
def test_invalid_numeric_setting_fails_without_overflow(value):
    r = record()
    config = json.loads(r["saved_config_json"])
    config["risk_pct"] = value
    r["saved_config_json"] = json.dumps(config)
    with pytest.raises(ValueError):
        project_saved_provenance(r)


def test_missing_fields_or_oversized_unavailable_snapshot_are_not_full_config():
    r = record()
    r["saved_config_json"] = '{"risk_pct":0.5}'
    assert project_saved_provenance(r)["state"] == "INCOMPLETE_SAVED_SETTINGS"
    r["saved_config_json"] = None
    p = project_saved_provenance(r)
    assert p["saved_config"] is None and p["saved_config_hash"] is None
    assert p["state"] == "INCOMPLETE_SAVED_SETTINGS"


@pytest.mark.parametrize("mutation", [
    {"saved_config_hash": "b" * 64}, {"code_commit": "b" * 7},
    {"strategy_version": "other"}, {"correlation_id": "other"}, {"session_id": "other"},
    {"source_attestation_verified": True}, {"full_strategy_config_verified": True},
    {"all_evaluations_verified": True}, {"state": "VERIFIED"}, {"api_key": "do-not-export"},
    {"schema_version": True}, {"full_strategy_config_verified": 0}, {"saved_config_scope": []},
])
def test_exported_claims_hash_or_identity_cannot_be_forged(mutation):
    row = exported_row()
    row["decision_provenance"].update(mutation)
    with pytest.raises(ValueError):
        provenance_metadata(row)


def test_only_allowed_fields_are_accepted_and_unknown_never_acquires_identity():
    row = exported_row()
    metadata = provenance_metadata(row)
    assert "config_hash" not in metadata and metadata["exact_version_verified"] is False
    row["decision_provenance"]["saved_config"]["api_key"] = "do-not-export"
    with pytest.raises(ValueError):
        provenance_metadata(row)
    assert provenance_metadata({})["decision_provenance"] == unknown_provenance()
    forged = copy.deepcopy(unknown_provenance())
    forged["code_commit"] = "a" * 40
    with pytest.raises(ValueError):
        provenance_metadata({"decision_provenance": forged})


def test_shared_contract_remains_pure_and_has_no_io_or_trading_dependencies():
    tree = ast.parse(inspect.getsource(provenance))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0  # no store/runtime imported via a relative alias
            imports.add(node.module)
    assert imports <= {"__future__", "hashlib", "json", "math", "re", "collections.abc"}
