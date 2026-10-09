"""Retained processing diagnostics must not confuse a heartbeat with progress."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from time import perf_counter

import pytest

from tradexa.guardian import pipeline_health as diagnostics
from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.incidents import GuardianIncidentEngine
from tradexa.guardian.notifications import GuardianNotifications
from tradexa.guardian.service import GuardianService
from tradexa.guardian.store import GuardianStore
from tests.test_guardian_service import READ_KEY, SOURCE_KEY, _request

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
MONITORS = ("guardian_incident_engine", "guardian_notifications")


@pytest.fixture
def store(tmp_path):
    result = GuardianStore(tmp_path / "guardian.db")
    GuardianIncidentEngine(result)
    GuardianNotifications(result)
    for name in MONITORS:
        result.record_heartbeat(name, "HEALTHY", observed_at=NOW)
    return result


def _append(store, *, sequence, age=0, outage=False, received_at=None, evidence=None):
    event = GuardianEvent(
        source_service="smc_lab", source_component="agent",
        event_type="worker_crashed" if outage else "condition_failed",
        event_id=f"fixture_event_{sequence:08d}",
        timestamp=NOW-timedelta(days=7), evidence=evidence if evidence is not None else {"blocker": "NO_SETUP"})
    receipt = received_at if received_at is not None else (NOW-timedelta(seconds=age)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO events(sequence,event_id,timestamp,received_at,source_service,"
            "source_component,event_type,severity,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (sequence, event.event_id, event.timestamp.isoformat(), receipt,
             event.source_service, event.source_component, event.event_type,
             event.severity, event.canonical_json()))


def _view(store):
    return diagnostics.pipeline_health(store.path, now=NOW)


def _pipe(view, name="guardian_incident_engine"):
    return view["components"][name]


def _cursor(store, sequence, *, notification=False):
    table, column, name = (("guardian_notification_cursor", "last_update_sequence", "in_app_v1")
                           if notification else
                           ("guardian_analysis_cursor", "last_event_sequence", "incidents_v1"))
    with store._connect() as conn:
        conn.execute(f"UPDATE {table} SET {column}=? WHERE name=?", (sequence, name))


def test_empty_snapshot_caught_up_is_not_ingestion_or_trading_certification(store):
    view = _view(store)
    assert view["state"] == "HEALTHY"
    assert view["processing_state"] == "CAUGHT_UP"
    assert view["database_snapshot_atomic"] is True
    for row in view["components"].values():
        assert row["pending_count"] == row["pending_count_lower_bound"] == 0
        assert row["cursor_sequence"] == row["latest_sequence"] == 0
        assert row["first_pending_sequence"] is None
    for key in ("producer_queue_depth", "producer_dropped_count", "producer_ingestion_delay_seconds"):
        assert view[key] is None
    for key in ("trading_integrity_verified", "full_history_verified", "remote_delivery_verified",
                "automatic_action_allowed"):
        assert view[key] is False


def test_count_rows_not_sequence_difference_and_ignore_source_backfill_age(store):
    _append(store, sequence=1)
    _append(store, sequence=100_000_000)
    _append(store, sequence=2_000_000_000)
    _cursor(store, 1)
    view = _view(store)
    row = _pipe(view)
    assert row["cursor_sequence"] == 1 and row["latest_sequence"] == 2_000_000_000
    assert row["pending_count"] == row["pending_count_lower_bound"] == 2
    assert row["first_pending_sequence"] == 100_000_000
    assert row["first_pending_received_age_seconds"] == 0
    assert row["processing_state"] == "PENDING" and row["state"] == "HEALTHY"


@pytest.mark.parametrize("age,state", [(59, "PENDING"), (60, "LAGGING"), (120, "LAGGING")])
def test_fresh_heartbeat_does_not_hide_old_retained_pending_events(store, age, state):
    _append(store, sequence=1, age=age)
    row = _pipe(_view(store))
    assert row["heartbeat"]["state"] == "HEALTHY"
    assert row["processing_state"] == state
    assert row["state"] == ("HEALTHY" if state == "PENDING" else "DEGRADED")
    assert row["first_pending_received_age_seconds"] == age


@pytest.mark.parametrize("count,processing", [(999, "PENDING"), (1000, "LAGGING"), (5000, "LAGGING"), (5001, "LAGGING")])
def test_bounded_row_counts_and_explicit_truncation(store, count, processing):
    # One short fixture transaction; no material payload is selected by diagnostics.
    sample = GuardianEvent(source_service="smc_lab", source_component="agent",
                          event_type="condition_failed", event_id="bulk_fixture_00000001",
                          timestamp=NOW).canonical_json()
    with store._connect() as conn:
        conn.executemany(
            "INSERT INTO events(event_id,timestamp,received_at,source_service,source_component,"
            "event_type,severity,payload_json) VALUES(?,?,?,?,?,?,?,?)",
            [(f"bulk_fixture_{i:08d}", NOW.isoformat(), NOW.isoformat(), "smc_lab", "agent",
              "condition_failed", "INFO", sample) for i in range(count)])
    view = _view(store)
    row = _pipe(view)
    assert row["processing_state"] == processing
    assert row["pending_count"] == (None if count > 5000 else count)
    assert row["pending_count_lower_bound"] == count
    assert row["truncated"] is (count > 5000)
    assert view["evidence_complete"] is (count <= 5000)


def test_notification_age_is_evidence_age_not_queue_residency(store):
    _append(store, sequence=1, age=86_400, outage=True)
    assert GuardianIncidentEngine(store).scan() == 1
    row = _pipe(_view(store), MONITORS[1])
    assert row["pending_count"] == 1 and row["processing_state"] == "PENDING"
    assert row["queue_residency_seconds"] is None
    assert row["first_pending_evidence_received_age_seconds"] == 86_400
    assert row["first_pending_received_age_seconds"] is None
    assert row["state"] == "HEALTHY"


@pytest.mark.parametrize("notification", [False, True])
@pytest.mark.parametrize("fault", ["missing", "negative", "fractional", "text", "ahead", "missing_anchor"])
def test_invalid_cursors_are_unknown_never_reset_or_caught_up(store, fault, notification):
    _append(store, sequence=10, outage=True)
    GuardianIncidentEngine(store).scan()
    table, column, name = (("guardian_notification_cursor", "last_update_sequence", "in_app_v1")
                           if notification else
                           ("guardian_analysis_cursor", "last_event_sequence", "incidents_v1"))
    with store._connect() as conn:
        if fault == "missing":
            conn.execute(f"DELETE FROM {table} WHERE name=?", (name,))
        else:
            value = {"negative": -1, "fractional": 0.5, "text": "credential-secret",
                     "ahead": 9999, "missing_anchor": 5 if not notification else 1}[fault]
            if notification and fault == "missing_anchor":
                # Move the retained update to a legitimate gapped sequence.
                conn.execute("UPDATE guardian_incident_updates SET sequence=10")
            conn.execute(f"UPDATE {table} SET {column}=? WHERE name=?", (value, name))
        before = conn.execute(f"SELECT * FROM {table}").fetchall()
    view = _view(store)
    row = _pipe(view, MONITORS[int(notification)])
    assert row["state"] == row["processing_state"] == "UNKNOWN"
    assert row["pending_count"] is None
    assert "credential-secret" not in json.dumps(view)
    with store._connect() as conn:
        assert conn.execute(f"SELECT * FROM {table}").fetchall() == before


@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-10-07T12:00:00", "2026-10-07T12:00:00+00:00\x00extra",
                                        "x"*10000, "2026-10-07T12:00:10+00:00", 1234])
def test_malformed_or_future_pending_timestamp_cannot_claim_readiness(store, timestamp):
    # Non-null schema is deliberately violated only via a separate raw schema for None.
    if timestamp is None:
        timestamp = b"not-a-text-time"
    _append(store, sequence=1, received_at=timestamp)
    row = _pipe(_view(store))
    assert row["state"] == row["processing_state"] == "UNKNOWN"
    assert row["first_pending_received_age_seconds"] is None


@pytest.mark.parametrize("state,age,expected", [("FAILED", 0, "FAILED"), ("BLOCKED", 0, "BLOCKED"),
    ("DEGRADED", 0, "DEGRADED"), ("UNKNOWN", 0, "UNKNOWN"), ("HEALTHY", 91, "UNKNOWN"),
    ("HEALTHY", -6, "UNKNOWN")])
def test_reported_failure_or_unverified_heartbeat_cannot_be_hidden(store, state, age, expected):
    store.record_heartbeat(MONITORS[0], state, reason="unrelated-sensitive-prose",
                           observed_at=NOW-timedelta(seconds=age))
    view = _view(store)
    assert _pipe(view)["state"] == expected
    assert "unrelated-sensitive-prose" not in json.dumps(view)


def test_missing_heartbeat_is_unknown_even_if_retained_queue_is_empty(store):
    with store._connect() as conn:
        conn.execute("DELETE FROM heartbeats WHERE component=?", (MONITORS[0],))
    row = _pipe(_view(store))
    assert row["state"] == "UNKNOWN" and row["pending_count"] == 0


@pytest.mark.parametrize("column,value", [
    ("state", "HEALTHY\x00FAILED"), ("state", "H"*10000), ("state", 42),
    ("observed_at", NOW.isoformat()+"\x00hidden"), ("observed_at", "x"*10000),
    ("observed_at", b"not-text"), ("observed_at", "2026-10-07T12:00:00"),
], ids=("state-nul", "oversized-state", "state-integer", "timestamp-nul", "oversized-time", "time-blob", "time-naive"))
def test_invalid_heartbeat_cells_cannot_manufacture_readiness(store, column, value):
    with store._connect() as conn:
        conn.execute(f"UPDATE heartbeats SET {column}=? WHERE component=?", (value, MONITORS[0]))
    row = _pipe(_view(store))
    assert row["state"] == row["heartbeat"]["state"] == "UNKNOWN"
    # SQLite may reject the entire oversized stored record under the 4 KiB
    # record limit before CASE can return its individual metadata cells.
    assert row["pending_count"] in (None, 0)


def test_notification_gaps_are_not_notice_backlog_counts(store):
    for sequence in (1, 10, 100):
        _append(store, sequence=sequence, outage=True)
    assert GuardianIncidentEngine(store).scan() == 3
    with store._connect() as conn:
        conn.execute("UPDATE guardian_incident_updates SET sequence=2000000000 WHERE sequence=3")
        conn.execute("UPDATE guardian_incident_updates SET sequence=1000000000 WHERE sequence=2")
    _cursor(store, 1, notification=True)
    row = _pipe(_view(store), MONITORS[1])
    assert row["pending_count"] == 2 and row["latest_sequence"] == 2000000000
    assert row["first_pending_sequence"] == 1000000000


def test_large_notification_backlog_is_visible_without_false_age_alarm(store):
    _append(store, sequence=1, age=86400, outage=True)
    GuardianIncidentEngine(store).scan()
    with store._connect() as conn:
        [update] = conn.execute("SELECT incident_id,observed_at,transition,summary FROM guardian_incident_updates").fetchall()
        # Unique incident/event pairs have actual retained source links.
        event = conn.execute("SELECT * FROM events WHERE sequence=1").fetchone()
        conn.executemany(
            "INSERT INTO events(event_id,timestamp,received_at,source_service,source_component,event_type,severity,payload_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(f"notification_fixture_{i:08d}", event["timestamp"], event["received_at"],
              event["source_service"], event["source_component"], event["event_type"],
              event["severity"], event["payload_json"]) for i in range(1, 1000)])
        conn.executemany(
            "INSERT INTO guardian_incident_updates(incident_id,event_id,observed_at,transition,summary) VALUES(?,?,?,?,?)",
            [(update["incident_id"], f"notification_fixture_{i:08d}", update["observed_at"],
              update["transition"], update["summary"]) for i in range(1, 1000)])
    row = _pipe(_view(store), MONITORS[1])
    assert row["pending_count"] == 1000 and row["processing_state"] == "LAGGING"
    assert row["queue_residency_seconds"] is None
    assert row["first_pending_evidence_received_age_seconds"] == 86400


@pytest.mark.parametrize("target", ["event", "incident"])
def test_unlinked_pending_notification_cannot_disappear_behind_inner_join(store, target):
    _append(store, sequence=1, outage=True)
    GuardianIncidentEngine(store).scan()
    with store._connect() as conn:
        column = "event_id" if target == "event" else "incident_id"
        conn.execute(f"UPDATE guardian_incident_updates SET {column}='missing-link'")
    row = _pipe(_view(store), MONITORS[1])
    assert row["state"] == row["processing_state"] == "UNKNOWN"
    assert row["reason"] == "PENDING_UPDATE_LINK_UNVERIFIED"


def test_metadata_byte_budget_marks_exact_backlog_unknown(store, monkeypatch):
    for i in range(1, 6):
        _append(store, sequence=i)
    monkeypatch.setattr(diagnostics, "MAX_METADATA_BYTES", 100)
    view = _view(store)
    row = _pipe(view)
    assert row["pending_count"] is None and row["truncated"] is True
    assert row["pending_count_lower_bound"] > 0
    assert view["evidence_complete"] is False


def test_gets_do_not_append_reclassify_process_or_checkpoint(store, monkeypatch):
    _append(store, sequence=1, outage=True)
    before = _view(store)
    def forbidden(*args, **kwargs):
        raise AssertionError("read must not use persistence constructor, writer or processor")
    monkeypatch.setattr(store, "_connect", forbidden)
    monkeypatch.setattr(GuardianStore, "__init__", forbidden)
    monkeypatch.setattr(GuardianIncidentEngine, "scan", forbidden)
    monkeypatch.setattr(GuardianNotifications, "scan", forbidden)
    for _ in range(100):
        assert _view(store) == before


def test_wal_uncommitted_write_reads_last_atomic_snapshot_then_commit(store):
    _append(store, sequence=1, age=90)
    writer = store._connect()
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE guardian_analysis_cursor SET last_event_sequence=1")
        for _ in range(100):
            view = _view(store)
            assert view["database_snapshot_atomic"] is True
            assert _pipe(view)["cursor_sequence"] == 0 and _pipe(view)["pending_count"] == 1
        writer.commit()
        assert _pipe(_view(store))["pending_count"] == 0
    finally:
        writer.close()


def test_restart_and_processor_drain_recover_without_notices_or_events_from_reader(store):
    _append(store, sequence=1, outage=True)
    _append(store, sequence=100)
    assert _pipe(_view(store))["pending_count"] == 2
    assert GuardianIncidentEngine(store).scan(limit=1) == 1
    assert _pipe(_view(store))["pending_count"] == 1
    assert _pipe(_view(store), MONITORS[1])["pending_count"] == 1
    assert GuardianNotifications(store).scan() == 1
    assert GuardianIncidentEngine(store).scan() == 2  # normal event + notice audit
    assert _view(store)["processing_state"] == "CAUGHT_UP"
    before = store.count()
    for _ in range(3):
        restarted = GuardianStore(store.path)
        assert GuardianIncidentEngine(restarted).scan() == 0
        assert GuardianNotifications(restarted).scan() == 0
        assert _view(restarted)["processing_state"] == "CAUGHT_UP"
    assert store.count() == before
    assert len(GuardianNotifications(store).list()) == 1
    assert len(GuardianIncidentEngine(store).list()) == 1


@pytest.mark.parametrize("fault", ["missing_file", "missing_schema", "corrupt", "symlink", "hardlink", "permissions"])
def test_storage_failure_is_sanitized_unknown_without_creating_or_repairing(store, tmp_path, fault):
    path = store.path
    if fault == "missing_file":
        path = tmp_path/"no-parent"/"not-created.db"
    elif fault == "missing_schema":
        with store._connect() as conn:
            conn.execute("DROP TABLE guardian_notification_cursor")
    elif fault == "corrupt":
        path = tmp_path/"broken.db"
        path.write_bytes(b"not a sqlite database")
        path.chmod(0o600)
    elif fault == "symlink":
        path = tmp_path/"link.db"
        path.symlink_to(store.path)
    elif fault == "hardlink":
        import os
        path = tmp_path/"hardlink.db"
        os.link(store.path, path)
    else:
        path.chmod(0o644)
    view = diagnostics.pipeline_health(path, now=NOW)
    assert view["state"] == view["processing_state"] == "UNKNOWN"
    assert view["database_snapshot_atomic"] is False
    assert all(row["pending_count"] is None for row in view["components"].values())
    assert str(tmp_path) not in json.dumps(view)
    if fault == "missing_file":
        assert not path.parent.exists()


def test_exclusive_rollback_lock_returns_unknown_then_retry_works(store):
    with store._connect() as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    writer = store._connect()
    try:
        writer.execute("BEGIN EXCLUSIVE")
        start = perf_counter()
        view = _view(store)
        assert perf_counter()-start < 1.5
        assert view["state"] == "UNKNOWN" and view["reason"] == "GUARDIAN_DB_READ_BLOCKED"
        writer.rollback()
        assert _view(store)["state"] == "HEALTHY"
    finally:
        writer.close()


def test_read_deadline_is_unknown_not_zero_backlog(store, monkeypatch):
    ticks = iter((0.0, 1.0))
    monkeypatch.setattr(diagnostics, "monotonic", lambda: next(ticks, 1.0))
    view = _view(store)
    assert view["state"] == "UNKNOWN" and view["reason"] == "GUARDIAN_DB_READ_BLOCKED"
    assert _pipe(view)["pending_count"] is None


def test_sql_progress_deadline_interrupts_a_large_scan(store, monkeypatch):
    for sequence in range(1, 101):
        _append(store, sequence=sequence)
    ticks = iter((0.0, 0.0))
    monkeypatch.setattr(diagnostics, "monotonic", lambda: next(ticks, 1.0))
    view = _view(store)
    assert view["reason"] == "GUARDIAN_DB_READ_BLOCKED"
    assert view["database_snapshot_atomic"] is False
    assert _pipe(view)["pending_count"] is None


@pytest.mark.parametrize("large_payload", [False, True])
def test_read_snapshot_authorizer_prohibits_writes_and_all_payload_columns(store, monkeypatch, large_payload):
    _append(store, sequence=1, outage=True,
            evidence={"retained_notes": ["x"*2000]*30} if large_payload else None)
    real = sqlite3.connect
    statements = []
    def connect(*args, **kwargs):
        assert "mode=ro" in args[0] and kwargs["uri"] is True
        assert kwargs["timeout"] == 0.25
        conn = real(*args, **kwargs)
        def authorize(action, table, column, *_):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                pytest.fail("diagnostics attempted SQL write")
            if action == sqlite3.SQLITE_READ and column in ("payload_json", "summary", "reason"):
                pytest.fail("diagnostics selected payload or untrusted prose")
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(diagnostics.sqlite3, "connect", connect)
    view = _view(store)
    assert _pipe(view)["pending_count"] == 1
    assert "PRAGMA query_only=ON" in statements and "PRAGMA busy_timeout=250" in statements
    assert statements.count("BEGIN") == 1 and statements.count("ROLLBACK") == 1
    assert not any("COUNT(" in sql.upper() or "CHECKPOINT" in sql.upper() for sql in statements)


def test_api_auth_method_query_boundaries_and_read_only_snapshot(store, monkeypatch):
    app = GuardianService(store, source_keys={"smc_lab": SOURCE_KEY}, read_key=READ_KEY,
                          required_components=("guardian", "smc_lab"),
                          research_key="independent-research-key-for-this-test",
                          admin_key="independent-owner-admin-key-for-this-test")
    route = "/v1/pipeline-health"
    import tradexa.guardian.service as service
    real = service.pipeline_health
    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized/invalid requests must not inspect storage")
    monkeypatch.setattr(service, "pipeline_health", forbidden)
    for key in ("", SOURCE_KEY, "independent-research-key-for-this-test",
                "independent-owner-admin-key-for-this-test", "unrelated-control-key-for-this-test"):
        assert _request(app, "GET", route, key=key)[0] == 401
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert _request(app, method, route, key=READ_KEY)[0] == 405
    for query in ("path=other.db", "limit=9000", "?", "after=0", "unknown=1"):
        assert _request(app, "GET", route, key=READ_KEY, query=query)[0] == 400
    monkeypatch.setattr(service, "pipeline_health", real)
    with store._connect() as conn:
        conn.execute("DELETE FROM heartbeats WHERE component=?", (MONITORS[0],))
    status, view, headers = _request(app, "GET", route, key=READ_KEY)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert view["state"] == "UNKNOWN"  # an actually missing monitor, not a clock assumption
    assert store.count() == 0


def test_overall_health_masks_fresh_heartbeat_when_retained_processing_lags(store):
    app = GuardianService(store, source_keys={"smc_lab": SOURCE_KEY}, read_key=READ_KEY,
                          required_components=("guardian", "smc_lab"))
    for name in ("guardian", "smc_lab", *MONITORS):
        store.record_heartbeat(name, "HEALTHY")
    _append(store, sequence=1, received_at=(datetime.now(timezone.utc)-timedelta(seconds=120)).isoformat())
    status, health, _ = _request(app, "GET", "/v1/health", key=READ_KEY)
    assert status == 200 and health["state"] == "DEGRADED"
    assert health["state_reason"] == "GUARDIAN_PROCESSING_BACKLOG"
    assert health["components"][MONITORS[0]]["heartbeat"]["state"] == "HEALTHY"
    assert health["components"][MONITORS[0]]["state"] == "DEGRADED"
    assert health["components"]["smc_lab"]["state"] == "HEALTHY"
    assert health["pipeline_health"]["automatic_action_allowed"] is False


def test_unavailable_pipeline_snapshot_cannot_erase_a_reported_failure(store):
    app = GuardianService(store, source_keys={"smc_lab": SOURCE_KEY}, read_key=READ_KEY,
                          required_components=("guardian", "smc_lab", *MONITORS))
    for name in ("guardian", "smc_lab", *MONITORS):
        store.record_heartbeat(name, "HEALTHY")
    store.record_heartbeat(MONITORS[0], "FAILED", reason="INCIDENT_ANALYSIS_FAILED")
    with store._connect() as conn:
        conn.execute("DROP TABLE guardian_notification_cursor")
    status, view, _ = _request(app, "GET", "/v1/pipeline-health", key=READ_KEY)
    assert status == 200 and view["state"] == "UNKNOWN"
    status, health, _ = _request(app, "GET", "/v1/health", key=READ_KEY)
    assert status == 200 and health["state"] == "FAILED" and health["evidence_complete"] is False
    assert health["components"][MONITORS[0]]["state"] == "FAILED"
    assert health["pipeline_health"]["state"] == "UNKNOWN"
    assert health["pipeline_health"]["database_snapshot_atomic"] is False
