"""Durable in-app incident notices, deduplicated and audited without trading I/O.

No remote destination or delivery credential is accepted. Notifications are not
evidence of remediation. Only a new incident, severity escalation or verified
source recovery generates another notice; repeated outage events do not spam.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone

from .events import GuardianEvent
from .incidents import _instance_ledger_signals, classify_incident
from .store import GuardianStore

_SEVERITY = {"INFO": 0, "WATCH": 1, "WARNING": 2, "HIGH": 3, "CRITICAL": 4}


class GuardianNotifications:
    def __init__(self, store: GuardianStore):
        self.store = store
        with closing(store._connect()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS guardian_notifications (
                    notification_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL,
                    category TEXT NOT NULL, severity TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                    UNIQUE(incident_id,category,severity)
                );
                CREATE TABLE IF NOT EXISTS guardian_notification_cursor (
                    name TEXT PRIMARY KEY,last_update_sequence INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO guardian_notification_cursor VALUES('in_app_v1',0);
                CREATE TRIGGER IF NOT EXISTS guardian_notifications_no_update
                  BEFORE UPDATE ON guardian_notifications BEGIN SELECT RAISE(ABORT,'Guardian notice is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS guardian_notifications_no_delete
                  BEFORE DELETE ON guardian_notifications BEGIN SELECT RAISE(ABORT,'Guardian notice is immutable'); END;
            """)
            conn.commit()

    def scan(self, *, limit: int = 500) -> int:
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("invalid notification scan limit")
        processed = 0
        with closing(self.store._connect()) as conn:
            while processed < limit:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    last = conn.execute("SELECT last_update_sequence FROM guardian_notification_cursor "
                                        "WHERE name='in_app_v1'").fetchone()[0]
                    row = conn.execute(
                        "SELECT u.*,i.fingerprint,i.title,e.payload_json FROM guardian_incident_updates u "
                        "JOIN guardian_incidents i ON i.incident_id=u.incident_id "
                        "JOIN events e ON e.event_id=u.event_id WHERE u.sequence>? "
                        "ORDER BY u.sequence LIMIT 1", (last,)).fetchone()
                    if row is None:
                        conn.commit()
                        break
                    event = json.loads(row["payload_json"])
                    signals = _instance_ledger_signals(event)
                    generic = classify_incident(event)
                    if generic:
                        signals.append(generic)
                    signal = next((item for item in signals if item.fingerprint == row["fingerprint"]), None)
                    if signal and _SEVERITY[signal.severity] >= 2:
                        highest = conn.execute(
                            "SELECT severity FROM guardian_notifications WHERE incident_id=? "
                            "AND category IN ('OPEN','ESCALATED')", (row["incident_id"],)).fetchall()
                        level = max((_SEVERITY[item[0]] for item in highest), default=-1)
                        category = None
                        if row["transition"] == "OPEN" and _SEVERITY[signal.severity] > level:
                            category = "OPEN" if level < 0 else "ESCALATED"
                        elif row["transition"] == "RECOVERED" and level >= 2:
                            category = "RECOVERED"
                        if category:
                            self._insert(conn, row, signal, category)
                    conn.execute("UPDATE guardian_notification_cursor SET last_update_sequence=? "
                                 "WHERE name='in_app_v1'", (row["sequence"],))
                    conn.commit()
                    processed += 1
                except Exception:
                    conn.rollback()
                    raise
        return processed

    def _insert(self, conn, row, signal, category):
        identity = hashlib.sha256(
            f"in_app_v1:{row['incident_id']}:{category}:{signal.severity}".encode()).hexdigest()
        if conn.execute("SELECT 1 FROM guardian_notifications WHERE notification_id=?",
                        (identity,)).fetchone():
            return
        now = datetime.now(timezone.utc)
        notice = {"notification_id": identity, "incident_id": row["incident_id"],
                  "channel": "IN_APP", "category": category, "severity": signal.severity,
                  "state": row["transition"], "confidence": signal.confidence,
                  "title": row["title"], "summary": row["summary"],
                  "observed_at": row["observed_at"], "created_at": now.isoformat(),
                  "evidence_id": row["event_id"], "remediation_performed": False}
        conn.execute("INSERT INTO guardian_notifications VALUES(?,?,?,?,?,?)", (
            identity, row["incident_id"], category, signal.severity, now.isoformat(),
            json.dumps(notice, sort_keys=True, separators=(",", ":"))))
        self.store._append_in_transaction(conn, GuardianEvent(
            source_service="guardian_notifications", source_component="notifications",
            event_type="notification_created", event_id="notice_" + identity, timestamp=now,
            evidence={"notification_id": identity, "incident_id": row["incident_id"],
                      "category": category, "channel": "IN_APP", "source_event_id": row["event_id"]}),
            now.isoformat())

    def list(self, *, limit: int = 50) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid notification limit")
        with closing(self.store._connect()) as conn:
            rows = conn.execute("SELECT payload_json FROM guardian_notifications "
                                "ORDER BY created_at DESC,notification_id LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]
