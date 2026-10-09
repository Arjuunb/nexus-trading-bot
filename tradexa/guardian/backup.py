"""Owner-run, bounded Guardian snapshots. No trading DB, migrations or overwrite.

Use SQLite's online backup API, not a copy of a live main/WAL file. Restore
only to a NEW, separate file; choosing a service cutover remains an owner task.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

DEFAULT_MAX_BYTES = 4 * 1024**3
MAX_CELL_BYTES = 2 * 1024**2
RESERVE_BYTES = 64 * 1024**2
_SIDECARS = ("-wal", "-shm", "-journal")
_CORE = {
    "events": {"sequence", "event_id", "timestamp", "received_at", "source_service",
               "source_component", "event_type", "severity", "payload_json"},
    "heartbeats": {"component", "state", "reason", "observed_at"},
    "observer_cursors": {"component", "source_sequence", "anchor_id", "updated_at"},
    "observer_snapshot_state": {"component", "material_digest", "event_id", "updated_at"},
    "observer_scan_state": {"component", "cursor_json", "updated_at"},
}
_TABLES = {*_CORE, "sqlite_sequence", "guardian_incidents", "guardian_incident_updates",
           "guardian_analysis_cursor", "guardian_notifications", "guardian_notification_cursor",
           "guardian_reports", "guardian_research_hypotheses", "guardian_research_results",
           "guardian_research_reviews"}


class RecoveryError(RuntimeError):
    """Sanitized codes only: never expose saved evidence or SQLite error text."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _limits(timeout_seconds, max_bytes):
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not .01 <= timeout_seconds <= 300 or type(max_bytes) is not int
            or not 1024 <= max_bytes <= 64 * 1024**3):
        raise RecoveryError("INVALID_SNAPSHOT_LIMITS")
    return monotonic() + timeout_seconds


def _check(deadline):
    if monotonic() >= deadline:
        raise RecoveryError("SNAPSHOT_DEADLINE_EXCEEDED")


def _digest_argument(value, *, required=False):
    if (required and value is None) or (value is not None and
            (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value))):
        raise RecoveryError("INVALID_EXPECTED_DIGEST")


def _source(path):
    path = Path(path).absolute()
    try:
        info = path.lstat()
    except OSError as exc:
        raise RecoveryError("SOURCE_UNAVAILABLE") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            info.st_mode & 0o077 or info.st_uid != os.getuid()):
        raise RecoveryError("UNSAFE_SOURCE_FILE")
    # A private file in another user's writable directory is not safe to open.
    parent = path.parent.resolve()
    if parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o022:
        raise RecoveryError("UNSAFE_SOURCE_DIRECTORY")
    return parent / path.name


def _output(path):
    path = Path(path).absolute()
    if path.name.endswith(_SIDECARS) or path.name.startswith(".guardian-snapshot-"):
        raise RecoveryError("UNSAFE_OUTPUT_NAME")
    if os.path.lexists(path):
        raise RecoveryError("OUTPUT_EXISTS")
    if any(os.path.lexists(str(path)+suffix) for suffix in _SIDECARS):
        raise RecoveryError("OUTPUT_SIDECAR_EXISTS")
    try:
        parent = path.parent.resolve(strict=True)
        info = parent.stat()
    except OSError as exc:
        raise RecoveryError("OUTPUT_DIRECTORY_UNAVAILABLE") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RecoveryError("UNSAFE_OUTPUT_DIRECTORY")
    return parent / path.name


def _connect_read(path, deadline):
    db = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=.25)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        db.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
        db.execute("BEGIN")
    except BaseException:
        db.close()
        raise
    return db


def _encoded(value):
    # Preserve SQLite types: 1, 1.0, text "1" and blob b"1" must not collide.
    if value is None:
        return ["null"]
    if type(value) is int:
        return ["integer", value]
    if type(value) is float:
        return ["real", value.hex()]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    return ["text", value]


def _inspect(db, *, deadline, max_bytes):
    _check(deadline)
    schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name").fetchmany(257)
    tables = {r[1] for r in schema if r[0] == "table"}
    if (len(schema) > 256 or not set(_CORE) <= tables or not tables <= _TABLES
            or any(r[0] == "view" or (r[3] and len(r[3].encode()) > 65536) for r in schema)):
        raise RecoveryError("NOT_GUARDIAN_STORE")
    for name, columns in _CORE.items():
        if {r[1] for r in db.execute(f'PRAGMA table_info("{name}")')} != columns:
            raise RecoveryError("NOT_GUARDIAN_STORE")
    for action in ("update", "delete"):
        expected = (f"CREATE TRIGGER events_no_{action} BEFORE {action.upper()} ON events BEGIN "
                    "SELECT RAISE(ABORT, 'Guardian evidence is immutable'); END;")
        matches = [r for r in schema if r[0] == "trigger" and r[1] == f"events_no_{action}"]
        if len(matches) != 1 or " ".join(matches[0][3].split()).rstrip(";") != expected.rstrip(";"):
            raise RecoveryError("IMMUTABILITY_GUARD_MISSING")
    size = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
    if size > max_bytes:
        raise RecoveryError("SNAPSHOT_SIZE_EXCEEDED")
    if db.execute("PRAGMA integrity_check(1)").fetchall() != [("ok",)]:
        raise RecoveryError("SNAPSHOT_INTEGRITY_FAILED")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RecoveryError("SNAPSHOT_INTEGRITY_FAILED")
    schema_json = json.dumps(schema, ensure_ascii=True, separators=(",", ":")).encode()
    logical = hashlib.sha256(schema_json)
    counts, scanned_bytes = {}, len(schema_json)
    for name in sorted(tables):
        _check(deadline)
        columns = [r[1] for r in db.execute(f'PRAGMA table_info("{name}")')]
        if not columns or len(columns) > 64 or any(not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*", c) for c in columns):
            raise RecoveryError("NOT_GUARDIAN_STORE")
        logical.update(json.dumps([name, columns], separators=(",", ":")).encode())
        # Bound each SQLite value BEFORE Python materialization. Oversize is
        # an error, never omission/truncation of an allegedly complete backup.
        too_large = " OR ".join(f'length(CAST("{c}" AS BLOB))>{MAX_CELL_BYTES}' for c in columns)
        selected = ",".join(f'CASE WHEN length(CAST("{c}" AS BLOB))>{MAX_CELL_BYTES} THEN NULL ELSE "{c}" END' for c in columns)
        rows = db.execute(f'SELECT rowid,{selected},CASE WHEN {too_large} THEN 1 ELSE 0 END FROM "{name}" ORDER BY rowid')
        count = 0
        for row in rows:
            _check(deadline)
            if row[-1]:
                raise RecoveryError("SNAPSHOT_CELL_BOUND_EXCEEDED")
            raw = json.dumps([_encoded(v) for v in row[:-1]], ensure_ascii=True, separators=(",", ":")).encode()
            scanned_bytes += len(raw)
            if scanned_bytes > max_bytes:
                raise RecoveryError("SNAPSHOT_SIZE_EXCEEDED")
            logical.update(len(raw).to_bytes(8, "big"))
            logical.update(raw)
            count += 1
        counts[name] = count
    return {"logical_sha256": logical.hexdigest(), "schema_sha256": hashlib.sha256(schema_json).hexdigest(),
            "table_rows": counts, "database_bytes": size}


def _copy_pages(source, target, *, deadline, max_bytes, output_directory):
    page_size = source.execute("PRAGMA page_size").fetchone()[0]
    def progress(status, remaining, total):
        _check(deadline)
        if total * page_size > max_bytes:
            raise RecoveryError("SNAPSHOT_SIZE_EXCEEDED")
        if shutil.disk_usage(output_directory).free < RESERVE_BYTES:
            raise RecoveryError("INSUFFICIENT_SNAPSHOT_SPACE")
    source.backup(target, pages=128, progress=progress, sleep=.01)


def _file_digest(path, deadline):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            _check(deadline)
            chunk = handle.read(1024**2)
            if not chunk:
                return result.hexdigest()
            result.update(chunk)


def _report(details, *, file_digest, operation):
    return {**details, "file_sha256": file_digest, "operation": operation,
            "state": "CONSISTENT_GUARDIAN_SNAPSHOT", "scope": "GUARDIAN_OWNED_DATABASE_ONLY",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "trading_integrity_verified": False, "external_authenticity_verified": False,
            "service_cutover_performed": False}


def verify_backup(path, *, expected_digest=None, timeout_seconds=30, max_bytes=DEFAULT_MAX_BYTES):
    """Read-only structural/integrity verification, optionally bound to a known digest."""
    _digest_argument(expected_digest)
    deadline = _limits(timeout_seconds, max_bytes)
    try:
        path = _source(path)
        if any(os.path.lexists(str(path)+suffix) for suffix in _SIDECARS):
            raise RecoveryError("NOT_STANDALONE_SNAPSHOT")
        with closing(_connect_read(path, deadline)) as db:
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise RecoveryError("NOT_STANDALONE_SNAPSHOT")
            details = _inspect(db, deadline=deadline, max_bytes=max_bytes)
            if expected_digest is not None and details["logical_sha256"] != expected_digest:
                raise RecoveryError("DIGEST_MISMATCH")
            file_digest = _file_digest(path, deadline)
        return _report(details, file_digest=file_digest, operation="VERIFY")
    except (sqlite3.Error, OSError, ValueError, UnicodeError, OverflowError) as exc:
        code = "SNAPSHOT_DEADLINE_EXCEEDED" if monotonic() >= deadline else "SNAPSHOT_VERIFICATION_FAILED"
        raise RecoveryError(code) from exc


def _snapshot(source_path, output_path, *, expected_digest, timeout_seconds, max_bytes, restore):
    _digest_argument(expected_digest, required=restore)
    deadline = _limits(timeout_seconds, max_bytes)
    temporary, published = None, False
    try:
        source_path, output_path = _source(source_path), _output(output_path)
        if restore and any(os.path.lexists(str(source_path)+suffix) for suffix in _SIDECARS):
            raise RecoveryError("NOT_STANDALONE_SNAPSHOT")
        with closing(_connect_read(source_path, deadline)) as source:
            if restore and source.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise RecoveryError("NOT_STANDALONE_SNAPSHOT")
            # This read pins ONE committed source snapshot for schema, all raw
            # events, derived records and every checkpoint, despite WAL writes.
            original = _inspect(source, deadline=deadline, max_bytes=max_bytes)
            if expected_digest is not None and original["logical_sha256"] != expected_digest:
                raise RecoveryError("DIGEST_MISMATCH")
            if shutil.disk_usage(output_path.parent).free < original["database_bytes"] + RESERVE_BYTES:
                raise RecoveryError("INSUFFICIENT_SNAPSHOT_SPACE")
            descriptor, name = tempfile.mkstemp(prefix=".guardian-snapshot-", suffix=".partial", dir=output_path.parent)
            os.close(descriptor)
            temporary = Path(name)
            with closing(sqlite3.connect(temporary, timeout=.25)) as target:
                target.execute("PRAGMA synchronous=FULL")
                target.execute("PRAGMA journal_mode=DELETE")
                _copy_pages(source, target, deadline=deadline, max_bytes=max_bytes, output_directory=output_path.parent)
                # backup() copies the source WAL header too. Change ONLY the
                # new private copy, before validation/publication, into a
                # self-contained file with no required WAL/SHM sidecar.
                if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
                    raise RecoveryError("NOT_STANDALONE_SNAPSHOT")
                target.execute("PRAGMA query_only=ON")
                target.execute("PRAGMA busy_timeout=250")
                target.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
                target.execute("BEGIN")
                copied = _inspect(target, deadline=deadline, max_bytes=max_bytes)
                if copied != original:
                    raise RecoveryError("SNAPSHOT_CONTENT_MISMATCH")
        file_digest = _file_digest(temporary, deadline)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        _check(deadline)
        # Atomic create-if-absent, NOT os.replace(): even a racing output or
        # dangling symlink cannot cause an existing file to be overwritten.
        try:
            os.link(temporary, output_path, follow_symlinks=False)
        except FileExistsError as exc:
            raise RecoveryError("OUTPUT_EXISTS") from exc
        published = True
        temporary.unlink()
        temporary = None
        directory_fd = os.open(output_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return _report(copied, file_digest=file_digest, operation="RESTORE_TO_NEW_FILE" if restore else "BACKUP")
    except (sqlite3.Error, OSError, ValueError, UnicodeError, OverflowError) as exc:
        code = ("SNAPSHOT_PUBLICATION_UNCERTAIN" if published else
                "SNAPSHOT_DEADLINE_EXCEEDED" if monotonic() >= deadline else "SNAPSHOT_FAILED")
        raise RecoveryError(code) from exc
    finally:
        # Remove only this invocation's unpublished, generated artifact. Never
        # clean source/WAL/history, a published snapshot, or another run's file.
        if temporary is not None and not published:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise RecoveryError("SNAPSHOT_PARTIAL_RETAINED") from exc


def create_backup(source, output, *, timeout_seconds=30, max_bytes=DEFAULT_MAX_BYTES):
    return _snapshot(source, output, expected_digest=None, timeout_seconds=timeout_seconds,
                     max_bytes=max_bytes, restore=False)


def restore_backup(source, output, *, expected_digest, timeout_seconds=30, max_bytes=DEFAULT_MAX_BYTES):
    return _snapshot(source, output, expected_digest=expected_digest, timeout_seconds=timeout_seconds,
                     max_bytes=max_bytes, restore=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "verify", "restore"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--output")
    parser.add_argument("--expected-digest")
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)
    try:
        limits = dict(timeout_seconds=args.timeout_seconds, max_bytes=args.max_bytes)
        if args.operation == "verify":
            if args.output:
                raise RecoveryError("INVALID_ARGUMENTS")
            report = verify_backup(args.source, expected_digest=args.expected_digest, **limits)
        else:
            if not args.output or (args.operation == "backup" and args.expected_digest):
                raise RecoveryError("INVALID_ARGUMENTS")
            report = (restore_backup(args.source, args.output, expected_digest=args.expected_digest, **limits)
                      if args.operation == "restore" else create_backup(args.source, args.output, **limits))
    except RecoveryError as exc:
        print(json.dumps({"state": "UNVERIFIED", "code": exc.code, "service_cutover_performed": False}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
