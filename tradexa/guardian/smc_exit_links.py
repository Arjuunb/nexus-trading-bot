"""Exact retained SMC exit IDs, not current positions or journal-close proof.

Reads only Guardian's database. Time/price similarity never establishes a
relationship. The source journal has no exit-fill/position IDs, so an entry
trade association cannot certify that a journal close records this exit.
"""
from __future__ import annotations

from .events import GuardianEvent
from .health import component_health
from .lab_fill_history import digest, _timestamp
from .smc_execution_links import validate_key, _origin, _matches
from . import smc_intent_history as intents
from . import smc_fill_positions as positions
from . import smc_exit_fills as exits
from . import smc_journal_history as journals

MAX_ROWS = 128
MAX_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 16384
PROBES = (intents.PROBE, positions.PROBE, exits.PROBE, journals.PROBE)


def project_retained(event, stream):
    """Revalidate imported envelopes, including false certification flags.

Reconstruct the collector's exact event, not its inferred trading history.
This also detects privileged restore/corruption of cached evidence.
"""
    decoded = GuardianEvent.from_payload({k: v for k, v in event.items()
                                         if k not in ("guardian_sequence", "received_at")})
    evidence = event["evidence"]
    if stream in (1, 2):
        module = positions if stream == 1 else exits
        row = module.project_event(event)
        account = evidence["account_id"]
        expected = GuardianEvent(source_service=module.PROBE, source_component=module.COMPONENT,
            event_type="smc_fill_position_observed" if stream == 1 else "smc_exit_evidence_observed",
            timestamp=_timestamp(row["timestamp"]),
            event_id=digest(["smc-fill-position-event-v1" if stream == 1 else "smc-exit-fill-event-v1", account, row["fill_id"]]),
            lab_id="SMC", order_id=row["order_id"], symbol=row["symbol"],
            reason="RECORDED_PAPER_POSITION_TRANSITION_ONLY" if stream == 1 else "RECORDED_PAPER_EXIT_EVIDENCE_ONLY",
            evidence={"account_id": account, "account_type": "SMC_LAB", "fill": row},
            metadata={"coverage": module.SCOPE, "paper_only": True, "full_lifecycle_verified": False})
        origin = account
    elif stream in (0, 3):
        module = intents if stream == 0 else journals
        row = module.project_transition(evidence["transition"]) if stream == 0 else module.project_trade(evidence["trade"])
        origin = _origin(evidence["journal_origin"])
        _matches(event, row, key=row["execution_key"] if stream == 0 else None)
        common = dict(source_service=module.PROBE, source_component=module.COMPONENT,
            lab_id="SMC", agent_id="smc_agent", symbol=row["symbol"], timeframe=row["timeframe"],
            metadata={"coverage": module.SCOPE, "paper_only": True})
        if stream == 0:
            expected = GuardianEvent(**common, event_type="smc_intent_transition_observed",
                timestamp=_timestamp(row["created_at"]), event_id=digest(["smc-intent-event-v1", origin, row["id"]]),
                session_id=row["session_id"], execution_id=row["execution_key"], order_id=row["broker_order_id"],
                state_after=row["state"], reason="RECORDED_EXECUTION_INTENT_EVENT_ONLY",
                evidence={"transition": row, "journal_origin": origin, "broker_execution_verified": False,
                          "journal_trade_verified": False, "position_lifecycle_verified": False})
        else:
            if row["closed_at"] is None:
                raise ValueError("Retained journal record is not closed")
            expected = GuardianEvent(**common, event_type="smc_closed_journal_observed",
                timestamp=_timestamp(row["closed_at"]), event_id=digest(["smc-closed-journal-v1", origin, row["id"]]),
                order_id=row["order_id"], correlation_id=row["decision_id"], state_after="JOURNAL_CLOSED",
                reason="RECORDED_JOURNAL_CLOSE_ONLY", evidence={"trade": row, "journal_origin": origin,
                    "broker_execution_verified": False, "position_lifecycle_verified": False,
                    "net_pnl_verified": False, "currency_verified": False})
    else:
        raise ValueError("Unknown retained stream")
    if decoded.canonical_json() != expected.canonical_json():
        raise ValueError("Retained evidence disagrees with collector contract")
    return row, origin


def smc_exit_links_view(store, execution_key, *, now=None):
    key = validate_key(execution_key)
    snapshot = store.smc_exit_link_snapshot(key)
    health = component_health(snapshot["heartbeats"], PROBES, now=now)["components"]
    current = all(health[p]["state"] == "HEALTHY" for p in PROBES[:3])
    journal_current = health[PROBES[3]]["state"] == "HEALTHY"
    intent_rows = [project_retained(e, 0) for e in snapshot["intent_events"]]
    position_rows = [project_retained(e, 1) for e in snapshot["position_events"]]
    exit_rows = [(e, *project_retained(e, 2)) for e in snapshot["exit_events"]]
    journal_rows = [(e, *project_retained(e, 3)) for e in snapshot["journal_events"]]
    if any(r["execution_key"] != key for r, _ in intent_rows):
        raise ValueError("Intent execution key mismatch")
    findings = {"JOURNAL_EXIT_IDS_NOT_CAPTURED", "FULL_POSITION_LIFECYCLE_UNVERIFIED"}
    conflicts = set()
    orders = {r["broker_order_id"] for r, _ in intent_rows if r["broker_order_id"]}
    trades = {r["trade_id"] for r, _ in intent_rows if r["trade_id"]}
    origins = {o for _, o in intent_rows}
    markets = {(r["symbol"], r["timeframe"]) for r, _ in intent_rows}
    identity = {(r["intent_id"], r["session_id"], r["symbol"], r["timeframe"], r["candle_time"], r["proposal_id"])
                for r, _ in intent_rows}
    for values, code in ((orders, "MULTIPLE_RECORDED_ORDER_IDS"), (trades, "MULTIPLE_RECORDED_TRADE_IDS"),
                         (origins, "MULTIPLE_JOURNAL_ORIGINS"), (identity, "INTENT_IDENTITY_CONFLICT")):
        if len(values) > 1:
            conflicts.add(code)
    if (len({(o, r["source_sequence"]) for r, o in intent_rows}) != len(intent_rows) or
            len({(o, r["id"]) for r, o in intent_rows}) != len(intent_rows)):
        conflicts.add("DUPLICATE_INTENT_TRANSITION_ID")
    latest = (max(intent_rows, key=lambda v: v[0]["source_sequence"])[0]["state"]
              if len(origins) == 1 and "DUPLICATE_INTENT_TRANSITION_ID" not in conflicts else None)
    if latest == "EXECUTION_UNCERTAIN":
        findings.add("EXECUTION_UNCERTAIN_RECORDED")
    if latest == "EXECUTION_FAILED" and position_rows:
        conflicts.add("FAILED_INTENT_WITH_RECORDED_FILL")
    if not intent_rows:
        findings.add("EXECUTION_INTENT_NOT_OBSERVED")

    accounts = {a for _, a in position_rows} | {a for _, _, a in exit_rows}
    if len(accounts) > 1:
        conflicts.add("MULTIPLE_PAPER_ACCOUNTS")
    seeds, identities, entry_origins = set(), set(), set()
    for row, account in position_rows:
        value = row["transition"]
        for side in ("before", "after"):
            pos = value[side] if value else None
            if not pos or pos["entry_execution_key"] != key:
                continue
            if not all(pos[f] for f in ("position_id", "entry_order_id", "entry_timeframe")):
                findings.add("ORIGIN_IDENTITY_INCOMPLETE")
                continue
            seed = (account, pos["position_id"])
            seeds.add(seed)
            identities.add((account, pos["position_id"], pos["entry_order_id"], pos["entry_timeframe"], row["symbol"], pos["side"]))
            if orders and pos["entry_order_id"] not in orders:
                conflicts.add("ORIGIN_ORDER_LINK_CONFLICT")
            if markets and (row["symbol"], pos["entry_timeframe"]) not in markets:
                conflicts.add("ORIGIN_MARKET_CONFLICT")
            # Reversals create a legitimate new origin too. Increasing an old
            # net position is not a new origin for a different execution key.
            if (side == "after" and value["effect"] in {"OPEN", "REVERSE"} and
                    row["order_id"] == pos["entry_order_id"] and row["order_id"] in orders and
                    row["side"] == ("buy" if pos["side"] == "long" else "sell")):
                entry_origins.add(seed)
    if len(seeds) > 1:
        conflicts.add("MULTIPLE_ORIGIN_POSITION_IDS")
    if len(identities) > 1:
        conflicts.add("ORIGIN_POSITION_IDENTITY_CONFLICT")
    position_by_fill = {(a, r["fill_id"]): r for r, a in position_rows}
    if len(position_by_fill) != len(position_rows):
        conflicts.add("DUPLICATE_RETAINED_FILL_ID")
    linked, observed = [], []
    for event, row, account in exit_rows:
        value = row["exit_evidence"]
        pos = value["position"] if value else None
        complete = bool(pos and all(pos[f] for f in ("position_id", "entry_order_id", "entry_execution_key", "entry_timeframe")))
        counterpart = position_by_fill.get((account, row["fill_id"]))
        transition = counterpart["transition"] if counterpart else None
        # The cursor stream includes every fill, including entries/additions
        # with null exit capture. An explicit non-reducing transition is not
        # an exit candidate. Null/legacy transitions remain unverified.
        if value is None and transition is not None and transition["effect"] not in {"REDUCE", "CLOSE", "REVERSE"}:
            continue
        candidate = pos or (transition["before"] if transition else None)
        if (candidate and candidate["entry_execution_key"] not in (None, key) and
                (account, candidate["position_id"]) not in seeds and candidate["entry_order_id"] not in orders):
            # A reversal's single fill closes the OLD origin and opens a NEW
            # one. The cross-fill lookup must not assign the old exit to the
            # incoming execution merely because they share a fill/order.
            continue
        explicit = False
        if not complete:
            findings.add("EXIT_ORIGIN_OR_CAPTURE_UNVERIFIED")
        elif transition is None:
            findings.add("EXIT_TRANSITION_NOT_OBSERVED")
        else:
            if (any(row[f] != counterpart[f] for f in ("source_sequence", "fill_id", "order_id", "timestamp", "symbol", "side", "quantity", "price")) or
                    pos != transition["before"] or value["reduce_only"] != transition["reduce_only"] or
                    value["persisted_order"] != transition["persisted_order"] or transition["effect"] not in {"REDUCE", "CLOSE", "REVERSE"}):
                conflicts.add("EXIT_TRANSITION_EVIDENCE_CONFLICT")
            if pos["entry_execution_key"] != key:
                conflicts.add("POSITION_EXECUTION_KEY_CONFLICT")
            if orders and pos["entry_order_id"] not in orders:
                conflicts.add("ORIGIN_ORDER_LINK_CONFLICT")
            if markets and (row["symbol"], pos["entry_timeframe"]) not in markets:
                conflicts.add("ORIGIN_MARKET_CONFLICT")
            if identities and (account, pos["position_id"], pos["entry_order_id"], pos["entry_timeframe"], row["symbol"], pos["side"]) not in identities:
                conflicts.add("ORIGIN_POSITION_IDENTITY_CONFLICT")
            explicit = bool(intent_rows and len(orders) == 1 and pos["entry_order_id"] in orders and
                            pos["entry_execution_key"] == key and len(markets) == 1 and
                            (row["symbol"], pos["entry_timeframe"]) in markets and
                            (account, pos["position_id"]) in entry_origins)
        if explicit:
            observed.append(row["fill_id"])
        linked.append({"event_id": event["event_id"], "account_id": account, "fill_id": row["fill_id"],
            "order_id": row["order_id"], "timestamp": row["timestamp"], "exit_evidence": value,
            "link_state": "EXPLICIT_ACCOUNT_POSITION_FILL_IDS_OBSERVED" if explicit else "UNVERIFIED"})

    closed = []
    if len({o for _, _, o in journal_rows}) > 1:
        conflicts.add("MULTIPLE_CLOSED_JOURNAL_ORIGINS")
    sides = {i[5] for i in identities}
    for event, row, _ in journal_rows:
        if row["order_id"] and orders and row["order_id"] not in orders:
            conflicts.add("JOURNAL_ORDER_LINK_CONFLICT")
        if trades and row["id"] not in trades:
            conflicts.add("JOURNAL_TRADE_LINK_CONFLICT")
        if markets and (row["symbol"], row["timeframe"]) not in markets:
            conflicts.add("JOURNAL_MARKET_CONFLICT")
        if sides and sides != {row["direction"]}:
            conflicts.add("JOURNAL_DIRECTION_CONFLICT")
        explicit = (len(orders) == len(trades) == 1 and row["order_id"] in orders and row["id"] in trades and
                    len(markets) == 1 and (row["symbol"], row["timeframe"]) in markets and sides == {row["direction"]})
        closed.append({"event_id": event["event_id"], "trade_id": row["id"], "entry_order_id": row["order_id"],
            "decision_id": row["decision_id"], "closed_at": row["closed_at"],
            "link_state": "EXPLICIT_ENTRY_ORDER_TRADE_IDS_OBSERVED" if explicit else "UNVERIFIED",
            "observation_state": "CURRENT" if journal_current else "UNKNOWN",
            "journal_close_verified": False, "broker_exit_verified": False})
    if snapshot["truncated"]:
        findings.add("LINK_EVIDENCE_TRUNCATED")
    if not current:
        findings.add("EXIT_SOURCE_OBSERVATION_UNKNOWN")
    if not journal_current:
        findings.add("JOURNAL_SOURCE_OBSERVATION_UNKNOWN")
    eligible = bool(observed and len(observed) == len(linked)) and not conflicts
    state = ("UNKNOWN" if not current or snapshot["truncated"] else "CONFLICTING_EVIDENCE" if conflicts else
             "EXPLICIT_EXIT_POSITION_LINKS_OBSERVED" if eligible else "INSUFFICIENT_EVIDENCE")
    # Even historical per-row associations must not conceal contradictory IDs.
    if conflicts:
        for row in linked + closed:
            row["link_state"] = "CONFLICTING_EVIDENCE"
    return {"scope": "RETAINED_SMC_EXPLICIT_EXIT_POSITION_AND_ENTRY_JOURNAL_LINKS", "execution_key": key,
        "link_state": state, "observation_state": "CURRENT" if current else "UNKNOWN", "sources": health,
        "guardian_snapshot_atomic": True, "cross_database_atomic": False, "truncated": snapshot["truncated"],
        "evidence_rows_loaded": snapshot["evidence_rows_loaded"], "findings": sorted(findings | conflicts),
        "latest_recorded_state": latest if not snapshot["truncated"] else None,
        "intent_origins": sorted(origins), "paper_account_ids": sorted(accounts),
        "origin_position_ids": sorted({p for _, p in seeds}), "recorded_order_ids": sorted(orders),
        "recorded_trade_ids": sorted(trades), "exit_links": linked, "closed_journal_links": closed,
        "observed_exit_fill_ids": observed if eligible and current and not snapshot["truncated"] else [],
        "journal_close_verified": False, "exit_link_verified": False, "execution_integrity_verified": False,
        "current_execution_state_verified": False, "current_position_state_verified": False,
        "current_protection_verified": False, "position_lifecycle_verified": False, "full_lifecycle_verified": False,
        "source_history_immutable_verified": False, "paper_account_binding_verified": False,
        "net_pnl_verified": False, "currency_verified": False}
