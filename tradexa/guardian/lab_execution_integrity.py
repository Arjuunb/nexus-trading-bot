"""Pure bounded paper-broker comparisons, without execution authority.

Broker position origin is not proof of every addition/reduction in its lifetime.
Entry-to-stop amounts exclude costs and gaps and have unverified currency;
they must never be advertised as aggregate live risk or guaranteed protection.
"""
from __future__ import annotations

import math
from collections import defaultdict

MAX_ORDERS, MAX_POSITIONS, MAX_FILLS = 64, 16, 128
ACTIVE = {"open", "partially_filled", "triggered"}


def number(value, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError("Invalid paper execution numeric evidence")
    return float(value)


def text(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("Invalid paper execution identity")
    return value


def reconcile_lab_paper(snapshot: dict) -> dict:
    lab = snapshot.get("lab")
    expected = {"PRICE_ACTION": "PA_LAB", "SMC": "SMC_LAB"}.get(lab)
    if (not expected or snapshot.get("account_type") != expected or
            snapshot.get("atomic_snapshot") is not True or
            snapshot.get("open_coverage_complete") is not True or
            type(snapshot.get("fill_window_complete")) is not bool):
        raise ValueError("Unverified isolated paper evidence contract")
    account = text(snapshot.get("account_id"))
    for key, bound in (("orders", MAX_ORDERS), ("positions", MAX_POSITIONS), ("fills", MAX_FILLS)):
        rows = snapshot.get(key)
        if not isinstance(rows, list) or len(rows) > bound or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Incomplete or oversized paper evidence")
        field = "position_id" if key == "positions" else "id"
        identities = [text(row.get(field)) for row in rows]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicated paper record identity")
    orders = {row["id"]: row for row in snapshot["orders"]}
    fills_by_order = defaultdict(list)
    for fill in snapshot["fills"]:
        for field in ("order_id", "symbol", "side"):
            text(fill.get(field))
        number(fill.get("quantity"), positive=True)
        number(fill.get("price"), positive=True)
        if fill.get("account_id") != account or fill.get("execution_engine") != expected:
            raise ValueError("Fill belongs to another account or engine")
        fills_by_order[fill["order_id"]].append(fill)
    findings, order_rows, position_rows = [], [], []

    def finding(code, kind, identity, *, fact=False):
        findings.append({"code": code, "record_type": kind, "record_id": identity,
                         "confidence": "CONFIRMED_RECORD_FACT" if fact else "UNVERIFIED"})

    keys = defaultdict(list)
    for order in sorted(orders.values(), key=lambda row: row["id"]):
        oid = order["id"]
        for field in ("symbol", "side", "status"):
            text(order.get(field))
        if order["side"] not in {"buy", "sell"}:
            raise ValueError("Invalid paper order side")
        quantity = number(order.get("quantity"), positive=True)
        filled, remaining = number(order.get("filled")), number(order.get("remaining"))
        if (order.get("account_id") != account or order.get("execution_engine") != expected or
                order.get("execution_class") != "REAL_PAPER"):
            finding("ORDER_IDENTITY_MISMATCH", "order", oid, fact=True)
        if not math.isclose(quantity, filled + remaining, rel_tol=1e-8, abs_tol=1e-9):
            finding("ORDER_QUANTITY_MISMATCH", "order", oid, fact=True)
        if ((order["status"] == "filled" and (filled <= 0 or remaining > 1e-9)) or
                (order["status"] == "partially_filled" and not 0 < filled < quantity)):
            finding("ORDER_STATUS_QUANTITY_MISMATCH", "order", oid, fact=True)
        if type(order.get("reduce_only")) not in (int, bool) or order["reduce_only"] not in (0, 1):
            raise ValueError("Invalid reduce-only evidence")
        action = order.get("action_class")
        if action in {"CLOSE", "REDUCE", "FLATTEN"} and not order["reduce_only"]:
            finding("EXIT_NOT_REDUCE_ONLY", "order", oid, fact=True)
        if action == "ENTRY" and order["reduce_only"]:
            finding("ENTRY_MARKED_REDUCE_ONLY", "order", oid, fact=True)
        if action not in {"ENTRY", "CLOSE", "REDUCE", "FLATTEN"}:
            finding("ORDER_ACTION_UNVERIFIED", "order", oid)
        if order.get("order_key"):
            keys[text(order["order_key"])].append(oid)
        matched = fills_by_order[oid]
        total = math.fsum(number(row["quantity"]) for row in matched)
        equal = math.isclose(total, filled, rel_tol=1e-8, abs_tol=1e-9)
        if not equal:
            if snapshot["fill_window_complete"] or total > filled:
                finding("FILLED_QUANTITY_MISMATCH", "order", oid, fact=True)
            else:
                finding("FILL_HISTORY_INCOMPLETE", "order", oid)
        average = order.get("average_price")
        if average is not None:
            number(average, positive=filled > 0)
        if filled > 0 and average is None:
            finding("ORDER_AVERAGE_PRICE_UNVERIFIED", "order", oid)
        elif equal and total > 0:
            weighted = math.fsum(row["quantity"] * row["price"] for row in matched) / total
            number(weighted, positive=True)
            if not math.isclose(weighted, average, rel_tol=1e-8, abs_tol=1e-9):
                finding("FILLED_PRICE_MISMATCH", "order", oid, fact=True)
        if any(row["symbol"] != order["symbol"] or row["side"] != order["side"] for row in matched):
            finding("FILL_ORDER_IDENTITY_MISMATCH", "order", oid, fact=True)
        meta = order.get("metadata")
        if meta is not None:
            if not isinstance(meta, dict):
                raise ValueError("Invalid order metadata")
            text(meta.get("session_id"))
            text(meta.get("status"))
        if action == "ENTRY" and (not meta or order.get("session_found") is not True):
            finding("ORDER_SESSION_LINK_UNVERIFIED", "order", oid)
        if lab == "PRICE_ACTION" and meta and not order.get("setup_journal_id"):
            finding("PA_SETUP_JOURNAL_UNVERIFIED", "order", oid)
        order_rows.append({"order_id": oid, "symbol": order["symbol"],
                           "action": action, "status": order["status"],
                           "quantity": quantity, "filled": filled, "remaining": remaining,
                           "average_fill_price": average,
                           "reduce_only": bool(order["reduce_only"]),
                           "decision_key": order.get("decision_key"),
                           "execution_key": order.get("candle_id"),
                           "session_id": (meta or {}).get("session_id"),
                           "metadata_status": (meta or {}).get("status"),
                           "setup_journal_id": order.get("setup_journal_id"),
                           "sampled_fill_quantity": total, "fill_quantity_matches": equal})
    for members in keys.values():
        if len(members) > 1:
            for oid in members:
                finding("DUPLICATE_ORDER_KEY", "order", oid, fact=True)
    symbols = set()
    for position in sorted(snapshot["positions"], key=lambda row: row["position_id"]):
        pid, symbol = position["position_id"], text(position.get("symbol"))
        if symbol in symbols or position.get("side") not in {"long", "short"}:
            raise ValueError("Invalid isolated position identity")
        symbols.add(symbol)
        size, entry = number(position.get("size"), positive=True), number(position.get("entry_price"), positive=True)
        stop = position.get("stop_loss")
        target = position.get("take_profit")
        if target is not None:
            number(target, positive=True)
        risk = None
        if stop is None:
            finding("POSITION_STOP_UNVERIFIED", "position", pid)
        else:
            stop = number(stop, positive=True)
            # A trailing/breakeven stop can validly cross the original entry.
            risk = size * max(0., (entry - stop) * (1 if position["side"] == "long" else -1))
            number(risk)
        order = orders.get(position.get("entry_order_id"))
        origin_match = bool(order and order["symbol"] == symbol and order.get("action_class") == "ENTRY"
                            and order["side"] == ("buy" if position["side"] == "long" else "sell")
                            and order["filled"] > 0 and order.get("account_id") == account
                            and order.get("execution_engine") == expected
                            and order.get("execution_class") == "REAL_PAPER" and not order["reduce_only"])
        if not origin_match:
            finding("POSITION_ORIGIN_UNVERIFIED", "position", pid)
        reliable_origin = origin_match and not any(
            row["record_type"] == "order" and row["record_id"] == order["id"] and
            row["code"] not in {"ORDER_SESSION_LINK_UNVERIFIED", "PA_SETUP_JOURNAL_UNVERIFIED"}
            for row in findings)
        position_rows.append({"position_id": pid, "symbol": symbol, "side": position["side"],
                              "size": size, "entry_price": entry, "stop_loss": stop,
                              "take_profit": target, "entry_order_id": position.get("entry_order_id"),
                              "origin_order_matches": origin_match,
                              "entry_to_stop_amount": risk if reliable_origin else None,
                              "full_position_lifecycle_verified": False})
    # Synthetic protective/remediation fills can legitimately have no v2_order.
    # Do not invent reduce-only flags or infer their parent position from symbol.
    unlinked = [row for row in snapshot["fills"] if row["order_id"] not in orders]
    return {"lab": lab, "account_id": account, "account_type": expected,
            "scope": "ISOLATED_LAB_PAPER_BROKER", "atomic_snapshot": True,
            "open_orders": sum(row["status"] in ACTIVE for row in order_rows),
            "open_positions": len(position_rows), "orders_sampled": len(order_rows),
            "fills_sampled": len(snapshot["fills"]),
            "fill_window_complete": snapshot["fill_window_complete"],
            "unlinked_sampled_fill_count": len(unlinked),
            "exit_link_state": "UNVERIFIED" if unlinked else "NO_UNLINKED_FILL_IN_SAMPLE",
            "historical_coverage_complete": False, "journal_trade_verified": False,
            "protection_execution_verified": False, "currency_verified": False,
            "live_exposure_verified": False, "global_risk_amount": None,
            "orders": order_rows, "positions": position_rows,
            "findings": findings}
