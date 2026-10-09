"""Typed opt-in transport reports; not a producer inventory or delivery proof."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Sequence

from .events import GuardianEvent, GuardianEventError, _NAME
from .self_health import _file_sizes, _ReadDeadline, _state, _UnsafePath
from .sqlite_reads import bound_read_values, read_is_blocked

KIND = "producer_transport_observed"
COUNTERS = ("enqueued", "delivered", "invalid", "backpressure_dropped", "delivery_failed")
MAX_SOURCES = 128
MAX_REPORT_BYTES = 4096
_FIELDS = frozenset({
    "transport_schema_version", "producer_epoch", "snapshot_sequence", "uptime_seconds", "queue_capacity",
    "queued_events", "in_flight_events", "oldest_pending_age_seconds", "last_delivery_latency_ms",
    "accepting_events", "worker_alive", "counters", "diagnostic_delivery_failed",
})


def _integer(value, lower=0, upper=2**63-1) -> bool:
    return type(value) is int and lower <= value <= upper


def _duration(value) -> bool:
    return type(value) in (int, float) and 0 <= value <= 2**53 and math.isfinite(value)


def validate_transport_event(event: GuardianEvent) -> dict:
    """Validate a versioned, coherent snapshot, returning only typed fields."""
    data = event.evidence
    if (event.event_type != KIND or event.source_component != "transport" or
            event.severity != "INFO" or not isinstance(data, dict) or set(data) != _FIELDS or
            type(data["transport_schema_version"]) is not int or data["transport_schema_version"] != 1 or
            not isinstance(data["producer_epoch"], str) or not re.fullmatch(r"[0-9a-f]{32}", data["producer_epoch"]) or
            not _integer(data["snapshot_sequence"], 1) or
            event.event_id != f"transport_{data['producer_epoch']}_{data['snapshot_sequence']}" or
            not _integer(data["queue_capacity"], 1, 65536) or
            not _integer(data["queued_events"], upper=data["queue_capacity"]) or
            not _integer(data["in_flight_events"], upper=1) or
            not _integer(data["diagnostic_delivery_failed"]) or not _duration(data["uptime_seconds"]) or
            any(type(data[field]) is not bool for field in ("accepting_events", "worker_alive"))):
        raise GuardianEventError("invalid transport snapshot")
    counters = data["counters"]
    if (not isinstance(counters, dict) or set(counters) != set(COUNTERS) or
            any(not _integer(value) for value in counters.values()) or
            counters["enqueued"] != counters["delivered"] + counters["delivery_failed"] +
            data["queued_events"] + data["in_flight_events"]):
        raise GuardianEventError("incoherent transport counters")
    pending = data["queued_events"] + data["in_flight_events"]
    completed = counters["delivered"] + counters["delivery_failed"]
    if (not _duration(data["oldest_pending_age_seconds"]) if pending else
            data["oldest_pending_age_seconds"] is not None):
        raise GuardianEventError("invalid pending age")
    if (not _duration(data["last_delivery_latency_ms"]) if completed else
            data["last_delivery_latency_ms"] is not None):
        raise GuardianEventError("invalid delivery latency")
    if ((pending and data["oldest_pending_age_seconds"] > data["uptime_seconds"] + 0.001) or
            (completed and data["last_delivery_latency_ms"] > data["uptime_seconds"]*1000 + 1)):
        raise GuardianEventError("transport age exceeds process uptime")
    if any(getattr(event, name) is not None for name in (
        "agent_id", "instance_id", "lab_id", "strategy_id", "strategy_version", "symbol", "timeframe",
        "state_before", "state_after", "decision", "reason", "correlation_id", "session_id",
        "execution_id", "order_id", "position_id", "latency_ms")) or event.metadata:
        raise GuardianEventError("transport reports cannot claim trading evidence")
    if len(event.canonical_json().encode("utf-8")) > MAX_REPORT_BYTES:
        raise GuardianEventError("transport report is oversized")
    return {**data, "counters": dict(counters)}


def _age(raw, now: datetime) -> float:
    if not isinstance(raw, str) or len(raw) > 64 or "\x00" in raw:
        raise ValueError("invalid report timestamp")
    observed = datetime.fromisoformat(raw)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("report clock is naive")
    age = (now-observed.astimezone(timezone.utc)).total_seconds()
    if age < -5 or age > 90:
        raise ValueError("report is stale or ahead of clock")
    return round(max(0, age), 3)


def _empty() -> dict:
    return {"state": "UNKNOWN", "reason": "NO_TRANSPORT_REPORT_OBSERVED", "metrics": None,
            "report_age_seconds": None, "source_report_age_seconds": None,
            "counter_history_verified": False, "process_inventory_verified": False}


def _source_view(rows, name: str, now: datetime) -> dict:
    result = _empty()
    if not rows:
        return result
    try:
        latest = GuardianEvent.from_payload(json.loads(rows[0]["payload_json"]))
        data = validate_transport_event(latest)
        if latest.source_service != name:
            raise ValueError("source mismatch")
        result["report_age_seconds"] = _age(rows[0]["received_at"], now)
        result["source_report_age_seconds"] = _age(latest.timestamp.isoformat(), now)
        if len(rows) == 2:
            previous_event = GuardianEvent.from_payload(json.loads(rows[1]["payload_json"]))
            previous = validate_transport_event(previous_event)
            if previous_event.source_service != name:
                raise ValueError("previous source mismatch")
            if data["producer_epoch"] == previous["producer_epoch"]:
                if (data["snapshot_sequence"] <= previous["snapshot_sequence"] or
                        data["queue_capacity"] != previous["queue_capacity"] or
                        data["uptime_seconds"] < previous["uptime_seconds"] or
                        data["diagnostic_delivery_failed"] < previous["diagnostic_delivery_failed"] or
                        any(data["counters"][key] < previous["counters"][key] for key in COUNTERS)):
                    result["reason"] = "TRANSPORT_COUNTER_REGRESSION"
                    return result
                result["counter_history_verified"] = True  # adjacent pair only, never full history
    except (GuardianEventError, ValueError, TypeError, KeyError, OverflowError):
        result["reason"] = "TRANSPORT_REPORT_UNVERIFIED"
        return result
    result["metrics"] = data
    if not data["worker_alive"] or not data["accepting_events"]:
        state, reason = "BLOCKED", "PRODUCER_REPORTED_STOPPED"
    elif any(data["counters"][key] for key in ("invalid", "backpressure_dropped", "delivery_failed")) or data["diagnostic_delivery_failed"]:
        state, reason = "DEGRADED", "PRODUCER_REPORTED_TELEMETRY_LOSS"
    elif (data["queued_events"] >= data["queue_capacity"] or
          (data["oldest_pending_age_seconds"] or 0) >= 60):
        state, reason = "DEGRADED", "PRODUCER_BACKLOG_PRESSURE"
    else:
        state, reason = "HEALTHY", "LATEST_PROCESS_REPORT_WITHIN_WARNING_THRESHOLDS"
    result.update(state=state, reason=reason)
    return result


def transport_health(path: str | Path, sources: Sequence[str], *, now: datetime | None = None) -> dict:
    """Bounded latest-two-report view; no zeroes inferred for silent producers."""
    if (not sources or len(sources) > MAX_SOURCES or len(set(sources)) != len(sources) or
            any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in sources)):
        raise ValueError("invalid producer configuration")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("aware report clock required")
    moment = moment.astimezone(timezone.utc)
    result = {
        "scope": "LATEST_REPORTED_PRODUCER_PROCESS_ONLY", "state": "UNKNOWN",
        "reason": "TRANSPORT_EVIDENCE_UNAVAILABLE", "observed_at": moment.isoformat(),
        "sources": {name: _empty() for name in sources}, "database_snapshot_atomic": False,
        "producer_inventory_verified": False, "source_clock_verified": False,
        "all_events_delivered_verified": False, "full_history_verified": False,
        "network_ingestion_delay_ms": None, "trading_integrity_verified": False,
        "automatic_action_allowed": False,
    }
    path = Path(os.path.abspath(path))
    try:
        _file_sizes(path)
        deadline = monotonic()+0.5
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=0.25)) as conn:
            if monotonic() > deadline:
                raise _ReadDeadline()
            conn.row_factory = sqlite3.Row
            bound_read_values(conn, MAX_REPORT_BYTES+1024)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=250")
            conn.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
            conn.execute("BEGIN")
            observed = {}
            for name in sources:
                rows = conn.execute(
                    "SELECT CASE WHEN typeof(received_at)='text' AND length(CAST(received_at AS BLOB))<=64 "
                    "THEN received_at ELSE NULL END AS received_at, "
                    "CASE WHEN typeof(payload_json)='text' AND length(CAST(payload_json AS BLOB))<=? "
                    "THEN payload_json ELSE NULL END AS payload_json FROM events "
                    "WHERE source_service=? AND event_type='producer_transport_observed' "
                    "ORDER BY sequence DESC LIMIT 2", (MAX_REPORT_BYTES, name)).fetchall()
                observed[name] = _source_view(rows, name, moment)
                if monotonic() > deadline:
                    raise _ReadDeadline()
            conn.rollback()
            result["sources"] = observed
    except _UnsafePath:
        result["reason"] = "UNSAFE_STORAGE_PATH"
        return result
    except (OSError, ValueError, TypeError, OverflowError):
        return result
    except sqlite3.Error as exc:
        result["reason"] = ("GUARDIAN_DB_READ_BLOCKED" if read_is_blocked(exc)
                            else "GUARDIAN_DB_READ_FAILED")
        return result
    result.update(database_snapshot_atomic=True, state=_state(result["sources"]),
                  reason="LATEST_TRANSPORT_REPORTS_OBSERVED")
    return result
