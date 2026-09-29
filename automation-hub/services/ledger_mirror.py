"""A local, read-only copy of the Supabase ledger tables the journal reads.

The journal recorder reads the ledger with SQL: trades, their fills,
executions and lifecycle logs. In production the ledger is Supabase, which
it cannot query that way, so it skipped it -- and no Trading Instance trade
reached the Journal. This mirror copies those four tables from Supabase into
a local SQLite file with the ledger's own schema, and the recorder reads the
copy with the same SQL it already reads a local ledger with.

It never writes to Supabase. Each sync is incremental: rows at or after each
table's last seen timestamp (with a short overlap, so a row written late in
the same second is not missed), plus a re-read of every row still in a
non-final state (a claimed or pending order), so a promotion or a released
claim is reflected. Rows deleted remotely in a final state are kept: the
journal never loses history because of a reset upstream.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Protocol

from data.ledger import SqliteLedger, remote_call_with_retry

_ENGINE_LOGS_DDL = ("CREATE TABLE IF NOT EXISTS instance_engine_logs ("
                    "id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, ts TEXT NOT NULL, "
                    "level TEXT NOT NULL, message TEXT NOT NULL)")

#: table -> primary key, timestamp cursors, (status column, non-final states)
TABLES: dict[str, dict] = {
    "paper_trades": {"key": "id", "cursors": ("opened_at", "closed_at"), "revisit": None},
    "paper_executions": {"key": "execution_id", "cursors": ("created_at",), "revisit": None},
    "webhook_events": {"key": "id", "cursors": ("received_at",),
                       "revisit": ("status", ("claimed", "pending"))},
    "instance_engine_logs": {"key": "id", "cursors": ("ts",), "revisit": None},
}


class RemoteTables(Protocol):
    def page(self, table: str, *, order: str, since: Optional[str], offset: int,
             limit: int) -> list[dict]: ...

    def by_ids(self, table: str, key: str, ids: list[str]) -> list[dict]: ...


class PostgrestTables:
    """The Supabase ledger's tables, read through PostgREST. Reads only."""

    def __init__(self, supabase_ledger):
        self._ledger = supabase_ledger

    def page(self, table, *, order, since, offset, limit):
        def query():
            q = self._ledger._t(table).select("*")
            if since:
                q = q.gte(order, since)
            return q.order(order).range(offset, offset + limit - 1).execute()
        return list(remote_call_with_retry(query).data or [])

    def by_ids(self, table, key, ids):
        if not ids:
            return []
        return list(remote_call_with_retry(
            lambda: self._ledger._t(table).select("*").in_(key, ids).execute()).data or [])


def _shift(stamp: str, seconds: float) -> str:
    try:
        return (datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                - timedelta(seconds=seconds)).isoformat()
    except ValueError:
        return stamp


class LedgerMirror:
    def __init__(self, remote: RemoteTables, path: str | Path, *, overlap_s: float = 600.0,
                 page_size: int = 1000):
        self.remote = remote
        self.ledger = SqliteLedger(path)        # the ledger's own schema and migrations
        self.overlap_s = float(overlap_s)
        self.page_size = int(page_size)
        self._sync_lock = threading.Lock()
        with self.ledger._lock:
            self.ledger._c.execute(_ENGINE_LOGS_DDL)
            self.ledger._c.execute("CREATE TABLE IF NOT EXISTS mirror_state "
                                   "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.ledger._c.commit()
        self.last_sync: Optional[dict] = None

    # -------------------------------------------------------------- state
    def _state(self, key: str) -> Optional[str]:
        row = self.ledger._c.execute("SELECT value FROM mirror_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_state(self, key: str, value: str) -> None:
        self.ledger._c.execute("INSERT OR REPLACE INTO mirror_state(key,value) VALUES (?,?)", (key, value))

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self.ledger._c.execute(f"PRAGMA table_info({table})")}

    def _upsert(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        columns = self._columns(table)
        for name in sorted({k for row in rows for k in row} - columns):
            # A column the remote has and the local schema does not: keep it.
            self.ledger._c.execute(f'ALTER TABLE {table} ADD COLUMN "{name}"')
            columns.add(name)
        for row in rows:
            keys = [k for k in row if k in columns]
            self.ledger._c.execute(
                f"INSERT OR REPLACE INTO {table}({','.join(chr(34) + k + chr(34) for k in keys)}) "
                f"VALUES ({','.join('?' for _ in keys)})",
                [json.dumps(row[k]) if isinstance(row[k], (dict, list)) else row[k] for k in keys])
        return len(rows)

    # --------------------------------------------------------------- sync
    def sync(self) -> dict:
        """Bring the copy up to date. Safe to call at any time; one at a time."""
        report: dict = {"tables": {}, "errors": []}
        with self._sync_lock, self.ledger._lock:
            for table, spec in TABLES.items():
                copied = revisited = removed = 0
                try:
                    for cursor in spec["cursors"]:
                        mark_key = f"{table}.{cursor}"
                        mark = self._state(mark_key)
                        since = _shift(mark, self.overlap_s) if mark else None
                        offset, newest = 0, mark
                        while True:
                            rows = self.remote.page(table, order=cursor, since=since,
                                                    offset=offset, limit=self.page_size)
                            copied += self._upsert(table, rows)
                            stamps = [str(r[cursor]) for r in rows if r.get(cursor)]
                            if stamps:
                                newest = max([newest or "", *stamps])
                            if len(rows) < self.page_size:
                                break
                            offset += len(rows)
                        if newest:
                            self._set_state(mark_key, newest)
                    if spec["revisit"]:
                        column, states = spec["revisit"]
                        key = spec["key"]
                        open_ids = [r[0] for r in self.ledger._c.execute(
                            f"SELECT {key} FROM {table} WHERE {column} IN "
                            f"({','.join('?' for _ in states)})", states)]
                        for start in range(0, len(open_ids), 200):
                            chunk = open_ids[start:start + 200]
                            fresh = self.remote.by_ids(table, key, chunk)
                            revisited += self._upsert(table, fresh)
                            gone = set(chunk) - {str(r[key]) for r in fresh}
                            for dead in gone:           # a released claim no longer exists
                                self.ledger._c.execute(f"DELETE FROM {table} WHERE {key}=?", (dead,))
                            removed += len(gone)
                    self.ledger._c.commit()
                except Exception as exc:  # noqa: BLE001 -- one table cannot stop the others
                    self.ledger._c.rollback()
                    report["errors"].append(f"{table}: {type(exc).__name__}: {exc}"[:300])
                report["tables"][table] = {"copied": copied, "revisited": revisited, "removed": removed}
            self.last_sync = report
        if report["errors"]:
            raise RuntimeError("ledger mirror: " + "; ".join(report["errors"]))
        return report

    def counts(self) -> dict:
        with self.ledger._lock:
            try:
                return {t: int(self.ledger._c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                        for t in TABLES}
            except sqlite3.Error:
                return {}
