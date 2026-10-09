"""Source-local instance audit capture; no Guardian runtime or trading dependency."""
from __future__ import annotations

import json
import os
import re
import sqlite3

_SETTINGS = ("strategy_key", "strategy_version", "config_revision", "entry_mode",
             "trading_mode", "min_quality_score")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def applied_settings_json(decision: dict) -> str | None:
    """Snapshot supplied applied values only, never infer current/legacy settings."""
    settings = decision.get("applied_settings")
    if not isinstance(settings, dict):
        return None
    projected = {key: settings.get(key) for key in _SETTINGS}
    projected.update(symbol=decision["symbol"], timeframe=decision.get("timeframe"))
    # Invalid optional telemetry is unavailable evidence, not a new reason to
    # change the existing decision. Validate scalars BEFORE encoding to avoid
    # retaining nested secrets, arbitrary objects or an unbounded payload.
    for key, value in projected.items():
        if value is None:
            continue
        if key in {"config_revision", "min_quality_score"}:
            if type(value) is not int or not (1 if key == "config_revision" else 0) <= value <= 2**53:
                return None
        elif not isinstance(value, str) or not 1 <= len(value) <= 128:
            return None
    encoded = json.dumps(projected, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return encoded if len(encoded.encode()) <= 2048 else None


def install_instance_provenance(connection: sqlite3.Connection) -> None:
    reported = [os.environ.get(key) for key in ("GIT_COMMIT", "RENDER_GIT_COMMIT") if os.environ.get(key)]
    valid = bool(reported) and all(_COMMIT.fullmatch(value) for value in reported) and len(set(reported)) == 1
    commit = "'" + reported[0] + "'" if valid else "NULL"
    connection.execute("""CREATE TABLE IF NOT EXISTS guardian_instance_decision_provenance(
        decision_id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL,
        decision_identity TEXT NOT NULL, code_commit TEXT, saved_config_json TEXT)""")
    for operation in ("UPDATE", "DELETE"):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS guardian_instance_provenance_no_{operation.lower()}
            BEFORE {operation} ON guardian_instance_decision_provenance
            BEGIN SELECT RAISE(ABORT,'instance decision provenance is immutable'); END""")
    connection.execute("DROP TRIGGER IF EXISTS temp.guardian_capture_instance_provenance")
    connection.execute(f"""CREATE TEMP TRIGGER guardian_capture_instance_provenance
        AFTER INSERT ON main.decisions WHEN NEW.instance_id <> ''
        BEGIN INSERT INTO guardian_instance_decision_provenance(
            decision_id,instance_id,decision_identity,code_commit,saved_config_json)
        VALUES(NEW.id,NEW.instance_id,NEW.decision_identity,{commit},NEW.applied_settings_json); END""")
