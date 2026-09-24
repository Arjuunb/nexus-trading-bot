"""Durable store for EVERY trade decision (accepted AND rejected).

One row per evaluated signal — the unified "decision object" the dashboard
reads. Accepted decisions go on to the risk pipeline; rejected ones never
reach execution. Lives under HUB_DATA_DIR so it survives restarts (and is
covered by the same persistence guidance as the other stores).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from data.tenant_scope import ensure_column, ensure_tenant_column


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class DecisionStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.execute(
            """CREATE TABLE IF NOT EXISTS decisions (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts TEXT NOT NULL,
                   symbol TEXT NOT NULL,
                   timeframe TEXT,
                   strategy TEXT,
                   side TEXT,
                   regime TEXT,
                   htf_bias TEXT,
                   setup_quality_score REAL,
                   volume_score REAL,
                   rr_score REAL,
                   confidence REAL,
                   passed_json TEXT,
                   failed_json TEXT,
                   decision TEXT NOT NULL,      -- immutable strategy-quality verdict
                   reason TEXT,
                   executed INTEGER NOT NULL DEFAULT 0,
                   final_state TEXT NOT NULL DEFAULT '',
                   gate_stage TEXT NOT NULL DEFAULT '',
                   blocker TEXT NOT NULL DEFAULT '',
                   components_json TEXT,
                   instance_id TEXT NOT NULL DEFAULT '',
                   decision_identity TEXT NOT NULL DEFAULT ''
               )""")
        self._c.execute("CREATE INDEX IF NOT EXISTS ix_decisions_ts ON decisions(ts)")
        self._c.execute("CREATE INDEX IF NOT EXISTS ix_decisions_decision ON decisions(decision)")
        ensure_column(self._c, "decisions", "instance_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column(self._c, "decisions", "decision_identity", "TEXT NOT NULL DEFAULT ''")
        ensure_column(self._c, "decisions", "final_state", "TEXT NOT NULL DEFAULT ''")
        ensure_column(self._c, "decisions", "gate_stage", "TEXT NOT NULL DEFAULT ''")
        ensure_column(self._c, "decisions", "blocker", "TEXT NOT NULL DEFAULT ''")
        self._c.execute(
            "UPDATE decisions SET final_state=CASE "
            "WHEN executed=1 THEN 'FILLED' "
            "WHEN decision='rejected' THEN 'GATE_REJECTED' "
            "ELSE 'QUALIFIED' END WHERE final_state=''"
        )
        self._c.execute("CREATE INDEX IF NOT EXISTS ix_decisions_instance_ts ON decisions(instance_id, ts)")
        self._c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_decisions_identity "
            "ON decisions(decision_identity) WHERE decision_identity <> ''")
        ensure_tenant_column(self._c, "decisions")   # Phase C-3: schema-only, additive
        self._c.commit()

    def record(self, d: dict) -> int:
        with self._lock:
            cur = self._c.execute(
                """INSERT INTO decisions
                   (ts, symbol, timeframe, strategy, side, regime, htf_bias,
                    setup_quality_score, volume_score, rr_score, confidence,
                    passed_json, failed_json, decision, reason, executed,
                    final_state, gate_stage, blocker,
                    components_json, instance_id, decision_identity)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(decision_identity) WHERE decision_identity <> '' DO NOTHING""",
                (d.get("ts") or _utcnow(), d["symbol"], d.get("timeframe"),
                 d.get("strategy"), d.get("side"), d.get("regime"),
                 d.get("htf_bias"),
                 d.get("setup_quality_score"), d.get("volume_score"),
                 d.get("rr_score"), d.get("confidence"),
                 json.dumps(d.get("passed_rules") or []),
                 json.dumps(d.get("failed_rules") or []),
                 d["decision"], d.get("reason"),
                 1 if d.get("executed") else 0,
                 d.get("final_state") or ("GATE_REJECTED" if d["decision"] == "rejected" else "QUALIFIED"),
                 d.get("gate_stage") or "", d.get("blocker") or "",
                 json.dumps(d.get("components") or {}), d.get("instance_id") or "",
                 d.get("decision_identity") or ""))
            if cur.rowcount == 0 and d.get("decision_identity"):
                row = self._c.execute(
                    "SELECT id FROM decisions WHERE decision_identity=?",
                    (d["decision_identity"],),
                ).fetchone()
                self._c.commit()
                return int(row["id"])
            self._c.commit()
            return int(cur.lastrowid)

    def mark_executed(self, decision_id: int) -> None:
        with self._lock:
            self._c.execute(
                "UPDATE decisions SET executed=1, final_state='FILLED', "
                "gate_stage='execution', blocker='' WHERE id=?", (decision_id,))
            self._c.commit()

    def finalize(self, decision_id: int, *, final_state: str, gate_stage: str,
                 reason: str, blocker: str = "") -> None:
        """Persist the downstream terminal state without rewriting quality."""
        state = str(final_state or "").strip().upper()
        if state not in {"QUALIFIED", "PENDING_INTENT", "SIGNALS_ONLY",
                         "APPROVAL_REQUIRED", "FILLED", "GATE_REJECTED"}:
            raise ValueError(f"unsupported decision final_state: {final_state}")
        with self._lock:
            self._c.execute(
                "UPDATE decisions SET final_state=?, gate_stage=?, reason=?, blocker=? WHERE id=?",
                (state, str(gate_stage or ""), str(reason or ""),
                 str(blocker or ""), int(decision_id)))
            self._c.commit()

    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["passed_rules"] = json.loads(d.pop("passed_json") or "[]")
        d["failed_rules"] = json.loads(d.pop("failed_json") or "[]")
        d["components"] = json.loads(d.pop("components_json") or "{}")
        d["executed"] = bool(d["executed"])
        return d

    def list(self, *, limit: int = 50, decision: Optional[str] = None,
             symbol: Optional[str] = None, instance_id: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM decisions"
        cond, args = [], []
        if decision:
            cond.append("decision = ?"); args.append(decision)
        if symbol:
            cond.append("symbol = ?"); args.append(symbol.upper())
        if instance_id is not None:
            cond.append("instance_id = ?"); args.append(instance_id)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [self._row(r) for r in self._c.execute(sql, args)]

    def page(self, *, limit: int = 50, before_id: Optional[int] = None,
             decision: Optional[str] = None, symbol: Optional[str] = None,
             since: Optional[str] = None) -> list[dict]:
        """Newest first, for cursor pagination: ``before_id`` is the smallest
        id already seen. ``since`` is an ISO timestamp lower bound."""
        cond, args = [], []
        if before_id:
            cond.append("id < ?"); args.append(int(before_id))
        if decision:
            cond.append("decision = ?"); args.append(decision)
        if symbol:
            cond.append("symbol = ?"); args.append(symbol.upper())
        if since:
            cond.append("ts >= ?"); args.append(since)
        sql = "SELECT * FROM decisions" + (" WHERE " + " AND ".join(cond) if cond else "")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [self._row(r) for r in self._c.execute(sql, args)]

    def after(self, after_id: int, limit: int = 500) -> list[dict]:
        """Decisions with a larger id, oldest first (for event streaming)."""
        with self._lock:
            return [self._row(r) for r in self._c.execute(
                "SELECT * FROM decisions WHERE id > ? ORDER BY id ASC LIMIT ?", (int(after_id), int(limit)))]

    def max_id(self) -> int:
        with self._lock:
            row = self._c.execute("SELECT MAX(id) m FROM decisions").fetchone()
            return int(row["m"] or 0)

    def prune(self, keep: int = 20000) -> int:
        """Retention cap — keep the most recent ``keep`` decisions (by id, which
        is chronological), delete older. Bounds growth on a persistent disk."""
        with self._lock:
            cur = self._c.execute(
                "DELETE FROM decisions WHERE id NOT IN "
                "(SELECT id FROM decisions ORDER BY id DESC LIMIT ?)", (int(keep),))
            self._c.commit()
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def count(self) -> int:
        with self._lock:
            return int(self._c.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"])

    def get(self, decision_id: int) -> Optional[dict]:
        with self._lock:
            r = self._c.execute("SELECT * FROM decisions WHERE id=?",
                                (decision_id,)).fetchone()
            return self._row(r) if r else None
