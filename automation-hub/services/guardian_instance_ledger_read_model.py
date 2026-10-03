"""Bounded, read-only projection of the PRIMARY Trading Instance paper ledger.

SQLite reads one transaction. Supabase/PostgREST requires separate requests,
so even matching rows there remain a racing observation, never a confirmed
execution or live-venue integrity verdict.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from data.ledger import SqliteLedger, SupabaseLedger, remote_call_with_retry
from tradexa.guardian.instance_ledger_integrity import (
    MAX_OPEN_ROWS, reconcile_instance_paper_ledger)


def _bounded(rows) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > MAX_OPEN_ROWS or \
            not all(isinstance(row, dict) for row in rows):
        raise ValueError("instance paper-ledger source coverage exceeded")
    return rows


def _sqlite_snapshot(ledger: SqliteLedger) -> dict:
    path = Path(ledger.path)
    if not path.is_file():
        raise sqlite3.OperationalError("instance paper ledger is unavailable")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=0.25)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=250")
        conn.execute("BEGIN")
        positions = _bounded([dict(row) for row in conn.execute(
            "SELECT id,instance_id,simulation_session_id,status,symbol,side,size,entry,stop "
            "FROM positions WHERE status='open' AND instance_id<>'' LIMIT ?",
            (MAX_OPEN_ROWS + 1,))])
        trades = _bounded([dict(row) for row in conn.execute(
            "SELECT id,instance_id,simulation_session_id,status,source,symbol,side,size,entry "
            "FROM paper_trades WHERE status='open' AND instance_id<>'' LIMIT ?",
            (MAX_OPEN_ROWS + 1,))])
        if positions:
            placeholders = ",".join("?" for _ in positions)
            executions = _bounded([dict(row) for row in conn.execute(
                "SELECT execution_id,action,position_id,trade_id,instance_id "
                "FROM paper_executions WHERE action IN ('OPEN','REDUCE') "
                f"AND position_id IN ({placeholders}) LIMIT ?",
                [*[row["id"] for row in positions], MAX_OPEN_ROWS + 1])])
        else:
            executions = []
    result = reconcile_instance_paper_ledger(
        positions, trades, executions, atomic_snapshot=True)
    result["source_coverage_verified"] = True
    return result


def _remote_snapshot(ledger: SupabaseLedger) -> dict:
    def fetch(table: str, fields: str):
        return _bounded(remote_call_with_retry(lambda: ledger._t(table).select(fields)
                          .eq("status", "open").neq("instance_id", "")
                          .limit(MAX_OPEN_ROWS + 1).execute()).data)

    positions = fetch("positions", "id,instance_id,simulation_session_id,status,symbol,side,size,entry,stop")
    trades = fetch("paper_trades", "id,instance_id,simulation_session_id,status,source,symbol,side,size,entry")
    if positions:
        position_ids = [row["id"] for row in positions]
        executions = _bounded(remote_call_with_retry(
            lambda: ledger._t("paper_executions").select(
                "execution_id,action,position_id,trade_id,instance_id")
            .in_("position_id", position_ids).in_("action", ["OPEN", "REDUCE"])
            .limit(MAX_OPEN_ROWS + 1).execute()).data)
    else:
        executions = []
    result = reconcile_instance_paper_ledger(
        positions, trades, executions, atomic_snapshot=False)
    # Individual PostgREST responses may race one another. A successful,
    # untruncated page is not a serializable cross-table snapshot.
    result["source_coverage_verified"] = False
    return result


def instance_paper_ledger_snapshot(ledger: object) -> dict:
    if isinstance(ledger, SqliteLedger):
        return _sqlite_snapshot(ledger)
    if isinstance(ledger, SupabaseLedger):
        return _remote_snapshot(ledger)
    raise ValueError("primary instance paper ledger is unavailable")
