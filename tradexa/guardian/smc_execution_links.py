"""Explicit retained SMC ID links, not a broker or position reconciliation.

Only Guardian's own imported evidence is queried. Same-market or nearby-time
records never establish a link. Recorded journal closes cannot prove exits.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .health import component_health
from .lab_fill_history import project_fill
from .smc_intent_history import project_transition
from .smc_journal_history import project_trade

MAX_ROWS = 128
MAX_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 16384
PROBES = ("guardian_smc_intent_history", "guardian_smc_fill_history", "guardian_smc_journal_history")


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", key):
        raise ValueError("Invalid execution identity")
    return key


def _origin(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid retained journal origin")
    return value


def _matches(event, row, *, key=None):
    if (event.get("lab_id") != "SMC" or event.get("order_id") != row.get("order_id", row.get("broker_order_id"))
            or event.get("symbol") != row["symbol"] or event.get("timeframe") != row["timeframe"]
            or (key is not None and event.get("execution_id") != key)):
        raise ValueError("Retained event identity disagrees with source projection")


def smc_execution_links_view(store, execution_key, *, now=None):
    key = validate_key(execution_key)
    snapshot = store.smc_execution_link_snapshot(key)
    moment = now or datetime.now(timezone.utc)
    health = component_health(snapshot["heartbeats"], PROBES, now=moment)["components"]
    entry_current = all(health[p]["state"] == "HEALTHY" for p in PROBES[:2])
    journal_current = health[PROBES[2]]["state"] == "HEALTHY"
    intents, fills, trades = [], [], []
    for event in snapshot["intent_events"]:
        row = project_transition(event["evidence"]["transition"])
        _matches(event, row, key=key)
        if row["execution_key"] != key or event.get("state_after") != row["state"]:
            raise ValueError("Retained intent execution identity disagrees")
        intents.append((event, row, _origin(event["evidence"]["journal_origin"])))
    for event in snapshot["fill_events"]:
        evidence = event["evidence"]
        if evidence.get("account_type") != "SMC_LAB":
            raise ValueError("Retained fill is not isolated SMC paper")
        row = project_fill(evidence["fill"], "SMC", evidence["account_id"])
        _matches(event, row)
        fills.append((event, row))
    for event in snapshot["journal_events"]:
        row = project_trade(event["evidence"]["trade"])
        _matches(event, row)
        _origin(event["evidence"]["journal_origin"])
        if row["closed_at"] is None or event.get("correlation_id") != row["decision_id"]:
            raise ValueError("Retained close event lacks a closed journal identity")
        trades.append((event, row))

    findings, conflicts = {"EXIT_AND_POSITION_LIFECYCLE_UNVERIFIED"}, set()
    origins = {origin for _, _, origin in intents}
    identity = {(r["intent_id"], r["session_id"], r["symbol"], r["timeframe"], r["candle_time"], r["proposal_id"])
                for _, r, _ in intents}
    order_ids = {r["broker_order_id"] for _, r, _ in intents if r["broker_order_id"]}
    trade_ids = {r["trade_id"] for _, r, _ in intents if r["trade_id"]}
    accounts = {r["account_id"] for _, r in fills}
    if (len({(origin, r["source_sequence"]) for _, r, origin in intents}) != len(intents)
            or len({(origin, r["id"]) for _, r, origin in intents}) != len(intents)):
        conflicts.add("DUPLICATE_INTENT_TRANSITION_ID")
    if len(origins) > 1:
        conflicts.add("MULTIPLE_JOURNAL_ORIGINS")
    if len(identity) > 1:
        conflicts.add("INTENT_IDENTITY_CONFLICT")
    if len(order_ids) > 1:
        conflicts.add("MULTIPLE_RECORDED_ORDER_IDS")
    if len(trade_ids) > 1:
        conflicts.add("MULTIPLE_RECORDED_TRADE_IDS")
    if len(accounts) > 1:
        conflicts.add("MULTIPLE_PAPER_ACCOUNTS")
    markets = {(r["symbol"], r["timeframe"]) for _, r, _ in intents}
    entry_fills = []
    fill_ids = set()
    for event, row in fills:
        if row["id"] in fill_ids:
            conflicts.add("DUPLICATE_RETAINED_FILL_ID")
        fill_ids.add(row["id"])
        if not row["candle_id"]:
            findings.add("FILL_EXECUTION_KEY_UNVERIFIED")
        elif row["candle_id"] != key:
            conflicts.add("FILL_EXECUTION_KEY_CONFLICT")
        if order_ids and row["order_id"] not in order_ids:
            conflicts.add("FILL_ORDER_LINK_CONFLICT")
        if not row["timeframe"]:
            findings.add("FILL_MARKET_UNVERIFIED")
        elif markets and (row["symbol"], row["timeframe"]) not in markets:
            conflicts.add("FILL_MARKET_CONFLICT")
        explicit = (len(order_ids) == 1 and row["order_id"] in order_ids and row["candle_id"] == key
                    and len(markets) == 1 and (row["symbol"], row["timeframe"]) in markets)
        entry_fills.append({"event_id": event["event_id"], "fill_id": row["id"],
                            "order_id": row["order_id"], "account_id": row["account_id"],
                            "symbol": row["symbol"], "timeframe": row["timeframe"], "side": row["side"],
                            "quantity": row["quantity"], "price": row["price"], "timestamp": row["timestamp"],
                            "link_state": "EXPLICIT_IDS_OBSERVED" if explicit else "UNVERIFIED"})
    if len({r["side"] for _, r in fills}) > 1:
        conflicts.add("FILL_SIDE_CONFLICT")
    if fills and not order_ids:
        findings.add("BROKER_FILL_UNRECORDED_ON_INTENT")
    if not intents:
        findings.add("EXECUTION_INTENT_NOT_OBSERVED")
    if not fills:
        findings.add("ENTRY_FILL_NOT_OBSERVED")
    latest = (max(intents, key=lambda item: item[1]["source_sequence"])[1]["state"]
              if len(origins) == 1 and "DUPLICATE_INTENT_TRANSITION_ID" not in conflicts else None)
    if latest == "EXECUTION_FAILED" and fills:
        conflicts.add("FAILED_INTENT_WITH_RECORDED_FILL")
    if latest == "EXECUTION_UNCERTAIN":
        findings.add("EXECUTION_UNCERTAIN_RECORDED")

    closed = []
    if len({_origin(e["evidence"]["journal_origin"]) for e, _ in trades}) > 1:
        conflicts.add("MULTIPLE_CLOSED_JOURNAL_ORIGINS")
    for event, row in trades:
        if not row["order_id"]:
            findings.add("JOURNAL_ORDER_LINK_UNVERIFIED")
        elif order_ids and row["order_id"] not in order_ids:
            conflicts.add("JOURNAL_ORDER_LINK_CONFLICT")
        if trade_ids and row["id"] not in trade_ids:
            conflicts.add("JOURNAL_TRADE_LINK_CONFLICT")
        if markets and (row["symbol"], row["timeframe"]) not in markets:
            conflicts.add("JOURNAL_MARKET_CONFLICT")
        if fills and {r["side"] for _, r in fills} != {"buy" if row["direction"] == "long" else "sell"}:
            conflicts.add("JOURNAL_DIRECTION_CONFLICT")
        explicit = (len(order_ids) == len(trade_ids) == 1 and row["order_id"] in order_ids
                    and row["id"] in trade_ids and len(markets) == 1
                    and (row["symbol"], row["timeframe"]) in markets)
        closed.append({"event_id": event["event_id"], "trade_id": row["id"], "order_id": row["order_id"],
                       "decision_id": row["decision_id"], "closed_at": row["closed_at"],
                       "direction": row["direction"],
                       "link_state": "EXPLICIT_ORDER_TRADE_IDS_OBSERVED" if explicit else "UNVERIFIED",
                       "observation_state": "CURRENT" if journal_current else "UNKNOWN",
                       "broker_exit_verified": False})
    if snapshot["truncated"]:
        findings.add("LINK_EVIDENCE_TRUNCATED")
    if not entry_current:
        findings.add("ENTRY_SOURCE_OBSERVATION_UNKNOWN")
    if not journal_current:
        findings.add("JOURNAL_SOURCE_OBSERVATION_UNKNOWN")
    eligible = (entry_current and not snapshot["truncated"] and not conflicts and bool(intents and fills)
                and all(r["link_state"] == "EXPLICIT_IDS_OBSERVED" for r in entry_fills))
    quantity, average = None, None
    if eligible:
        try:
            quantity = math.fsum(r["quantity"] for _, r in fills)
            average = math.fsum((r["quantity"] / quantity) * r["price"] for _, r in fills)
        except OverflowError as exc:
            raise ValueError("Retained fill arithmetic exceeded bound") from exc
        if not math.isfinite(quantity) or not math.isfinite(average) or average <= 0:
            raise ValueError("Retained fill arithmetic exceeded bound")
    state = ("UNKNOWN" if not entry_current or snapshot["truncated"] else
             "CONFLICTING_EVIDENCE" if conflicts else
             "EXPLICIT_ENTRY_FILL_LINKS_OBSERVED" if eligible else "INSUFFICIENT_EVIDENCE")
    return {"scope": "RETAINED_SMC_EXPLICIT_ENTRY_FILL_AND_JOURNAL_LINKS", "execution_key": key,
            "link_state": state, "observation_state": "CURRENT" if entry_current else "UNKNOWN",
            "guardian_snapshot_atomic": True, "cross_database_atomic": False,
            "truncated": snapshot["truncated"], "findings": sorted(findings | conflicts),
            "latest_recorded_state": latest if not snapshot["truncated"] else None,
            "current_execution_state_verified": False,
            "sources": health, "intent_origins": sorted(origins),
            "recorded_order_ids": sorted(order_ids), "recorded_trade_ids": sorted(trade_ids),
            "paper_account_ids": sorted(accounts), "entry_fills": entry_fills, "closed_journal_links": closed,
            "observed_entry_quantity": quantity, "observed_entry_average_price": average,
            "observed_quantity_is_complete": False, "execution_integrity_verified": False,
            "full_lifecycle_verified": False, "position_lifecycle_verified": False,
            "exit_link_verified": False, "paper_account_binding_verified": False,
            "net_pnl_verified": False, "currency_verified": False}
