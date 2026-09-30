"""Bounded, read-only projection of saved lab decisions for Guardian.

This is evidence export, not a strategy evaluation or an execution path. It
reads committed SQLite rows through a separate query-only connection and
never invokes a lab runtime, broker, market provider, or trading gate.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

LAB_TABLES = {
    "PRICE_ACTION": ("pa_sessions", "pa_evaluations"),
    "SMC": ("smc_sessions", "smc_evaluations"),
}
MAX_ROWS = 32
BACKFILL_PAGE_ROWS = 32


def _list(value, name: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"saved {name} is not a list")
    if len(value) > 128:
        raise ValueError(f"saved {name} exceeds the observer bound")
    return value


def _project_row(row: sqlite3.Row, lab: str) -> dict:
    payload = json.loads(row["payload_json"] or "{}")
    if not isinstance(payload, dict):
        raise ValueError("saved evaluation payload is not an object")
    source = payload.get("source_evaluation") if lab == "SMC" else payload.get("trace")
    if source is not None and not isinstance(source, dict):
        raise ValueError("saved evaluation trace is not an object")
    source = source or {}
    raw_conditions = source.get("ordered_condition_results") if lab == "SMC" else source.get("conditions")
    conditions = []
    for condition in _list(raw_conditions or [], "conditions"):
        if not isinstance(condition, dict):
            raise ValueError("saved condition is not an object")
        conditions.append({
            "key": str(condition.get("key") or "")[:80],
            "status": str(condition.get("status") or "UNKNOWN")[:32],
        })
    missing = [str(value)[:160] for value in
               _list(json.loads(row["missing_conditions_json"] or "[]"), "missing conditions")]
    return {
        "correlation_id": row["correlation_id"],
        "session_id": row["session_id"],
        "candle_time": row["candle_time"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "strategy_id": row["strategy_id"],
        "strategy_version": row["strategy_version"],
        "model_id": row["model_id"] if lab == "SMC" else None,
        "state": row["state"],
        "reason": str(row["reason"])[:500],
        "missing_conditions": missing,
        "conditions": conditions,
        "condition_trace_available": bool(raw_conditions),
    }


def lab_decision_snapshot(path: str | Path, lab: str, *, limit: int = MAX_ROWS) -> dict:
    """Return only the newest saved decisions for one active lab session.

    This intentionally does not claim full-history coverage or feed health.
    A missing database/session is UNKNOWN; corrupt or locked evidence raises.
    """
    if lab not in LAB_TABLES or not 1 <= limit <= MAX_ROWS:
        raise ValueError("invalid Guardian lab observation request")
    source = Path(path)
    if not source.is_file():
        return {"lab": lab, "state": "UNKNOWN", "reason": "SOURCE_DB_MISSING",
                "session_id": None, "coverage": "LATEST_ACTIVE_SESSION_ONLY", "evaluations": []}
    sessions, evaluations = LAB_TABLES[lab]
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=0.25)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=250")
        connection.execute("BEGIN")
        session = connection.execute(
            f"SELECT id FROM {sessions} WHERE status='active' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if session is None:
            return {"lab": lab, "state": "UNKNOWN", "reason": "NO_ACTIVE_SESSION",
                    "session_id": None, "coverage": "LATEST_ACTIVE_SESSION_ONLY", "evaluations": []}
        rows = connection.execute(
            f"SELECT correlation_id,session_id,candle_time,created_at,updated_at,"
            f"symbol,timeframe,strategy_id,strategy_version,"
            + ("model_id," if lab == "SMC" else "")
            + f"state,reason,missing_conditions_json,payload_json FROM {evaluations} "
              "WHERE session_id=? ORDER BY candle_time DESC LIMIT ?",
            (session["id"], limit),
        ).fetchall()
    return {"lab": lab, "state": "OBSERVED", "reason": "BOUNDED_SAVED_DECISIONS",
            "session_id": session["id"], "coverage": "LATEST_ACTIVE_SESSION_ONLY",
            "limit": limit, "evaluations": [_project_row(row, lab) for row in rows]}


def lab_decision_page(path: str | Path, lab: str, *, after: int = 0,
                      anchor: str = "", limit: int = BACKFILL_PAGE_ROWS) -> dict:
    """Page all committed evaluation identities without reading trading runtime state.

    The rowid cursor is valid only while the source table retains the last
    consumed row. The anchor check fails closed on a reset, deletion of that
    row, or a VACUUM that moves it; operators must not silently reset cursors.
    Evaluation rows can later change state, so this is *decision existence*
    coverage, not an immutable lifecycle-event stream.
    """
    if lab not in LAB_TABLES or type(after) is not int or after < 0 or \
            not 1 <= limit <= BACKFILL_PAGE_ROWS or \
            (after == 0 and anchor) or (after > 0 and not anchor):
        raise ValueError("invalid Guardian decision cursor")
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("source evidence database is unavailable")
    evaluations = LAB_TABLES[lab][1]
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=0.25)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=250")
        connection.execute("BEGIN")
        if after:
            saved = connection.execute(
                f"SELECT correlation_id FROM {evaluations} WHERE rowid=?", (after,)
            ).fetchone()
            if saved is None or saved["correlation_id"] != anchor:
                raise ValueError("Guardian decision source cursor is invalid")
        rows = connection.execute(
            f"SELECT rowid AS source_sequence,correlation_id,session_id,candle_time,"
            f"created_at,updated_at,symbol,timeframe,strategy_id,strategy_version,"
            + ("model_id," if lab == "SMC" else "")
            + f"state,reason,missing_conditions_json,payload_json FROM {evaluations} "
              "WHERE rowid>? ORDER BY rowid LIMIT ?",
            (after, limit + 1),
        ).fetchall()
    page = rows[:limit]
    return {
        "lab": lab, "coverage": "ALL_RETAINED_EVALUATION_IDENTITIES",
        "after": after, "anchor": anchor, "has_more": len(rows) > limit,
        "next_after": page[-1]["source_sequence"] if page else after,
        "next_anchor": page[-1]["correlation_id"] if page else anchor,
        "evaluations": [{"source_sequence": row["source_sequence"],
                         **_project_row(row, lab)} for row in page],
    }
