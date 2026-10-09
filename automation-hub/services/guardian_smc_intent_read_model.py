"""Query-only paging of retained Agent transitions with frozen parent identity."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

from tradexa.guardian.smc_intent_history import (
    PAGE_SIZE, cursor_anchor, project_transition, source_origin, validate_cursor,
)

_EVENT_FIELDS = ("id", "execution_key", "state", "broker_order_id", "trade_id", "created_at")
_PARENT_FIELDS = ("session_id", "symbol", "timeframe", "candle_time", "proposal_id")
_SELECT = ",".join(("e.rowid AS source_sequence", *(
    f"substr(e.{name},1,257) AS {name}" for name in _EVENT_FIELDS), *(
    f"substr(i.{name},1,257) AS {name}" for name in _PARENT_FIELDS),
    "substr(i.id,1,257) AS intent_id",
    "CASE WHEN e.error IS NOT NULL AND e.error<>'' THEN 1 ELSE 0 END AS error_recorded"))
_FROM = "execution_intent_events e LEFT JOIN execution_intents i ON i.execution_key=e.execution_key"


def smc_intent_event_page(path: str | Path, *, after=0, anchor=""):
    validate_cursor(after, anchor)
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("Intent history source missing")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        deadline = monotonic() + .5
        db.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        db.execute("BEGIN")

        def project(row):
            if row is None:
                return None
            value = dict(row)
            value["error_recorded"] = bool(value["error_recorded"])
            return project_transition(value)

        first = project(db.execute(f"SELECT {_SELECT} FROM {_FROM} ORDER BY e.rowid LIMIT 1").fetchone())
        previous = project(db.execute(f"SELECT {_SELECT} FROM {_FROM} WHERE e.rowid=?", (after,)).fetchone()) if after else None
        if after and (not first or not previous or cursor_anchor(first, previous) != anchor):
            raise ValueError("Intent history source cursor changed")
        rows = db.execute(f"SELECT {_SELECT} FROM {_FROM} WHERE e.rowid>? ORDER BY e.rowid LIMIT ?",
                          (after, PAGE_SIZE + 1)).fetchall()
        transitions = [project(row) for row in rows[:PAGE_SIZE]]
    return {"after": after, "anchor": anchor, "origin": source_origin(first), "atomic_snapshot": True,
            "first_transition": first, "previous_transition": previous, "transitions": transitions,
            "has_more": len(rows) > PAGE_SIZE,
            "next_after": transitions[-1]["source_sequence"] if transitions else after,
            "next_anchor": cursor_anchor(first, transitions[-1]) if transitions else anchor}
