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
from services.guardian.strategy import almost_trade, setup_identity

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

-- Derived from strategy events as they are stored (PRD §41: high-volume
-- telemetry is aggregated). Rebuildable from guardian_events; not evidence.
CREATE TABLE IF NOT EXISTS guardian_strategy_rollup (
    day TEXT NOT NULL,
    source_component TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    decision TEXT NOT NULL,
    blocker_code TEXT NOT NULL,
    lab_id TEXT, instance_id TEXT, strategy_version TEXT,
    count INTEGER NOT NULL,
    last_at TEXT NOT NULL,
    PRIMARY KEY (day, source_component, strategy_id, symbol, timeframe, decision, blocker_code)
);
-- One row per near-valid setup (PRD §9), however many candles it stayed one
-- condition away. Links to the first and latest trace in guardian_events.
CREATE TABLE IF NOT EXISTS guardian_almost_trades (
    identity TEXT PRIMARY KEY,
    first_event_id TEXT NOT NULL,
    last_event_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    sightings INTEGER NOT NULL,
    source_component TEXT NOT NULL,
    lab_id TEXT, instance_id TEXT, strategy_id TEXT, strategy_version TEXT,
    symbol TEXT, timeframe TEXT, direction TEXT,
    kind TEXT NOT NULL,
    classification TEXT NOT NULL,
    passed INTEGER, evaluated INTEGER,
    prevented_by TEXT,
    conditions TEXT,
    note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gat_seen ON guardian_almost_trades(last_seen);
"""
_FILTERS = ("source_component", "instance_id", "lab_id", "symbol", "timeframe",
            "event_type", "category", "strategy_id", "decision")


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
        plain = [r for r in rows if r["category"] != "strategy"]
        strategy = [r for r in rows if r["category"] == "strategy"]
        with self._lock:
            try:
                inserted = self._c.executemany(sql, plain).rowcount if plain else 0
                for row in strategy:
                    # One at a time: only a trace seen for the first time may
                    # count, or a re-read after a restart would count twice.
                    if self._c.execute(sql, row).rowcount == 1:
                        inserted += 1
                        self._index_strategy(row)
                self._c.commit()
                return max(0, inserted)
            except Exception:
                self._c.rollback()
                raise

    def _index_strategy(self, row: dict) -> None:
        trace = _load(row["evidence"]) or {}
        meta = _load(row["metadata"]) or {}
        blocker = str(meta.get("blocker_code") or trace.get("blocker_code") or "")
        self._c.execute(
            "INSERT INTO guardian_strategy_rollup(day,source_component,strategy_id,symbol,timeframe,"
            "decision,blocker_code,lab_id,instance_id,strategy_version,count,last_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(day,source_component,strategy_id,symbol,"
            "timeframe,decision,blocker_code) DO UPDATE SET count=count+1, "
            "last_at=MAX(last_at, excluded.last_at), strategy_version=excluded.strategy_version",
            (str(row["timestamp"])[:10], row["source_component"], row["strategy_id"] or "",
             row["symbol"] or "", row["timeframe"] or "", row["decision"] or "", blocker,
             row["lab_id"], row["instance_id"], row["strategy_version"], row["timestamp"]))
        almost = almost_trade(trace)
        if almost is None:
            return
        identity = setup_identity(row, trace)
        self._c.execute(
            "INSERT INTO guardian_almost_trades(identity,first_event_id,last_event_id,first_seen,"
            "last_seen,sightings,source_component,lab_id,instance_id,strategy_id,strategy_version,"
            "symbol,timeframe,direction,kind,classification,passed,evaluated,prevented_by,conditions,"
            "note) VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE "
            "SET sightings=sightings+1, last_seen=MAX(last_seen, excluded.last_seen), "
            "last_event_id=excluded.last_event_id",
            (identity, row["event_id"], row["event_id"], row["timestamp"], row["timestamp"],
             row["source_component"], row["lab_id"], row["instance_id"], row["strategy_id"],
             row["strategy_version"], row["symbol"], row["timeframe"], trace.get("direction"),
             almost["kind"], almost["classification"], almost["passed"], almost["evaluated"],
             _dump(almost["prevented_by"]), _dump(trace.get("conditions")), almost["note"]))

    # ------------------------------------------------------------ strategy
    def strategy_rollup(self, *, since_day: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT * FROM guardian_strategy_rollup WHERE day>=? ORDER BY day", (since_day,))]

    def almost_trades(self, *, limit: int = 100, since: Optional[str] = None,
                      strategy_id: Optional[str] = None,
                      source_component: Optional[str] = None) -> list[dict]:
        where, args = [], []
        for name, value in (("last_seen>=?", since), ("strategy_id=?", strategy_id),
                            ("source_component=?", source_component)):
            if value:
                where.append(name)
                args.append(value)
        sql = ("SELECT * FROM guardian_almost_trades" + (f" WHERE {' AND '.join(where)}" if where else "")
               + " ORDER BY last_seen DESC LIMIT ?")
        args.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = [dict(r) for r in self._c.execute(sql, args)]
        for row in rows:
            row["prevented_by"] = _load(row["prevented_by"])
            row["conditions"] = _load(row["conditions"])
        return rows

    def count_almost_trades(self, *, since: Optional[str] = None) -> int:
        with self._lock:
            return int(self._c.execute(
                "SELECT COUNT(*) FROM guardian_almost_trades" + (" WHERE last_seen>=?" if since else ""),
                (since,) if since else ()).fetchone()[0])

    def event(self, event_id: str) -> Optional[dict]:
        with self._lock:
            row = self._c.execute("SELECT * FROM guardian_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["evidence"], out["metadata"] = _load(out["evidence"]), _load(out["metadata"])
        out.pop("severity_rank", None)
        return out

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
