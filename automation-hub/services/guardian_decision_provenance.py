"""Capture bounded saved lab settings without editing the frozen lab modules.

A TEMP trigger belongs to its writer connection, not the database or another
process. It captures only new evaluation INSERTs in the same SQLite statement;
legacy rows, lifecycle updates and uninstrumented writers remain unknown.
There is no network call, broker call, strategy evaluation or hashing UDF.
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Mapping

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def install_lab_decision_provenance(connection: sqlite3.Connection, lab: str, *,
                                    environment: Mapping[str, str] | None = None) -> None:
    if lab not in {"pa", "smc"}:
        raise ValueError("unknown lab provenance source")
    # Probe native JSON token preservation before changing any schema. A
    # numeric json_extract/json_object round-trip rounds REAL values and
    # turns boolean tokens into integers.
    connection.execute("SELECT json('{}' -> '$')").fetchone()
    env = os.environ if environment is None else environment
    reported = [env.get(name) for name in ("RENDER_GIT_COMMIT", "GIT_COMMIT") if env.get(name)]
    # Never export invalid raw environment text, or choose between conflicting
    # declared identities. This is a declaration, not a source-hash attestation.
    valid = bool(reported) and all(isinstance(value, str) and _COMMIT.fullmatch(value)
                                   for value in reported) and len(set(reported)) == 1
    commit_literal = "'" + reported[0] + "'" if valid else "NULL"
    table = f"{lab}_guardian_decision_provenance"
    path = "saved_execution_config" if lab == "pa" else "saved_configuration"
    keys = (("operating_mode", "strategy_id", "risk_pct", "max_risk_pct",
             "max_concurrent_risk_pct", "target_r") if lab == "pa" else
            ("operating_mode", "model_id", "risk_pct"))
    scope = "PA_SAVED_EXECUTION_SETTINGS" if lab == "pa" else "SMC_SAVED_SESSION_SETTINGS"
    pairs = ["'symbol',NEW.symbol", "'timeframe',NEW.timeframe"]
    pairs.extend(f"'{key}',json(NEW.payload_json -> '$.{path}.{key}')" for key in keys)
    snapshot = "json_object(" + ",".join(pairs) + ")"
    connection.execute(f"""CREATE TABLE IF NOT EXISTS {table}(
        correlation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
        code_commit TEXT, saved_config_scope TEXT NOT NULL,
        saved_config_json TEXT, capture_method TEXT NOT NULL)""")
    for operation in ("UPDATE", "DELETE"):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}
            BEFORE {operation} ON {table}
            BEGIN SELECT RAISE(ABORT,'guardian decision provenance is immutable'); END""")
    trigger = f"{lab}_guardian_capture_provenance"
    connection.execute(f"DROP TRIGGER IF EXISTS temp.{trigger}")
    connection.execute(f"""CREATE TEMP TRIGGER {trigger} AFTER INSERT ON main.{lab}_evaluations
        BEGIN INSERT INTO {table}(correlation_id,session_id,strategy_id,strategy_version,
            code_commit,saved_config_scope,saved_config_json,capture_method)
        VALUES(NEW.correlation_id,NEW.session_id,NEW.strategy_id,NEW.strategy_version,
            {commit_literal},'{scope}',
            CASE WHEN length(CAST({snapshot} AS BLOB)) <= 2048 THEN {snapshot} ELSE NULL END,
            'CONNECTION_LOCAL_INSERT_TRIGGER'); END""")
