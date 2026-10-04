"""Bounded query-only PA/SMC paper evidence; never calls either runtime.

Each lab is one SQLite read transaction. Labs are not read atomically together.
Only scalar evidence is exported: no rolling charts or journal JSON payloads.
"""
from __future__ import annotations

import sqlite3
from time import monotonic
from contextlib import closing
from pathlib import Path

from tradexa.guardian.lab_execution_integrity import MAX_FILLS, MAX_ORDERS, MAX_POSITIONS, reconcile_lab_paper

ORDER_FIELDS = ("id,symbol,side,type,quantity,remaining,filled,average_price,"
                "reduce_only,status,account_id,execution_engine,execution_class,"
                "action_class,candle_id,decision_key,order_key,timeframe")


def lab_paper_execution_snapshot(path: str | Path, lab: str) -> dict:
    prefix, account_type = {"PRICE_ACTION": ("pa", "PA_LAB"),
                            "SMC": ("smc", "SMC_LAB")}[lab]
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("Lab evidence source unavailable")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=250")
        deadline = monotonic() + .5
        db.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        db.execute("BEGIN")
        account = db.execute("SELECT account_id,account_type FROM v2_account WHERE id=1").fetchone()
        if not account or account["account_type"] != account_type or not account["account_id"]:
            raise ValueError("Isolated paper account identity unavailable")
        positions = [dict(row) for row in db.execute(
            "SELECT position_id,symbol,side,size,entry_price,stop_loss,take_profit,"
            "entry_order_id FROM v2_positions ORDER BY symbol LIMIT ?", (MAX_POSITIONS + 1,))]
        active = [dict(row) for row in db.execute(
            f"SELECT {ORDER_FIELDS} FROM v2_orders "
            "WHERE status IN ('open','partially_filled','triggered') ORDER BY id LIMIT 33")]
        if len(positions) > MAX_POSITIONS or len(active) > 32:
            raise ValueError("Open paper exposure exceeds observation bound")
        # rowid is insertion order, NOT event time. Avoid sorting/scanning an
        # unbounded historical fill table on every poll.
        fills = [dict(row) for row in db.execute(
            "SELECT id,order_id,symbol,side,quantity,price,account_id,execution_engine "
            "FROM v2_fills ORDER BY rowid DESC LIMIT ?", (MAX_FILLS + 1,))]
        fills_complete = len(fills) <= MAX_FILLS
        fills = fills[:MAX_FILLS]
        orders = {row["id"]: row for row in active}
        for row in db.execute(f"SELECT {ORDER_FIELDS} FROM v2_orders ORDER BY rowid DESC LIMIT 8"):
            orders[row["id"]] = dict(row)
        # Every open position's origin is included, even outside the recent
        # history sample. Recent exit-fill links are checked separately below.
        for position in positions:
            row = db.execute(f"SELECT {ORDER_FIELDS} FROM v2_orders WHERE id=?",
                             (position["entry_order_id"],)).fetchone()
            if row:
                orders[row["id"]] = dict(row)
        for fill in fills[:16]:
            row = db.execute(f"SELECT {ORDER_FIELDS} FROM v2_orders WHERE id=?",
                             (fill["order_id"],)).fetchone()
            if row:
                orders[row["id"]] = dict(row)
        if len(orders) > MAX_ORDERS:
            raise ValueError("Paper execution evidence exceeds observation bound")
        for order in orders.values():
            meta = db.execute(
                f"SELECT session_id,setup_id,status FROM {prefix}_order_meta WHERE order_id=?",
                (order["id"],)).fetchone()
            order["metadata"] = dict(meta) if meta else None
            order["session_found"] = bool(meta and db.execute(
                f"SELECT 1 FROM {prefix}_sessions WHERE id=?", (meta["session_id"],)).fetchone())
            # PA immutable setup evidence is NOT an executed-trade journal.
            order["setup_journal_id"] = None
            if lab == "PRICE_ACTION" and meta:
                journal = db.execute(
                    "SELECT id FROM pa_journal_entries WHERE session_id=? AND setup_id=?",
                    (meta["session_id"], meta["setup_id"])).fetchone()
                order["setup_journal_id"] = journal["id"] if journal else None
    snapshot = {"lab": lab, "account_id": account["account_id"], "account_type": account_type,
                "atomic_snapshot": True, "open_coverage_complete": True,
                "fill_window_complete": fills_complete,
                "orders": sorted(orders.values(), key=lambda row: row["id"]),
                "positions": positions, "fills": sorted(fills, key=lambda row: row["id"])}
    reconcile_lab_paper(snapshot)  # Validate non-finite/malformed data before JSON response encoding.
    return snapshot
