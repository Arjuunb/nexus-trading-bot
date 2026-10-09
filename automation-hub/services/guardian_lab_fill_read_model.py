"""Query-only keyset paging of retained fills; no broker/runtime construction."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

from tradexa.guardian.lab_fill_history import (
    FIELDS, LABS, MAX_SEQUENCE, PAGE_SIZE, TEXT_FIELDS, TIME_FIELDS, cursor_anchor, identity, project_fill,
)

# Bound even corrupted oversized SQL strings before allocating a Python row.
_SELECT = "rowid AS source_sequence," + ",".join(
    f"substr({key},1,257) AS {key}" if key in TEXT_FIELDS + TIME_FIELDS else key for key in FIELDS)


def lab_fill_page(path: str | Path, lab: str, *, after=0, anchor="", limit=PAGE_SIZE):
    if (lab not in LABS or type(after) is not int or not 0 <= after <= MAX_SEQUENCE or
            type(limit) is not int or not 1 <= limit <= PAGE_SIZE or
            not isinstance(anchor, str) or len(anchor) > 64 or bool(after) != bool(anchor)):
        raise ValueError("Invalid fill history cursor")
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("Fill history source missing")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        deadline = monotonic() + .5
        db.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        db.execute("BEGIN")
        account = db.execute("SELECT substr(account_id,1,257) AS account_id,"
                             "substr(account_type,1,257) AS account_type FROM v2_account WHERE id=1").fetchone()
        if not account or account["account_type"] != LABS[lab]:
            raise ValueError("Isolated paper account unavailable")
        account_id = identity(account["account_id"])

        def project(row):
            return project_fill(dict(row), lab, account_id) if row is not None else None

        first = project(db.execute(f"SELECT {_SELECT} FROM v2_fills ORDER BY rowid LIMIT 1").fetchone())
        previous = project(db.execute(f"SELECT {_SELECT} FROM v2_fills WHERE rowid=?", (after,)).fetchone()) if after else None
        if after and (not first or not previous or
                      cursor_anchor(lab, account_id, first, previous) != anchor):
            raise ValueError("Fill history source cursor changed")
        rows = db.execute(f"SELECT {_SELECT} FROM v2_fills WHERE rowid>? ORDER BY rowid LIMIT ?",
                          (after, limit + 1)).fetchall()
        fills = [project(row) for row in rows[:limit]]
    return {"lab": lab, "account_id": account_id, "account_type": LABS[lab],
            "atomic_snapshot": True, "after": after, "anchor": anchor, "first_fill": first,
            "previous_fill": previous, "fills": fills, "has_more": len(rows) > limit,
            "next_after": fills[-1]["source_sequence"] if fills else after,
            "next_anchor": cursor_anchor(lab, account_id, first, fills[-1]) if fills else anchor}
