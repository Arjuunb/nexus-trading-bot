"""Bounded, read-only Guardian diagnostics, not trading or host certification.

No persistence constructors, checkpoint, cleanup, write test or network call.
Filesystem observations are not atomic with the SQLite heartbeat snapshot.
"""
from __future__ import annotations

import os
import re
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Sequence

from .health import component_health

CRITICAL_HEADROOM_BYTES = 64 * 1024**2
WARNING_HEADROOM_BYTES = 256 * 1024**2
WARNING_WAL_BYTES = 256 * 1024**2
MAX_MONITORS = 128
_NAME = re.compile(r"guardian(?:_[a-z0-9_]+)?\Z")
_COMPONENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
# A format check alone could leak a short all-uppercase saved credential.
# Only documented local monitor codes are exposed; arbitrary prose is hidden.
_REASONS = frozenset({
    "MONITOR_FAILED", "INCIDENT_ANALYSIS_FAILED", "NOTIFICATION_ANALYSIS_FAILED",
    "REPORT_GENERATION_FAILED", "PUBLIC_STATUS_PROBE_FAILED", "LAB_OBSERVATION_FAILED",
    "LAB_FEED_OBSERVATION_FAILED", "LAB_BACKFILL_FAILED", "LAB_LIFECYCLE_IMPORT_FAILED",
    "SMC_EXECUTION_OBSERVATION_FAILED", "INSTANCE_DECISION_IMPORT_FAILED",
    "INSTANCE_LEDGER_OBSERVATION_FAILED", "LAB_EXECUTION_EVIDENCE_UNAVAILABLE",
    "SMC_EXECUTION_EVIDENCE_UNAVAILABLE", "FILL_HISTORY_UNAVAILABLE", "INTENT_HISTORY_UNAVAILABLE",
    "JOURNAL_HISTORY_UNAVAILABLE", "FILL_POSITION_SOURCE_UNAVAILABLE", "EXIT_FILL_SOURCE_UNAVAILABLE",
    "STOP_HISTORY_SOURCE_UNAVAILABLE", "BACKFILL_IN_PROGRESS", "LIFECYCLE_IMPORT_IN_PROGRESS",
    "INSTANCE_DECISION_IMPORT_IN_PROGRESS", "FILL_IMPORT_IN_PROGRESS", "INTENT_IMPORT_IN_PROGRESS",
    "JOURNAL_PASS_IN_PROGRESS", "JOURNAL_PASS_FINISHED", "FILL_POSITION_IMPORT_IN_PROGRESS",
    "EXIT_FILL_IMPORT_IN_PROGRESS", "STOP_HISTORY_IMPORT_IN_PROGRESS",
})
_PRIORITY = ("FAILED", "BLOCKED", "DEGRADED", "UNKNOWN", "HEALTHY")


class _UnsafePath(ValueError):
    pass


class _ReadDeadline(sqlite3.OperationalError):
    sqlite_errorcode = sqlite3.SQLITE_INTERRUPT


def _file_sizes(path: Path) -> dict:
    # Do not follow a swapped symlink into another ledger. No source paths are
    # accepted from HTTP, and no database or parent is created by this reader.
    for parent in path.parents:
        if parent.is_symlink():
            raise _UnsafePath()
    sizes = {}
    for suffix, name in (("", "database_bytes"), ("-wal", "wal_bytes"),
                         ("-shm", "shm_bytes"), ("-journal", "rollback_journal_bytes")):
        candidate = Path(str(path)+suffix)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if not suffix:
                raise
            sizes[name] = 0
            continue
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or
                metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid()):
            raise _UnsafePath()
        sizes[name] = _count(metadata.st_size)
    return sizes


def _count(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 2**63-1:
        raise ValueError("invalid metadata count")
    return value


def _filesystem(path: Path) -> dict:
    stats = os.statvfs(path.parent)
    block_size, blocks, available = map(_count, (stats.f_frsize, stats.f_blocks, stats.f_bavail))
    if not block_size or not blocks or available > blocks:
        raise ValueError("invalid filesystem geometry")
    files, available_files = map(_count, (stats.f_files, stats.f_favail))
    if available_files > files:
        raise ValueError("invalid inode geometry")
    return {
        "filesystem_available_bytes": _count(block_size*available),
        "filesystem_total_bytes": _count(block_size*blocks),
        "filesystem_available_inodes": available_files if files else None,
        "filesystem_read_only": bool(_count(stats.f_flag) & os.ST_RDONLY),
    }


def _read_monitors(path: Path, names: tuple[str, ...]) -> tuple[dict, str]:
    deadline = monotonic()+0.5
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=0.25)) as conn:
        if monotonic() > deadline:
            raise _ReadDeadline()
        conn.row_factory = sqlite3.Row
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=250")
        conn.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        conn.execute("BEGIN")
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        placeholders = ",".join("?" for _ in names)
        # Do not deserialize events, journals or cursor payloads for readiness.
        rows = conn.execute(
            "SELECT component, CASE WHEN typeof(state)='text' AND length(CAST(state AS BLOB))<=16 "
            "THEN state ELSE NULL END AS state, "
            "CASE WHEN typeof(reason)='text' AND length(CAST(reason AS BLOB))<=64 "
            "THEN reason ELSE 'REPORTED_REASON_REDACTED' END AS reason, "
            "CASE WHEN typeof(observed_at)='text' AND length(CAST(observed_at AS BLOB))<=64 "
            "THEN observed_at ELSE NULL END AS observed_at FROM heartbeats WHERE component IN ("+
            placeholders+") LIMIT ?", (*names, len(names)+1)).fetchall()
        if monotonic() > deadline:
            raise _ReadDeadline()
        if len(rows) > len(names) or len({row["component"] for row in rows}) != len(rows):
            raise sqlite3.DatabaseError("invalid heartbeat identity")
        result = {}
        for row in rows:
            value = dict(row)
            # SQLite substr(TEXT) stops at NUL: never clip an invalid value
            # into a valid state/timestamp or trust a parser's permissiveness.
            if any(isinstance(value[field], str) and "\x00" in value[field]
                   for field in ("state", "observed_at")):
                value["state"] = None
            reason = value["reason"]
            value["reason"] = (reason if isinstance(reason, str) and reason in _REASONS
                               else "REPORTED_REASON_REDACTED" if reason else "")
            result[value["component"]] = value
        conn.rollback()
    if journal_mode not in ("wal", "delete", "truncate", "persist", "memory", "off"):
        raise sqlite3.DatabaseError("invalid journal mode")
    return result, journal_mode


def _state(components: dict) -> str:
    return next((state for state in _PRIORITY if any(
        row["state"] == state for row in components.values())), "UNKNOWN")


def self_health(path: str | Path, required_components: Sequence[str], *,
                now: datetime | None = None) -> dict:
    """Observe only the configured Guardian monitors and its own DB filesystem.

    HEALTHY means fresh reported monitor heartbeats, a successful bounded read,
    and observed headroom. It does not certify a future write, disk integrity,
    trading availability or effective queue/worker execution.
    """
    if (not required_components or len(required_components) > MAX_MONITORS or
            any(not isinstance(name, str) or not _COMPONENT_NAME.fullmatch(name)
                for name in required_components) or
            len(set(required_components)) != len(required_components)):
        raise ValueError("invalid monitor configuration")
    names = tuple(name for name in required_components
                  if name == "guardian" or name.startswith("guardian_"))
    if ("guardian" not in names or "guardian_storage" in names or
            any(not _NAME.fullmatch(name) for name in names)):
        raise ValueError("invalid Guardian monitor names")
    moment = now or datetime.now(timezone.utc)
    # Validate the clock even if the storage read will fail.
    monitors = component_health({}, names, now=moment)
    path = Path(os.path.abspath(path))
    storage = {
        "state": "UNKNOWN", "reason": "STORAGE_METADATA_UNAVAILABLE",
        "database_readable": False, "journal_mode": None,
        "database_bytes": None, "wal_bytes": None, "shm_bytes": None,
        "rollback_journal_bytes": None, "filesystem_available_bytes": None,
        "filesystem_total_bytes": None, "filesystem_available_inodes": None,
        "filesystem_read_only": None, "write_durability_verified": False,
        "policy": {"critical_headroom_bytes": CRITICAL_HEADROOM_BYTES,
                   "warning_headroom_bytes": WARNING_HEADROOM_BYTES,
                   "warning_wal_bytes": WARNING_WAL_BYTES,
                   "warning_available_inodes": 16},
    }
    files_safe = False
    try:
        storage.update(_file_sizes(path))
        files_safe = True
    except _UnsafePath:
        storage["reason"] = "UNSAFE_STORAGE_PATH"
    except (OSError, ValueError, TypeError, OverflowError):
        pass  # UNKNOWN, never expose paths or exception text.
    metadata_available = False
    if files_safe:
        try:
            storage.update(_filesystem(path))
            metadata_available = True
        except (OSError, ValueError, TypeError, OverflowError, AttributeError):
            pass
        try:
            heartbeats, journal_mode = _read_monitors(path, names)
            monitors = component_health(heartbeats, names, now=moment)
            storage.update(database_readable=True, journal_mode=journal_mode)
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", 0) or 0
            storage["reason"] = ("GUARDIAN_DB_READ_BLOCKED" if (code & 255) in
                                 (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT)
                                 else "GUARDIAN_DB_READ_FAILED")
    if metadata_available and storage["database_readable"]:
        free, inodes = storage["filesystem_available_bytes"], storage["filesystem_available_inodes"]
        if storage["filesystem_read_only"]:
            state, reason = "BLOCKED", "FILESYSTEM_READ_ONLY"
        elif free <= CRITICAL_HEADROOM_BYTES:
            state, reason = "BLOCKED", "LOW_DISK_HEADROOM"
        elif inodes == 0:
            state, reason = "BLOCKED", "LOW_INODE_HEADROOM"
        elif free <= WARNING_HEADROOM_BYTES:
            state, reason = "DEGRADED", "LOW_DISK_HEADROOM"
        elif inodes is not None and inodes < 16:
            state, reason = "DEGRADED", "LOW_INODE_HEADROOM"
        elif storage["wal_bytes"] >= WARNING_WAL_BYTES:
            state, reason = "DEGRADED", "WAL_PRESSURE"
        elif storage["journal_mode"] != "wal":
            state, reason = "DEGRADED", "JOURNAL_MODE_NOT_WAL"
        else:
            state, reason = "HEALTHY", "READABLE_WITH_OBSERVED_HEADROOM"
        storage.update(state=state, reason=reason)
    components = {**monitors["components"], "guardian_storage": storage}
    return {
        "scope": "GUARDIAN_ONLY", "state": _state(components),
        "observed_at": monitors["observed_at"], "components": components,
        "evidence_complete": all(row["state"] != "UNKNOWN" for row in components.values()),
        "database_snapshot_atomic": storage["database_readable"],
        "filesystem_atomic_with_database": False,
        "automatic_action_allowed": False, "trading_integrity_verified": False,
    }


def include_self_health(health: dict, diagnostics: dict) -> dict:
    """Mask overall platform green without changing any upstream/trading state."""
    previous = health["state"]
    health["components"].update(diagnostics["components"])
    health["state"] = _state(health["components"])
    health["self_health"] = diagnostics
    health["evidence_complete"] = all(
        row["state"] != "UNKNOWN" for row in health["components"].values())
    if health["state"] != previous:
        health["state_reason"] = "GUARDIAN_SELF_HEALTH"
    return health
