"""Pure partial instance provenance contract; never attest full alpha/risk config."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

_KEYS = frozenset({"symbol", "timeframe", "strategy_key", "strategy_version",
                   "config_revision", "entry_mode", "trading_mode", "min_quality_score"})
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def unknown_instance_provenance() -> dict:
    return {"schema_version": 1, "state": "UNKNOWN", "instance_id": None,
            "decision_identity": None, "code_commit": None, "saved_config": None,
            "saved_config_hash": None, "saved_config_scope": None,
            "capture_method": None, "source_attestation_verified": False,
            "full_strategy_config_verified": False, "all_evaluations_verified": False}


def project_instance_provenance(record: Mapping | None) -> dict:
    if record is None:
        return unknown_instance_provenance()
    owner, identity = record["instance_id"], record["decision_identity"]
    if not isinstance(owner, str) or not 1 <= len(owner) <= 128 or \
            not isinstance(identity, str) or len(identity) > 512:
        raise ValueError("instance provenance identity is invalid")
    commit, raw = record["code_commit"], record["saved_config_json"]
    if commit is not None and (not isinstance(commit, str) or not _COMMIT.fullmatch(commit)):
        raise ValueError("instance provenance commit is invalid")
    if raw is not None and (not isinstance(raw, str) or len(raw.encode()) > 2048):
        raise ValueError("instance provenance configuration exceeds bound")
    config = json.loads(raw) if raw is not None else None
    if config is not None:
        if not isinstance(config, dict) or set(config) - _KEYS:
            raise ValueError("instance provenance configuration is invalid")
        config = {key: value for key, value in config.items() if value is not None}
        for key, value in config.items():
            if key in {"config_revision", "min_quality_score"}:
                if type(value) is not int or not (1 if key == "config_revision" else 0) <= value <= 2**53:
                    raise ValueError("instance provenance integer setting is invalid")
            elif not isinstance(value, str) or not 1 <= len(value) <= 128:
                raise ValueError("instance provenance text setting is invalid")
    p = unknown_instance_provenance()
    p.update({"state": "CAPTURED_APPLIED_SETTINGS" if identity and config is not None and set(config) == _KEYS
                       else "INCOMPLETE_APPLIED_SETTINGS",
              "instance_id": owner, "decision_identity": identity, "code_commit": commit,
              "saved_config": config, "saved_config_hash": hashlib.sha256(json.dumps(config,
                  sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                  if config is not None else None,
              "saved_config_scope": "INSTANCE_APPLIED_DECISION_SETTINGS",
              "capture_method": "CONNECTION_LOCAL_INSERT_TRIGGER"})
    return p


def instance_provenance_metadata(row: dict) -> dict:
    value = row.get("instance_provenance")
    if value is None:
        p = unknown_instance_provenance()  # compatibility with older exporters
    else:
        baseline = unknown_instance_provenance()
        if not isinstance(value, dict) or set(value) != set(baseline) or \
                type(value.get("schema_version")) is not int or value["schema_version"] != 1 or \
                any(type(value.get(key)) is not bool for key in (
                    "source_attestation_verified", "full_strategy_config_verified", "all_evaluations_verified")):
            raise ValueError("instance provenance contract is invalid")
        if value["state"] == "UNKNOWN":
            p = baseline
        else:
            config = value["saved_config"]
            p = project_instance_provenance({"instance_id": value["instance_id"],
                "decision_identity": value["decision_identity"], "code_commit": value["code_commit"],
                "saved_config_json": json.dumps(config, allow_nan=False) if config is not None else None})
            if p["instance_id"] != row.get("instance_id") or p["decision_identity"] != row.get("decision_identity") or \
                    (p["saved_config"] is not None and any(p["saved_config"].get(key) != row.get(key)
                        for key in ("symbol", "timeframe") if key in p["saved_config"])):
                raise ValueError("instance provenance decision identity is invalid")
        if value != p:
            raise ValueError("instance provenance hash or claims are invalid")
    # Partial applied settings must NOT populate the full-engine config_hash.
    return {"instance_provenance": p, "code_commit": p["code_commit"],
            "saved_config_hash": p["saved_config_hash"], "saved_config_scope": p["saved_config_scope"],
            "exact_version_verified": False}
