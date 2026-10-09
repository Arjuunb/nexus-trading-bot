"""Query-only export of existing stop rows; no Agent, broker or migrations."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

from tradexa.guardian.smc_stop_moves import (
    PAGE_SIZE, TEXT_FIELDS, cursor_anchor, project_move, source_origin, validate_cursor)

_SELECT = ",".join(("rowid AS source_sequence", *(
    f"substr({name},1,257) AS {name}" for name in TEXT_FIELDS),
    "from_price", "to_price", "progress_r", "applied",
    "CASE WHEN error IS NOT NULL AND error<>'' THEN 1 ELSE 0 END AS error_recorded",
    "CASE WHEN reason IS NOT NULL AND reason<>'' THEN 1 ELSE 0 END AS reason_recorded"))


def smc_stop_move_page(path: str | Path, *, after=0, anchor=""):
    validate_cursor(after, anchor)
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("Stop history source missing")
    with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        deadline = monotonic()+.5
        db.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        db.execute("BEGIN")

        def project(row):
            if row is None:
                return None
            value = dict(row)
            for key in ("applied", "error_recorded", "reason_recorded"):
                if type(value[key]) is not int or value[key] not in (0, 1):
                    raise ValueError("Invalid recorded stop flag")
                value[key] = bool(value[key])
            return project_move(value)

        first = project(db.execute(f"SELECT {_SELECT} FROM agent_stop_moves ORDER BY rowid LIMIT 1").fetchone())
        previous = project(db.execute(f"SELECT {_SELECT} FROM agent_stop_moves WHERE rowid=?", (after,)).fetchone()) if after else None
        if after and (not first or not previous or cursor_anchor(first, previous) != anchor):
            raise ValueError("Stop history source cursor changed")
        rows = db.execute(f"SELECT {_SELECT} FROM agent_stop_moves WHERE rowid>? ORDER BY rowid LIMIT ?",
                          (after, PAGE_SIZE+1)).fetchall()
        moves = [project(row) for row in rows[:PAGE_SIZE]]
    return {"after": after, "anchor": anchor, "origin": source_origin(first), "atomic_snapshot": True,
            "first_move": first, "previous_move": previous, "moves": moves, "has_more": len(rows) > PAGE_SIZE,
            "next_after": moves[-1]["source_sequence"] if moves else after,
            "next_anchor": cursor_anchor(first, moves[-1]) if moves else anchor}
