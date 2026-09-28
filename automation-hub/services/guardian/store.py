"""Guardian's own evidence store: its own SQLite file, separate from every
trading database.

Two kinds of table:

* **Evidence** -- ``guardian_events`` and ``guardian_actions``. Append-only:
  triggers abort any UPDATE or DELETE, so neither Guardian's later analysis nor
  a bug can rewrite what was observed or what Guardian did (PRD §6, §39).
* **Current state** -- ``guardian_components`` and ``guardian_meta``. What
  Guardian believes right now; every change to it is itself recorded as a
  ``health_changed`` event, so the history lives in the evidence tables.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from services.guardian.schema import FIELDS, SEVERITY_RANK, GuardianEvent, utcnow

_EVENT_COLUMNS = tuple(f for f in FIELDS)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS guardian_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_service TEXT NOT NULL,
    source_component TEXT NOT NULL,
    agent_id TEXT, instance_id TEXT, lab_id TEXT, strategy_id TEXT, strategy_version TEXT,
    symbol TEXT, timeframe TEXT,
    event_type TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    severity_rank INTEGER NOT NULL,
    state_before TEXT, state_after TEXT, decision TEXT, reason TEXT,
    evidence TEXT,
    correlation_id TEXT, session_id TEXT, execution_id TEXT, order_id TEXT, position_id TEXT,
    latency_ms REAL,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_ge_component ON guardian_events(source_component, seq);
CREATE INDEX IF NOT EXISTS idx_ge_severity ON guardian_events(severity_rank, seq);
CREATE INDEX IF NOT EXISTS idx_ge_instance ON guardian_events(instance_id, seq);
CREATE INDEX IF NOT EXISTS idx_ge_type ON guardian_events(event_type, seq);
CREATE INDEX IF NOT EXISTS idx_ge_time ON guardian_events(timestamp);
CREATE TRIGGER IF NOT EXISTS trg_guardian_events_no_update BEFORE UPDATE ON guardian_events
BEGIN SELECT RAISE(ABORT, 'guardian events are immutable evidence'); END;
CREATE TRIGGER IF NOT EXISTS trg_guardian_events_no_delete BEFORE DELETE ON guardian_events
BEGIN SELECT RAISE(ABORT, 'guardian events are immutable evidence'); END;

CREATE TABLE IF NOT EXISTS guardian_actions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL UNIQUE,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    policy TEXT NOT NULL,
    result TEXT NOT NULL,
    evidence TEXT
);
CREATE TRIGGER IF NOT EXISTS trg_guardian_actions_no_update BEFORE UPDATE ON guardian_actions
BEGIN SELECT RAISE(ABORT, 'guardian actions are an append-only audit'); END;
CREATE TRIGGER IF NOT EXISTS trg_guardian_actions_no_delete BEFORE DELETE ON guardian_actions
BEGIN SELECT RAISE(ABORT, 'guardian actions are an append-only audit'); END;

CREATE TABLE IF NOT EXISTS guardian_components (
    component_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guardian_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_FILTERS = ("source_component", "instance_id", "lab_id", "symbol", "timeframe",
            "event_type", "category", "strategy_id")


def _dump(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def _load(value: Optional[str]) -> Any:
    return None if value is None else json.loads(value)


class GuardianStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(self.path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if self.path != ":memory:":
                self._c.execute("PRAGMA journal_mode=WAL")
            self._c.executescript(_SCHEMA)
            self._c.commit()

    # ------------------------------------------------------------ evidence
    def append_events(self, events: Iterable[GuardianEvent], *, received_at: Optional[str] = None) -> int:
        received_at = received_at or utcnow()
        rows = []
        for event in events:
            data = event.to_dict()
            data["received_at"] = received_at
            data["evidence"] = _dump(data.get("evidence"))
            data["metadata"] = _dump(data.get("metadata"))
            rows.append({**{c: data.get(c) for c in _EVENT_COLUMNS},
                         "severity_rank": SEVERITY_RANK[data["severity"]]})
        if not rows:
            return 0
        cols = (*_EVENT_COLUMNS, "severity_rank")
        sql = (f"INSERT OR IGNORE INTO guardian_events({','.join(cols)}) "
               f"VALUES ({','.join(':' + c for c in cols)})")
        with self._lock:
            try:
                before = self._c.total_changes
                self._c.executemany(sql, rows)
                self._c.commit()
                return self._c.total_changes - before
            except Exception:
                self._c.rollback()
                raise

    def events(self, *, limit: int = 200, before_seq: Optional[int] = None,
               after_seq: Optional[int] = None, min_severity: Optional[str] = None,
               since: Optional[str] = None, **filters: Optional[str]) -> list[dict]:
        where, args = [], []
        for name in _FILTERS:
            value = filters.get(name)
            if value:
                where.append(f"{name}=?")
                args.append(value)
        if min_severity:
            where.append("severity_rank>=?")
            args.append(SEVERITY_RANK[min_severity])
        if before_seq is not None:
            where.append("seq<?")
            args.append(int(before_seq))
        if after_seq is not None:
            where.append("seq>?")
            args.append(int(after_seq))
        if since:
            where.append("timestamp>=?")
            args.append(since)
        sql = "SELECT * FROM guardian_events" + (f" WHERE {' AND '.join(where)}" if where else "")
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = [dict(r) for r in self._c.execute(sql, args)]
        for row in rows:
            row["evidence"] = _load(row["evidence"])
            row["metadata"] = _load(row["metadata"])
            row.pop("severity_rank", None)
        return rows

    def count_events(self, *, since: Optional[str] = None, min_severity: Optional[str] = None) -> int:
        where, args = [], []
        if since:
            where.append("timestamp>=?")
            args.append(since)
        if min_severity:
            where.append("severity_rank>=?")
            args.append(SEVERITY_RANK[min_severity])
        sql = "SELECT COUNT(*) FROM guardian_events" + (f" WHERE {' AND '.join(where)}" if where else "")
        with self._lock:
            return int(self._c.execute(sql, args).fetchone()[0])

    # ------------------------------------------------------------- actions
    def record_action(self, action: str, *, reason: str, policy: str, result: str,
                      evidence: Any = None) -> dict:
        row = {"action_id": uuid.uuid4().hex, "at": utcnow(), "action": action,
               "reason": reason, "policy": policy, "result": result, "evidence": _dump(evidence)}
        with self._lock:
            self._c.execute("INSERT INTO guardian_actions(action_id,at,action,reason,policy,result,evidence) "
                            "VALUES (:action_id,:at,:action,:reason,:policy,:result,:evidence)", row)
            self._c.commit()
        return {**row, "evidence": evidence}

    def actions(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._c.execute(
                "SELECT * FROM guardian_actions ORDER BY seq DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),))]
        for row in rows:
            row["evidence"] = _load(row["evidence"])
        return rows

    # ------------------------------------------------------- current state
    def save_components(self, components: dict[str, dict]) -> None:
        now = utcnow()
        with self._lock:
            self._c.execute("DELETE FROM guardian_components")
            self._c.executemany(
                "INSERT INTO guardian_components(component_id,data,updated_at) VALUES (?,?,?)",
                [(cid, _dump(data), now) for cid, data in components.items()])
            self._c.commit()

    def components(self) -> dict[str, dict]:
        with self._lock:
            return {r["component_id"]: _load(r["data"])
                    for r in self._c.execute("SELECT * FROM guardian_components")}

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._c.execute("INSERT OR REPLACE INTO guardian_meta(key,value) VALUES (?,?)",
                            (key, _dump(value)))
            self._c.commit()

    def meta(self, key: str) -> Any:
        with self._lock:
            row = self._c.execute("SELECT value FROM guardian_meta WHERE key=?", (key,)).fetchone()
        return _load(row["value"]) if row else None
