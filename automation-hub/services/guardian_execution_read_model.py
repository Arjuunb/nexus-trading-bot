"""Read-only SMC Agent intent/order/journal integrity projection for Guardian.

Two SQLite files cannot be read atomically together. This module reports
observed relationships and uncertainty; it does not declare a confirmed
incident, alter an intent, reconcile a broker, or authorize a new entry.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

MAX_OUTSTANDING = 64
RECENT_TERMINAL_PER_STATE = 8
_TERMINAL = ("COMPLETE", "EXECUTION_FAILED")


def _read_connection(path: str | Path) -> sqlite3.Connection:
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("Guardian execution evidence source unavailable")
    conn = sqlite3.connect(source.resolve().as_uri() + "?mode=ro",
                           uri=True, timeout=0.25)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=250")
    conn.execute("BEGIN")
    return conn


def _integrity_code(intent: sqlite3.Row, order: sqlite3.Row | None,
                    trade: sqlite3.Row | None, position: sqlite3.Row | None,
                    *, matching_orders: int, account_id: str) -> str:
    state = intent["state"]
    order_id = intent["broker_order_id"]
    trade_id = intent["trade_id"]
    if matching_orders > 1:
        return "DUPLICATE_EXECUTION_KEY"
    if order_id and order is None:
        return "ORDER_ID_NOT_FOUND"
    if order is not None and (
            order["candle_id"] != intent["execution_key"] or
            order["symbol"] != intent["symbol"] or
            order["account_id"] != account_id or
            order["execution_engine"] != "SMC_LAB" or
            order["execution_class"] != "REAL_PAPER" or
            order["action_class"] != "ENTRY"):
        return "ORDER_IDENTITY_MISMATCH"
    if trade_id and trade is None:
        return "TRADE_ID_NOT_FOUND"
    observed_order_id = order["id"] if order is not None else order_id
    if trade is not None and (trade["order_id"] != observed_order_id or
                              trade["decision_id"] != intent["decision_id"]):
        return "TRADE_IDENTITY_MISMATCH"
    if order is not None and int(order["reduce_only"]) != 0:
        return "ENTRY_MARKED_REDUCE_ONLY"
    if order is not None and not order_id:
        # The broker committed, but the journal has not recorded its ID.
        # This is not an execution failure or evidence that no order exists.
        return "BROKER_ORDER_UNRECORDED"
    if state == "EXECUTION_UNCERTAIN":
        return "EXECUTION_UNCERTAIN"
    if state == "EXECUTION_FAILED" and order_id:
        return "FAILED_INTENT_WITH_ORDER_ID"
    if state in ("EXECUTED", "RECONCILED", "COMPLETE") and not order_id:
        # Absence of a recorded ID is NOT proof that the broker has no order.
        return "ORDER_ID_UNRECORDED"
    if order is not None and float(order["filled"] or 0) > 0 and not trade_id:
        return "FILLED_ORDER_JOURNAL_PENDING"
    if state == "COMPLETE" and not trade_id:
        return "COMPLETE_INTENT_TRADE_UNRECORDED"
    if order is not None and float(order["filled"] or 0) <= 0:
        # The agent may finalize its trade journal on broker acceptance. That
        # journal row must not be presented as proof of a filled position.
        return "AGENT_TRADE_PRECEDES_FILL" if trade is not None else "ORDER_AWAITING_FILL"
    if (order is not None and trade is not None and trade["closed_at"] is None
            and float(trade["size"]) > float(order["filled"] or 0) + 1e-9):
        return "JOURNAL_SIZE_EXCEEDS_BROKER_FILL"
    if (order is not None and float(order["filled"] or 0) > 0 and
            trade is not None and trade["closed_at"] is None and
            (position is None or position["entry_order_id"] != order_id)):
        # A cross-DB read can race a close. This remains an observation, not
        # a confirmed missing position or permission to recreate it.
        return "OPEN_TRADE_POSITION_UNVERIFIED"
    return "PENDING" if state in ("DECISION_APPROVED", "EXECUTION_PENDING") else "CONSISTENT"


def smc_execution_integrity_snapshot(journal_path: str | Path,
                                     broker_path: str | Path) -> dict:
    """Return bounded outstanding and recent terminal Agent executions.

    If the outstanding set exceeds the bound, fail closed instead of omitting
    a possibly unsafe intent. Historical terminal coverage is explicitly
    limited; this is not a replacement for a durable source event outbox.
    """
    with closing(_read_connection(journal_path)) as journal, \
            closing(_read_connection(broker_path)) as broker:
        account = broker.execute(
            "SELECT account_id,account_type FROM v2_account WHERE id=1"
        ).fetchone()
        if (account is None or str(account["account_type"]).upper() != "SMC_LAB"
                or not account["account_id"]):
            raise ValueError("SMC Agent broker is not a verified paper account")
        outstanding = journal.execute(
            "SELECT * FROM execution_intents WHERE state NOT IN (?,?) "
            "ORDER BY created_at LIMIT ?",
            (*_TERMINAL, MAX_OUTSTANDING + 1),
        ).fetchall()
        if len(outstanding) > MAX_OUTSTANDING:
            raise ValueError("SMC Agent outstanding integrity coverage exceeded")
        terminal = []
        for state in _TERMINAL:
            terminal.extend(journal.execute(
                "SELECT * FROM execution_intents WHERE state=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (state, RECENT_TERMINAL_PER_STATE),
            ).fetchall())
        # One bounded scan discovers a broker commit even when a crash came
        # before the journal could save its order ID. It never changes broker
        # state or concludes absence proves an order was not submitted.
        intents = (*outstanding, *terminal)
        by_key: dict[str, list[sqlite3.Row]] = {}
        if intents:
            keys = tuple(intent["execution_key"] for intent in intents)
            placeholders = ",".join("?" for _ in keys)
            for order in broker.execute(
                    "SELECT id,candle_id,symbol,filled,reduce_only,status,"
                    "account_id,execution_engine,execution_class,action_class "
                    f"FROM v2_orders WHERE candle_id IN ({placeholders}) "
                    "AND action_class='ENTRY'", keys):
                by_key.setdefault(order["candle_id"], []).append(order)
        rows = []
        for intent in intents:
            order_id, trade_id = intent["broker_order_id"], intent["trade_id"]
            order = (broker.execute(
                "SELECT id,candle_id,symbol,filled,reduce_only,status,account_id,"
                "execution_engine,execution_class,action_class FROM v2_orders WHERE id=?",
                (order_id,),
            ).fetchone() if order_id else next(iter(by_key.get(intent["execution_key"], [])), None))
            trade = (journal.execute(
                "SELECT id,decision_id,order_id,closed_at,size "
                "FROM agent_trades WHERE id=?",
                (trade_id,),
            ).fetchone() if trade_id else None)
            position = (broker.execute(
                "SELECT symbol,entry_order_id FROM v2_positions WHERE symbol=?",
                (intent["symbol"],),
            ).fetchone() if order is not None and float(order["filled"] or 0) > 0 else None)
            rows.append({
                "execution_key": intent["execution_key"],
                "decision_id": intent["decision_id"],
                "session_id": intent["session_id"],
                "symbol": intent["symbol"], "timeframe": intent["timeframe"],
                "state": intent["state"], "updated_at": intent["updated_at"],
                "broker_order_id": order_id,
                "discovered_broker_order_id": order["id"] if order else None,
                "matching_broker_order_count": len(by_key.get(intent["execution_key"], [])),
                "broker_order_found": bool(order) if order_id or order else None,
                "broker_order_status": order["status"] if order else None,
                "broker_filled_quantity": float(order["filled"] or 0) if order else None,
                "trade_id": trade_id, "journal_trade_found": bool(trade) if trade_id else None,
                "trade_closed": bool(trade["closed_at"]) if trade else None,
                "journal_trade_size": float(trade["size"]) if trade else None,
                "open_position_matches_order": (
                    position["entry_order_id"] == order["id"] if position is not None else False
                ) if order is not None and float(order["filled"] or 0) > 0 else None,
                "integrity_code": _integrity_code(
                    intent, order, trade, position,
                    matching_orders=len(by_key.get(intent["execution_key"], [])),
                    account_id=account["account_id"]),
            })
    return {
        "scope": "SMC_AGENT_PAPER_ONLY",
        "coverage": "ALL_OUTSTANDING_PLUS_RECENT_TERMINAL",
        "outstanding_count": len(outstanding),
        "terminal_sample_count": len(terminal),
        "cross_database_atomic": False,
        "broker_account_type": "SMC_LAB",
        "executions": rows,
    }
