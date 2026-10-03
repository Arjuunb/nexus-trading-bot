"""Guardian-owned SQLite evidence store, separate from every trading ledger."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .events import GuardianEvent, GuardianEventError, _NAME, _safe_json


class GuardianStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise PermissionError("Guardian evidence database cannot be a symlink")
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        file_stat = self.path.stat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise PermissionError("Guardian evidence database must be a regular unlinked file")
        if file_stat.st_mode & 0o077:
            raise PermissionError("Guardian evidence database must be owner-only")
        with closing(self._connect()) as conn:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise RuntimeError("Guardian evidence database requires WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    timestamp TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    source_service TEXT NOT NULL,
                    source_component TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_timestamp ON events(timestamp);
                CREATE INDEX IF NOT EXISTS events_source ON events(source_service, source_component);
                CREATE INDEX IF NOT EXISTS events_lab_decision_scan ON events(sequence DESC)
                  WHERE source_service IN ('guardian_lab_probe','guardian_lab_backfill')
                    AND event_type IN ('lab_evaluation_observed','lab_evaluation_backfilled');
                CREATE TRIGGER IF NOT EXISTS events_no_update
                  BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'Guardian evidence is immutable');
                  END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                  BEFORE DELETE ON events BEGIN
                    SELECT RAISE(ABORT, 'Guardian evidence is immutable');
                  END;
                CREATE TABLE IF NOT EXISTS heartbeats (
                    component TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observer_cursors (
                    component TEXT PRIMARY KEY,
                    source_sequence INTEGER NOT NULL,
                    anchor_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observer_snapshot_state (
                    component TEXT PRIMARY KEY,
                    material_digest TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @staticmethod
    def _append_in_transaction(conn: sqlite3.Connection, event: GuardianEvent,
                               received_at: str) -> bool:
        """Audit a Guardian-derived action in its caller's short transaction."""
        if not conn.in_transaction:
            raise RuntimeError("Guardian audit insertion requires an active transaction")
        payload = event.canonical_json()
        existing = conn.execute("SELECT payload_json FROM events WHERE event_id=?",
                                (event.event_id,)).fetchone()
        if existing is not None:
            if existing["payload_json"] != payload:
                raise GuardianEventError("event_id collision with different evidence")
            return False
        conn.execute(
            "INSERT INTO events(event_id,timestamp,received_at,source_service,"
            "source_component,event_type,severity,payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (event.event_id, event.timestamp.astimezone(timezone.utc).isoformat(),
             received_at, event.source_service, event.source_component,
             event.event_type, event.severity, payload))
        return True

    def append(self, event: GuardianEvent) -> bool:
        """Append immutable evidence; an identical retried ID is idempotent.

        Returns False for an identical retry. An ID collision with different
        content is an error, never a silent overwrite. No trading DB is used.
        """
        payload = event.canonical_json()
        received_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT payload_json FROM events WHERE event_id=?", (event.event_id,)
                ).fetchone()
                if existing is not None:
                    if existing["payload_json"] != payload:
                        raise GuardianEventError("event_id collision with different evidence")
                    conn.commit()
                    return False
                conn.execute(
                    """INSERT INTO events
                       (event_id, timestamp, received_at, source_service,
                        source_component, event_type, severity, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event.event_id, event.timestamp.astimezone(timezone.utc).isoformat(),
                     received_at, event.source_service, event.source_component,
                     event.event_type, event.severity, payload),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def observer_cursor(self, component: str) -> tuple[int, str]:
        if not _NAME.fullmatch(component or ""):
            raise ValueError("invalid observer component")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT source_sequence,anchor_id FROM observer_cursors WHERE component=?",
                (component,),
            ).fetchone()
        return (int(row["source_sequence"]), row["anchor_id"]) if row else (0, "")

    def append_observed_page(self, component: str, *, expected: tuple[int, str],
                             next_cursor: tuple[int, str],
                             events: list[GuardianEvent]) -> int:
        """Commit a source page and its checkpoint in one Guardian transaction.

        A crash or failed insert cannot advance the cursor beyond durable
        evidence. Repeated identical pages are idempotent; changed evidence
        under the same ID is a hard error.
        """
        if not _NAME.fullmatch(component or "") or len(events) > 32:
            raise ValueError("invalid observer page")
        old_sequence, old_anchor = expected
        new_sequence, new_anchor = next_cursor
        if (type(old_sequence) is not int or type(new_sequence) is not int or
                old_sequence < 0 or new_sequence < old_sequence or
                not isinstance(old_anchor, str) or not isinstance(new_anchor, str) or
                len(old_anchor) > 128 or len(new_anchor) > 128 or
                bool(old_sequence) != bool(old_anchor) or
                bool(new_sequence) != bool(new_anchor) or
                (bool(events) != (new_sequence > old_sequence))):
            raise ValueError("invalid observer checkpoint")
        payloads = [event.canonical_json() for event in events]
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT source_sequence,anchor_id FROM observer_cursors WHERE component=?",
                    (component,),
                ).fetchone()
                actual = (int(row["source_sequence"]), row["anchor_id"]) if row else (0, "")
                if actual != expected:
                    raise ValueError("observer checkpoint changed concurrently")
                appended = 0
                for event, payload in zip(events, payloads):
                    existing = conn.execute(
                        "SELECT payload_json FROM events WHERE event_id=?", (event.event_id,)
                    ).fetchone()
                    if existing is not None:
                        if existing["payload_json"] != payload:
                            raise GuardianEventError("event_id collision with different evidence")
                        continue
                    conn.execute(
                        """INSERT INTO events
                           (event_id,timestamp,received_at,source_service,
                            source_component,event_type,severity,payload_json)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (event.event_id, event.timestamp.astimezone(timezone.utc).isoformat(),
                         now, event.source_service, event.source_component,
                         event.event_type, event.severity, payload),
                    )
                    appended += 1
                if next_cursor != expected:
                    conn.execute(
                        """INSERT INTO observer_cursors
                           (component,source_sequence,anchor_id,updated_at)
                           VALUES (?,?,?,?)
                           ON CONFLICT(component) DO UPDATE SET
                           source_sequence=excluded.source_sequence,
                           anchor_id=excluded.anchor_id,updated_at=excluded.updated_at""",
                        (component, new_sequence, new_anchor, now),
                    )
                conn.commit()
                return appended
            except Exception:
                conn.rollback()
                raise

    def append_observed_snapshot(self, component: str, digest: str,
                                 event: GuardianEvent) -> bool:
        """Atomically append a changed observation and its dedupe checkpoint."""
        if (not _NAME.fullmatch(component or "") or
                not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                event.source_component != component):
            raise ValueError("invalid Guardian observer snapshot")
        payload = event.canonical_json()
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                prior = conn.execute(
                    "SELECT material_digest FROM observer_snapshot_state WHERE component=?",
                    (component,),
                ).fetchone()
                if prior is not None and prior["material_digest"] == digest:
                    conn.commit()
                    return False
                conn.execute(
                    """INSERT INTO events(event_id,timestamp,received_at,source_service,
                       source_component,event_type,severity,payload_json)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (event.event_id, event.timestamp.astimezone(timezone.utc).isoformat(),
                     now, event.source_service, event.source_component,
                     event.event_type, event.severity, payload),
                )
                conn.execute(
                    """INSERT INTO observer_snapshot_state
                       (component,material_digest,event_id,updated_at) VALUES (?,?,?,?)
                       ON CONFLICT(component) DO UPDATE SET
                       material_digest=excluded.material_digest,
                       event_id=excluded.event_id,updated_at=excluded.updated_at""",
                    (component, digest, event.event_id, now),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def recent(self, limit: int = 50, *, source_service: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with closing(self._connect()) as conn:
            if source_service is None:
                rows = conn.execute(
                    "SELECT received_at, payload_json FROM events ORDER BY sequence DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT received_at, payload_json FROM events
                       WHERE source_service=? ORDER BY sequence DESC LIMIT ?""",
                    (source_service, limit),
                ).fetchall()
        return [{**json.loads(row["payload_json"]), "received_at": row["received_at"]}
                for row in rows]

    def recent_lab_evidence(self, limit: int = 2000) -> list[dict]:
        """Bounded source snapshots for a derived decision view, newest first.

        This is deliberately not an all-history strategy statistic. Raw events
        remain immutable; the caller must disclose that the scan is bounded.
        """
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("invalid lab evidence scan limit")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT sequence,received_at,payload_json FROM events
                   WHERE source_service IN ('guardian_lab_probe','guardian_lab_backfill',
                                            'guardian_lab_lifecycle')
                     AND event_type IN ('lab_evaluation_observed','lab_evaluation_backfilled',
                                        'lab_lifecycle_observed')
                   ORDER BY sequence DESC LIMIT ?""", (limit,),
            ).fetchall()
        return [{**json.loads(row["payload_json"]), "received_at": row["received_at"],
                 "guardian_sequence": row["sequence"]} for row in rows]

    def recent_instance_decision_evidence(self, limit: int = 2000) -> list[dict]:
        """Bounded received instance transitions, newest ingestion first."""
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("invalid instance evidence scan limit")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT sequence,received_at,payload_json FROM events
                   WHERE source_service='guardian_instance_decisions'
                     AND event_type='instance_decision_observed'
                   ORDER BY sequence DESC LIMIT ?""", (limit,),
            ).fetchall()
        return [{**json.loads(row["payload_json"]), "received_at": row["received_at"],
                 "guardian_sequence": row["sequence"]} for row in rows]

    def count(self) -> int:
        with closing(self._connect()) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def observed_snapshot(self, component: str) -> dict | None:
        """Read the event committed with a change-only observer checkpoint."""
        if not _NAME.fullmatch(component or ""):
            raise ValueError("invalid observer component")
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT e.payload_json,e.received_at FROM observer_snapshot_state s "
                "JOIN events e ON e.event_id=s.event_id WHERE s.component=?",
                (component,),
            ).fetchone()
        return ({**json.loads(row["payload_json"]), "received_at": row["received_at"]}
                if row else None)

    def record_heartbeat(self, component: str, state: str, *, reason: str = "",
                         observed_at: datetime | None = None) -> None:
        """Keep only the current heartbeat; lifecycle evidence stays append-only."""
        if not _NAME.fullmatch(component or "") or state not in (
                "HEALTHY", "DEGRADED", "BLOCKED", "FAILED", "UNKNOWN"):
            raise ValueError("invalid Guardian heartbeat")
        when = observed_at or datetime.now(timezone.utc)
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("heartbeat timestamp must include a timezone")
        if not isinstance(reason, str) or len(reason) > 500:
            raise ValueError("heartbeat reason is too long")
        _safe_json(reason)
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO heartbeats(component, state, reason, observed_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(component) DO UPDATE SET
                     state=excluded.state, reason=excluded.reason,
                     observed_at=excluded.observed_at""",
                (component, state, reason, when.astimezone(timezone.utc).isoformat()),
            )
            conn.commit()

    def heartbeats(self) -> dict[str, dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT component, state, reason, observed_at FROM heartbeats").fetchall()
        return {row["component"]: dict(row) for row in rows}
