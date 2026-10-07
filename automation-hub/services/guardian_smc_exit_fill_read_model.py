"""Bounded query-only export of retained SMC paper exit-fill facts.

Never opens the broker, installs schema, reconstructs history or queries a
journal. A null record cannot establish whether a fill was an entry or exit.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

from execution.paper_exit_provenance import MAX_BYTES, decode_exit_fill
from execution.paper_fill_provenance import decode_transition, _number, _text
from tradexa.guardian.smc_exit_fills import SCOPE, PAGE_SIZE, cursor_anchor, project_exit_fill
from tradexa.guardian.smc_intent_history import validate_cursor


def smc_exit_fill_page(path: str | Path, *, after=0, anchor="", limit=PAGE_SIZE):
    validate_cursor(after, anchor)
    if type(limit) is not int or not 1 <= limit <= PAGE_SIZE:
        raise ValueError("Invalid exit evidence page size")
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("Exit evidence source missing")
    with closing(sqlite3.connect(source.resolve().as_uri()+"?mode=ro", uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        deadline = monotonic()+.5
        db.set_progress_handler(lambda: int(monotonic()>deadline), 1000)
        db.execute("BEGIN")
        account = db.execute("SELECT substr(account_id,1,257) AS account_id,account_type FROM v2_account WHERE id=1").fetchone()
        if not account or account["account_type"] != "SMC_LAB":
            raise ValueError("Source is not an isolated SMC account")
        account_id = account["account_id"]
        _text(account_id)
        columns = {r["name"] for r in db.execute("PRAGMA table_info(v2_fills)")}
        payloads = []
        for column, prefix in (("fill_exit_json", "exit"), ("fill_position_json", "position")):
            payloads.append(f"length(CAST({column} AS BLOB)) AS {prefix}_bytes,substr({column},1,8193) AS {prefix}_payload"
                            if column in columns else f"NULL AS {prefix}_bytes,NULL AS {prefix}_payload")
        select = "rowid AS source_sequence," + ",".join(
            f"substr({key},1,257) AS {key}" for key in ("id", "order_id", "symbol", "side", "timestamp", "account_id", "execution_engine"))
        select += ",quantity,price," + ",".join(payloads)

        def project(row):
            if row is None:
                return None
            for key in ("id", "order_id", "symbol", "account_id"):
                _text(row[key])
            if row["account_id"] != account_id or row["execution_engine"] != "SMC_LAB":
                raise ValueError("Exit source account or engine mismatch")
            _number(row["quantity"])
            _number(row["price"])
            value = None
            if row["exit_payload"] is not None:
                if row["exit_bytes"] > MAX_BYTES or row["position_bytes"] is None or row["position_bytes"] > MAX_BYTES:
                    raise ValueError("Exit evidence bound or original position evidence unavailable")
                value = decode_exit_fill(row["exit_payload"])
                transition = decode_transition(row["position_payload"])
                if (transition["effect"] not in ("REDUCE", "CLOSE", "REVERSE") or
                        value["position"] != transition["before"] or
                        any(value[k] != transition[k] for k in ("account_id", "fill_id", "order_id", "symbol", "side", "quantity", "price", "reduce_only", "persisted_order"))):
                    raise ValueError("Exit evidence contradicts atomic fill-position evidence")
            return project_exit_fill({"source_sequence": row["source_sequence"], "fill_id": row["id"],
                "order_id": row["order_id"], "timestamp": row["timestamp"], "symbol": row["symbol"],
                "side": row["side"], "quantity": row["quantity"], "price": row["price"],
                "exit_evidence": value,
                "capture_state": "RECORDED_SOURCE_EXIT" if value is not None else "UNVERIFIED_NO_EXIT_CAPTURE"}, account_id)

        first = project(db.execute(f"SELECT {select} FROM v2_fills ORDER BY rowid LIMIT 1").fetchone())
        previous = project(db.execute(f"SELECT {select} FROM v2_fills WHERE rowid=?", (after,)).fetchone()) if after else None
        if after and (not first or not previous or cursor_anchor(account_id, first, previous) != anchor):
            raise ValueError("Exit evidence source cursor changed")
        rows = db.execute(f"SELECT {select} FROM v2_fills WHERE rowid>? ORDER BY rowid LIMIT ?", (after, limit+1)).fetchall()
        fills = [project(row) for row in rows[:limit]]
    return {"account_id": account_id, "account_type": "SMC_LAB", "atomic_snapshot": True,
            "source_capture_post_install_only": True, "full_lifecycle_verified": False,
            "after": after, "anchor": anchor, "first_fill": first, "previous_fill": previous,
            "fills": fills, "has_more": len(rows)>limit,
            "next_after": fills[-1]["source_sequence"] if fills else after,
            "next_anchor": cursor_anchor(account_id, first, fills[-1]) if fills else anchor}
