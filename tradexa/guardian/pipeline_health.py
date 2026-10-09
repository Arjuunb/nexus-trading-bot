"""Bounded read-only progress of Guardian's retained, local processing queues.

No producer ingestion/drop inference, trading I/O, schema creation, checkpoint,
payload deserialization or processing. Sequence gaps are not queued events.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from .health import component_health
from .self_health import _file_sizes, _ReadDeadline, _state, _UnsafePath
from .sqlite_reads import bound_read_values, read_is_blocked

MAX_PENDING_ROWS = 5000
MAX_METADATA_BYTES = 1024**2
WARNING_PENDING_ROWS = 1000
WARNING_RECEIPT_AGE_SECONDS = 60
MONITORS = ("guardian_incident_engine", "guardian_notifications")
_TIME = ("CASE WHEN typeof({0})='text' AND length(CAST({0} AS BLOB))<=64 "
         "THEN {0} ELSE NULL END")


def _empty() -> dict:
    return {
        "state": "UNKNOWN", "reason": "PIPELINE_EVIDENCE_UNAVAILABLE",
        "processing_state": "UNKNOWN", "cursor_sequence": None, "latest_sequence": None,
        "pending_count": None, "pending_count_lower_bound": None,
        "first_pending_sequence": None, "first_pending_received_age_seconds": None,
        "first_pending_evidence_received_age_seconds": None, "queue_residency_seconds": None,
        "truncated": False, "queue_evidence_complete": False,
        "heartbeat": {"state": "UNKNOWN", "reason": "HEARTBEAT_UNVERIFIED"},
    }


def _sequence(value, *, zero=False) -> bool:
    return type(value) is int and int(not zero) <= value <= 2**63-1


def _age(value, now: datetime) -> float | None:
    if not isinstance(value, str) or "\x00" in value:
        return None
    try:
        observed = datetime.fromisoformat(value)
        if observed.tzinfo is None or observed.utcoffset() is None:
            return None
        age = (now-observed.astimezone(timezone.utc)).total_seconds()
        return round(max(0, age), 2) if age >= -5 else None
    except (ValueError, OverflowError):
        return None


def _queue(conn: sqlite3.Connection, *, notification: bool, now: datetime) -> dict:
    result = _empty()
    table, cursor_table, cursor_column, cursor_name = (
        ("guardian_incident_updates", "guardian_notification_cursor", "last_update_sequence", "in_app_v1")
        if notification else ("events", "guardian_analysis_cursor", "last_event_sequence", "incidents_v1"))
    # Bounded fixed SQL identifiers only. CASE prevents an oversized/malformed
    # cursor cell being returned, and never clips invalid evidence into valid.
    cursor_rows = conn.execute(
        f"SELECT CASE WHEN typeof({cursor_column})='integer' THEN {cursor_column} "
        f"ELSE NULL END FROM {cursor_table} WHERE name=? LIMIT 2", (cursor_name,)).fetchall()
    if len(cursor_rows) != 1 or not _sequence(cursor_rows[0][0], zero=True):
        result["reason"] = "CURSOR_UNVERIFIED"
        return result
    cursor = result["cursor_sequence"] = cursor_rows[0][0]
    latest_row = conn.execute(f"SELECT sequence FROM {table} ORDER BY sequence DESC LIMIT 1").fetchone()
    first_row = conn.execute(f"SELECT sequence FROM {table} ORDER BY sequence LIMIT 1").fetchone()
    if latest_row is None:
        latest = 0
    elif not _sequence(latest_row[0]) or not _sequence(first_row[0]):
        result["reason"] = "RETAINED_SEQUENCE_UNVERIFIED"
        return result
    else:
        latest = latest_row[0]
    result["latest_sequence"] = latest
    if cursor > latest:
        result["reason"] = "CURSOR_AHEAD_OF_RETAINED_QUEUE"
        return result
    if cursor and conn.execute(f"SELECT 1 FROM {table} WHERE sequence=? LIMIT 1", (cursor,)).fetchone() is None:
        result["reason"] = "CURSOR_ANCHOR_UNVERIFIED"
        return result
    if notification:
        # LEFT JOIN deliberately keeps unlinked queued updates visible. The
        # processor's inner join is not proof that a missing link was consumed.
        sql = ("SELECT u.sequence,"+_TIME.format("u.observed_at")+" AS receipt,"
               "(e.event_id IS NOT NULL AND i.incident_id IS NOT NULL) AS linked "
               "FROM guardian_incident_updates u LEFT JOIN events e ON e.event_id=u.event_id "
               "LEFT JOIN guardian_incidents i ON i.incident_id=u.incident_id "
               "WHERE u.sequence>? ORDER BY u.sequence LIMIT ?")
    else:
        sql = ("SELECT sequence,"+_TIME.format("received_at")+" AS receipt,1 AS linked "
               "FROM events WHERE sequence>? ORDER BY sequence LIMIT ?")
    count, metadata_bytes, last_sequence, first_age = 0, 0, cursor, None
    invalid = None
    for row in conn.execute(sql, (cursor, MAX_PENDING_ROWS+1)):
        count += 1
        metadata_bytes += 32 + (len(row["receipt"].encode("utf-8")) if isinstance(row["receipt"], str) else 0)
        if not _sequence(row["sequence"]) or row["sequence"] <= last_sequence:
            invalid = "RETAINED_SEQUENCE_UNVERIFIED"
        else:
            last_sequence = row["sequence"]
        age = _age(row["receipt"], now)
        if age is None:
            invalid = "PENDING_TIMESTAMP_UNVERIFIED"
        if row["linked"] != 1:
            invalid = "PENDING_UPDATE_LINK_UNVERIFIED"
        if count == 1:
            result["first_pending_sequence"] = row["sequence"] if _sequence(row["sequence"]) else None
            first_age = age
        if count > MAX_PENDING_ROWS or metadata_bytes > MAX_METADATA_BYTES:
            result["truncated"] = True
            break
    result["pending_count_lower_bound"] = count
    result["pending_count"] = None if result["truncated"] else count
    if invalid:
        result["reason"] = invalid
        return result
    result["queue_evidence_complete"] = not result["truncated"]
    if notification:
        # observed_at is original event receipt, NOT insertion into this queue.
        # Historical backfills must not trigger queue-delay alarms from it.
        result["first_pending_evidence_received_age_seconds"] = first_age
    else:
        result["first_pending_received_age_seconds"] = first_age
        result["queue_residency_seconds"] = first_age
    pressure = (result["truncated"] or count >= WARNING_PENDING_ROWS or
                (not notification and first_age is not None and first_age >= WARNING_RECEIPT_AGE_SECONDS))
    result.update(
        processing_state="LAGGING" if pressure else "PENDING" if count else "CAUGHT_UP",
        state="DEGRADED" if pressure else "HEALTHY",
        reason="BACKLOG_PRESSURE" if pressure else "PENDING_WITHIN_WARNING_THRESHOLDS" if count else "RETAINED_QUEUE_CAUGHT_UP",
    )
    return result


def _snapshot(path: Path, now: datetime) -> dict:
    deadline = monotonic()+0.5
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=0.25)) as conn:
        if monotonic() > deadline:
            raise _ReadDeadline()
        conn.row_factory = sqlite3.Row
        bound_read_values(conn, 4096)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=250")
        conn.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        conn.execute("BEGIN")
        # Do not return heartbeat prose, event IDs, summary or payload columns.
        rows = conn.execute(
            "SELECT component,CASE WHEN typeof(state)='text' AND length(CAST(state AS BLOB))<=16 "
            "THEN state ELSE NULL END AS state,"+_TIME.format("observed_at")+
            " AS observed_at FROM heartbeats WHERE component IN (?,?) LIMIT 3", MONITORS).fetchall()
        if len(rows) > 2 or len({row["component"] for row in rows}) != len(rows):
            raise sqlite3.DatabaseError("invalid monitor identity")
        beats = {}
        for row in rows:
            beat = dict(row)
            if any(isinstance(beat[field], str) and "\x00" in beat[field] for field in ("state", "observed_at")):
                beat["state"] = None
            beats[beat["component"]] = beat
        monitors = component_health(beats, MONITORS, now=now)["components"]
        result = {}
        for index, name in enumerate(MONITORS):
            queue = _queue(conn, notification=bool(index), now=now)
            monitor = monitors[name]
            queue["heartbeat"] = {**monitor, "reason": "HEARTBEAT_UNVERIFIED" if monitor["state"] == "UNKNOWN"
                                  else "MONITOR_REPORTED_"+monitor["state"]}
            # Known reported failures retain priority even if queue evidence is
            # unavailable; an available queue never certifies worker execution.
            queue["state"] = _state({"queue": queue, "monitor": monitor})
            if monitor["state"] != "HEALTHY" and queue["state"] == monitor["state"]:
                queue["reason"] = queue["heartbeat"]["reason"]
            result[name] = queue
        if monotonic() > deadline:
            raise _ReadDeadline()
        conn.rollback()
        return result


def pipeline_health(path: str | Path, *, now: datetime | None = None) -> dict:
    """Inspect only retained Guardian queues in one bounded database snapshot.

    CAUGHT_UP applies only to retained rows, not unreceived/dropped telemetry,
    full historical integrity, delivered remote alerts or healthy trading.
    """
    moment = now or datetime.now(timezone.utc)
    observed = component_health({}, MONITORS, now=moment)["observed_at"]
    moment = moment.astimezone(timezone.utc)
    path = Path(os.path.abspath(path))
    result = {
        "scope": "GUARDIAN_RETAINED_PROCESSING_ONLY", "state": "UNKNOWN",
        "processing_state": "UNKNOWN", "reason": "PIPELINE_EVIDENCE_UNAVAILABLE",
        "observed_at": observed, "components": {name: _empty() for name in MONITORS},
        "database_snapshot_atomic": False, "evidence_complete": False,
        "producer_queue_depth": None, "producer_dropped_count": None,
        "producer_ingestion_delay_seconds": None, "remote_delivery_verified": False,
        "full_history_verified": False, "trading_integrity_verified": False,
        "automatic_action_allowed": False,
        "policy": {"max_pending_rows_per_queue": MAX_PENDING_ROWS,
                   "max_metadata_bytes_per_queue": MAX_METADATA_BYTES,
                   "warning_pending_rows": WARNING_PENDING_ROWS,
                   "warning_event_receipt_age_seconds": WARNING_RECEIPT_AGE_SECONDS,
                   "notification_queue_age_verified": False},
    }
    try:
        _file_sizes(path)
        result["components"] = _snapshot(path, moment)
    except _UnsafePath:
        result["reason"] = "UNSAFE_STORAGE_PATH"
        return result
    except OSError:
        return result  # No paths, exception prose, automatic repair or DB creation.
    except sqlite3.Error as exc:
        result["reason"] = ("GUARDIAN_DB_READ_BLOCKED" if read_is_blocked(exc)
                            else "GUARDIAN_DB_READ_FAILED")
        return result
    components = result["components"]
    result["state"] = _state(components)
    result["processing_state"] = next(
        (state for state in ("UNKNOWN", "LAGGING", "PENDING", "CAUGHT_UP")
         if any(row["processing_state"] == state for row in components.values())), "UNKNOWN")
    result["database_snapshot_atomic"] = True
    result["evidence_complete"] = all(row["queue_evidence_complete"] and
                                      row["heartbeat"]["state"] != "UNKNOWN" for row in components.values())
    result["reason"] = "RETAINED_PROCESSING_OBSERVED"
    return result


def include_pipeline_health(health: dict, diagnostics: dict) -> dict:
    """Read-model masking only; never rewrite processor heartbeats or gates."""
    previous = health["state"]
    for name, row in diagnostics["components"].items():
        previous_row = health["components"].get(name, {})
        if (not diagnostics["database_snapshot_atomic"] and
                previous_row.get("state") in ("FAILED", "BLOCKED", "DEGRADED")):
            # A later unavailable queue read cannot erase an already observed
            # failure. Its UNKNOWN heartbeat/queue stay explicit in the child.
            row = {**row, "state": previous_row["state"],
                   "reason": "REPORTED_FAILURE_WITH_PIPELINE_UNAVAILABLE"}
        health["components"][name] = row
    health["state"] = _state(health["components"])
    health["pipeline_health"] = diagnostics
    health["evidence_complete"] = (diagnostics["evidence_complete"] and all(
        row["state"] != "UNKNOWN" for row in health["components"].values()))
    if health["state"] != previous:
        health["state_reason"] = "GUARDIAN_PROCESSING_BACKLOG"
    return health
