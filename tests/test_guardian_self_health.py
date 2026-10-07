"""Guardian-only liveness/readiness: no heartbeat manufacture or disk repair."""
from __future__ import annotations

import io
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tradexa.guardian import self_health as diagnostic
from tradexa.guardian.service import GuardianService
from tradexa.guardian.store import GuardianStore

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
READ = "independent-reader-key-for-tests"
SOURCE = "independent-source-key-for-tests"
OWN = ("guardian", "guardian_incident_engine", "guardian_reports")
REQUIRED = (*OWN, "smc_lab")


def request(app, route="/v1/self-health", *, key=READ, method="GET", query=""):
    result = {}

    def response(status, headers):
        result.update(status=int(status.split()[0]), headers=dict(headers))

    body = b"".join(app({"REQUEST_METHOD": method, "PATH_INFO": route,
                         "QUERY_STRING": query, "HTTP_X_GUARDIAN_KEY": key,
                         "wsgi.input": io.BytesIO()}, response))
    return result["status"], json.loads(body), result["headers"]


def filesystem(available=1024**3, *, free_inodes=1000, flags=0):
    return SimpleNamespace(f_frsize=4096, f_blocks=1024**2,
                           f_bavail=available // 4096, f_files=10000,
                           f_favail=free_inodes, f_flag=flags)


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = GuardianStore(tmp_path / "own.db")
    for name in REQUIRED:
        store.record_heartbeat(name, "HEALTHY", observed_at=NOW)
    monkeypatch.setattr(diagnostic.os, "statvfs", lambda _: filesystem())
    return store


def view(store, **kwargs):
    return diagnostic.self_health(store.path, REQUIRED, now=NOW, **kwargs)


def service(store):
    return GuardianService(store, source_keys={"smc_lab": SOURCE}, read_key=READ,
                           required_components=REQUIRED)


def test_closed_scope_metadata_only_and_sql_read_only(store, monkeypatch):
    before = store.heartbeats()
    connect = diagnostic.sqlite3.connect
    statements = []

    def tracked(path, **kwargs):
        assert "mode=ro" in str(path) and kwargs["uri"] is True
        assert kwargs["timeout"] <= 0.25
        conn = connect(path, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(diagnostic.sqlite3, "connect", tracked)
    data = view(store)
    assert data["scope"] == "GUARDIAN_ONLY" and data["state"] == "HEALTHY"
    assert data["evidence_complete"] is True
    assert data["database_snapshot_atomic"] is True
    assert data["filesystem_atomic_with_database"] is False
    assert data["automatic_action_allowed"] is False
    assert data["trading_integrity_verified"] is False
    assert set(data["components"]) == {*OWN, "guardian_storage"}
    storage = data["components"]["guardian_storage"]
    assert storage["database_readable"] is True
    assert storage["journal_mode"] == "wal"
    assert storage["filesystem_available_bytes"] == 1024**3
    assert storage["database_bytes"] == store.path.stat().st_size
    assert any("QUERY_ONLY" in sql.upper() for sql in statements)
    assert any("BEGIN" == sql.upper() for sql in statements)
    assert not any(word in sql.upper() for sql in statements
                   for word in ("INSERT", "UPDATE", "DELETE", "CHECKPOINT", "VACUUM"))
    monkeypatch.undo()
    assert store.heartbeats() == before and store.count() == 0


@pytest.mark.parametrize("state", ["FAILED", "BLOCKED", "DEGRADED", "UNKNOWN"])
def test_own_failure_is_not_hidden_by_fresh_other_heartbeats(store, state):
    store.record_heartbeat("guardian_reports", state, reason="REPORT_GENERATION_FAILED", observed_at=NOW)
    data = view(store)
    assert data["state"] == state
    assert data["components"]["guardian_reports"]["reason"] == "REPORT_GENERATION_FAILED"


@pytest.mark.parametrize("age", [91, -6])
def test_dead_or_future_monitor_remains_unknown(store, age):
    store.record_heartbeat("guardian", "HEALTHY", observed_at=NOW-timedelta(seconds=age))
    assert view(store)["state"] == "UNKNOWN"


def test_missing_monitor_and_upstream_failure_are_not_conflated(store):
    with closing(store._connect()) as conn:
        conn.execute("DELETE FROM heartbeats WHERE component='guardian_reports'")
        conn.commit()
    store.record_heartbeat("smc_lab", "FAILED", observed_at=NOW)
    result = view(store)
    assert result["state"] == "UNKNOWN"
    assert result["components"]["guardian_reports"]["state"] == "UNKNOWN"
    assert "smc_lab" not in result["components"]


@pytest.mark.parametrize("available,state,reason", [
    (0, "BLOCKED", "LOW_DISK_HEADROOM"),
    (64*1024**2, "BLOCKED", "LOW_DISK_HEADROOM"),
    (64*1024**2+4096, "DEGRADED", "LOW_DISK_HEADROOM"),
    (256*1024**2, "DEGRADED", "LOW_DISK_HEADROOM"),
    (256*1024**2+4096, "HEALTHY", "READABLE_WITH_OBSERVED_HEADROOM"),
])
def test_free_space_boundaries_do_not_claim_durability(store, monkeypatch, available, state, reason):
    monkeypatch.setattr(diagnostic.os, "statvfs", lambda _: filesystem(available))
    data = view(store)
    storage = data["components"]["guardian_storage"]
    assert storage["state"] == state and storage["reason"] == reason
    assert data["state"] == state and data["automatic_action_allowed"] is False
    assert storage["write_durability_verified"] is False


@pytest.mark.parametrize("free,state", [(0, "BLOCKED"), (15, "DEGRADED"), (16, "HEALTHY")])
def test_inode_pressure_is_observation_only(store, monkeypatch, free, state):
    monkeypatch.setattr(diagnostic.os, "statvfs", lambda _: filesystem(free_inodes=free))
    assert view(store)["components"]["guardian_storage"]["state"] == state


def test_filesystem_without_inode_reporting_does_not_invent_zero_inodes(store, monkeypatch):
    stats = filesystem()
    stats.f_files = stats.f_favail = 0
    monkeypatch.setattr(diagnostic.os, "statvfs", lambda _: stats)
    storage = view(store)["components"]["guardian_storage"]
    assert storage["state"] == "HEALTHY" and storage["filesystem_available_inodes"] is None


def test_read_only_filesystem_is_blocked_without_trying_to_write(store, monkeypatch):
    monkeypatch.setattr(diagnostic.os, "statvfs", lambda _: filesystem(flags=os.ST_RDONLY))
    storage = view(store)["components"]["guardian_storage"]
    assert storage["state"] == "BLOCKED" and storage["reason"] == "FILESYSTEM_READ_ONLY"


@pytest.mark.parametrize("field,value", [("f_bavail", -1), ("f_frsize", 0),
                                        ("f_blocks", 2**64), ("f_favail", -1),
                                        ("f_bavail", "not-a-count")])
def test_malformed_filesystem_metadata_is_unknown(store, monkeypatch, field, value):
    stats = filesystem()
    setattr(stats, field, value)
    monkeypatch.setattr(diagnostic.os, "statvfs", lambda _: stats)
    storage = view(store)["components"]["guardian_storage"]
    assert storage["state"] == "UNKNOWN" and storage["reason"] == "STORAGE_METADATA_UNAVAILABLE"


@pytest.mark.parametrize("error", [OSError, PermissionError, OverflowError])
def test_filesystem_errors_are_redacted_and_do_not_erase_observed_monitor_failure(store, monkeypatch, error):
    store.record_heartbeat("guardian_reports", "FAILED", observed_at=NOW)

    def fail(_):
        raise error("SECRET private path and credential")

    monkeypatch.setattr(diagnostic.os, "statvfs", fail)
    result = view(store)
    assert result["state"] == "FAILED"
    assert result["components"]["guardian_storage"]["state"] == "UNKNOWN"
    assert "SECRET" not in json.dumps(result)


def test_missing_database_does_not_create_a_file_or_parent(tmp_path):
    path = tmp_path / "not-created" / "missing.db"
    result = diagnostic.self_health(path, OWN, now=NOW)
    assert result["state"] == "UNKNOWN" and not path.parent.exists()
    assert result["components"]["guardian_storage"]["database_readable"] is False


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "sidecar", "parent", "permissions"])
def test_unsafe_database_location_is_not_opened(store, tmp_path, monkeypatch, unsafe):
    path = store.path
    if unsafe == "symlink":
        path = tmp_path / "alias.db"
        path.symlink_to(store.path)
    elif unsafe == "hardlink":
        os.link(store.path, tmp_path / "other.db")
    elif unsafe == "sidecar":
        sidecar = tmp_path / "own.db-journal"
        sidecar.symlink_to(store.path)
    elif unsafe == "parent":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        path = alias / "own.db"
    else:
        path.chmod(0o644)

    def forbidden(*args, **kwargs):
        raise AssertionError("must reject unsafe paths before opening SQLite")

    monkeypatch.setattr(diagnostic.sqlite3, "connect", forbidden)
    result = diagnostic.self_health(path, OWN, now=NOW)
    assert result["state"] == "UNKNOWN"
    assert result["components"]["guardian_storage"]["reason"] == "UNSAFE_STORAGE_PATH"


def test_real_exclusive_lock_is_bounded_unknown_and_retry_recovers(tmp_path, monkeypatch):
    store = GuardianStore(tmp_path / "locked.db")
    store.record_heartbeat("guardian", "HEALTHY", observed_at=NOW)
    monkeypatch.setattr(diagnostic.os, "statvfs", lambda _: filesystem())
    with closing(store._connect()) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("BEGIN EXCLUSIVE")
        result = view(store)
        assert result["state"] == "UNKNOWN"
        assert result["components"]["guardian_storage"]["reason"] == "GUARDIAN_DB_READ_BLOCKED"
        conn.rollback()
        conn.execute("PRAGMA journal_mode=WAL")
    assert diagnostic.self_health(store.path, ("guardian",), now=NOW)["state"] == "HEALTHY"


def test_one_hundred_refreshes_during_uncommitted_wal_write_neither_block_nor_write(store):
    before = store.heartbeats()
    with closing(store._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE heartbeats SET state='FAILED' WHERE component='guardian'")
        for _ in range(100):
            result = view(store)
            assert result["state"] == "HEALTHY"
        conn.rollback()
    assert store.heartbeats() == before and store.count() == 0


def test_one_hundred_authenticated_requests_during_short_write_do_not_500(store):
    app = service(store)
    for name in REQUIRED:
        store.record_heartbeat(name, "HEALTHY")
    before = store.heartbeats()
    with closing(store._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE heartbeats SET state='FAILED' WHERE component='guardian'")
        for _ in range(100):
            status, data, _ = request(app)
            assert status == 200 and data["state"] == "HEALTHY"
        conn.rollback()
    assert store.heartbeats() == before and store.count() == 0


def test_committed_wal_failure_is_visible_and_recovery_does_not_require_restart(store):
    assert view(store)["state"] == "HEALTHY"
    with closing(store._connect()) as conn:
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("UPDATE heartbeats SET state='FAILED' WHERE component='guardian'")
        conn.commit()
        assert view(store)["state"] == "FAILED"
        conn.execute("UPDATE heartbeats SET state='HEALTHY' WHERE component='guardian'")
        conn.commit()
        assert view(store)["state"] == "HEALTHY"


def test_non_wal_database_is_degraded_not_silently_reconfigured(store):
    with closing(store._connect()) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    data = view(store)
    storage = data["components"]["guardian_storage"]
    assert storage["state"] == "DEGRADED" and storage["reason"] == "JOURNAL_MODE_NOT_WAL"
    with closing(store._connect()) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


@pytest.mark.parametrize("boundary", ["open", "query"])
def test_sql_read_deadline_fails_closed_even_for_small_queries(store, monkeypatch, boundary):
    # Tiny indexed reads may not invoke SQLite's 1,000-op progress callback.
    values = iter([0, 1] if boundary == "open" else [0, 0, 1])
    monkeypatch.setattr(diagnostic, "monotonic", lambda: next(values, 1))
    storage = view(store)["components"]["guardian_storage"]
    assert storage["state"] == "UNKNOWN"
    assert storage["reason"] == "GUARDIAN_DB_READ_BLOCKED"


def test_corrupt_database_is_unknown_not_reinitialized_or_reported_healthy(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a SQLite database")
    path.chmod(0o600)
    monkeypatch.setattr(diagnostic.os, "statvfs", lambda _: filesystem())
    original = path.read_bytes()
    result = diagnostic.self_health(path, OWN, now=NOW)
    assert result["state"] == "UNKNOWN"
    assert result["components"]["guardian_storage"]["reason"] == "GUARDIAN_DB_READ_FAILED"
    assert path.read_bytes() == original


def test_missing_heartbeat_table_is_unknown_and_never_created(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    path.chmod(0o600)
    monkeypatch.setattr(diagnostic.os, "statvfs", lambda _: filesystem())
    assert diagnostic.self_health(path, OWN, now=NOW)["state"] == "UNKNOWN"
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0


def test_short_uppercase_private_reason_is_also_redacted(store):
    with closing(store._connect()) as conn:
        conn.execute("UPDATE heartbeats SET reason='PRIVATE_API_KEY' WHERE component='guardian'")
        conn.commit()
    data = view(store)
    assert data["components"]["guardian"]["reason"] == "REPORTED_REASON_REDACTED"
    assert "PRIVATE_API_KEY" not in json.dumps(data)


def test_unrelated_trading_sentinel_is_not_opened_or_changed(store, tmp_path, monkeypatch):
    sentinel = tmp_path / "smc_paper.db"
    sentinel.write_bytes(b"paper order and journal identities are outside scope")
    before = sentinel.read_bytes()
    connect = diagnostic.sqlite3.connect

    def guarded(path, **kwargs):
        assert str(path).startswith(store.path.as_uri()+"?")
        return connect(path, **kwargs)

    monkeypatch.setattr(diagnostic.sqlite3, "connect", guarded)
    assert view(store)["state"] == "HEALTHY"
    assert sentinel.read_bytes() == before


def test_large_wal_is_pressure_not_corruption_and_is_never_checkpointed(store, monkeypatch):
    actual = diagnostic._file_sizes

    def large(path):
        result = actual(path)
        result["wal_bytes"] = 256*1024**2
        return result

    monkeypatch.setattr(diagnostic, "_file_sizes", large)
    before = store.heartbeats()
    result = view(store)
    storage = result["components"]["guardian_storage"]
    assert storage["state"] == "DEGRADED" and storage["reason"] == "WAL_PRESSURE"
    assert storage["database_readable"] is True
    assert result["trading_integrity_verified"] is False
    assert store.heartbeats() == before


def test_bad_or_private_heartbeat_fields_are_bounded_and_redacted(store):
    with closing(store._connect()) as conn:
        conn.execute("UPDATE heartbeats SET reason=? WHERE component='guardian_reports'",
                     ("SECRET /private-path " * 1000,))
        conn.execute("UPDATE heartbeats SET observed_at=? WHERE component='guardian'", ("x"*100000,))
        conn.commit()
    result = view(store)
    assert result["state"] == "UNKNOWN"
    assert "SECRET" not in json.dumps(result) and str(store.path) not in json.dumps(result)
    assert len(json.dumps(result)) < 8192


@pytest.mark.parametrize("column,value", [
    ("state", "HEALTHY\x00invalid"),
    ("observed_at", NOW.isoformat()+"\x00invalid"),
    ("state", "HEALTHY"+"x"*100000),
])
def test_malformed_heartbeat_is_not_clipped_into_a_healthy_value(store, column, value):
    with closing(store._connect()) as conn:
        conn.execute("UPDATE heartbeats SET "+column+"=? WHERE component='guardian'", (value,))
        conn.commit()
    result = view(store)
    assert result["state"] == "UNKNOWN"
    assert result["evidence_complete"] is False


@pytest.mark.parametrize("names", [(), ("smc_lab",), ("guardian", "guardian"),
                                   ("guardian", "guardian/secret"),
                                   ("guardian", "guardian_storage"),
                                   tuple("guardian_"+str(n) for n in range(129))])
def test_invalid_or_unbounded_monitor_configuration_fails_closed(store, names):
    with pytest.raises(ValueError):
        diagnostic.self_health(store.path, names, now=NOW)


def test_naive_clock_rejected(store):
    with pytest.raises(ValueError):
        diagnostic.self_health(store.path, OWN, now=NOW.replace(tzinfo=None))


def test_read_authority_query_and_method_contract(store, monkeypatch):
    app = service(store)

    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized or invalid request must not inspect storage")

    monkeypatch.setattr("tradexa.guardian.service.self_health", forbidden)
    for key in ("", SOURCE, "incorrect"):
        assert request(app, key=key)[:2] == (401, {"error": "UNAUTHORIZED"})
    for method in ("POST", "DELETE", "PUT"):
        assert request(app, method=method)[0] == 405
    assert request(app, query="path=/private")[:2] == (400, {"error": "INVALID_SELF_HEALTH_QUERY"})


def test_self_endpoint_and_platform_health_do_not_reset_a_dead_monitor(store):
    app = service(store)
    moment = datetime.now(timezone.utc)
    for name in REQUIRED:
        store.record_heartbeat(name, "HEALTHY", observed_at=moment)
    store.record_heartbeat("guardian", "FAILED", reason="MONITOR_FAILED", observed_at=moment)
    before = store.heartbeats()
    assert request(app, "/healthz")[1]["self_state"] == "ALIVE"
    status, data, headers = request(app)
    assert status == 200 and data["state"] == "FAILED"
    assert headers["Cache-Control"] == "no-store"
    _, health, _ = request(app, "/v1/health")
    assert health["state"] == "FAILED" and health["self_health"]["state"] == "FAILED"
    assert store.heartbeats() == before


def test_low_space_masks_green_platform_health_without_changing_source_state(store, monkeypatch):
    app = service(store)
    for name in REQUIRED:
        store.record_heartbeat(name, "HEALTHY")
    before = store.heartbeats()
    monkeypatch.setattr(diagnostic.os, "statvfs", lambda _: filesystem(0))
    _, result, _ = request(app, "/v1/health")
    assert result["state"] == "BLOCKED"
    assert result["state_reason"] == "GUARDIAN_SELF_HEALTH"
    assert result["components"]["smc_lab"]["state"] == "HEALTHY"
    assert result["self_health"]["automatic_action_allowed"] is False
    assert store.heartbeats() == before


def test_persistence_error_self_endpoint_is_available_and_truthful(store, monkeypatch):
    app = service(store)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("SECRET path and credential")

    monkeypatch.setattr(diagnostic.sqlite3, "connect", fail)
    status, data, _ = request(app)
    assert status == 200 and data["state"] == "UNKNOWN"
    assert data["components"]["guardian_storage"]["database_readable"] is False
    assert "SECRET" not in json.dumps(data)
    assert request(app, "/healthz")[1]["self_state"] == "ALIVE"
