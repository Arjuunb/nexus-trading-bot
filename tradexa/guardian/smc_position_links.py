"""Bounded exact-ID source position relationships, not current risk/accounting."""
from __future__ import annotations

from .health import component_health
from .smc_execution_links import validate_key, _origin, _matches
from .smc_intent_history import project_transition
from .smc_fill_positions import PROBE, project_fill_position, project_event

MAX_ROWS = 128
MAX_BYTES = 2 * 1024 * 1024
PROBES = ("guardian_smc_intent_history", PROBE)


def smc_position_links_view(store, execution_key, *, now=None):
    key = validate_key(execution_key)
    snapshot = store.smc_position_link_snapshot(key)
    health = component_health(snapshot["heartbeats"], PROBES, now=now)["components"]
    current = all(health[p]["state"] == "HEALTHY" for p in PROBES)
    intents = []
    for event in snapshot["intent_events"]:
        row = project_transition(event["evidence"]["transition"])
        _matches(event, row, key=key)
        if row["execution_key"] != key or event.get("state_after") != row["state"]:
            raise ValueError("Intent identity disagrees with execution key")
        intents.append((row, _origin(event["evidence"]["journal_origin"])))
    rows = [(e, project_event(e)) for e in snapshot["position_events"]]
    findings = {"FULL_POSITION_LIFECYCLE_UNVERIFIED", "JOURNAL_CLOSE_UNVERIFIED"}
    conflicts = set()
    order_ids = {r["broker_order_id"] for r, _ in intents if r["broker_order_id"]}
    trade_ids = {r["trade_id"] for r, _ in intents if r["trade_id"]}
    markets = {(r["symbol"], r["timeframe"]) for r, _ in intents}
    origins = {o for _, o in intents}
    if len(origins)>1:
        conflicts.add("MULTIPLE_JOURNAL_ORIGINS")
    if len({(r["intent_id"], r["session_id"], r["symbol"], r["timeframe"], r["candle_time"], r["proposal_id"]) for r, _ in intents})>1:
        conflicts.add("INTENT_IDENTITY_CONFLICT")
    if len({(o,r["id"]) for r,o in intents})!=len(intents) or len({(o,r["source_sequence"]) for r,o in intents})!=len(intents):
        conflicts.add("DUPLICATE_INTENT_TRANSITION_ID")
    if len(order_ids)>1:
        conflicts.add("MULTIPLE_RECORDED_ORDER_IDS")
    if len(trade_ids)>1:
        conflicts.add("MULTIPLE_RECORDED_TRADE_IDS")
    accounts = {e["evidence"]["account_id"] for e,_ in rows}
    if len(accounts)>1:
        conflicts.add("MULTIPLE_PAPER_ACCOUNTS")
    if len({(e["evidence"]["account_id"], r["fill_id"]) for e,r in rows})!=len(rows):
        conflicts.add("DUPLICATE_RETAINED_FILL_ID")
    seeds, identities = set(), set()
    for event, row in rows:
        value = row["transition"]
        if value is None:
            continue
        account = event["evidence"]["account_id"]
        for side in ("before", "after"):
            pos = value[side]
            if not pos or pos["entry_execution_key"]!=key:
                continue
            if not pos["position_id"] or not pos["entry_order_id"] or not pos["entry_timeframe"]:
                findings.add("ORIGIN_IDENTITY_INCOMPLETE")
                continue
            seeds.add((account, pos["position_id"]))
            identities.add((account, pos["position_id"], pos["entry_order_id"], pos["entry_timeframe"], row["symbol"], pos["side"]))
            if order_ids and pos["entry_order_id"] not in order_ids:
                conflicts.add("ORIGIN_ORDER_LINK_CONFLICT")
            if markets and (row["symbol"],pos["entry_timeframe"]) not in markets:
                conflicts.add("ORIGIN_MARKET_CONFLICT")
    if len(seeds)>1:
        conflicts.add("MULTIPLE_ORIGIN_POSITION_IDS")
    if len(identities)>1:
        conflicts.add("ORIGIN_POSITION_IDENTITY_CONFLICT")
    if not intents:
        findings.add("EXECUTION_INTENT_NOT_OBSERVED")
    if rows and not order_ids:
        findings.add("BROKER_FILL_UNRECORDED_ON_INTENT")
    if not rows:
        findings.add("POSITION_TRANSITION_NOT_OBSERVED")
    latest = (max(intents, key=lambda r:r[0]["source_sequence"])[0]["state"]
              if intents and len(origins)==1 and "DUPLICATE_INTENT_TRANSITION_ID" not in conflicts else None)
    if latest == "EXECUTION_FAILED" and rows:
        conflicts.add("FAILED_INTENT_WITH_RECORDED_FILL")
    if latest == "EXECUTION_UNCERTAIN":
        findings.add("EXECUTION_UNCERTAIN_RECORDED")
    linked, exits = [], []
    for event, row in rows:
        value, account = row["transition"], event["evidence"]["account_id"]
        positions = [value[k] for k in ("before","after") if value and value[k] and (account,value[k]["position_id"]) in seeds]
        relation = bool(positions)
        contribution = value is not None and row["order_id"] in order_ids
        if contribution and not relation:
            findings.add("ORDER_FILL_POSITION_ORIGIN_UNVERIFIED")
        for pos in positions:
            if pos["entry_execution_key"] not in (None,key):
                conflicts.add("POSITION_EXECUTION_KEY_CONFLICT")
            if pos["entry_order_id"] and order_ids and pos["entry_order_id"] not in order_ids:
                conflicts.add("POSITION_ORIGIN_ORDER_CONFLICT")
            if pos["entry_timeframe"] and markets and (row["symbol"],pos["entry_timeframe"]) not in markets:
                conflicts.add("POSITION_MARKET_CONFLICT")
            if identities and (row["symbol"] not in {i[4] for i in identities} or pos["side"] not in {i[5] for i in identities}):
                conflicts.add("POSITION_SIDE_OR_MARKET_CONFLICT")
        before = value["before"] if value else None
        if value and value["reduce_only"] and before and (account,before["position_id"]) in seeds:
            opposing = row["side"] == ("sell" if before["side"]=="long" else "buy")
            if (not opposing or row["quantity"]>before["size"]+1e-10 or value["effect"] not in {"REDUCE","CLOSE"}):
                conflicts.add("REDUCE_ONLY_TRANSITION_CONFLICT")
            else:
                exits.append(row["fill_id"])
        linked.append({"event_id": event["event_id"], "guardian_sequence": event["guardian_sequence"],
                       "account_id": account, "fill": row,
                       "link_state": "EXPLICIT_ACCOUNT_POSITION_IDS_OBSERVED" if relation else
                                     "EXPLICIT_ORDER_ID_OBSERVED" if contribution else "UNVERIFIED"})
    if snapshot["truncated"]:
        findings.add("LINK_EVIDENCE_TRUNCATED")
    if not current:
        findings.add("POSITION_SOURCE_OBSERVATION_UNKNOWN")
    eligible = bool(intents and order_ids and len(seeds)==1 and linked) and not conflicts and all(r["link_state"]=="EXPLICIT_ACCOUNT_POSITION_IDS_OBSERVED" for r in linked)
    state = ("UNKNOWN" if not current or snapshot["truncated"] else "CONFLICTING_EVIDENCE" if conflicts else
             "EXPLICIT_POSITION_EXIT_LINKS_OBSERVED" if eligible and exits else
             "EXPLICIT_POSITION_LINKS_OBSERVED" if eligible else "INSUFFICIENT_EVIDENCE")
    return {"scope": "RETAINED_SMC_EXPLICIT_POSITION_ID_LINKS", "execution_key": key, "link_state": state,
            "observation_state": "CURRENT" if current else "UNKNOWN", "sources": health,
            "guardian_snapshot_atomic": True, "cross_database_atomic": False, "truncated": snapshot["truncated"],
            "findings": sorted(findings|conflicts), "origin_position_ids": sorted({p for _,p in seeds}),
            "paper_account_ids": sorted(accounts), "recorded_order_ids": sorted(order_ids), "recorded_trade_ids": sorted(trade_ids),
            "position_transitions": linked, "observed_exit_fill_ids": sorted(exits) if not conflicts and not snapshot["truncated"] else [],
            "latest_recorded_state": latest if not snapshot["truncated"] else None,
            "current_position_state_verified": False, "execution_integrity_verified": False,
            "exit_link_verified": False, "full_lifecycle_verified": False, "position_lifecycle_verified": False,
            "paper_account_binding_verified": False, "net_pnl_verified": False, "currency_verified": False}
