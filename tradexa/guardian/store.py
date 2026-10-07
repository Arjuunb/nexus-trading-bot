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
                CREATE INDEX IF NOT EXISTS events_lab_fill_history ON events(source_service,sequence)
                  WHERE event_type='lab_paper_fill_observed';
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
                CREATE TABLE IF NOT EXISTS observer_scan_state (
                    component TEXT PRIMARY KEY,
                    cursor_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_smc_closed_journal ON events(sequence)
                  WHERE source_service='guardian_smc_journal_history'
                    AND event_type='smc_closed_journal_observed';
                CREATE INDEX IF NOT EXISTS events_smc_intent_history ON events(sequence)
                  WHERE source_service='guardian_smc_intent_history'
                    AND event_type='smc_intent_transition_observed';
                CREATE INDEX IF NOT EXISTS smc_link_intent_key
                  ON events(json_extract(payload_json,'$.execution_id'),sequence)
                  WHERE source_service='guardian_smc_intent_history' AND event_type='smc_intent_transition_observed';
                CREATE INDEX IF NOT EXISTS smc_link_fill_key
                  ON events(json_extract(payload_json,'$.evidence.fill.candle_id'),sequence)
                  WHERE source_service='guardian_smc_fill_history' AND event_type='lab_paper_fill_observed';
                CREATE INDEX IF NOT EXISTS smc_link_fill_order
                  ON events(json_extract(payload_json,'$.order_id'),sequence)
                  WHERE source_service='guardian_smc_fill_history' AND event_type='lab_paper_fill_observed';
                CREATE INDEX IF NOT EXISTS smc_link_journal_order
                  ON events(json_extract(payload_json,'$.order_id'),sequence)
                  WHERE source_service='guardian_smc_journal_history' AND event_type='smc_closed_journal_observed';
                CREATE INDEX IF NOT EXISTS smc_link_journal_trade
                  ON events(json_extract(payload_json,'$.evidence.trade.id'),sequence)
                  WHERE source_service='guardian_smc_journal_history' AND event_type='smc_closed_journal_observed';
                CREATE INDEX IF NOT EXISTS smc_position_history ON events(sequence)
                  WHERE source_service='guardian_smc_fill_positions' AND event_type='smc_fill_position_observed';
                CREATE INDEX IF NOT EXISTS smc_exit_history ON events(sequence)
                  WHERE source_service='guardian_smc_exit_fills' AND event_type='smc_exit_evidence_observed';
                CREATE INDEX IF NOT EXISTS smc_position_order ON events(json_extract(payload_json,'$.order_id'),sequence)
                  WHERE source_service='guardian_smc_fill_positions' AND event_type='smc_fill_position_observed';
                CREATE INDEX IF NOT EXISTS smc_position_before_key
                  ON events(json_extract(payload_json,'$.evidence.fill.transition.before.entry_execution_key'),sequence)
                  WHERE source_service='guardian_smc_fill_positions' AND event_type='smc_fill_position_observed';
                CREATE INDEX IF NOT EXISTS smc_position_after_key
                  ON events(json_extract(payload_json,'$.evidence.fill.transition.after.entry_execution_key'),sequence)
                  WHERE source_service='guardian_smc_fill_positions' AND event_type='smc_fill_position_observed';
                CREATE INDEX IF NOT EXISTS smc_position_before_id
                  ON events(json_extract(payload_json,'$.evidence.account_id'),json_extract(payload_json,'$.evidence.fill.transition.before.position_id'),sequence)
                  WHERE source_service='guardian_smc_fill_positions' AND event_type='smc_fill_position_observed';
                CREATE INDEX IF NOT EXISTS smc_position_after_id
                  ON events(json_extract(payload_json,'$.evidence.account_id'),json_extract(payload_json,'$.evidence.fill.transition.after.position_id'),sequence)
                  WHERE source_service='guardian_smc_fill_positions' AND event_type='smc_fill_position_observed';
                CREATE INDEX IF NOT EXISTS smc_position_fill
                  ON events(json_extract(payload_json,'$.evidence.account_id'),json_extract(payload_json,'$.evidence.fill.fill_id'),sequence)
                  WHERE source_service='guardian_smc_fill_positions' AND event_type='smc_fill_position_observed';
                CREATE INDEX IF NOT EXISTS smc_exit_origin_key
                  ON events(json_extract(payload_json,'$.evidence.fill.exit_evidence.position.entry_execution_key'),sequence)
                  WHERE source_service='guardian_smc_exit_fills' AND event_type='smc_exit_evidence_observed';
                CREATE INDEX IF NOT EXISTS smc_exit_origin_order
                  ON events(json_extract(payload_json,'$.evidence.fill.exit_evidence.position.entry_order_id'),sequence)
                  WHERE source_service='guardian_smc_exit_fills' AND event_type='smc_exit_evidence_observed';
                CREATE INDEX IF NOT EXISTS smc_exit_position
                  ON events(json_extract(payload_json,'$.evidence.account_id'),json_extract(payload_json,'$.evidence.fill.exit_evidence.position.position_id'),sequence)
                  WHERE source_service='guardian_smc_exit_fills' AND event_type='smc_exit_evidence_observed';
                CREATE INDEX IF NOT EXISTS smc_exit_fill
                  ON events(json_extract(payload_json,'$.evidence.account_id'),json_extract(payload_json,'$.evidence.fill.fill_id'),sequence)
                  WHERE source_service='guardian_smc_exit_fills' AND event_type='smc_exit_evidence_observed';
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

    def observer_scan_cursor(self, component: str) -> dict:
        from .smc_journal_history import COMPONENT, INITIAL_CURSOR, validate_cursor
        if component != COMPONENT:
            raise ValueError("Invalid journal scan component")
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT cursor_json FROM observer_scan_state WHERE component=?",
                               (component,)).fetchone()
        return validate_cursor(json.loads(row[0]) if row else dict(INITIAL_CURSOR))

    def append_observed_scan_page(self, component: str, *, expected: dict,
                                  next_cursor: dict, events: list[GuardianEvent]) -> int:
        """A repeat scan may advance through open rows with no evidence to insert.

        Its finite-pass reset is different from the monotonic append-only fill
        cursor. Both immutable close events and the CAS checkpoint commit here.
        """
        from .smc_journal_history import COMPONENT, INITIAL_CURSOR, PROBE, validate_cursor
        expected, next_cursor = validate_cursor(expected), validate_cursor(next_cursor)
        if (component != COMPONENT or len(events) > 32 or
                any(e.source_component != COMPONENT or e.source_service != PROBE or
                    e.event_type != "smc_closed_journal_observed" for e in events)):
            raise ValueError("Invalid journal scan events")
        same_pass = (next_cursor["cycle"] == expected["cycle"] and
                     next_cursor["after"] > expected["after"] and
                     (not expected["upper"] or next_cursor["upper"] == expected["upper"]))
        finished = next_cursor["cycle"] == expected["cycle"] + 1 and next_cursor["after"] == 0
        if not (same_pass or finished) or (expected["origin"] and next_cursor["origin"] != expected["origin"]):
            raise ValueError("Invalid journal scan transition")
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT cursor_json FROM observer_scan_state WHERE component=?",
                                   (component,)).fetchone()
                actual = json.loads(row[0]) if row else INITIAL_CURSOR
                if actual != expected:
                    raise ValueError("Journal scan checkpoint changed concurrently")
                appended = sum(self._append_in_transaction(conn, event, now) for event in events)
                conn.execute("INSERT INTO observer_scan_state(component,cursor_json,updated_at) VALUES (?,?,?) "
                             "ON CONFLICT(component) DO UPDATE SET cursor_json=excluded.cursor_json,updated_at=excluded.updated_at",
                             (component, json.dumps(next_cursor, sort_keys=True), now))
                conn.commit()
                return appended
            except Exception:
                conn.rollback()
                raise

    def smc_journal_history_page(self, *, after: int = 0) -> dict:
        if type(after) is not int or not 0 <= after <= 2**63 - 1:
            raise ValueError("Invalid journal history cursor")
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT sequence,received_at,payload_json FROM events "
                                "WHERE source_service='guardian_smc_journal_history' "
                                "AND event_type='smc_closed_journal_observed' AND sequence>? "
                                "ORDER BY sequence LIMIT 33", (after,)).fetchall()
        events = [{**json.loads(row["payload_json"]), "received_at": row["received_at"],
                   "guardian_sequence": row["sequence"]} for row in rows[:32]]
        return {"events": events, "after": after, "has_more": len(rows) > 32,
                "next_after": events[-1]["guardian_sequence"] if events else after}

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

    def lab_fill_history_page(self, source_service: str, *, after: int = 0) -> dict:
        """Page immutable observed fills by Guardian sequence, not source time."""
        if (source_service not in {"guardian_pa_fill_history", "guardian_smc_fill_history"} or
                type(after) is not int or not 0 <= after <= 2**63 - 1):
            raise ValueError("Invalid lab history cursor")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT sequence,received_at,payload_json FROM events "
                "WHERE source_service=? AND event_type='lab_paper_fill_observed' AND sequence>? "
                "ORDER BY sequence LIMIT 33", (source_service, after)).fetchall()
        events = [{**json.loads(row["payload_json"]), "received_at": row["received_at"],
                   "guardian_sequence": row["sequence"]} for row in rows[:32]]
        return {"events": events, "after": after, "has_more": len(rows) > 32,
                "next_after": events[-1]["guardian_sequence"] if events else after}

    def smc_intent_history_page(self, *, after: int = 0) -> dict:
        if type(after) is not int or not 0 <= after <= 2**63 - 1:
            raise ValueError("Invalid intent history cursor")
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT sequence,received_at,payload_json FROM events "
                                "WHERE source_service='guardian_smc_intent_history' "
                                "AND event_type='smc_intent_transition_observed' AND sequence>? "
                                "ORDER BY sequence LIMIT 33", (after,)).fetchall()
        events = [{**json.loads(row["payload_json"]), "received_at": row["received_at"],
                   "guardian_sequence": row["sequence"]} for row in rows[:32]]
        return {"events": events, "after": after, "has_more": len(rows) > 32,
                "next_after": events[-1]["guardian_sequence"] if events else after}

    def smc_execution_link_snapshot(self, execution_key: str) -> dict:
        """Bounded indexed ID lookups and probe age in one Guardian read snapshot.

        No trading database is opened. The separate source imports are not
        thereby made atomic. Overflow preserves partial evidence, not claims.
        """
        from time import monotonic
        from .smc_execution_links import MAX_ROWS, MAX_BYTES, MAX_EVENT_BYTES, PROBES, validate_key
        from .smc_intent_history import project_transition
        key = validate_key(execution_key)
        total, truncated = 0, False
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=250")
            deadline = monotonic() + 1
            db.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
            db.execute("BEGIN")

            def select(source, kind, field, values):
                nonlocal total, truncated
                if not values:
                    return []
                slots = ",".join("?" for _ in values)
                rows = db.execute("SELECT sequence,length(CAST(payload_json AS BLOB)) AS bytes,"
                                  "substr(payload_json,1,16385) AS payload FROM events "
                                  f"WHERE source_service='{source}' AND event_type='{kind}' "
                                  f"AND json_extract(payload_json,'{field}') IN ({slots}) "
                                  "ORDER BY sequence LIMIT ?", (*values, MAX_ROWS + 1))
                result = []
                for row in rows:
                    if row["bytes"] > MAX_EVENT_BYTES:
                        raise ValueError("Retained linkage event exceeded size bound")
                    if len(result) == MAX_ROWS or total + row["bytes"] > MAX_BYTES:
                        truncated = True
                        break
                    total += row["bytes"]
                    result.append({**json.loads(row["payload"]), "guardian_sequence": row["sequence"]})
                return result

            intents = select(PROBES[0], "smc_intent_transition_observed", "$.execution_id", (key,))
            projected = [project_transition(e.get("evidence", {}).get("transition")) for e in intents]
            order_ids = sorted({e.get("order_id") for e in intents if e.get("order_id")})
            trade_ids = sorted({e["trade_id"] for e in projected if e["trade_id"]})
            fills = select(PROBES[1], "lab_paper_fill_observed", "$.evidence.fill.candle_id", (key,))
            fills += select(PROBES[1], "lab_paper_fill_observed", "$.order_id", order_ids)
            trades = select(PROBES[2], "smc_closed_journal_observed", "$.order_id", order_ids)
            trades += select(PROBES[2], "smc_closed_journal_observed", "$.evidence.trade.id", trade_ids)

            def unique(rows):
                nonlocal truncated
                result = {r["guardian_sequence"]: r for r in rows}
                if len(result) > MAX_ROWS:
                    truncated = True
                return [result[k] for k in sorted(result)[:MAX_ROWS]]

            fills, trades = unique(fills), unique(trades)
            heartbeats = {r["component"]: dict(r) for r in db.execute(
                "SELECT component,state,reason,observed_at FROM heartbeats WHERE component IN (?,?,?)", PROBES)}
        return {"intent_events": intents, "fill_events": fills, "journal_events": trades,
                "heartbeats": heartbeats, "truncated": truncated}

    def smc_fill_positions_page(self, *, after: int = 0) -> dict:
        """Own imported history only; missing files are never recreated by GET."""
        from .smc_fill_positions import COMPONENT, PROBE
        if type(after) is not int or not 0 <= after <= 2**63-1:
            raise ValueError("Invalid fill-position history cursor")
        with closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True, timeout=.25)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=250")
            db.execute("BEGIN")
            rows = db.execute("SELECT sequence,received_at,length(CAST(payload_json AS BLOB)) AS bytes,"
                              "substr(payload_json,1,16385) AS payload FROM events "
                              "WHERE source_service='guardian_smc_fill_positions' AND event_type='smc_fill_position_observed' "
                              "AND sequence>? ORDER BY sequence LIMIT 33", (after,)).fetchall()
            if any(r["bytes"] > 16384 for r in rows):
                raise ValueError("Fill-position history evidence exceeds bound")
            events = [{**json.loads(r["payload"]), "guardian_sequence": r["sequence"], "received_at": r["received_at"]} for r in rows[:32]]
            heartbeat = db.execute("SELECT * FROM heartbeats WHERE component=?", (PROBE,)).fetchone()
            cursor = db.execute("SELECT source_sequence,anchor_id FROM observer_cursors WHERE component=?", (COMPONENT,)).fetchone()
        return {"events": events, "after": after, "next_after": events[-1]["guardian_sequence"] if events else after,
                "has_more": len(rows)>32, "guardian_snapshot_atomic": True,
                "source_cursor": {"after": cursor["source_sequence"] if cursor else 0, "anchor": cursor["anchor_id"] if cursor else ""},
                "heartbeats": {PROBE: dict(heartbeat)} if heartbeat else {}}

    def smc_exit_fills_page(self, *, after: int = 0) -> dict:
        """Own bounded imported exit records; GET never creates or migrates a DB."""
        from time import monotonic
        from .smc_exit_fills import COMPONENT, PROBE, _unique_fields
        if type(after) is not int or not 0 <= after <= 2**63-1:
            raise ValueError("Invalid exit history cursor")
        with closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True, timeout=.25)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=250")
            deadline = monotonic()+1
            db.set_progress_handler(lambda: int(monotonic()>deadline), 1000)
            db.execute("BEGIN")
            rows = db.execute("SELECT sequence,received_at,length(CAST(payload_json AS BLOB)) AS bytes,"
                              "substr(payload_json,1,16385) AS payload FROM events INDEXED BY smc_exit_history "
                              "WHERE source_service='guardian_smc_exit_fills' AND event_type='smc_exit_evidence_observed' "
                              "AND sequence>? ORDER BY sequence LIMIT 33", (after,)).fetchall()
            if any(r["bytes"]>16384 for r in rows):
                raise ValueError("Exit history evidence exceeds bound")
            events = [{**json.loads(r["payload"], object_pairs_hook=_unique_fields),
                       "guardian_sequence": r["sequence"], "received_at": r["received_at"]} for r in rows[:32]]
            heartbeat = db.execute("SELECT * FROM heartbeats WHERE component=?", (PROBE,)).fetchone()
            cursor = db.execute("SELECT source_sequence,anchor_id FROM observer_cursors WHERE component=?", (COMPONENT,)).fetchone()
        return {"events": events, "after": after, "next_after": events[-1]["guardian_sequence"] if events else after,
                "has_more": len(rows)>32, "guardian_snapshot_atomic": True,
                "source_cursor": {"after": cursor["source_sequence"] if cursor else 0, "anchor": cursor["anchor_id"] if cursor else ""},
                "heartbeats": {PROBE: dict(heartbeat)} if heartbeat else {}}

    def smc_position_link_snapshot(self, execution_key: str) -> dict:
        """Indexed exact key/account-position associations, never time/price joins."""
        from time import monotonic
        from .smc_position_links import MAX_ROWS, MAX_BYTES, PROBES
        from .smc_execution_links import validate_key
        from .smc_fill_positions import project_fill_position
        key = validate_key(execution_key)
        total, truncated, loaded = 0, False, set()
        with closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True, timeout=.25)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=250")
            deadline = monotonic()+1
            db.set_progress_handler(lambda: int(monotonic()>deadline), 1000)
            db.execute("BEGIN")

            def select(source, kind, field, values, account=None):
                nonlocal total, truncated
                if not values or truncated:
                    return []
                slots = ",".join("?" for _ in values)
                clause = " AND json_extract(payload_json,'$.evidence.account_id')=?" if account else ""
                rows = db.execute("SELECT sequence,length(CAST(payload_json AS BLOB)) AS bytes,substr(payload_json,1,16385) AS payload "
                                  f"FROM events WHERE source_service='{source}' AND event_type='{kind}' "
                                  f"AND json_extract(payload_json,'{field}') IN ({slots})"+clause+" ORDER BY sequence LIMIT ?",
                                  (*values, *((account,) if account else ()), MAX_ROWS+1))
                result = []
                for row in rows:
                    if row["sequence"] in loaded:
                        continue
                    if row["bytes"]>16384:
                        raise ValueError("Position-link event exceeds bound")
                    if len(result)==MAX_ROWS or total+row["bytes"]>MAX_BYTES:
                        truncated = True
                        break
                    loaded.add(row["sequence"])
                    total += row["bytes"]
                    result.append({**json.loads(row["payload"]), "guardian_sequence": row["sequence"]})
                return result

            intents = select(PROBES[0], "smc_intent_transition_observed", "$.execution_id", (key,))
            transitions = []
            orders = sorted({e.get("order_id") for e in intents if e.get("order_id")})
            transitions += select(PROBES[1], "smc_fill_position_observed", "$.order_id", orders)
            for side in ("before", "after"):
                transitions += select(PROBES[1], "smc_fill_position_observed", f"$.evidence.fill.transition.{side}.entry_execution_key", (key,))
            seeds = set()
            for e in transitions:
                account = e["evidence"]["account_id"]
                fill = project_fill_position(e["evidence"]["fill"], account)
                for side in ("before", "after"):
                    pos = fill["transition"][side] if fill["transition"] else None
                    if pos and pos["entry_execution_key"]==key and pos["position_id"]:
                        seeds.add((account, pos["position_id"]))
            if len(seeds)>MAX_ROWS:
                truncated = True
            for account, position in sorted(seeds)[:MAX_ROWS]:
                for side in ("before", "after"):
                    transitions += select(PROBES[1], "smc_fill_position_observed", f"$.evidence.fill.transition.{side}.position_id", (position,), account)
            if len(transitions)>MAX_ROWS:
                truncated = True
            heartbeats = {r["component"]: dict(r) for r in db.execute("SELECT * FROM heartbeats WHERE component IN (?,?)", PROBES)}
        return {"intent_events": intents, "position_events": sorted(transitions, key=lambda e:e["guardian_sequence"])[:MAX_ROWS],
                "heartbeats": heartbeats, "truncated": truncated}

    def smc_exit_link_snapshot(self, execution_key: str) -> dict:
        """One bounded own-DB read snapshot; no source reads or time/price joins.

        Every query uses an exact-ID partial index, including old decisions.
        The row/byte budget covers all four streams together, not each query.
        """
        from time import monotonic
        from .smc_exit_links import MAX_ROWS, MAX_BYTES, MAX_EVENT_BYTES, PROBES, project_retained
        from .smc_execution_links import validate_key
        from .smc_exit_fills import _unique_fields
        key = validate_key(execution_key)
        kinds = ("smc_intent_transition_observed", "smc_fill_position_observed",
                 "smc_exit_evidence_observed", "smc_closed_journal_observed")
        specs = {
            "intent": (0, "smc_link_intent_key", "$.execution_id"),
            "position_order": (1, "smc_position_order", "$.order_id"),
            "before_key": (1, "smc_position_before_key", "$.evidence.fill.transition.before.entry_execution_key"),
            "after_key": (1, "smc_position_after_key", "$.evidence.fill.transition.after.entry_execution_key"),
            "before_id": (1, "smc_position_before_id", "$.evidence.fill.transition.before.position_id"),
            "after_id": (1, "smc_position_after_id", "$.evidence.fill.transition.after.position_id"),
            "position_fill": (1, "smc_position_fill", "$.evidence.fill.fill_id"),
            "exit_key": (2, "smc_exit_origin_key", "$.evidence.fill.exit_evidence.position.entry_execution_key"),
            "exit_order": (2, "smc_exit_origin_order", "$.evidence.fill.exit_evidence.position.entry_order_id"),
            "exit_position": (2, "smc_exit_position", "$.evidence.fill.exit_evidence.position.position_id"),
            "exit_fill": (2, "smc_exit_fill", "$.evidence.fill.fill_id"),
            "journal_trade": (3, "smc_link_journal_trade", "$.evidence.trade.id"),
            "journal_order": (3, "smc_link_journal_order", "$.order_id"),
        }
        total, truncated, loaded = 0, False, set()
        groups = [[] for _ in PROBES]
        with closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True, timeout=.25)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=250")
            deadline = monotonic()+1
            db.set_progress_handler(lambda: int(monotonic()>deadline), 1000)
            db.execute("BEGIN")

            def select(spec, values, account=None):
                nonlocal total, truncated
                if not values or truncated:
                    return
                if monotonic() > deadline:
                    raise sqlite3.OperationalError("Exit-link snapshot deadline exceeded")
                stream, index, field = specs[spec]
                slots = ",".join("?" for _ in values)
                clause = "json_extract(payload_json,'$.evidence.account_id')=? AND " if account is not None else ""
                # All SQL identifiers/paths come from the fixed internal table.
                rows = db.execute("SELECT sequence,length(CAST(payload_json AS BLOB)) AS bytes,"
                    f"substr(payload_json,1,{MAX_EVENT_BYTES+1}) AS payload FROM events INDEXED BY {index} "
                    f"WHERE source_service='{PROBES[stream]}' AND event_type='{kinds[stream]}' AND "
                    + clause + f"json_extract(payload_json,'{field}') IN ({slots}) ORDER BY sequence LIMIT ?",
                    (*((account,) if account is not None else ()), *values, MAX_ROWS+1))
                for row in rows:
                    if monotonic() > deadline:
                        raise sqlite3.OperationalError("Exit-link snapshot deadline exceeded")
                    if row["sequence"] in loaded:
                        continue
                    if row["bytes"] > MAX_EVENT_BYTES:
                        raise ValueError("Exit-link event exceeds bound")
                    if len(loaded) == MAX_ROWS or total+row["bytes"] > MAX_BYTES:
                        truncated = True
                        break
                    event = json.loads(row["payload"], object_pairs_hook=_unique_fields)
                    project_retained(event, stream)
                    loaded.add(row["sequence"])
                    total += row["bytes"]
                    groups[stream].append({**event, "guardian_sequence": row["sequence"]})

            select("intent", (key,))
            orders = sorted({e["order_id"] for e in groups[0] if e["order_id"]})
            trade_ids = sorted({e["evidence"]["transition"]["trade_id"] for e in groups[0]
                                if e["evidence"]["transition"]["trade_id"]})
            select("position_order", orders)
            for spec in ("before_key", "after_key", "exit_key"):
                select(spec, (key,))
            select("exit_order", orders)
            seeds = set()
            for stream in (1, 2):
                for event in groups[stream]:
                    row, account = project_retained(event, stream)
                    if stream == 1:
                        value = row["transition"]
                        snapshots = [value[s] for s in ("before", "after")] if value else []
                    else:
                        value = row["exit_evidence"]
                        snapshots = [value["position"]] if value else []
                    for pos in snapshots:
                        if pos and pos["entry_execution_key"] == key and pos["position_id"]:
                            seeds.add((account, pos["position_id"]))
            for account, position in sorted(seeds):
                for spec in ("before_id", "after_id", "exit_position"):
                    select(spec, (position,), account)
            # Null legacy captures have no origin key. Exact account+fill ID
            # finds their counterpart without inventing historical evidence.
            fills = {(e["evidence"]["account_id"], e["evidence"]["fill"]["fill_id"])
                     for stream in (1, 2) for e in groups[stream]}
            for account, fill_id in sorted(fills):
                for spec in ("position_fill", "exit_fill"):
                    select(spec, (fill_id,), account)
            select("journal_trade", trade_ids)
            select("journal_order", orders)
            heartbeats = {r["component"]: dict(r) for r in db.execute(
                "SELECT * FROM heartbeats WHERE component IN (?,?,?,?)", PROBES)}
        return {name: sorted(events, key=lambda e: e["guardian_sequence"])
                for name, events in zip(("intent_events", "position_events", "exit_events", "journal_events"), groups)} | {
                    "heartbeats": heartbeats, "truncated": truncated, "evidence_rows_loaded": len(loaded)}

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
