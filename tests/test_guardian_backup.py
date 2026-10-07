"""Consistent Guardian-only snapshots; never a trading-account restore."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tradexa.guardian import backup
from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.store import GuardianStore


def event(number=1):
    return GuardianEvent(source_service="fixture", source_component="smc_intent_history",
        event_type="execution_uncertain", event_id=f"execution_{number:08d}",
        timestamp=datetime(2026, 10, 7, 12, tzinfo=timezone.utc),
        execution_id=f"key_{number}", evidence={"broker_execution_verified": False})


def seeded(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    store.append_observed_page("smc_intent_history", expected=(0, ""),
                              next_cursor=(1, "anchor1"), events=[event()])
    store.record_heartbeat("guardian_smc_intent_history", "FAILED", reason="SOURCE_UNAVAILABLE")
    return store


def dump(path):
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as db:
        return tuple(db.iterdump())


def test_online_backup_includes_uncheckpointed_wal_and_pinned_cursor(tmp_path):
    store = seeded(tmp_path)
    # Keep WAL open: raw copying guardian.db would miss committed records.
    with closing(store._connect()) as keeper:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        store.append(event(2))
        assert (tmp_path / "guardian.db-wal").stat().st_size > 0
        before = dump(store.path)
        report = backup.create_backup(store.path, tmp_path / "snapshot.db")
        assert dump(tmp_path / "snapshot.db") == before == dump(store.path)
    assert report["state"] == "CONSISTENT_GUARDIAN_SNAPSHOT"
    assert report["table_rows"]["events"] == 2
    assert report["table_rows"]["observer_cursors"] == 1
    assert report["trading_integrity_verified"] is False
    assert report["external_authenticity_verified"] is False
    assert (tmp_path / "snapshot.db").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "snapshot.db-wal").exists()
    assert backup.verify_backup(tmp_path / "snapshot.db", expected_digest=report["logical_sha256"])["logical_sha256"] == report["logical_sha256"]


def test_writer_commit_during_backup_cannot_split_evidence_and_checkpoint(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    original = backup._copy_pages

    def copy_with_writer(source, target, **kwargs):
        store.append_observed_page("smc_intent_history", expected=(1, "anchor1"),
                                  next_cursor=(2, "anchor2"), events=[event(2)])
        return original(source, target, **kwargs)

    monkeypatch.setattr(backup, "_copy_pages", copy_with_writer)
    report = backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert store.count() == 2
    assert report["table_rows"]["events"] == 1
    with closing(sqlite3.connect(tmp_path / "snapshot.db")) as db:
        assert db.execute("SELECT source_sequence,anchor_id FROM observer_cursors").fetchone() == (1, "anchor1")
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_restore_to_new_file_preserves_identity_replay_and_immutable_evidence(tmp_path):
    store = seeded(tmp_path)
    report = backup.create_backup(store.path, tmp_path / "snapshot.db")
    restored = backup.restore_backup(tmp_path / "snapshot.db", tmp_path / "restored.db",
                                     expected_digest=report["logical_sha256"])
    assert restored["logical_sha256"] == report["logical_sha256"]
    assert dump(store.path) == dump(tmp_path / "restored.db")
    reopened = GuardianStore(tmp_path / "restored.db")
    assert reopened.observer_cursor("smc_intent_history") == (1, "anchor1")
    assert reopened.append(event()) is False
    assert reopened.count() == 1
    assert reopened.heartbeats()["guardian_smc_intent_history"]["state"] == "FAILED"
    with closing(sqlite3.connect(reopened.path)) as db:
        for sql in ("DELETE FROM events", "UPDATE events SET severity='INFO'"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(sql)
    assert store.count() == 1


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_existing_output_is_never_overwritten(tmp_path, operation):
    store = seeded(tmp_path)
    report = backup.create_backup(store.path, tmp_path / "snapshot.db")
    occupied = tmp_path / "occupied.db"
    occupied.write_bytes(b"existing historical evidence")
    before = occupied.read_bytes()
    with pytest.raises(backup.RecoveryError, match="OUTPUT_EXISTS"):
        if operation == "backup":
            backup.create_backup(store.path, occupied)
        else:
            backup.restore_backup(tmp_path / "snapshot.db", occupied, expected_digest=report["logical_sha256"])
    assert occupied.read_bytes() == before


def test_wrong_digest_never_publishes_restore_or_changes_evidence(tmp_path):
    store = seeded(tmp_path)
    backup.create_backup(store.path, tmp_path / "snapshot.db")
    before = dump(store.path)
    with pytest.raises(backup.RecoveryError, match="DIGEST_MISMATCH"):
        backup.restore_backup(tmp_path / "snapshot.db", tmp_path / "restored.db", expected_digest="f"*64)
    assert not (tmp_path / "restored.db").exists()
    assert before == dump(store.path)


def test_interrupted_copy_does_not_publish_or_erase_source(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    before = dump(store.path)

    def fail(source, target, **kwargs):
        source.backup(target)
        raise OSError("injected disk failure, token=do-not-export")

    monkeypatch.setattr(backup, "_copy_pages", fail)
    with pytest.raises(backup.RecoveryError, match="SNAPSHOT_FAILED") as error:
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert "token" not in str(error.value)
    assert not (tmp_path / "snapshot.db").exists()
    assert not list(tmp_path.glob(".guardian-snapshot-*"))
    assert before == dump(store.path)


def test_missing_source_is_not_created_and_trading_ledger_is_rejected(tmp_path):
    with pytest.raises(backup.RecoveryError, match="SOURCE_UNAVAILABLE"):
        backup.create_backup(tmp_path / "missing.db", tmp_path / "snapshot.db")
    assert not (tmp_path / "missing.db").exists()
    trading = tmp_path / "paper.db"
    with closing(sqlite3.connect(trading)) as db:
        db.execute("CREATE TABLE v2_orders(id TEXT PRIMARY KEY)")
        db.commit()
    trading.chmod(0o600)
    before = dump(trading)
    with pytest.raises(backup.RecoveryError, match="NOT_GUARDIAN_STORE"):
        backup.create_backup(trading, tmp_path / "snapshot.db")
    assert before == dump(trading)
    assert not (tmp_path / "snapshot.db").exists()


def test_all_derived_tables_and_three_checkpoint_kinds_are_preserved(tmp_path):
    from tradexa.guardian.service import GuardianService
    store = seeded(tmp_path)
    service = GuardianService(store, source_keys={"fixture": "f"*32}, read_key="r"*32,
                              required_components=("guardian",))
    service.incidents.scan()
    service.notifications.scan()
    service.reports.generate("DAILY", datetime(2026, 10, 6, tzinfo=timezone.utc),
                             now=datetime(2026, 10, 7, 12, tzinfo=timezone.utc))
    with closing(store._connect()) as db:
        db.execute("INSERT INTO observer_snapshot_state VALUES(?,?,?,?)", ("snapshot", "digest", "execution_00000001", "2026-10-07T12:00:00Z"))
        db.execute("INSERT INTO observer_scan_state VALUES(?,?,?)", ("scan", '{"cycle":1}', "2026-10-07T12:00:00Z"))
        for table, values in (
                ("guardian_research_hypotheses", ("h", "2026-10-07", "UNPROVEN", '{"identity":"h"}')),
                ("guardian_research_results", ("r", "h", "BACKTEST", "2026-10-07", '{"verified":false}')),
                ("guardian_research_reviews", ("v", "h", "digest", "REJECT", "2026-10-07", '{"decision":"REJECT"}'))):
            db.execute(f"INSERT INTO {table} VALUES({','.join('?' for _ in values)})", values)
        db.commit()
    before = dump(store.path)
    report = backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert dump(tmp_path / "snapshot.db") == before == dump(store.path)
    for name in ("observer_cursors", "observer_snapshot_state", "observer_scan_state", "guardian_reports",
                 "guardian_research_hypotheses", "guardian_research_results", "guardian_research_reviews"):
        assert report["table_rows"][name] == 1
    assert set(report["table_rows"]) == backup._TABLES


def test_uncommitted_wal_transaction_is_excluded(tmp_path):
    store = seeded(tmp_path)
    with closing(store._connect()) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE observer_cursors SET source_sequence=999")
        report = backup.create_backup(store.path, tmp_path / "snapshot.db")
        with closing(sqlite3.connect(tmp_path / "snapshot.db")) as copied:
            assert copied.execute("SELECT source_sequence FROM observer_cursors").fetchone()[0] == 1
        writer.rollback()
    assert report["table_rows"]["events"] == 1


def test_exclusive_lock_is_sanitized_and_retry_after_release_works(tmp_path):
    store = seeded(tmp_path)
    with closing(store._connect()) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN EXCLUSIVE")
        with pytest.raises(backup.RecoveryError, match="SNAPSHOT_FAILED") as error:
            backup.create_backup(store.path, tmp_path / "snapshot.db")
        assert str(store.path) not in str(error.value)
        assert not (tmp_path / "snapshot.db").exists()
        db.rollback()
    assert backup.create_backup(store.path, tmp_path / "snapshot.db")["table_rows"]["events"] == 1


@pytest.mark.parametrize("where", ["copy", "validation", "file_sync", "publish"])
def test_failure_at_each_boundary_leaves_no_public_backup(tmp_path, monkeypatch, where):
    store = seeded(tmp_path)
    before = dump(store.path)
    if where == "copy":
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        monkeypatch.setattr(backup, "_copy_pages", interrupted)
        error = KeyboardInterrupt
    elif where == "validation":
        original, calls = backup._inspect, []
        def corrupt_validation(db, **kwargs):
            report = original(db, **kwargs)
            calls.append(1)
            if len(calls) == 2:
                report["logical_sha256"] = "f"*64
            return report
        monkeypatch.setattr(backup, "_inspect", corrupt_validation)
        error = backup.RecoveryError
    else:
        def fail(*args, **kwargs):
            raise OSError("private source path token=secret")
        monkeypatch.setattr(backup.os, "fsync" if where == "file_sync" else "link", fail)
        error = backup.RecoveryError
    with pytest.raises(error):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()
    assert not list(tmp_path.glob(".guardian-snapshot-*"))
    assert dump(store.path) == before


def test_directory_sync_failure_retains_published_snapshot_and_reports_uncertain(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    sync, calls = backup.os.fsync, []
    def fail_second(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("disk error")
        sync(fd)
    monkeypatch.setattr(backup.os, "fsync", fail_second)
    with pytest.raises(backup.RecoveryError, match="SNAPSHOT_PUBLICATION_UNCERTAIN"):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert (tmp_path / "snapshot.db").exists()
    assert backup.verify_backup(tmp_path / "snapshot.db")["table_rows"]["events"] == 1
    assert dump(store.path) == dump(tmp_path / "snapshot.db")


def test_output_race_cannot_overwrite_existing_evidence(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    link = backup.os.link
    def race(source, output, **kwargs):
        output.write_bytes(b"another completed backup")
        link(source, output, **kwargs)
    monkeypatch.setattr(backup.os, "link", race)
    with pytest.raises(backup.RecoveryError, match="OUTPUT_EXISTS"):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert (tmp_path / "snapshot.db").read_bytes() == b"another completed backup"
    assert not list(tmp_path.glob(".guardian-snapshot-*"))


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "readable", "directory"])
def test_unsafe_source_is_rejected_without_following_or_chmod(tmp_path, kind):
    store = seeded(tmp_path)
    path = tmp_path / "unsafe.db"
    if kind == "symlink":
        path.symlink_to(store.path)
    elif kind == "hardlink":
        os.link(store.path, path)
    elif kind == "readable":
        store.path.chmod(0o644)
        path = store.path
    else:
        path.mkdir()
    mode = store.path.stat().st_mode
    with pytest.raises(backup.RecoveryError, match="UNSAFE_SOURCE_FILE"):
        backup.create_backup(path, tmp_path / "snapshot.db")
    assert store.path.stat().st_mode == mode
    assert not (tmp_path / "snapshot.db").exists()


@pytest.mark.parametrize("dangling", [True, False])
def test_output_symlinks_are_not_followed(tmp_path, dangling):
    store = seeded(tmp_path)
    link = tmp_path / "snapshot.db"
    target = tmp_path / "not-here.db" if dangling else store.path
    link.symlink_to(target)
    before = dump(store.path)
    with pytest.raises(backup.RecoveryError, match="OUTPUT_EXISTS"):
        backup.create_backup(store.path, link)
    assert link.is_symlink()
    assert dump(store.path) == before
    if dangling:
        assert not target.exists()


def test_destination_directory_must_be_existing_private_and_owner_controlled(tmp_path):
    store = seeded(tmp_path)
    missing = tmp_path / "missing"
    with pytest.raises(backup.RecoveryError, match="OUTPUT_DIRECTORY_UNAVAILABLE"):
        backup.create_backup(store.path, missing / "snapshot.db")
    assert not missing.exists()
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(backup.RecoveryError, match="UNSAFE_OUTPUT_DIRECTORY"):
        backup.create_backup(store.path, public / "snapshot.db")
    assert not (public / "snapshot.db").exists()


def test_insufficient_disk_space_fails_before_copy(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: SimpleNamespace(free=0))
    with pytest.raises(backup.RecoveryError, match="INSUFFICIENT_SNAPSHOT_SPACE"):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()
    assert not list(tmp_path.glob(".guardian-snapshot-*"))


def test_disk_space_exhaustion_during_copy_fails_without_publication(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    calls = []
    def space(p):
        calls.append(1)
        return SimpleNamespace(free=1024**3 if len(calls) == 1 else 0)
    monkeypatch.setattr(backup.shutil, "disk_usage", space)
    with pytest.raises(backup.RecoveryError, match="INSUFFICIENT_SNAPSHOT_SPACE"):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()
    assert not list(tmp_path.glob(".guardian-snapshot-*"))


def test_deadline_during_copy_is_enforced_and_partial_cleaned(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    original = backup._copy_pages
    def timed_out(source, target, **kwargs):
        monkeypatch.setattr(backup, "monotonic", lambda: kwargs["deadline"]+1)
        original(source, target, **kwargs)
    monkeypatch.setattr(backup, "_copy_pages", timed_out)
    with pytest.raises(backup.RecoveryError, match="SNAPSHOT_DEADLINE_EXCEEDED"):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()
    assert not list(tmp_path.glob(".guardian-snapshot-*"))


@pytest.mark.parametrize("timeout,size", [(0, 4096), (301, 4096), (float("nan"), 4096),
    (float("inf"), 4096), (True, 4096), (30, True), (30, 0), (30, 65*1024**3)])
def test_invalid_limits_are_rejected_before_io(tmp_path, timeout, size):
    with pytest.raises(backup.RecoveryError, match="INVALID_SNAPSHOT_LIMITS"):
        backup.create_backup(tmp_path / "missing.db", tmp_path / "snapshot.db", timeout_seconds=timeout, max_bytes=size)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("digest", [None, "", "F"*64, "a"*63, 1, True, "../path"])
def test_restore_requires_explicit_valid_expected_digest_before_io(tmp_path, digest):
    with pytest.raises(backup.RecoveryError, match="INVALID_EXPECTED_DIGEST"):
        backup.restore_backup(tmp_path / "missing.db", tmp_path / "restored.db", expected_digest=digest)
    assert not list(tmp_path.iterdir())


def test_corrupt_and_tampered_backup_cannot_restore_as_validated(tmp_path):
    store = seeded(tmp_path)
    report = backup.create_backup(store.path, tmp_path / "snapshot.db")
    with closing(sqlite3.connect(tmp_path / "snapshot.db")) as db:
        db.execute("UPDATE heartbeats SET state='HEALTHY'")
        db.commit()
    with pytest.raises(backup.RecoveryError, match="DIGEST_MISMATCH"):
        backup.restore_backup(tmp_path / "snapshot.db", tmp_path / "restored.db", expected_digest=report["logical_sha256"])
    assert not (tmp_path / "restored.db").exists()
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not SQLite: token=secret")
    corrupt.chmod(0o600)
    with pytest.raises(backup.RecoveryError, match="SNAPSHOT_VERIFICATION_FAILED") as error:
        backup.verify_backup(corrupt)
    assert "secret" not in str(error.value)


def test_missing_or_fake_immutability_guards_are_not_certified(tmp_path):
    store = seeded(tmp_path)
    with closing(store._connect()) as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT 1; END")
        db.commit()
    with pytest.raises(backup.RecoveryError, match="IMMUTABILITY_GUARD_MISSING"):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()


@pytest.mark.parametrize("kind", ["oversize", "foreign_table", "missing_column"])
def test_unbounded_or_wrong_source_schema_is_not_silently_truncated(tmp_path, kind):
    store = seeded(tmp_path)
    with closing(store._connect()) as db:
        if kind == "oversize":
            db.execute("UPDATE heartbeats SET reason=?", ("x"*(backup.MAX_CELL_BYTES+1),))
        elif kind == "foreign_table":
            db.execute("CREATE TABLE v2_positions(id TEXT)")
        else:
            db.execute("ALTER TABLE heartbeats ADD COLUMN unexpected TEXT")
        db.commit()
    code = "SNAPSHOT_CELL_BOUND_EXCEEDED" if kind == "oversize" else "NOT_GUARDIAN_STORE"
    with pytest.raises(backup.RecoveryError, match=code):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()


def test_size_bound_fails_closed_without_partial_snapshot(tmp_path):
    store = seeded(tmp_path)
    with pytest.raises(backup.RecoveryError, match="SNAPSHOT_SIZE_EXCEEDED"):
        backup.create_backup(store.path, tmp_path / "snapshot.db", max_bytes=1024)
    assert not (tmp_path / "snapshot.db").exists()


def test_cli_returns_structured_receipts_and_sanitized_failures(tmp_path, capsys):
    store = seeded(tmp_path)
    path = tmp_path / "snapshot.db"
    assert backup.main(["backup", "--source", str(store.path), "--output", str(path)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["operation"] == "BACKUP"
    assert backup.main(["verify", "--source", str(path), "--expected-digest", receipt["logical_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "VERIFY"
    assert backup.main(["restore", "--source", str(path), "--output", str(tmp_path / "restored.db"), "--expected-digest", receipt["logical_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["operation"] == "RESTORE_TO_NEW_FILE"
    assert backup.main(["backup", "--source", str(store.path), "--output", str(path)]) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure == {"state": "UNVERIFIED", "code": "OUTPUT_EXISTS", "service_cutover_performed": False}
    assert str(tmp_path) not in json.dumps(receipt)


def test_tool_never_imports_trading_workers_or_constructs_source_stores():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c", "import sys; import tradexa.guardian.backup; "
        "assert not any(m.split('.')[0] in {'services','execution','bot','webhook_api','app'} for m in sys.modules)"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_no_store_constructor_migration_or_source_write_runs(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    before = dump(store.path)
    def forbidden(*args, **kwargs):
        raise AssertionError("store construction would migrate the source")
    monkeypatch.setattr(GuardianStore, "__init__", forbidden)
    backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert dump(store.path) == before


def test_real_mid_copy_wal_commit_is_excluded_from_pinned_snapshot(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    with closing(store._connect()) as writer:
        writer.execute("BEGIN IMMEDIATE")
        for number in range(2, 1002):
            store._append_in_transaction(writer, event(number), "2026-10-07T12:00:00Z")
        writer.commit()
    writes = []
    def copy_one_page_at_a_time(source, target, **kwargs):
        def during_copy(status, remaining, total):
            assert total > 1
            if remaining and not writes:
                store.append_observed_page("smc_intent_history", expected=(1, "anchor1"),
                                          next_cursor=(2, "anchor2"), events=[event(1002)])
                writes.append(1)
        source.backup(target, pages=1, progress=during_copy, sleep=.01)
    monkeypatch.setattr(backup, "_copy_pages", copy_one_page_at_a_time)
    report = backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert writes == [1]
    assert store.count() == 1002
    assert report["table_rows"]["events"] == 1001
    with closing(sqlite3.connect(tmp_path / "snapshot.db")) as db:
        assert db.execute("SELECT source_sequence,anchor_id FROM observer_cursors").fetchone() == (1, "anchor1")
        assert db.execute("SELECT COUNT(*) FROM events WHERE event_id='execution_00001002'").fetchone()[0] == 0


def test_restored_checkpoint_resumes_once_and_repeated_recovery_does_not_duplicate(tmp_path):
    store = seeded(tmp_path)
    first = backup.create_backup(store.path, tmp_path / "snapshot.db")
    backup.restore_backup(tmp_path / "snapshot.db", tmp_path / "restored.db", expected_digest=first["logical_sha256"])
    restored = GuardianStore(tmp_path / "restored.db")
    assert restored.append_observed_page("smc_intent_history", expected=restored.observer_cursor("smc_intent_history"),
                                        next_cursor=(2, "anchor2"), events=[event(2)]) == 1
    assert restored.append(event(2)) is False
    second = backup.create_backup(restored.path, tmp_path / "second.db")
    backup.restore_backup(tmp_path / "second.db", tmp_path / "third.db", expected_digest=second["logical_sha256"])
    reopened = GuardianStore(tmp_path / "third.db")
    assert reopened.count() == 2
    assert reopened.observer_cursor("smc_intent_history") == (2, "anchor2")
    assert reopened.append(event(2)) is False
    assert store.count() == 1


def test_unrelated_paper_orders_positions_and_journal_files_are_untouched(tmp_path):
    store = seeded(tmp_path)
    sources = []
    for name in ("smc-paper.db", "pa-paper.db", "agent-journal.db", "instance-ledger.db"):
        path = tmp_path / name
        with closing(sqlite3.connect(path)) as db:
            for table in ("orders", "positions", "journal", "execution_intents"):
                db.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
                db.execute(f"INSERT INTO {table} VALUES('original')")
            db.commit()
        sources.append((path, dump(path), path.read_bytes()))
    receipt = backup.create_backup(store.path, tmp_path / "snapshot.db")
    backup.restore_backup(tmp_path / "snapshot.db", tmp_path / "restored.db", expected_digest=receipt["logical_sha256"])
    for path, before, contents in sources:
        assert dump(path) == before
        assert path.read_bytes() == contents


def test_foreign_key_corruption_is_not_published(tmp_path):
    from tradexa.guardian.incidents import GuardianIncidentEngine
    store = seeded(tmp_path)
    GuardianIncidentEngine(store)
    with closing(store._connect()) as db:
        db.execute("INSERT INTO guardian_incident_updates(incident_id,event_id,observed_at,transition,summary) VALUES('missing','event','now','OPEN','fixture')")
        db.commit()
    with pytest.raises(backup.RecoveryError, match="SNAPSHOT_INTEGRITY_FAILED"):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()


def test_cleanup_failure_is_truthful_and_preserves_partial_and_source(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    before = dump(store.path)
    from pathlib import Path
    original = Path.unlink
    def fail_unlink(path, **kwargs):
        if path.name.startswith(".guardian-snapshot-"):
            raise PermissionError("private/path")
        return original(path, **kwargs)
    def fail_copy(*args, **kwargs):
        raise OSError("disk-full")
    monkeypatch.setattr(backup, "_copy_pages", fail_copy)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(backup.RecoveryError, match="SNAPSHOT_PARTIAL_RETAINED"):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()
    assert len(list(tmp_path.glob(".guardian-snapshot-*"))) == 1
    assert dump(store.path) == before


@pytest.mark.parametrize("arguments", [["backup"], ["restore"], ["verify", "--output", "not-allowed"]])
def test_cli_invalid_operation_contract_does_not_write(tmp_path, capsys, arguments):
    store = seeded(tmp_path)
    assert backup.main([*arguments, "--source", str(store.path)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "INVALID_ARGUMENTS"
    assert set(p.name for p in tmp_path.iterdir()) <= {"guardian.db", "guardian.db-wal", "guardian.db-shm"}


def test_verify_is_read_only_and_rejects_live_wal_not_standalone(tmp_path):
    store = seeded(tmp_path)
    before = dump(store.path)
    with pytest.raises(backup.RecoveryError, match="NOT_STANDALONE_SNAPSHOT"):
        backup.verify_backup(store.path)
    assert before == dump(store.path)
    report = backup.create_backup(store.path, tmp_path / "snapshot.db")
    contents = (tmp_path / "snapshot.db").read_bytes()
    assert backup.verify_backup(tmp_path / "snapshot.db")["logical_sha256"] == report["logical_sha256"]
    assert (tmp_path / "snapshot.db").read_bytes() == contents


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_snapshot_cannot_be_published_as_a_sqlite_sidecar(tmp_path, suffix):
    store = seeded(tmp_path)
    before = dump(store.path)
    with pytest.raises(backup.RecoveryError, match="UNSAFE_OUTPUT_NAME"):
        backup.create_backup(store.path, tmp_path / ("guardian.db"+suffix))
    assert dump(store.path) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_existing_output_sidecar_is_preserved_not_overwritten_or_ignored(tmp_path, suffix):
    store = seeded(tmp_path)
    sidecar = tmp_path / ("snapshot.db"+suffix)
    sidecar.write_bytes(b"retained pending history")
    with pytest.raises(backup.RecoveryError, match="OUTPUT_SIDECAR_EXISTS"):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()
    assert sidecar.read_bytes() == b"retained pending history"


@pytest.mark.parametrize("operation", ["verify", "restore"])
def test_backup_with_an_existing_sidecar_is_not_certified_standalone(tmp_path, operation):
    store = seeded(tmp_path)
    report = backup.create_backup(store.path, tmp_path / "snapshot.db")
    sidecar = tmp_path / "snapshot.db-journal"
    sidecar.write_bytes(b"do not discard")
    with pytest.raises(backup.RecoveryError, match="NOT_STANDALONE_SNAPSHOT"):
        if operation == "verify":
            backup.verify_backup(tmp_path / "snapshot.db")
        else:
            backup.restore_backup(tmp_path / "snapshot.db", tmp_path / "restored.db", expected_digest=report["logical_sha256"])
    assert sidecar.read_bytes() == b"do not discard"
    assert not (tmp_path / "restored.db").exists()


def test_sqlite_progress_interruption_reports_deadline_not_an_arbitrary_error(tmp_path, monkeypatch):
    store = seeded(tmp_path)
    def timeout(db, *, deadline, **kwargs):
        monkeypatch.setattr(backup, "monotonic", lambda: deadline+1)
        raise sqlite3.OperationalError("interrupted: private path")
    monkeypatch.setattr(backup, "_inspect", timeout)
    with pytest.raises(backup.RecoveryError, match="SNAPSHOT_DEADLINE_EXCEEDED"):
        backup.create_backup(store.path, tmp_path / "snapshot.db")
    assert not (tmp_path / "snapshot.db").exists()
