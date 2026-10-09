"""Guardian readers support 3.10 without weakening their SQL/read-only bounds."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from tradexa.guardian import pipeline_health, self_health, transport_health
from tradexa.guardian.incidents import GuardianIncidentEngine
from tradexa.guardian.notifications import GuardianNotifications
from tradexa.guardian.store import GuardianStore
from tests.test_guardian_transport_health import NOW, report, snapshot


class LegacyConnection(sqlite3.Connection):
    def __getattribute__(self, name):
        if name == "setlimit":
            raise AttributeError("setlimit was added in Python 3.11")
        return super().__getattribute__(name)


def test_service_import_without_python_311_result_constants():
    # Isolated interpreter also catches import-time/class-definition failures.
    code = """
import sqlite3
for name in ('SQLITE_BUSY', 'SQLITE_LOCKED', 'SQLITE_INTERRUPT', 'SQLITE_LIMIT_LENGTH'):
    if hasattr(sqlite3, name):
        delattr(sqlite3, name)
from tradexa.guardian.service import GuardianService
from tradexa.guardian.self_health import _ReadDeadline
assert _ReadDeadline.sqlite_errorcode == 9
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=15)


@pytest.mark.parametrize("reader", ["self", "pipeline", "transport"])
def test_legacy_sqlite_readers_remain_bounded_read_only_and_recover(tmp_path, monkeypatch, reader):
    store = GuardianStore(tmp_path / "guardian.db")
    GuardianIncidentEngine(store)
    GuardianNotifications(store)
    for name in ("guardian", *pipeline_health.MONITORS):
        store.record_heartbeat(name, "HEALTHY", observed_at=NOW)
    event = report()
    with closing(store._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        store._append_in_transaction(conn, event, NOW.isoformat())
        conn.commit()
    connect = sqlite3.connect
    statements = []

    def legacy_connect(path, **kwargs):
        assert "mode=ro" in str(path) and kwargs["uri"] is True
        assert kwargs["timeout"] <= 0.25
        conn = connect(path, factory=LegacyConnection, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    for name in ("SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_INTERRUPT", "SQLITE_LIMIT_LENGTH"):
        monkeypatch.delattr(sqlite3, name, raising=False)
    monkeypatch.setattr(sqlite3, "connect", legacy_connect)

    def view():
        if reader == "self":
            return self_health.self_health(store.path, ("guardian",), now=NOW)
        if reader == "pipeline":
            return pipeline_health.pipeline_health(store.path, now=NOW)
        return transport_health.transport_health(store.path, ("smc_lab",), now=NOW)

    assert view()["state"] == "HEALTHY"
    with closing(connect(store.path)) as writer:
        if reader == "self":
            writer.execute("UPDATE heartbeats SET observed_at=? WHERE component='guardian'", ("SECRET" * 20000,))
        elif reader == "pipeline":
            writer.execute("UPDATE heartbeats SET observed_at=? WHERE component='guardian_incident_engine'",
                           ("SECRET" * 20000,))
        else:
            malformed = json.loads(report(snapshot(snapshot_sequence=2)).canonical_json())
            malformed["evidence"]["private_oversized_field"] = "SECRET" * 20000
            writer.execute(
                "INSERT INTO events(event_id,timestamp,received_at,source_service,source_component,"
                "event_type,severity,payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (malformed["event_id"], NOW.isoformat(), NOW.isoformat(), "smc_lab", "transport",
                 "producer_transport_observed", "INFO", json.dumps(malformed)))
        writer.commit()
        data = view()
        assert data["state"] == "UNKNOWN" and "SECRET" not in json.dumps(data)
        assert len(json.dumps(data)) < 16384
        if reader == "self":
            writer.execute("UPDATE heartbeats SET observed_at=? WHERE component='guardian'", (NOW.isoformat(),))
        elif reader == "pipeline":
            writer.execute("UPDATE heartbeats SET observed_at=? WHERE component='guardian_incident_engine'",
                           (NOW.isoformat(),))
        else:
            # Never erase/alter the invalid immutable evidence. The latest-two
            # view recovers only after two newer valid reports arrive.
            writer.execute("BEGIN IMMEDIATE")
            for number in (3, 4):
                store._append_in_transaction(writer, report(snapshot(snapshot_sequence=number)), NOW.isoformat())
        writer.commit()
    assert view()["state"] == "HEALTHY"
    assert any("QUERY_ONLY" in sql.upper() for sql in statements)
    assert any("BUSY_TIMEOUT=250" in sql.upper() for sql in statements)
    assert not any(sql.lstrip().upper().startswith(word) for sql in statements
                   for word in ("INSERT", "UPDATE", "DELETE", "CHECKPOINT", "VACUUM"))


@pytest.mark.parametrize("message", ["database is locked", "database table is locked", "interrupted"])
def test_legacy_error_without_numeric_code_remains_blocked(message):
    from tradexa.guardian.sqlite_reads import read_is_blocked
    assert read_is_blocked(sqlite3.OperationalError(message)) is True


@pytest.mark.parametrize("code,blocked", [(5, True), (6, True), (9, True), (261, True), (11, False), (13, False)])
def test_numeric_primary_or_extended_code_is_authoritative(code, blocked):
    from tradexa.guardian.sqlite_reads import read_is_blocked
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = code
    assert read_is_blocked(error) is blocked


def test_legacy_unknown_error_is_not_a_blocker_or_exposed():
    from tradexa.guardian.sqlite_reads import read_is_blocked
    assert read_is_blocked(sqlite3.OperationalError("SECRET private database failure")) is False


def test_available_native_value_limit_is_used_and_failure_propagates():
    from tradexa.guardian.sqlite_reads import bound_read_values
    calls = []

    class Connection:
        def setlimit(self, category, size):
            calls.append((category, size))
            raise sqlite3.OperationalError("limit setup failed")

    with pytest.raises(sqlite3.OperationalError):
        bound_read_values(Connection(), 4096)
    assert calls == [(0, 4096)]
