"""Read-only SMC Agent intent/order/journal integrity projection for Guardian.

Two SQLite files cannot be read atomically together. This module reports
observed relationships and uncertainty; it does not declare a confirmed
incident, alter an intent, reconcile a broker, or authorize a new entry.
"""
from __future__ import annotations

import sqlite3
import math
from contextlib import closing
from pathlib import Path
from time import monotonic

MAX_OUTSTANDING = 64
MAX_OPEN_TRADES = 64
RECENT_TERMINAL_PER_STATE = 8
MAX_EXECUTIONS = MAX_OUTSTANDING + MAX_OPEN_TRADES + 2 * RECENT_TERMINAL_PER_STATE
COVERAGE = "ALL_OUTSTANDING_AND_OPEN_JOURNAL_PLUS_RECENT_TERMINAL"
_TERMINAL = ("COMPLETE", "EXECUTION_FAILED")
_INTENT_FIELDS = ("execution_key", "decision_id", "session_id", "symbol", "timeframe",
                  "state", "broker_order_id", "trade_id", "updated_at")
_TRADE_FIELDS = ("id", "decision_id", "order_id", "symbol", "timeframe", "direction",
                 "opened_at", "closed_at")
_ORDER_FIELDS = ("id", "candle_id", "symbol", "timeframe", "side", "status", "account_id",
                 "execution_engine", "execution_class", "action_class")


def _select(fields: tuple[str, ...], *numeric: str) -> str:
    # Never load the full intent payload, candle windows or journal prose.
    # The extra character detects oversize identity values instead of silently
    # truncating two different identities into the same key.
    return ",".join([*(f"substr({name},1,257) AS {name}" for name in fields), *numeric])


_INTENT_SELECT = _select(_INTENT_FIELDS)
_TRADE_SELECT = _select(_TRADE_FIELDS, "size")
_ORDER_SELECT = _select(_ORDER_FIELDS, "filled", "reduce_only")


def _checked(row: sqlite3.Row | None, fields: tuple[str, ...]) -> sqlite3.Row | None:
    if row is not None and any(row[key] is not None and
                              (not isinstance(row[key], str) or len(row[key]) > 256)
                              for key in fields):
        raise ValueError("SMC integrity source identity is invalid or oversized")
    return row


def _side(direction: str) -> str | None:
    return {"long": "buy", "bullish": "buy", "buy": "buy",
            "short": "sell", "bearish": "sell", "sell": "sell"}.get(direction)


def _quantity(value) -> float:
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ValueError("SMC integrity source quantity is invalid")
    return float(value)


def _read_connection(path: str | Path) -> sqlite3.Connection:
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("Guardian execution evidence source unavailable")
    conn = sqlite3.connect(source.resolve().as_uri() + "?mode=ro",
                           uri=True, timeout=0.25)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=250")
        deadline = monotonic() + .5
        conn.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
        conn.execute("BEGIN")
    except Exception:
        conn.close()
        raise
    return conn


def _integrity_code(intent: sqlite3.Row, order: sqlite3.Row | None,
                    trade: sqlite3.Row | None, position: sqlite3.Row | None,
                    *, matching_orders: int, matching_intents: int, account_id: str) -> str:
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
            order["timeframe"] != intent["timeframe"] or
            order["side"] not in ("buy", "sell") or
            order["account_id"] != account_id or
            order["execution_engine"] != "SMC_LAB" or
            order["execution_class"] != "REAL_PAPER" or
            order["action_class"] != "ENTRY"):
        return "ORDER_IDENTITY_MISMATCH"
    if trade_id and trade is None:
        return "TRADE_ID_NOT_FOUND"
    if trade is not None and matching_intents > 1:
        return "MULTIPLE_INTENTS_FOR_TRADE"
    observed_order_id = order["id"] if order is not None else order_id
    if trade is not None and (trade["order_id"] != observed_order_id or
                              trade["decision_id"] != intent["decision_id"] or
                              trade["symbol"] != intent["symbol"] or
                              trade["timeframe"] != intent["timeframe"] or
                              _side(trade["direction"]) is None or
                              (order is not None and _side(trade["direction"]) != order["side"])):
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
    if position is not None and position["entry_order_id"] == observed_order_id:
        if _side(position["side"]) != order["side"]:
            return "POSITION_SIDE_MISMATCH"
        if trade is not None and trade["closed_at"] is not None:
            return "CLOSED_TRADE_POSITION_STILL_OPEN"
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
    """Observe all outstanding intents and all open Agent trades, plus a sample.

    Overflow fails closed, never silently dropping older open trades. Reverse
    journal links are read in the same journal transaction. Broker comparisons
    still use a separate snapshot, not certified lifecycle reconciliation.
    """
    with closing(_read_connection(journal_path)) as journal, \
            closing(_read_connection(broker_path)) as broker:
        account = broker.execute(
            "SELECT substr(account_id,1,257) AS account_id,account_type FROM v2_account WHERE id=1"
        ).fetchone()
        if (account is None or str(account["account_type"]).upper() != "SMC_LAB"
                or not account["account_id"] or len(account["account_id"]) > 256):
            raise ValueError("SMC Agent broker is not a verified paper account")
        outstanding = journal.execute(
            f"SELECT {_INTENT_SELECT} FROM execution_intents WHERE state NOT IN (?,?) "
            "ORDER BY created_at,execution_key LIMIT ?",
            (*_TERMINAL, MAX_OUTSTANDING + 1),
        ).fetchall()
        if len(outstanding) > MAX_OUTSTANDING:
            raise ValueError("SMC Agent outstanding integrity coverage exceeded")
        terminal = []
        for state in _TERMINAL:
            terminal.extend(journal.execute(
                f"SELECT {_INTENT_SELECT} FROM execution_intents WHERE state=? "
                "ORDER BY updated_at DESC,execution_key LIMIT ?",
                (state, RECENT_TERMINAL_PER_STATE),
            ).fetchall())
        open_trades = journal.execute(
            f"SELECT {_TRADE_SELECT} FROM agent_trades WHERE closed_at IS NULL "
            "ORDER BY opened_at,id LIMIT ?", (MAX_OPEN_TRADES + 1,),
        ).fetchall()
        if len(open_trades) > MAX_OPEN_TRADES:
            raise ValueError("SMC Agent open journal integrity coverage exceeded")
        selected = {}
        for intent in (*outstanding, *terminal):
            _checked(intent, _INTENT_FIELDS)
            selected[intent["execution_key"]] = intent
        links = []
        base_count = len(selected)
        for trade in open_trades:
            _checked(trade, _TRADE_FIELDS)
            linked = journal.execute(
                f"SELECT {_INTENT_SELECT} FROM execution_intents WHERE trade_id=? "
                "ORDER BY execution_key LIMIT ?", (trade["id"], MAX_OPEN_TRADES + 1),
            ).fetchall()
            if len(linked) > MAX_OPEN_TRADES:
                raise ValueError("SMC Agent journal link integrity coverage exceeded")
            for intent in linked:
                _checked(intent, _INTENT_FIELDS)
                selected[intent["execution_key"]] = intent
            if len(selected) - base_count > MAX_OPEN_TRADES:
                raise ValueError("SMC Agent linked intent integrity coverage exceeded")
            links.append({
                "trade_id": trade["id"], "decision_id": trade["decision_id"],
                "symbol": trade["symbol"], "timeframe": trade["timeframe"],
                "direction": trade["direction"], "opened_at": trade["opened_at"],
                "order_id": trade["order_id"], "journal_trade_size": _quantity(trade["size"]),
                "execution_keys": [row["execution_key"] for row in linked],
                "matching_intent_count": len(linked),
                "integrity_code": ("JOURNAL_INTENT_NOT_FOUND" if not linked else
                                   "MULTIPLE_INTENTS_FOR_TRADE" if len(linked) > 1 else
                                   "INTENT_LINK_FOUND"),
            })
        # COUNT does not allocate an unbounded duplicate-order list. A SQL
        # deadline bounds scans on a large source without adding source indexes.
        intents = list(selected.values())
        if len(intents) > MAX_EXECUTIONS:
            raise ValueError("SMC Agent execution integrity coverage exceeded")
        by_key = {}
        if intents:
            keys = tuple(intent["execution_key"] for intent in intents)
            placeholders = ",".join("?" for _ in keys)
            by_key = dict(broker.execute(
                f"SELECT candle_id,COUNT(*) FROM v2_orders WHERE candle_id IN ({placeholders}) "
                "AND action_class='ENTRY' GROUP BY candle_id", keys).fetchall())
        rows = []
        for intent in intents:
            order_id, trade_id = intent["broker_order_id"], intent["trade_id"]
            order = _checked(broker.execute(
                f"SELECT {_ORDER_SELECT} FROM v2_orders WHERE id=?", (order_id,),
            ).fetchone() if order_id else broker.execute(
                f"SELECT {_ORDER_SELECT} FROM v2_orders WHERE candle_id=? "
                "AND action_class='ENTRY' ORDER BY id LIMIT 1", (intent["execution_key"],),
            ).fetchone(), _ORDER_FIELDS)
            trade = (journal.execute(
                f"SELECT {_TRADE_SELECT} FROM agent_trades WHERE id=?",
                (trade_id,),
            ).fetchone() if trade_id else None)
            _checked(trade, _TRADE_FIELDS)
            if order is not None:
                _quantity(order["filled"])
            if trade is not None:
                _quantity(trade["size"])
            matching_intents = (journal.execute(
                "SELECT COUNT(*) FROM execution_intents WHERE trade_id=?", (trade_id,),
            ).fetchone()[0] if trade_id else 0)
            position = (broker.execute(
                "SELECT symbol,substr(side,1,257) AS side,substr(entry_order_id,1,257) "
                "AS entry_order_id FROM v2_positions WHERE symbol=?",
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
                "matching_broker_order_count": by_key.get(intent["execution_key"], 0),
                "matching_journal_intent_count": matching_intents,
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
                    matching_orders=by_key.get(intent["execution_key"], 0),
                    matching_intents=matching_intents,
                    account_id=account["account_id"]),
            })
    return {
        "scope": "SMC_AGENT_PAPER_ONLY",
        "coverage": COVERAGE,
        "outstanding_count": len(outstanding),
        "terminal_sample_count": len(terminal),
        "extra_open_intent_count": len(selected) - base_count,
        "open_journal_trade_count": len(open_trades),
        "open_journal_trades": links,
        "cross_database_atomic": False,
        "broker_account_type": "SMC_LAB",
        "broker_account_id": account["account_id"],
        "executions": rows,
    }
