"""Read-only bounded scans; never construct the Agent journal or its broker."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

from tradexa.guardian.smc_journal_history import (
    FIELDS, INITIAL_CURSOR, PAGE_SIZE, TEXT_FIELDS, TIME_FIELDS,
    next_scan_cursor, project_trade, scan_anchor, source_origin, validate_cursor,
)

_SELECT = "rowid AS source_sequence," + ",".join(
    f"substr({key},1,257) AS {key}" if key in TEXT_FIELDS + TIME_FIELDS else key for key in FIELDS)


def smc_journal_page(path: str | Path, *, cursor=None):
    cursor = validate_cursor(INITIAL_CURSOR if cursor is None else cursor)
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("Journal source missing")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        deadline = monotonic() + .5
        db.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        db.execute("BEGIN")

        def project(row):
            return project_trade(dict(row)) if row is not None else None

        first = project(db.execute(f"SELECT {_SELECT} FROM agent_trades ORDER BY rowid LIMIT 1").fetchone())
        origin = source_origin(first)
        if cursor["origin"] and origin != cursor["origin"]:
            raise ValueError("Journal source origin changed")
        if cursor["after"]:
            upper = project(db.execute(f"SELECT {_SELECT} FROM agent_trades WHERE rowid=?", (cursor["upper"],)).fetchone())
            previous = project(db.execute(f"SELECT {_SELECT} FROM agent_trades WHERE rowid=?", (cursor["after"],)).fetchone())
            if not upper or not previous or scan_anchor(origin, upper, previous) != cursor["anchor"]:
                raise ValueError("Journal scan anchor changed")
        else:
            upper = project(db.execute(f"SELECT {_SELECT} FROM agent_trades ORDER BY rowid DESC LIMIT 1").fetchone())
            previous = None
        rows = db.execute(f"SELECT {_SELECT} FROM agent_trades WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?",
                          (cursor["after"], upper["source_sequence"] if upper else 0, PAGE_SIZE + 1)).fetchall()
        trades = [project(row) for row in rows[:PAGE_SIZE]]
        has_more = len(rows) > PAGE_SIZE
        next_cursor = next_scan_cursor(cursor, origin, upper, trades, has_more)
    return {"cursor": cursor, "origin": origin, "atomic_snapshot": True,
            "first_trade": first, "upper_trade": upper, "previous_trade": previous,
            "trades": trades, "has_more": has_more, "next_cursor": next_cursor}
