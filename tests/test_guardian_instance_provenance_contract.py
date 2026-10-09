"""Strict partial instance snapshots cannot masquerade as source attestations."""
import ast
import inspect
import json

import pytest

from tradexa.guardian import instance_provenance as contract


def record():
    return {"instance_id": "instance", "decision_identity": "decision", "code_commit": "a" * 40,
            "saved_config_json": json.dumps({"symbol": "BTCUSDT", "timeframe": "5m",
                "strategy_key": "adaptive_mtf", "strategy_version": "1", "config_revision": 1,
                "entry_mode": "limit", "trading_mode": "full", "min_quality_score": 0})}


def row():
    r = record()
    return {"instance_id": r["instance_id"], "decision_identity": r["decision_identity"],
            "symbol": "BTCUSDT", "timeframe": "5m",
            "instance_provenance": contract.project_instance_provenance(r)}


def test_hash_is_canonical_but_configuration_changes_are_separated():
    r = record()
    original = contract.project_instance_provenance(r)
    settings = json.loads(r["saved_config_json"])
    r["saved_config_json"] = json.dumps(dict(reversed(list(settings.items()))), indent=3)
    assert contract.project_instance_provenance(r) == original
    for key, value in (("config_revision", 2), ("entry_mode", "market"),
                       ("strategy_version", "2"), ("min_quality_score", 60)):
        changed = settings | {key: value}
        r["saved_config_json"] = json.dumps(changed)
        assert contract.project_instance_provenance(r)["saved_config_hash"] != original["saved_config_hash"]
    metadata = contract.instance_provenance_metadata(row())
    assert "config_hash" not in metadata and metadata["exact_version_verified"] is False


@pytest.mark.parametrize("key,value", [("instance_id", ""), ("instance_id", "x" * 129),
    ("decision_identity", "x" * 513), ("code_commit", "short"),
    ("saved_config_json", "[]"), ("saved_config_json", "{}" + " " * 2049),
    ("saved_config_json", '{"api_key":"secret"}')])
def test_invalid_record_is_rejected(key, value):
    r = record()
    r[key] = value
    with pytest.raises(ValueError):
        contract.project_instance_provenance(r)


@pytest.mark.parametrize("key,value", [("config_revision", 0), ("config_revision", True),
    ("config_revision", 1.5), ("min_quality_score", -1), ("min_quality_score", True),
    ("min_quality_score", 2**2000), ("min_quality_score", float("nan")),
    ("strategy_version", []), ("strategy_key", ""), ("entry_mode", "x" * 129)])
def test_invalid_setting_cannot_be_exported(key, value):
    r = record()
    settings = json.loads(r["saved_config_json"])
    r["saved_config_json"] = json.dumps(settings | {key: value})
    with pytest.raises(ValueError):
        contract.project_instance_provenance(r)


@pytest.mark.parametrize("key,value", [("instance_id", "other"), ("decision_identity", "other"),
    ("symbol", "ETHUSDT"), ("timeframe", "15m")])
def test_provenance_is_bound_to_exported_decision_identity(key, value):
    changed = row() | {key: value}
    with pytest.raises(ValueError):
        contract.instance_provenance_metadata(changed)


def test_legacy_missing_empty_identity_and_null_settings_are_explicit():
    assert contract.instance_provenance_metadata({})["instance_provenance"] == contract.unknown_instance_provenance()
    for changes in ({"saved_config_json": None}, {"decision_identity": ""},
                    {"saved_config_json": '{"symbol":"BTCUSDT"}'}):
        p = contract.project_instance_provenance(record() | changes)
        assert p["state"] == "INCOMPLETE_APPLIED_SETTINGS"
        assert not p["all_evaluations_verified"] and not p["full_strategy_config_verified"]
    unknown = contract.unknown_instance_provenance() | {"code_commit": "a" * 40}
    with pytest.raises(ValueError):
        contract.instance_provenance_metadata({"instance_provenance": unknown})


def test_contract_has_no_io_store_broker_strategy_or_runtime_dependencies():
    imports = set()
    for node in ast.walk(ast.parse(inspect.getsource(contract))):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imports.add(node.module)
    assert imports <= {"__future__", "hashlib", "json", "re", "collections.abc"}
