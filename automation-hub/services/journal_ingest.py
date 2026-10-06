"""Ingest lab and backtest trades into the canonical journal.

Price Action Lab and SMC Strategy Lab execute through their own isolated
``PaperBrokerV2`` ledgers. Their trades are reconstructed here from the fills
those brokers stored — a round trip starts when the net position leaves zero
and ends when it returns to zero — and joined to the order metadata and frozen
setup evidence each lab saved when it placed the order. Nothing is derived
from current market data, and nothing a lab did not store is invented.

Ingestion is idempotent and incremental: running it again appends only new
fills (keyed by fill id) and finalises trips that have since closed. A lab
reset or factory reset never removes journal history.

The JSON replay journal is mapped in as BACKTEST so it can never be counted
as forward performance.
"""
from __future__ import annotations

import os
from bisect import bisect_right
from typing import Optional

from execution.paper_broker_v2 import PaperBrokerV2
from services.journal_sessions import timing_fields
from services.trade_journal import (
    TradeJournalRecorder, _iso, _now, _num, normalize_exit_reason, risk_fields,
    split_conditions, split_symbol, strategy_setup,
)

_EPS = 1e-12
_LAB_NAMES = {"PRICE_ACTION_LAB": "Price Action Lab", "SMC_LAB": "SMC Lab"}
_LAB_FAMILY = {"PRICE_ACTION_LAB": "PRICE_ACTION", "SMC_LAB": "SMC"}


def round_trips(fills: list[dict]) -> list[dict]:
    """Group fills (oldest first) into position round trips per symbol."""
    trips: list[dict] = []
    current: dict[str, dict] = {}
    for fill in fills:
        symbol = fill["symbol"]
        qty = float(fill["quantity"] or 0)
        if qty <= 0:
            continue
        signed = qty if str(fill["side"]).lower() == "buy" else -qty
        trip = current.get(symbol)
        if trip is None:
            trip = {"symbol": symbol, "direction": "LONG" if signed > 0 else "SHORT",
                    "entries": [], "exits": [], "sequence": [], "position": 0.0, "closed": False}
            current[symbol] = trip
            trips.append(trip)
        same_way = (signed > 0) == (trip["position"] > 0) or abs(trip["position"]) <= _EPS
        if same_way:
            trip["entries"].append(fill)
            trip["sequence"].append(("entry", fill))
            trip["position"] += signed
            continue
        closing = min(abs(signed), abs(trip["position"]))
        exit_fill = {**fill, "quantity": closing}
        trip["exits"].append(exit_fill)
        trip["sequence"].append(("exit", exit_fill))
        trip["position"] += closing if signed > 0 else -closing
        if abs(trip["position"]) <= _EPS * max(1.0, qty):
            trip["closed"] = True
            current.pop(symbol, None)
            remainder = abs(signed) - closing
            if remainder > _EPS:
                # a reversal fill: the excess opens a new trip
                opening = {**fill, "quantity": remainder, "realized_pnl": 0.0, "fee": 0.0,
                           "id": f"{fill['id']}:flip"}
                flip = {"symbol": symbol, "direction": "LONG" if signed > 0 else "SHORT",
                        "entries": [opening], "exits": [], "sequence": [("entry", opening)],
                        "position": remainder if signed > 0 else -remainder, "closed": False}
                current[symbol] = flip
                trips.append(flip)
    return trips


def _research(export: dict, order_id: str) -> dict:
    lookup = export.get("research_lookup")
    if callable(lookup):
        try:
            return lookup(order_id) or {}
        except Exception:  # noqa: BLE001 — enrichment only
            return {}
    return (export.get("research") or {}).get(order_id) or {}


def _candidate(export: dict, proposal_id: Optional[str]) -> dict:
    lookup = export.get("candidate_lookup")
    if callable(lookup) and proposal_id:
        try:
            return lookup(proposal_id) or {}
        except Exception:  # noqa: BLE001 — enrichment only
            return {}
    return (export.get("candidates") or {}).get(proposal_id) or {}


def _fill_time(fill: dict) -> Optional[str]:
    return _iso(fill.get("fill_timestamp") or fill.get("timestamp"))


def _lab_snapshot(lab_id: str, meta: dict, export: dict, decided_at: Optional[str]) -> dict:
    config = meta.get("config") or {}
    family = _LAB_FAMILY[lab_id]
    if lab_id == "PRICE_ACTION_LAB":
        setup = config.get("setup") or {}
        proposal = config.get("proposal") or {}
        context = setup.get("context_snapshot") or {}
        zone = context.get("zone") or {}
        event = context.get("trigger_event") or {}
        reads = [{"name": str(r), "status": "Passed"} for r in setup.get("reasons") or []]
        reads += [{"name": str(r), "status": "Missing"} for r in setup.get("missing_conditions") or []]
        patterns = [str(row.get("pattern") or row.get("name")) for row in setup.get("pattern_metadata") or []
                    if isinstance(row, dict)]
        not_used = {"status": "NOT_EVALUATED", "detail": "not part of this strategy"}
        fields = {
            "support_resistance_level": {"status": "PASSED" if zone else "NOT_EVALUATED",
                                         "detail": f"{zone.get('role')} {zone.get('low')}–{zone.get('high')}"
                                         if zone else "zone not captured"},
            "rejection_level": {"status": "PASSED" if event else "NOT_EVALUATED",
                                "detail": f"{event.get('event_type')} at {event.get('level')}" if event else
                                "trigger event not captured"},
            "flip_retest": {"status": "PASSED" if zone.get("flipped") else "NOT_EVALUATED",
                            "detail": "flipped zone" if zone.get("flipped") else "zone not flipped"},
            "trend_direction": {"status": "PASSED" if context.get("structure_state") else "NOT_EVALUATED",
                                "detail": str(context.get("structure_state") or "not captured")},
            "rejection_candle": {"status": "PASSED" if patterns else "NOT_EVALUATED",
                                 "detail": ", ".join(p for p in patterns if "dominan" not in p.lower())
                                 or "no pattern recorded"},
            "dominance_candle": {"status": "PASSED" if any("dominan" in p.lower() for p in patterns)
                                 else "NOT_EVALUATED",
                                 "detail": ", ".join(p for p in patterns if "dominan" in p.lower())
                                 or "not recorded for this setup"},
            "ema_relationship": not_used, "volume_confirmation": not_used, "liquidity_sweep": not_used,
            "entry_trigger": {"status": "PASSED", "detail": f"{proposal.get('entry_model')}; "
                                                            f"trigger {proposal.get('trigger_low')}–"
                                                            f"{proposal.get('trigger_high')}"},
        }
        passed, failed, missing = split_conditions(reads)
        research = _research(export, meta.get("order_id"))
        mtf = (research.get("mtf_evidence") or {}).get("primary") or {}
        return {
            "captured_at": meta.get("created_at"), "decision": "ENTER_" + ("LONG" if meta.get("direction") == "bullish" else "SHORT"),
            "decision_reason": f"{meta.get('strategy_id')} {meta.get('direction')} {proposal.get('entry_model') or ''}"
                               f" at zone {zone.get('low')}–{zone.get('high')}".strip(),
            "conditions_passed": passed, "conditions_failed": failed, "conditions_missing": missing,
            "setup_score": None, "confidence": None,
            "market_bias": context.get("structure_state"), "htf_bias": mtf.get("htf_bias"),
            "risk_decision": f"ALLOWED — risk {config.get('risk_pct')}% within the lab limits",
            "feed_health": context.get("market_data_health"),
            "strategy_state": setup.get("phase"), "execution_state": meta.get("status"),
            "strategy_family": family, "setup": strategy_setup(family, reads, fields),
            "market_context": {"structure_state": context.get("structure_state"), "zone": zone,
                               "trigger_event": event, "zone_age": context.get("zone_age"),
                               "mtf_evidence": research.get("mtf_evidence")},
            "risk": {"risk_pct": config.get("risk_pct"), "risk_amount": config.get("risk_amount"),
                     "planned_rr": config.get("planned_rr"), "execution": config.get("execution")},
            "raw": {"proposal": proposal, "setup_id": meta.get("setup_id")},
            "source": lab_id,
        }
    # SMC
    candidate = _candidate(export, meta.get("proposal_id"))
    evaluation = (candidate.get("payload") or {}).get("evaluation") or {}
    conditions = evaluation.get("ordered_condition_results") or []
    passed, failed, missing = split_conditions(conditions)
    mtf = (evaluation.get("mtf_evidence") or {}).get("primary") or {}
    return {
        "captured_at": candidate.get("created_at") or meta.get("created_at"),
        "decision": "ENTER_" + ("LONG" if meta.get("direction") == "bullish" else "SHORT"),
        "decision_reason": f"{meta.get('model_id') or 'SMC'} {evaluation.get('state') or ''}: "
                           + (evaluation.get("next_required_event") or "entry conditions met"),
        "conditions_passed": passed, "conditions_failed": failed, "conditions_missing": missing
        + [{"name": str(m), "status": "MISSING"} for m in evaluation.get("missing_conditions") or []],
        "setup_score": None, "confidence": None, "market_bias": mtf.get("htf_bias"),
        "htf_bias": mtf.get("htf_bias"),
        "risk_decision": f"ALLOWED — risk {meta.get('risk_pct')}% within the lab limits",
        "feed_health": "SYNCHRONIZED" if candidate.get("status") != "DATA_PAUSED" else "UNRELIABLE",
        "strategy_state": evaluation.get("state"), "execution_state": meta.get("status"),
        "strategy_family": family, "setup": strategy_setup(family, conditions),
        "market_context": {"mtf_evidence": evaluation.get("mtf_evidence"),
                           "native_object_ids": evaluation.get("native_object_ids")},
        "risk": {"trade_plan": evaluation.get("trade_plan"), "risk_pct": meta.get("risk_pct")},
        "raw": {"proposal_id": meta.get("proposal_id"), "setup_id": meta.get("setup_id")},
        "source": lab_id,
    }


def _position_entry(trip: dict) -> tuple[float, dict]:
    """The broker's position entry price after the trip's last entry fill,
    and that fill. PaperBrokerV2 re-averages the entry on each entry fill over
    the size still open, so an exit between two entry fills changes the
    weights; the protection it arms is resolved from this price."""
    size, entry, last = 0.0, None, None
    for kind, fill in trip.get("sequence") or [("entry", f) for f in trip["entries"]]:
        qty, price = float(fill["quantity"]), float(fill["price"])
        if kind == "entry":
            entry = price if (entry is None or size <= _EPS) else (entry * size + price * qty) / (size + qty)
            size += qty
            last = fill
        else:
            size -= qty
    return entry, last


def _armed_protection(direction: str, entry_price: float, order: dict,
                      stop: Optional[float], target: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """The stop and target the lab broker actually armed on the position.

    When an order carries a frozen target R the broker re-anchors the target to
    the real average fill (``PaperBrokerV2._resolved_protection``), so the
    order's pre-fill target is only the plan. The same rule is applied here to
    the same stored inputs; anything else keeps the stored plan."""
    if order.get("protection_stop_loss") is None or order.get("protection_target_r") is None:
        return stop, target
    try:
        return PaperBrokerV2._resolved_protection(
            {"side": "long" if direction == "LONG" else "short", "entry_price": entry_price},
            {"stop_loss": order["protection_stop_loss"], "take_profit": order.get("protection_take_profit"),
             "target_r": order["protection_target_r"], "tick_size": order.get("protection_tick_size")})
    except (TypeError, ValueError):
        return stop, target


def _exit_reason(fill: dict, meta: dict, stop: Optional[float], target: Optional[float],
                 orders: dict) -> tuple[str, str]:
    order_id = str(fill.get("order_id") or "")
    ownership = str(meta.get("ownership") or "")
    if order_id.startswith("liquidation-"):
        return "LIQUIDATION", "LAB_LIQUIDATION_FILL"
    if "target_1" in ownership:
        return "PARTIAL_TAKE_PROFIT", "LAB_ORDER_OWNERSHIP"
    if order_id.startswith("protective-") and stop is not None and target is not None:
        price = float(fill["price"])
        return (("STOP_LOSS" if abs(price - stop) <= abs(price - target) else "TAKE_PROFIT"),
                "INFERRED_FROM_FILL_PRICE")
    order = orders.get(order_id) or {}
    reason = normalize_exit_reason(order.get("reason"))
    if reason != "UNKNOWN":
        return reason, "LAB_ORDER_REASON"
    if "manual" in ownership or "manual" in str(order.get("reason") or "").lower():
        return "MANUAL_CLOSE", "LAB_ORDER_OWNERSHIP"
    return "UNKNOWN", "NOT_RECORDED"


def _funding_owners(trips: list[dict], funding: list[dict]) -> dict[int, list[dict]]:
    """Attribute each booked funding event to the round trip it was charged to.

    A lab broker holds at most one position per symbol, and funding is only
    booked while that position is open, so an event belongs to the trip on
    its symbol that opened at or before it and before the next trip on that
    symbol opened. Events of one broker position always stay together.
    Trips per symbol are in fill order, so each event is placed by a binary
    search over their start times (O(events x log trips))."""
    starts: dict[str, list[str]] = {}
    owners: dict[str, list[int]] = {}
    for i, trip in enumerate(trips):
        start = _fill_time(trip["entries"][0])
        if start:
            starts.setdefault(trip["symbol"], []).append(start)
            owners.setdefault(trip["symbol"], []).append(i)
    owner_of: dict[int, int] = {}
    position_owner: dict[str, int] = {}
    ordered = sorted(enumerate(funding), key=lambda item: _iso(item[1].get("funding_timestamp")
                                                                or item[1].get("created_at")) or "")
    for k, event in ordered:
        at = _iso(event.get("funding_timestamp") or event.get("created_at"))
        position = event.get("position_id")
        if position and position in position_owner:
            owner_of[k] = position_owner[position]
            continue
        symbol = event.get("symbol")
        j = bisect_right(starts[symbol], at) - 1 if (at and symbol in starts) else -1
        if j < 0:
            continue
        owner_of[k] = owners[symbol][j]
        if position:
            position_owner[position] = owners[symbol][j]
    out: dict[int, list[dict]] = {}
    for k, i in owner_of.items():
        out.setdefault(i, []).append(funding[k])
    return out


def ingest_v2_lab(recorder: TradeJournalRecorder, export: dict) -> dict:
    """Bring one lab's stored round trips into the canonical journal."""
    store = recorder.store
    lab_id = export["lab_id"]
    lab_name = _LAB_NAMES[lab_id]
    family = _LAB_FAMILY[lab_id]
    account_id = export.get("account_id") or lab_id
    orders = export.get("orders") or {}
    meta_by_order = export.get("meta") or {}
    sessions = export.get("sessions") or {}
    summary = {"created": 0, "exits_added": 0, "finalised": 0}
    trips = round_trips(export.get("fills") or [])
    funding_by_trip = _funding_owners(trips, export.get("funding") or [])
    finished = store.finalised_refs("LAB_FILL")
    for index, trip in enumerate(trips):
        if trip["closed"] and str(trip["entries"][0]["id"]) in finished:
            continue   # a finished trip can gain no new fills
        # One trip per lock hold: a live fill hook waits for at most one
        # trip's writes, never for a whole import.
        with recorder.bulk_item():
            first = trip["entries"][0]
            entry_order_id = str(first.get("order_id") or "")
            meta = meta_by_order.get(entry_order_id) or {}
            order = orders.get(entry_order_id) or {}
            config = meta.get("config") or {}
            session = sessions.get(meta.get("session_id")) or {}
            key = f"{account_id}:{first['id']}"
            trade_id = store.resolve_link("LAB_FILL", str(first["id"]))
            direction = trip["direction"]
            entry_qty = sum(float(f["quantity"]) for f in trip["entries"])
            entry_price = sum(float(f["quantity"]) * float(f["price"]) for f in trip["entries"]) / entry_qty
            stop = _num(meta.get("stop") if meta.get("stop") is not None else config.get("stop")) \
                or _num(order.get("protection_stop_loss"))
            target = _num(meta.get("target") if meta.get("target") is not None else
                          (meta.get("target_2") if meta.get("target_2") is not None else config.get("target"))) \
                or _num(order.get("protection_take_profit"))
            planned_target = target
            position_entry, last_entry = _position_entry(trip)
            armed_by = orders.get(str(last_entry.get("order_id") or "")) or order
            stop, target = _armed_protection(direction, position_entry, armed_by, stop, target)
            stop, target = _num(stop), _num(target)
            # Entry facts settle when the entry order does (or the trip has
            # closed), not at the first exit: a partially filled order can keep
            # adding to the same position after a scale-out.
            entry_complete = trip["closed"] or str(order.get("status") or "") in (
                "filled", "cancelled", "expired", "rejected") or not order
            if trade_id is None:
                filled_at = _fill_time(first)
                leverage = _num(config.get("leverage"))
                equity = _num(config.get("account_equity_before"))
                risk = risk_fields(direction=direction, entry=entry_price, stop=stop, target=target,
                                   quantity=entry_qty, equity_before=equity,
                                   max_allowed_risk_pct=_num(config.get("max_risk_pct")
                                                             or (config.get("execution") or {}).get("max_risk_pct")),
                                   leverage=leverage)
                if risk.get("risk_pct") is None and _num(config.get("risk_pct") or meta.get("risk_pct")) is not None:
                    risk["risk_pct"] = _num(config.get("risk_pct") or meta.get("risk_pct"))
                strategy_id = meta.get("model_id") or meta.get("strategy_id") or first.get("strategy") or lab_id
                mode = ("ISOLATED_FORWARD_PAPER" if str(session.get("mode") or "LIVE_PAPER") == "LIVE_PAPER"
                        else "SIMULATION")
                base, quote = split_symbol(trip["symbol"])
                requested = _num(order.get("requested_price") or first.get("requested_price"))
                slippage = None
                if requested is not None:
                    slippage = (entry_price - requested) if direction == "LONG" else (requested - entry_price)
                fields = {
                    "source_system": lab_id, "source_trade_key": key, "order_id": entry_order_id or None,
                    "execution_id": str(first["id"]), "bot_id": lab_id, "lab_id": lab_id,
                    "lab_session_id": meta.get("session_id"),
                    "strategy_id": strategy_id, "strategy_name": lab_name, "strategy_family": family,
                    "strategy_version": meta.get("model_version") or export.get("strategy_version")
                    or first.get("strategy_version"),
                    "trade_source": lab_id, "trading_mode": mode,
                    "exchange": "Binance USD-M Futures", "market_type": "PERPETUAL_FUTURES",
                    "symbol": trip["symbol"].upper(), "base_asset": base, "quote_asset": quote,
                    "direction": direction, "timeframe": first.get("timeframe") or session.get("timeframe"),
                    "status": "OPEN",
                    "signal_at": _iso(order.get("signal_timestamp") or first.get("signal_timestamp")),
                    "order_created_at": _iso(order.get("created_at") or first.get("order_timestamp")),
                    "entry_filled_at": filled_at, "requested_entry_price": requested,
                    "entry_price": entry_price, "quantity": entry_qty,
                    "entry_slippage": slippage,
                    "entry_slippage_cost": slippage * entry_qty if slippage is not None else None,
                    "leverage": leverage, "leverage_source": "LAB_ACCOUNT_SETTING" if leverage else None,
                    "account_balance_before": _num(config.get("account_balance_before")),
                    "account_equity_before": equity,
                    "available_margin_before": _num(config.get("available_margin_before")),
                    "initial_stop": stop, "initial_target": target, "current_stop": stop, "current_target": target,
                    **{k: v for k, v in risk.items() if v is not None},
                    "decision": "ENTER_" + direction,
                    "data_completeness": "LAB_FILLS" + ("" if meta else "_NO_ORDER_METADATA"),
                    "entry_locked": 1 if entry_complete else 0,
                    **{k: v for k, v in timing_fields(filled_at).items() if k != "duration_s"},
                }
                # A trip's identity is its first fill. One entry order can open two
                # positions (its remainder fills after the first closed), so the
                # order id must not dedupe trips; it is linked to its first trip only.
                trade_id, created = store.create_trade(fields, links=[("LAB_FILL", str(first["id"]))])
                if created:
                    if entry_order_id:
                        store.add_link(trade_id, "LAB_ORDER", entry_order_id)
                    summary["created"] += 1
                    if meta:
                        snapshot = _lab_snapshot(lab_id, meta, export, fields["order_created_at"])
                        snapshot["risk"] = {**(snapshot.get("risk") or {}),
                                            "pre_fill_target": planned_target, "armed_stop": stop,
                                            "armed_target": target,
                                            "target_r": _num(order.get("protection_target_r"))}
                        store.save_snapshot(trade_id, snapshot)
                        mtf_primary = ((snapshot.get("market_context") or {}).get("mtf_evidence") or {})
                        mtf_primary = mtf_primary.get("primary") if isinstance(mtf_primary, dict) else None
                        store.update_trade(trade_id, {
                            "htf_bias": snapshot.get("htf_bias"),
                            "htf_timeframe": (mtf_primary or {}).get("htf_timeframe"),
                            "market_regime": snapshot.get("market_bias"),
                        })
                    if fields["signal_at"]:
                        store.add_event(trade_id, "setup-detected", f"{lab_name} {strategy_id} setup",
                                        ts=fields["signal_at"], actor=lab_id)
                    if fields["order_created_at"]:
                        store.add_event(trade_id, "order-submitted",
                                        f"{direction} {entry_qty:.8g} {trip['symbol']} (lab order {entry_order_id})",
                                        ts=fields["order_created_at"], actor=lab_id)
            trade = store.get_trade(trade_id)
            for fill in trip["entries"]:
                if store.add_execution(trade_id, {
                        "execution_id": f"lab:{fill['id']}", "kind": "ENTRY",
                        "side": str(fill["side"]).upper(), "quantity": float(fill["quantity"]),
                        "requested_price": _num(fill.get("requested_price")), "price": float(fill["price"]),
                        "fee": _num(fill.get("fee")), "slippage": _num(fill.get("slippage")),
                        "executed_at": _fill_time(fill), "source_ref": str(fill["id"]), "liquidity": "TAKER"}):
                    if _num(fill.get("fee")):
                        store.add_fee(trade_id, fee_type="ENTRY_COMMISSION", amount=float(fill["fee"]),
                                      source_ref=f"lab:{fill['id']}", rate=_num(export.get("fee_rate")),
                                      basis=float(fill["quantity"]) * float(fill["price"]))
                    store.add_event(trade_id, "order-filled", f"entry {float(fill['quantity']):.8g} @ {fill['price']}",
                                    ts=_fill_time(fill), actor=lab_id)
            if not trade["entry_locked"]:
                # scale-in fills keep arriving until the entry order completes;
                # the entry facts lock only once it has
                refreshed = risk_fields(direction=direction, entry=entry_price, stop=stop, target=target,
                                        quantity=entry_qty, equity_before=trade.get("account_equity_before"),
                                        max_allowed_risk_pct=trade.get("max_allowed_risk_pct"),
                                        leverage=trade.get("leverage"))
                store.update_trade(trade_id, {
                    "entry_price": entry_price, "quantity": entry_qty,
                    "initial_stop": stop, "initial_target": target,
                    "current_stop": stop, "current_target": target,
                    **{k: v for k, v in refreshed.items()
                       if v is not None and k not in ("max_allowed_risk_pct", "risk_rule_status")
                       and not (k == "risk_pct" and trade.get("account_equity_before") is None)},
                    "entry_locked": 1 if entry_complete else 0})
            for i, fill in enumerate(trip["exits"]):
                final = trip["closed"] and i == len(trip["exits"]) - 1
                if store.add_execution(trade_id, {
                        "execution_id": f"lab:{fill['id']}", "kind": "EXIT" if final else "PARTIAL_EXIT",
                        "side": str(fill["side"]).upper(), "quantity": float(fill["quantity"]),
                        "requested_price": None, "price": float(fill["price"]), "fee": _num(fill.get("fee")),
                        "slippage": _num(fill.get("slippage")),
                        "realized_gross_pnl": _num(fill.get("realized_pnl")),
                        "executed_at": _fill_time(fill), "source_ref": str(fill["id"]), "liquidity": "TAKER"}):
                    summary["exits_added"] += 1
                    if _num(fill.get("fee")):
                        store.add_fee(trade_id, fee_type="EXIT_COMMISSION", amount=float(fill["fee"]),
                                      source_ref=f"lab:{fill['id']}", rate=_num(export.get("fee_rate")),
                                      basis=float(fill["quantity"]) * float(fill["price"]))
                    reason, _source = _exit_reason(fill, meta_by_order.get(str(fill.get("order_id"))) or {},
                                                   stop, target, orders)
                    store.add_event(trade_id, "exit-filled" if final else "partial-exit",
                                    f"{reason.replace('_', ' ').lower()} {float(fill['quantity']):.8g} @ {fill['price']}",
                                    ts=_fill_time(fill), actor=lab_id)
                    if not final:
                        current = store.get_trade(trade_id)
                        store.update_trade(trade_id, {"partial_exit_count": int(current["partial_exit_count"] or 0) + 1,
                                                      "status": "PARTIALLY_CLOSED"})
            # funding the broker charged to this trip's position
            for event in funding_by_trip.get(index, []):
                recorder.add_funding(trade_id, amount=float(event["amount"]),
                                     source_ref=f"funding:{event.get('funding_key')}",
                                     at=_iso(event.get("funding_timestamp") or event.get("created_at")),
                                     rate=_num(event.get("rate")))
            trade = store.get_trade(trade_id)
            if not trade.get("finalised_at"):
                # commission and funding the broker has already charged count
                # while the trip is still open, not only once it has exits
                recorder._aggregate(trade_id, final=False)
            if trip["closed"] and not trade.get("finalised_at"):
                last = trip["exits"][-1]
                reason, source = _exit_reason(last, meta_by_order.get(str(last.get("order_id"))) or {},
                                              stop, target, orders)
                research = _research(export, entry_order_id)
                recorder._finalise(trade_id, {
                    "exit_reason": reason, "exit_reason_source": source,
                    "mfe_r": research.get("mfe_r") if isinstance(research.get("mfe_r"), (int, float)) else None,
                    "mae_r": research.get("mae_r") if isinstance(research.get("mae_r"), (int, float)) else None,
                    "excursion_source": "LAB_RESEARCH_ENGINE_R" if research.get("mfe_r") is not None else None,
                }, executed_at=_fill_time(last))
                summary["finalised"] += 1
    return summary


def ingest_replay_journal(recorder: TradeJournalRecorder, replay_store) -> dict:
    """Map replay (backtest) journal entries in as BACKTEST trades."""
    store = recorder.store
    summary = {"created": 0}
    entries = replay_store.list()
    known = store.linked_refs("REPLAY_JOURNAL")
    for entry in entries:
        key = str(entry.get("id") or "")
        if not key or key in known:
            continue
        # one entry per lock hold, so a backlog never blocks live hooks
        with recorder.bulk_item():
            direction = "LONG" if str(entry.get("side") or "").lower() in ("long", "buy") else "SHORT"
            rr = _num(entry.get("rr"))
            base, quote = split_symbol(entry.get("symbol") or "")
            result = None
            if rr is not None:
                result = "BREAK_EVEN" if abs(rr) <= 0.05 else "WIN" if rr > 0 else "LOSS"
            trade_id, created = store.create_trade({
                "source_system": "REPLAY_JOURNAL", "source_trade_key": key,
                "bot_id": "replay-backtest", "strategy_id": entry.get("strategy"),
                "strategy_name": entry.get("strategy"),
                "strategy_family": (entry.get("strategy") or "UNKNOWN").upper().replace(" ", "_")[:40],
                "trade_source": "BACKTEST_REPLAY", "trading_mode": "BACKTEST",
                "symbol": str(entry.get("symbol") or "").upper(), "base_asset": base, "quote_asset": quote,
                "direction": direction, "timeframe": entry.get("timeframe"),
                "status": "CLOSED", "entry_price": _num(entry.get("entry")), "exit_price": _num(entry.get("exit")),
                "realised_r": rr, "result": result, "counts_in_stats": 1 if result else 0,
                "exit_reason": "UNKNOWN", "exit_reason_source": "NOT_RECORDED",
                "data_completeness": "REPLAY_JOURNAL_R_ONLY", "entry_locked": 1,
                "created_at": entry.get("created_at"), "finalised_at": _now(),
            }, links=[("REPLAY_JOURNAL", key)])
            known.add(key)
            if created:
                summary["created"] += 1
                store.add_event(trade_id, "backtest-trade", "Imported from the replay journal (BACKTEST).",
                                ts=_iso(entry.get("created_at")) or _now(), actor="replay")
    return summary


class JournalSync:
    """Keeps the canonical journal complete: legacy migration, ledger
    reconciliation and lab/backtest ingestion. Runs once at boot and then on
    a timer; every step is idempotent and individually fault-isolated."""

    def __init__(self, recorder: TradeJournalRecorder, *, legacy_store=None, ledger=None,
                 mode_resolver=None, labs=(), replay_store=None, interval_s: float = 60.0,
                 logger=None):
        import threading
        self.recorder = recorder
        self.legacy_store = legacy_store
        self.ledger = ledger
        self.mode_resolver = mode_resolver
        self.labs = list(labs)
        self.replay_store = replay_store
        self.interval_s = max(10.0, float(interval_s))
        self.logger = logger
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._last: dict = {}
        # Change markers from the previous successful pass. A source whose
        # marker has not moved is skipped, so an idle pass reads no history.
        self._marks: dict = {}
        self._lab_ids: dict = {}
        self._ledger_recheck = True

    def run_once(self, *, include_legacy: bool = True) -> dict:
        """One pass. Legacy migration only needs to run at boot (and on an
        explicit sync): every trade since this build is journaled live."""
        with self._lock:
            result: dict = {"started_at": _now()}
            steps = []
            if self.legacy_store is not None and include_legacy:
                steps.append(("legacy_migration",
                              lambda: self.recorder.migrate_legacy(self.legacy_store, self.ledger)))
            if self.ledger is not None:
                steps.append(("ledger_reconciliation", self._ledger_step))
            for lab in self.labs:
                steps.append((f"lab:{getattr(lab, '__class__', type(lab)).__name__}",
                              lambda lab=lab: self._lab_step(lab)))
            if self.replay_store is not None:
                steps.append(("replay_journal", self._replay_step))
            for name, step in steps:
                try:
                    result[name] = step()
                except Exception as exc:  # noqa: BLE001 — one source must not stop the others
                    result[name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
                    if self.logger is not None:
                        try:
                            self.logger(f"journal sync {name} failed: {type(exc).__name__}: {exc}")
                        except Exception:  # noqa: BLE001
                            pass
            result["finished_at"] = _now()
            self._last = result
            return result

    def _watermark(self, source) -> Optional[tuple]:
        """A source's change marker. None (a full pass) when it has none or
        reading it fails, so a marker problem can only cost speed."""
        mark = getattr(source, "journal_watermark", None)
        if not callable(mark):
            return None
        try:
            return mark()
        except Exception as exc:  # noqa: BLE001 — fall back to a full pass
            if self.logger is not None:
                try:
                    self.logger(f"journal sync change marker failed, full pass: {type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001
                    pass
            return None

    def _ledger_step(self) -> dict:
        """Reconcile only when the ledger moved (a trade opened, reduced or
        closed), when the last pass left rows inside the grace window, or on
        the first pass. Stale PENDING orders are checked every pass."""
        mark = self._watermark(self.ledger)
        if mark is not None and not self._ledger_recheck and self._marks.get("ledger") == mark:
            return {"created": 0, "linked_remainders": 0, "closed": 0,
                    "uncertain": self.recorder.mark_stale_pending(), "skipped_recent": 0, "unchanged": True}
        summary = self.recorder.reconcile_ledger(self.ledger, mode_resolver=self.mode_resolver)
        self._marks["ledger"] = mark
        self._ledger_recheck = bool(summary.get("skipped_recent"))
        return summary

    def _lab_step(self, lab) -> dict:
        """Re-read a lab's ledger only when it booked a fill or funding event,
        or when one of its trades still waits for its entry order to complete
        (that can change without a fill)."""
        mark = self._watermark(getattr(lab, "broker", None))
        slot = id(lab)
        if (mark is not None and self._marks.get(slot) == mark
                and not self.recorder.store.lab_entry_pending(self._lab_ids.get(slot))):
            return {"created": 0, "exits_added": 0, "finalised": 0, "unchanged": True}
        export = lab.journal_export()
        summary = ingest_v2_lab(self.recorder, export)
        self._marks[slot] = mark
        self._lab_ids[slot] = export.get("lab_id")
        return summary

    def _replay_step(self) -> dict:
        """The replay journal is a JSON file: re-read it only when it changed."""
        mark = None
        try:
            stat = os.stat(getattr(self.replay_store, "path"))
            mark = (stat.st_mtime_ns, stat.st_size)
        except (AttributeError, TypeError, OSError):
            mark = None
        if mark is not None and self._marks.get("replay") == mark:
            return {"created": 0, "unchanged": True}
        summary = ingest_replay_journal(self.recorder, self.replay_store)
        self._marks["replay"] = mark
        return summary

    def status(self) -> dict:
        return {"running": bool(self._thread and self._thread.is_alive()), "interval_s": self.interval_s,
                "last": self._last}

    def start(self, *, timer: bool = True) -> None:
        """The boot pass (with legacy migration) runs on this thread, never on
        the caller's: a large first import cannot delay startup. ``timer=False``
        stops after the boot pass."""
        import threading
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            self.run_once()
            while timer and not self._stop.wait(self.interval_s):
                self.run_once(include_legacy=False)

        self._thread = threading.Thread(target=loop, name="journal-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
