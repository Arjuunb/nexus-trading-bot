"""Pure, paper-only reconciliation of primary Trading Instance ledger rows.

This never reads a database itself or instructs an engine. Callers must supply
complete, source-authoritative open rows and the execution links for them.
The result is a possible integrity observation, not a live-venue certificate.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Mapping, Sequence

MAX_OPEN_ROWS = 64


def _text(row: Mapping, key: str) -> str:
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _positive(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def reconcile_instance_paper_ledger(
        positions: Sequence[Mapping], trades: Sequence[Mapping],
        executions: Sequence[Mapping], *, atomic_snapshot: bool) -> dict:
    """Check one open position <-> trade pair through its OPEN/REDUCE link.

    Missing links on legacy rows are UNKNOWN, not evidence that an order never
    existed. Even an atomic paper-ledger match does not prove a broker fill.
    """
    if type(atomic_snapshot) is not bool or any(
            not isinstance(rows, (list, tuple)) or len(rows) > MAX_OPEN_ROWS or
            not all(isinstance(row, Mapping) for row in rows)
            for rows in (positions, trades, executions)):
        raise ValueError("instance paper-ledger evidence is incomplete or oversized")
    for row in (*positions, *trades, *executions):
        if not _text(row, "instance_id"):
            raise ValueError("instance paper-ledger evidence includes an unowned row")
    if any(_text(row, "status") != "open" for row in (*positions, *trades)):
        raise ValueError("instance paper-ledger snapshot includes a non-open row")
    ids = [(_text(row, "id"), kind) for kind, rows in
           (("position", positions), ("trade", trades)) for row in rows]
    if any(not identity for identity, _kind in ids) or len(set(ids)) != len(ids):
        raise ValueError("instance paper-ledger rows have missing or duplicate IDs")
    position_by_id = {_text(row, "id"): row for row in positions}
    trade_by_id = {_text(row, "id"): row for row in trades}
    links_by_position: dict[str, list[Mapping]] = defaultdict(list)
    links_by_trade: dict[str, list[Mapping]] = defaultdict(list)
    for link in executions:
        if (_text(link, "action") not in ("OPEN", "REDUCE") or
                not _text(link, "execution_id") or not _text(link, "position_id") or
                not _text(link, "trade_id")):
            raise ValueError("instance paper-ledger execution link is invalid")
        links_by_position[_text(link, "position_id")].append(link)
        links_by_trade[_text(link, "trade_id")].append(link)
    if len({_text(link, "execution_id") for link in executions}) != len(executions):
        raise ValueError("instance paper-ledger execution identity is duplicated")

    # SQL and PostgREST are free to return these bounded rows in different
    # orders. Keep material snapshots stable across identical polls.
    positions = sorted(positions, key=lambda row: _text(row, "id"))
    trades = sorted(trades, key=lambda row: _text(row, "id"))

    findings = []
    totals: dict[str, dict] = {}
    for position in positions:
        owner = _text(position, "instance_id")
        account = totals.setdefault(owner, {"open_positions": 0, "open_trades": 0,
                                            "risk_amount": 0.0, "risk_complete": True})
        account["open_positions"] += 1
        position_id = _text(position, "id")
        links = links_by_position.get(position_id, [])
        trade = trade_by_id.get(_text(links[0], "trade_id")) if len(links) == 1 else None
        codes = []
        if len(links) == 0:
            codes.append("EXECUTION_LINK_UNVERIFIED")
        elif len(links) > 1:
            codes.append("MULTIPLE_EXECUTION_LINKS")
        elif _text(links[0], "instance_id") != owner:
            codes.append("EXECUTION_OWNER_MISMATCH")
        if len(links) == 1 and trade is None:
            codes.append("OPEN_POSITION_TRADE_UNVERIFIED")
        if trade is not None:
            if len(links_by_trade.get(_text(trade, "id"), [])) != 1:
                codes.append("MULTIPLE_POSITION_LINKS")
            if _text(trade, "instance_id") != owner:
                codes.append("TRADE_OWNER_MISMATCH")
            if _text(trade, "simulation_session_id") != _text(position, "simulation_session_id"):
                codes.append("SESSION_MISMATCH")
            if (_text(trade, "symbol") != _text(position, "symbol") or
                    _text(trade, "side") != _text(position, "side")):
                codes.append("SYMBOL_SIDE_MISMATCH")
            if _text(trade, "source") != "paper":
                codes.append("PAPER_SOURCE_UNVERIFIED")
            for field in ("size", "entry"):
                left, right = _positive(position.get(field)), _positive(trade.get(field))
                if left is None or right is None or not math.isclose(
                        left, right, rel_tol=1e-8, abs_tol=1e-9):
                    codes.append(f"{field.upper()}_MISMATCH")
        entry, stop, size = (_positive(position.get(field)) for field in ("entry", "stop", "size"))
        side = _text(position, "side").lower()
        if entry is None or size is None:
            codes.append("POSITION_GEOMETRY_INVALID")
            risk = None
        elif stop is None:
            codes.append("MISSING_STOP")
            risk = None
        elif (side == "long" and stop >= entry) or (side == "short" and stop <= entry) or \
                side not in ("long", "short"):
            codes.append("STOP_GEOMETRY_INVALID")
            risk = None
        else:
            risk = size * abs(entry - stop)
            if not math.isfinite(risk):
                codes.append("POSITION_GEOMETRY_INVALID")
                risk = None
        if risk is None or not atomic_snapshot:
            account["risk_complete"] = False
        else:
            account["risk_amount"] += risk
        if codes:
            account["risk_complete"] = False
        findings.append({
            "instance_id": owner, "position_id": position_id,
            "trade_id": _text(trade, "id") if trade else None,
            "execution_id": _text(links[0], "execution_id") if len(links) == 1 else None,
            "symbol": _text(position, "symbol"), "side": side,
            "risk_amount": risk, "codes": sorted(set(codes)),
            "pairing_state": ("OBSERVED_MATCH" if atomic_snapshot and not codes
                              else "UNVERIFIED"),
        })
    for trade in trades:
        owner = _text(trade, "instance_id")
        account = totals.setdefault(owner, {"open_positions": 0, "open_trades": 0,
                                            "risk_amount": 0.0, "risk_complete": True})
        account["open_trades"] += 1
        links = links_by_trade.get(_text(trade, "id"), [])
        matching = [link for link in links if _text(link, "position_id") in position_by_id]
        if (_text(trade, "source") != "paper" or
                (len(matching) == 1 and
                 _text(position_by_id[_text(matching[0], "position_id")], "instance_id") != owner)):
            account["risk_complete"] = False
        if len(matching) != 1:
            account["risk_complete"] = False
            findings.append({
                "instance_id": owner, "position_id": None,
                "trade_id": _text(trade, "id"), "execution_id": None,
                "symbol": _text(trade, "symbol"), "side": _text(trade, "side"),
                "risk_amount": None,
                "codes": ["OPEN_TRADE_POSITION_UNVERIFIED"],
                "pairing_state": "UNVERIFIED",
            })
    return {
        "scope": "INSTANCE_ATTRIBUTED_PAPER_LEDGER_ONLY",
        "atomic_snapshot": atomic_snapshot,
        "broker_fill_verified": False,
        "live_exposure_verified": False,
        "source_coverage_verified": False,
        "instances": [
            {"instance_id": owner, **summary,
             "risk_amount": round(summary["risk_amount"], 8)
                 if summary["risk_complete"] else None}
            for owner, summary in sorted(totals.items())
        ],
        "findings": findings,
    }
