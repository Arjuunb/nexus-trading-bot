"""Bounded query-only source provenance, never broker/journal reconciliation."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

from execution.paper_fill_provenance import MAX_BYTES, decode_transition, _number, _text
from tradexa.guardian.events import _safe_json
from tradexa.guardian.lab_fill_history import digest
from tradexa.guardian.lab_observer import _timestamp
from tradexa.guardian.smc_intent_history import validate_cursor

PAGE_SIZE = 32
SCOPE = "RETAINED_SMC_PAPER_FILL_POSITION_TRANSITIONS"


def cursor_anchor(account_id, first, previous):
    return digest(["smc-fill-position-cursor-v1", account_id, first, previous])


def smc_fill_transition_page(path: str | Path, *, after=0, anchor="", limit=PAGE_SIZE):
    validate_cursor(after, anchor)
    if type(limit) is not int or not 1 <= limit <= PAGE_SIZE:
        raise ValueError("Invalid fill transition page size")
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("Fill transition source missing")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        deadline = monotonic() + .5
        db.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        db.execute("BEGIN")
        account = db.execute("SELECT substr(account_id,1,257) AS account_id,account_type FROM v2_account WHERE id=1").fetchone()
        if not account or account["account_type"] != "SMC_LAB":
            raise ValueError("Source is not an isolated SMC account")
        account_id = account["account_id"]
        _text(account_id)
        columns = {r["name"] for r in db.execute("PRAGMA table_info(v2_fills)")}
        payload = ("length(CAST(fill_position_json AS BLOB)) AS bytes,substr(fill_position_json,1,8193) AS payload"
                   if "fill_position_json" in columns else "NULL AS bytes,NULL AS payload")
        select = "rowid AS source_sequence," + ",".join(
            f"substr({k},1,257) AS {k}" for k in ("id", "order_id", "symbol", "side", "timestamp", "account_id", "execution_engine"))
        select += ",quantity,price," + payload

        def project(row):
            if row is None:
                return None
            for key in ("id", "order_id", "symbol", "account_id"):
                _text(row[key])
            if row["account_id"] != account_id or row["execution_engine"] != "SMC_LAB" or row["side"] not in {"buy", "sell"}:
                raise ValueError("Fill source account or side mismatch")
            _number(row["quantity"])
            _number(row["price"])
            try:
                timestamp = _timestamp(row["timestamp"]).isoformat()
            except OverflowError as exc:
                raise ValueError("Fill timestamp exceeds bound") from exc
            if row["bytes"] is not None and row["bytes"] > MAX_BYTES:
                raise ValueError("Fill transition exceeds bound")
            transition = decode_transition(row["payload"]) if row["payload"] is not None else None
            if transition is not None:
                if any(transition[k] != row[k] for k in ("order_id", "symbol", "side", "quantity", "price", "account_id")) or transition["fill_id"] != row["id"]:
                    raise ValueError("Fill transition identity disagrees with fill")
                _safe_json(transition)
            return _safe_json({"source_sequence": row["source_sequence"], "fill_id": row["id"],
                               "order_id": row["order_id"], "timestamp": timestamp,
                               "symbol": row["symbol"], "side": row["side"], "quantity": row["quantity"], "price": row["price"],
                               "transition": transition, "capture_state": "RECORDED_SOURCE_TRANSITION" if transition is not None else "UNVERIFIED_LEGACY_FILL"})

        first = project(db.execute(f"SELECT {select} FROM v2_fills ORDER BY rowid LIMIT 1").fetchone())
        previous = project(db.execute(f"SELECT {select} FROM v2_fills WHERE rowid=?", (after,)).fetchone()) if after else None
        if after and (not first or not previous or cursor_anchor(account_id, first, previous) != anchor):
            raise ValueError("Fill transition source cursor changed")
        rows = db.execute(f"SELECT {select} FROM v2_fills WHERE rowid>? ORDER BY rowid LIMIT ?", (after, limit + 1)).fetchall()
        fills = [project(row) for row in rows[:limit]]
    return {"account_id": account_id, "account_type": "SMC_LAB", "atomic_snapshot": True,
            "source_capture_post_install_only": True, "full_lifecycle_verified": False,
            "after": after, "anchor": anchor, "first_fill": first, "previous_fill": previous,
            "fills": fills, "has_more": len(rows) > limit,
            "next_after": fills[-1]["source_sequence"] if fills else after,
            "next_anchor": cursor_anchor(account_id, first, fills[-1]) if fills else anchor}
