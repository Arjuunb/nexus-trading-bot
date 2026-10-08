"""Strict partial-provenance contract; saved settings are not full alpha config."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SCOPES = {
    "PA_SAVED_EXECUTION_SETTINGS": frozenset({"symbol", "timeframe", "operating_mode",
        "strategy_id", "risk_pct", "max_risk_pct", "max_concurrent_risk_pct", "target_r"}),
    "SMC_SAVED_SESSION_SETTINGS": frozenset({"symbol", "timeframe", "operating_mode", "model_id", "risk_pct"}),
}
_NUMERIC = frozenset({"risk_pct", "max_risk_pct", "max_concurrent_risk_pct", "target_r"})


def unknown_provenance() -> dict:
    return {"schema_version": 1, "state": "UNKNOWN", "code_commit": None,
            "saved_config": None, "saved_config_hash": None, "saved_config_scope": None,
            "strategy_id": None, "strategy_version": None, "session_id": None,
            "correlation_id": None, "capture_method": None,
            "source_attestation_verified": False, "full_strategy_config_verified": False,
            "all_evaluations_verified": False}


def project_saved_provenance(record: Mapping | None) -> dict:
    if record is None:
        return unknown_provenance()
    scope = record["saved_config_scope"]
    if not isinstance(scope, str) or scope not in _SCOPES or record["capture_method"] != "CONNECTION_LOCAL_INSERT_TRIGGER":
        raise ValueError("saved provenance scope is invalid")
    commit = record["code_commit"]
    if commit is not None and (not isinstance(commit, str) or not _COMMIT.fullmatch(commit)):
        raise ValueError("saved provenance commit is invalid")
    raw = record["saved_config_json"]
    if raw is not None and (not isinstance(raw, str) or len(raw.encode()) > 2048):
        raise ValueError("saved provenance configuration exceeds bound")
    config = json.loads(raw) if raw is not None else None
    if config is not None:
        if not isinstance(config, dict) or set(config) - _SCOPES[scope]:
            raise ValueError("saved provenance configuration is invalid")
        config = {key: value for key, value in config.items() if value is not None}
        for key, value in config.items():
            if key in _NUMERIC:
                if type(value) not in (int, float) or not 0 < value <= 2**53 or not math.isfinite(value):
                    raise ValueError("saved provenance numeric setting is invalid")
            elif not isinstance(value, str) or not 1 <= len(value) <= 128:
                raise ValueError("saved provenance text setting is invalid")
    result = unknown_provenance()
    result.update({"state": "CAPTURED_SAVED_SETTINGS" if config is not None and set(config) == _SCOPES[scope]
                              else "INCOMPLETE_SAVED_SETTINGS",
                   "code_commit": commit, "saved_config": config,
                   "saved_config_hash": hashlib.sha256(json.dumps(config, sort_keys=True,
                       separators=(",", ":"), allow_nan=False).encode()).hexdigest() if config is not None else None,
                   "saved_config_scope": scope, "capture_method": record["capture_method"]})
    for key in ("strategy_id", "strategy_version", "session_id", "correlation_id"):
        value = record[key]
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ValueError("saved provenance decision identity is invalid")
        result[key] = value
    return result


def provenance_metadata(row: dict) -> dict:
    """Verify exported hashes/identity before accepting a page or its cursor."""
    value = row.get("decision_provenance")
    if value is None:
        p = unknown_provenance()  # older exporter; not a false attestation
    else:
        if not isinstance(value, dict) or set(value) != set(unknown_provenance()) or \
                type(value.get("schema_version")) is not int or value["schema_version"] != 1 or \
                any(type(value.get(key)) is not bool for key in (
                    "source_attestation_verified", "full_strategy_config_verified", "all_evaluations_verified")):
            raise ValueError("decision provenance is invalid")
        if value.get("state") == "UNKNOWN":
            p = unknown_provenance()
            if value != p:
                raise ValueError("unknown decision provenance contains claims")
        else:
            if not isinstance(value.get("saved_config"), (dict, type(None))):
                raise ValueError("decision provenance configuration is invalid")
            record = {key: value.get(key) for key in ("strategy_id", "strategy_version", "session_id",
                "correlation_id", "code_commit", "saved_config_scope", "capture_method")}
            record["saved_config_json"] = json.dumps(value["saved_config"], allow_nan=False) if value["saved_config"] is not None else None
            p = project_saved_provenance(record)
            if value != p or any(p[key] != row.get(key) for key in
                                 ("strategy_id", "strategy_version", "session_id", "correlation_id")):
                raise ValueError("decision provenance hash or identity is invalid")
    # Do NOT populate generic config_hash: the entire engine/agent/risk config
    # is not captured. Reports must keep exact_version_verified false.
    return {"decision_provenance": p, "code_commit": p["code_commit"],
            "saved_config_hash": p["saved_config_hash"], "saved_config_scope": p["saved_config_scope"],
            "exact_version_verified": False}
