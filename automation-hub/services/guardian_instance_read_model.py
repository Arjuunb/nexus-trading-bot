"""Bounded query-only export of immutable Trading Instance decision states."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from tradexa.guardian.instance_provenance import project_instance_provenance

MAX_ROWS = 32


def instance_decision_page(path: str | Path, *, after: int = 0,
                           anchor: str = "", limit: int = MAX_ROWS) -> dict:
    if type(after) is not int or after < 0 or type(limit) is not int or \
            not 1 <= limit <= MAX_ROWS or \
            (after == 0 and anchor) or (after > 0 and not anchor):
        raise ValueError("invalid Guardian instance decision cursor")
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("instance decision database is unavailable")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=0.25)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=250")
        connection.execute("BEGIN")
        if after:
            saved = connection.execute(
                "SELECT source_event_id FROM guardian_decision_lifecycle WHERE sequence=?",
                (after,),
            ).fetchone()
            if saved is None or saved["source_event_id"] != anchor:
                raise ValueError("Guardian instance decision source cursor is invalid")
        rows = connection.execute(
            "SELECT * FROM guardian_decision_lifecycle WHERE sequence>? "
            "ORDER BY sequence LIMIT ?", (after, limit + 1),
        ).fetchall()
        # Source-local audit rows survive decision pruning. Resolve at most one
        # PK per exported transition in the SAME bounded read transaction.
        present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='guardian_instance_decision_provenance'").fetchone() is not None
        provenance = {row["decision_id"]: project_instance_provenance(connection.execute(
            "SELECT * FROM guardian_instance_decision_provenance WHERE decision_id=?",
            (row["decision_id"],)).fetchone() if present else None) for row in rows[:limit]}
    page = rows[:limit]

    def project(row: sqlite3.Row) -> dict:
        def rules(column: str) -> list[dict]:
            raw = json.loads(row[column])
            if not isinstance(raw, list) or len(raw) > 128 or not all(
                    isinstance(item, dict) for item in raw):
                raise ValueError("saved instance decision rules are invalid")
            return [{"key": str(item.get("key") or "")[:100],
                     "status": str(item.get("status") or "UNKNOWN")[:32]}
                    for item in raw]

        return {
            "source_sequence": row["sequence"],
            "source_event_id": row["source_event_id"],
            "decision_id": row["decision_id"],
            "decision_identity": row["decision_identity"],
            "instance_id": row["instance_id"],
            "decision_time": row["decision_time"],
            "event_time": row["event_time"],
            "symbol": row["symbol"], "timeframe": row["timeframe"],
            "strategy": row["strategy"], "side": row["side"],
            "strategy_verdict": row["decision"],
            "final_state": row["final_state"],
            "gate_stage": row["gate_stage"],
            "blocker": row["blocker"], "reason": row["reason"],
            "executed": bool(row["executed"]),
            "passed_rules": rules("passed_rules_json"),
            "failed_rules": rules("failed_rules_json"),
            "instance_provenance": provenance[row["decision_id"]],
        }

    return {
        "coverage": "POST_INSTALL_INSTANCE_DECISION_LIFECYCLE",
        "after": after, "anchor": anchor, "has_more": len(rows) > limit,
        "next_after": page[-1]["sequence"] if page else after,
        "next_anchor": page[-1]["source_event_id"] if page else anchor,
        "transitions": [project(row) for row in page],
    }
