"""Durable store for per-cycle Decision Reports — every analysis cycle is
recorded, INCLUDING the ones where nothing happened (WAIT). The bot never
trades or skips silently; this is the paper trail that proves it.

Bounded by ``keep``: old cycles are pruned so a 5m × 3-symbol engine
(~860 cycles/day) can run forever on a small disk.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from data.tenant_scope import ensure_column, ensure_tenant_column


class CycleStore:
    def __init__(self, path: str = ":memory:", keep: int = 5000) -> None:
        self.path = str(path)
        self.keep = int(keep)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(self.path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._c.executescript("""
            CREATE TABLE IF NOT EXISTS cycle_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT,
                price REAL,
                decision TEXT NOT NULL,     -- BUY | SELL | WAIT | SKIP
                score INTEGER,
                report_json TEXT,
                instance_id TEXT NOT NULL DEFAULT '',
                strategy_version TEXT NOT NULL DEFAULT '',
                decision_identity TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_cycles_ts ON cycle_reports(ts);
            CREATE INDEX IF NOT EXISTS ix_cycles_symbol ON cycle_reports(symbol);
            """)
            ensure_tenant_column(self._c, "cycle_reports")   # Phase C-3: schema-only, additive
            ensure_column(self._c, "cycle_reports", "instance_id", "TEXT NOT NULL DEFAULT ''")
            ensure_column(self._c, "cycle_reports", "strategy_version", "TEXT NOT NULL DEFAULT ''")
            ensure_column(self._c, "cycle_reports", "decision_identity", "TEXT NOT NULL DEFAULT ''")
            self._c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_cycles_decision_identity "
                "ON cycle_reports(decision_identity) WHERE decision_identity <> ''")
            self._c.commit()

    def record(self, report: dict) -> int:
        with self._lock:
            cur = self._c.execute(
                """INSERT INTO cycle_reports
                   (ts, symbol, timeframe, price, decision, score, report_json,
                    instance_id, strategy_version, decision_identity)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(decision_identity) WHERE decision_identity <> '' DO NOTHING""",
                (report.get("ts"), report.get("symbol"), report.get("timeframe"),
                 report.get("price"), report.get("decision"), report.get("score"),
                 json.dumps(report), report.get("instance_id") or "",
                 report.get("strategy_version") or "",
                 report.get("decision_identity") or ""))
            if cur.rowcount == 0 and report.get("decision_identity"):
                row = self._c.execute(
                    "SELECT id FROM cycle_reports WHERE decision_identity=?",
                    (report["decision_identity"],),
                ).fetchone()
                self._c.commit()
                return int(row["id"])
            # prune beyond the retention cap (cheap: only when we crossed it)
            self._c.execute(
                "DELETE FROM cycle_reports WHERE id <= "
                "(SELECT MAX(id) FROM cycle_reports) - ?", (self.keep,))
            self._c.commit()
            return int(cur.lastrowid)

    def _row(self, r: sqlite3.Row, full: bool) -> dict:
        d = dict(r)
        report = json.loads(d.pop("report_json") or "{}")
        if full:
            d["report"] = report
        return d

    def list(self, *, limit: int = 100, symbol: Optional[str] = None,
             decision: Optional[str] = None, instance_id: Optional[str] = None,
             full: bool = False) -> list[dict]:
        sql = "SELECT * FROM cycle_reports"
        cond, args = [], []
        if symbol:
            cond.append("symbol = ?"); args.append(symbol.upper())
        if decision:
            cond.append("decision = ?"); args.append(decision.upper())
        if instance_id:
            cond.append("instance_id = ?"); args.append(instance_id)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [self._row(r, full=full) for r in self._c.execute(sql, args)]

    def get(self, cid: int) -> Optional[dict]:
        with self._lock:
            r = self._c.execute("SELECT * FROM cycle_reports WHERE id=?", (cid,)).fetchone()
            return self._row(r, full=True) if r else None

    def count(self) -> int:
        with self._lock:
            return int(self._c.execute("SELECT COUNT(*) c FROM cycle_reports").fetchone()["c"])
