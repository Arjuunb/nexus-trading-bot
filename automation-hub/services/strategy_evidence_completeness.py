"""Read-only completeness assessment against committed accounting receipts.

Delivery, attribution, and economics are tested independently. An idempotent
consumer is not an exactly-once cross-database transaction. Unknown legacy
history, bounded reads and unavailable cost models cannot become verified by
successfully replaying the receipts that happen to be visible.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from typing import Mapping

from services.strategy_intelligence_metrics import EvidenceCohort


REPORT_VERSION = "strategy_evidence_completeness.v1"
_SCOPE = ("strategy_id", "strategy_version", "strategy_config_hash", "instance_id",
          "simulation_session_id", "execution_mode", "owner_id", "account_id", "lab_id", "source_kind", "symbol")
_COSTS_KNOWN = {"BOOKED", "MODELED", "VERIFIED_ZERO"}
_ACTION = {"OPEN": {"opened", "recovered"}, "REDUCE": {"reduced"}, "CLOSE": {"closed"}}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      default=lambda item: str(item) if isinstance(item, Decimal) else _unsupported(item))


def _unsupported(item):
    raise TypeError(f"unsupported evidence value: {type(item).__name__}")


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _number(value):
    if value is None or isinstance(value, bool): return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError): return None


def _text(value):
    if value is None: return None
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _configuration_key(identity):
    return (identity.get("strategy_id"), identity.get("strategy_version") or identity.get("observed_version"),
            identity.get("strategy_config_hash") or identity.get("config_fingerprint"))


def _configuration_issue(identity, configurations):
    """Only an existing immutable canonical snapshot can verify a fingerprint."""
    from services.strategy_identity import configuration_fingerprint
    key = _configuration_key(identity)
    stored = configurations.get(key) if isinstance(configurations, Mapping) else next(
        (item for item in configurations if _configuration_key(item) == key), None)
    if stored is None:
        return "MISSING"
    try:
        if configuration_fingerprint(stored.get("configuration")) != key[-1]:
            return "CONFLICTED"
        if identity.get("configuration") is not None and configuration_fingerprint(identity["configuration"]) != key[-1]:
            return "CONFLICTED"
    except (ValueError, TypeError):
        return "CONFLICTED"
    for field in ("source_hash", "source_manifest"):
        if identity.get(field) is not None and identity[field] != stored.get(field):
            return "CONFLICTED"
    return None


def _scope(row, configurations=()):
    context = row.get("context") or {}
    identity = context.get("strategy_identity") or {}
    recovery_identity = context.get("recovery_strategy_identity") or {}
    provenance = context.get("journal_execution") or {}
    result = {**identity, **context, **provenance}
    result = {key: result.get(key) for key in _SCOPE}
    result["strategy_config_hash"] = result.get("strategy_config_hash") or identity.get("config_fingerprint")
    if recovery_identity and _configuration_issue(recovery_identity, configurations) is None:
        # A producer may have masked live labels while the journal was offline.
        # The exact detached preexecution snapshot is usable only after replay
        # has validated and persisted it, including source identity.
        result.update(strategy_id=recovery_identity.get("strategy_id"),
                      strategy_version=recovery_identity.get("strategy_version") or recovery_identity.get("observed_version"),
                      strategy_config_hash=recovery_identity.get("config_fingerprint") or recovery_identity.get("strategy_config_hash"))
    result["symbol"] = result.get("symbol") or (row.get("receipt") or {}).get("symbol")
    for key in ("instance_id", "simulation_session_id", "symbol"):
        if row.get(key): result[key] = row[key]
    return result


def metrics_episode_watermark(episodes, cohort: EvidenceCohort | Mapping | None):
    """Bind completeness to the exact episode input, including open episodes."""
    selected = []
    expected = cohort.as_dict() if isinstance(cohort, EvidenceCohort) else dict(cohort or {})
    for original in episodes:
        row = dict(original)
        if "config_fingerprint" not in row: row["config_fingerprint"] = row.get("strategy_config_hash")
        if all(row.get(key) == value for key, value in expected.items() if key != "symbol") and (
                expected.get("symbol") is None or row.get("symbol") == expected["symbol"]):
            selected.append(row)
    return _hash(sorted(selected, key=_canonical))


def _store_snapshot(store, bound):
    if isinstance(store, Mapping): return dict(store)
    if hasattr(store, "get_evidence_completeness_snapshot"):
        return store.get_evidence_completeness_snapshot(max_records=bound)
    # Older consumers can be inspected, but a multi-call fallback is not a
    # verified consistent snapshot and must remain UNKNOWN.
    events = store.evidence_events(limit=bound + 1)
    episodes = store.episodes()
    journals = [store.get(trade_id) for episode in episodes for trade_id in episode.get("trade_ids", [])]
    return {"events": events[:bound], "episodes": episodes[:bound],
            "journals": [row for row in journals[:bound] if row], "source_complete": False,
            "read_error": "consistent_journal_snapshot_unavailable"}


def assess_evidence_completeness(snapshot, store, *, scope=None, cohort=None, recovering=False,
                                 last_successful_reconciliation=None, calculated_at=None, max_records=100_000):
    """Compare authoritative executions with immutable fills and journal facts.

    A caller-provided cohort separates known different configurations; unknown
    attribution in the requested instance/account remains visible and blocks
    complete-history claims. This function performs no writes or order calls.
    """
    if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
        raise ValueError("max_records must be a positive integer")
    now = calculated_at or datetime.now(timezone.utc).isoformat()
    exact_cohort = cohort.as_dict() if isinstance(cohort, EvidenceCohort) else dict(cohort) if cohort is not None else None
    requested = dict(scope or {})
    if exact_cohort:
        requested.update({key: exact_cohort[key] for key in ("instance_id", "simulation_session_id", "owner_id", "account_id")})
    details = defaultdict(list)
    try:
        journal = _store_snapshot(store, max_records)
    except Exception as error:
        journal = {"events": [], "episodes": [], "journals": [], "source_complete": False,
                   "read_error": type(error).__name__}
    snapshot = dict(snapshot or {})
    collections = ("trades", "positions", "executions", "outbox")
    if any(len(snapshot.get(key) or []) > max_records for key in collections):
        snapshot["source_complete"] = False
        details["read_failures"].append("authoritative_record_bound_exceeded")
    if not snapshot.get("source_complete"):
        details["read_failures"].append(snapshot.get("read_error") or "authoritative_snapshot_incomplete")
    if not journal.get("source_complete"):
        details["read_failures"].append(journal.get("read_error") or "journal_snapshot_incomplete")
    for key in ("events", "episodes", "journals", "legs", "configurations"):
        if len(journal.get(key) or []) > max_records:
            details["read_failures"].append("journal_record_bound_exceeded")
    configurations = {_configuration_key(item): item for item in journal.get("configurations") or []}
    outboxes = {}
    for row in snapshot.get("outbox", [])[:max_records]:
        execution_id = row.get("execution_id")
        if not execution_id:
            details["conflicting_authoritative_metadata"].append("missing_execution_id")
        elif execution_id in outboxes:
            details["duplicate_authoritative_metadata"].append(execution_id)
        else:
            outboxes[execution_id] = row
    # Exits committed during a journal outage can lack observer context. The
    # primary REDUCE parent/remainder IDs prove inheritance from the actual
    # entry; coincident symbols, dates and a current strategy never do.
    entry_contexts = {}
    for execution_id, original in list(outboxes.items()):
        metadata = dict(original)
        action = str(metadata.get("action") or "").upper()
        parent_id = metadata.get("parent_trade_id") or metadata.get("trade_id")
        context = metadata.get("context")
        if not context and action in ("REDUCE", "CLOSE") and parent_id in entry_contexts:
            context = entry_contexts[parent_id]
            metadata["context"] = context
            outboxes[execution_id] = metadata
        if context:
            if action == "OPEN":
                entry_contexts[metadata.get("trade_id")] = context
            elif action == "REDUCE" and metadata.get("remainder_trade_id"):
                entry_contexts[metadata["remainder_trade_id"]] = context
    expected, known_other = [], 0
    for execution in snapshot.get("executions", [])[:max_records]:
        execution = dict(execution)
        metadata = outboxes.get(execution.get("execution_id"))
        observed_scope = _scope(metadata or execution, configurations)
        # Old CLOSE calls did not always carry instance_id. Blank receipt scope
        # is unknown; exact committed outbox/trade IDs can supply its ownership.
        if metadata and any(execution.get(key) not in (None, "") and observed_scope.get(key) not in (None, "")
                            and execution[key] != observed_scope[key] for key in ("instance_id", "simulation_session_id")):
            details["conflicting_authoritative_metadata"].append(execution.get("execution_id"))
        if any(execution.get(key) not in (None, "") and execution[key] != value for key, value in requested.items()
               if key in ("instance_id", "simulation_session_id")):
            known_other += 1; continue
        if exact_cohort:
            cohort_fields = {**exact_cohort, "strategy_config_hash": exact_cohort.get("config_fingerprint")}
            cohort_fields.pop("config_fingerprint", None)
            differences = [key for key, value in cohort_fields.items()
                           if not (key == "symbol" and value is None) and
                           observed_scope.get(key) is not None and observed_scope[key] != value]
            if differences:
                known_other += 1; continue
        if not metadata:
            details["missing_authoritative_metadata"].append(execution.get("execution_id"))
        else:
            action = str(metadata.get("action") or "").upper()
            identifiers = {"action": action, "instance_id": metadata.get("instance_id"),
                           "trade_id": metadata.get("remainder_trade_id") if action == "REDUCE" else metadata.get("trade_id"),
                           "position_id": metadata.get("remainder_position_id") if action == "REDUCE" else metadata.get("position_id")}
            if any(field in execution and execution[field] != value for field, value in identifiers.items()
                   if not (field == "instance_id" and execution[field] in (None, ""))):
                details["conflicting_authoritative_metadata"].append(execution.get("execution_id"))
            source_identity = ((metadata.get("context") or {}).get("recovery_strategy_identity") or
                               (metadata.get("context") or {}).get("strategy_identity"))
            if source_identity and _configuration_issue(source_identity, configurations) == "CONFLICTED":
                details["conflicting_configuration_snapshots"].append(execution.get("execution_id"))
        if not observed_scope.get("strategy_config_hash") or not observed_scope.get("strategy_version"):
            details["missing_configuration_fingerprints"].append(execution.get("execution_id"))
        expected.append((execution, metadata, observed_scope))

    execution_ids = {row.get("execution_id") for row in snapshot.get("executions", [])}
    for execution_id in outboxes.keys() - execution_ids:
        details["conflicting_authoritative_metadata"].append(execution_id)

    events = [row for row in journal.get("events", [])[:max_records]
              if row.get("kind") in ("execution_fill", "FILL")]
    event_index = defaultdict(list)
    for event in events:
        payload = event.get("payload") or {}
        event_index[(event.get("instance_id"), event.get("simulation_session_id"),
                     payload.get("execution_id"))].append(event)
    relevant_events, relevant_trade_ids = [], set()
    expected_keys = set()
    for execution, metadata, expected_scope in expected:
        execution_id = execution.get("execution_id")
        key = (expected_scope.get("instance_id"), expected_scope.get("simulation_session_id"), execution_id)
        if key in expected_keys:
            details["conflicting_authoritative_receipts"].append(execution_id)
        expected_keys.add(key)
        matches = event_index.get(key, [])
        authoritative = metadata or execution
        action = str(authoritative.get("action") or execution.get("action") or "").upper()
        parent_trade_id = authoritative.get("parent_trade_id") if action == "REDUCE" else authoritative.get("trade_id")
        parent_position_id = authoritative.get("parent_position_id") if action == "REDUCE" else authoritative.get("position_id")
        if parent_trade_id: relevant_trade_ids.add(parent_trade_id)
        if action == "REDUCE" and authoritative.get("remainder_trade_id"):
            relevant_trade_ids.add(authoritative["remainder_trade_id"])
        if not matches:
            details["missing_fill_events"].append(execution_id)
            continue
        if len(matches) != 1:
            details["duplicate_fill_events"].append(execution_id)
        relevant_events.extend(matches)
        for event in matches:
            payload = event.get("payload") or {}
            conflicts = []
            if payload.get("action") not in _ACTION.get(action, set()): conflicts.append("action")
            for field, expected_value in (("trade_id", parent_trade_id), ("position_id", parent_position_id)):
                if expected_value is not None and event.get(field) != expected_value: conflicts.append(field)
            for field in _SCOPE:
                if expected_scope.get(field) is not None and event.get(field) != expected_scope[field]: conflicts.append(field)
            receipt = (metadata or {}).get("receipt") or {}
            economics = {"net_pnl": receipt.get("net_pnl"), "gross_pnl": receipt.get("gross_pnl"),
                         "fees": receipt.get("booked_fees", receipt.get("fees")), "funding": receipt.get("funding")}
            if action == "OPEN": economics = {"initial_risk": receipt.get("initial_risk_amount", receipt.get("risk_amount_at_entry"))}
            for field, expected_value in economics.items():
                if expected_value is not None and (_number(expected_value) is None or
                        _number(payload.get(field)) != _number(expected_value)):
                    conflicts.append(field)
            if action == "REDUCE":
                for field in ("remainder_trade_id", "remainder_position_id"):
                    if authoritative.get(field) is not None and payload.get(field) != authoritative[field]: conflicts.append(field)
            if conflicts: details["conflicting_fill_events"].append({"execution_id": execution_id, "fields": sorted(set(conflicts))})
            if not event.get("episode_id"): details["unresolved_episodes"].append(execution_id)
    for key, values in event_index.items():
        if key not in expected_keys and any(all(event.get(field) == value for field, value in requested.items()) for event in values):
            # Known other cohorts were deliberately separated above, not lost.
            if not exact_cohort or any(_matches_cohort(event, exact_cohort) for event in values):
                details["unexpected_fill_events"].append(key[-1])
    def in_primary_scope(row):
        if not all(row.get(key) == value for key, value in requested.items() if key in ("instance_id", "simulation_session_id")):
            return False
        if row.get("id") in relevant_trade_ids:
            # The ledger's existing routing tag can differ from the canonical
            # observed strategy identity. Proven committed IDs link its rows;
            # filtering this financial row by that legacy tag loses real P&L.
            return True
        return not exact_cohort or all(row.get(key) is None or value is None or row[key] == value
                                      for key, value in exact_cohort.items() if key in ("strategy_id", "symbol"))
    scoped_trades = [row for row in snapshot.get("trades", [])[:max_records] if in_primary_scope(row)]
    receipt_trade_ids = {row.get("trade_id") for row in snapshot.get("executions", [])}
    receipt_trade_ids.update(row.get("parent_trade_id") for row in snapshot.get("outbox", []))
    for trade in scoped_trades:
        if trade.get("id") not in receipt_trade_ids:
            details["unreceipted_legacy_trades"].append(trade.get("id"))
    closing_trade_ids = {(row.get("parent_trade_id") or row.get("trade_id")) for row in outboxes.values()
                         if row.get("action") in ("REDUCE", "CLOSE")}
    closing_trade_ids.update(row.get("trade_id") for row in snapshot.get("executions", []) if row.get("action") == "CLOSE")
    for trade in scoped_trades:
        if trade.get("status") == "closed" and trade.get("id") not in closing_trade_ids:
            details["missing_authoritative_closes"].append(trade.get("id"))
    position_ids = {row.get(field) for row in snapshot.get("executions", []) for field in ("position_id",)}
    position_ids.update(row.get(field) for row in outboxes.values()
                        for field in ("position_id", "parent_position_id", "remainder_position_id"))
    for position in snapshot.get("positions", [])[:max_records]:
        if in_primary_scope(position) and position.get("id") not in position_ids:
            details["unreceipted_positions"].append(position.get("id"))
    journals = {row.get("trade_id"): row for row in journal.get("journals", [])[:max_records]}
    primary_trades_by_id = {row.get("id"): row for row in scoped_trades}
    for trade_id in sorted(relevant_trade_ids):
        entry = journals.get(trade_id)
        if entry is None:
            details["missing_journals"].append(trade_id)
            continue
        source_trade = primary_trades_by_id.get(trade_id)
        if source_trade and source_trade.get("status") == "closed":
            if entry.get("status") != "closed" or not any(row.get("kind") == "trade-closed" for row in entry.get("timeline", entry.get("events", []))):
                details["missing_close_events"].append(trade_id)
            if entry.get("status") == "closed" and _number(source_trade.get("pnl")) is not None and _number(entry.get("pnl")) != _number(source_trade.get("pnl")):
                details["conflicting_journal_economics"].append(trade_id)
    episode_ids = {event.get("episode_id") for event in relevant_events if event.get("episode_id")}
    fills_by_episode = defaultdict(list)
    for event in relevant_events:
        fills_by_episode[event.get("episode_id")].append(event)
    all_episodes = journal.get("episodes", [])[:max_records]
    episodes = [row for row in all_episodes if row.get("episode_id") in episode_ids]
    missing_headers = episode_ids - {row.get("episode_id") for row in episodes}
    details["unresolved_episodes"].extend(sorted(missing_headers))
    if "legs" in journal:
        leg_index = defaultdict(list)
        event_ids = {row.get("event_id"): row for row in journal.get("events", [])[:max_records]}
        for leg in journal.get("legs", [])[:max_records]:
            leg_index[(leg.get("episode_id"), leg.get("trade_id"))].append(leg)
        for event in relevant_events:
            key = (event.get("episode_id"), event.get("trade_id"))
            matching_legs = leg_index.get(key, [])
            if not matching_legs:
                details["unresolved_episodes"].append(event.get("event_id"))
                continue
            if len(matching_legs) != 1 or matching_legs[0].get("position_id") != event.get("position_id"):
                details["conflicting_episode_references"].append(event.get("event_id"))
            for leg in matching_legs:
                entry_event = event_ids.get(leg.get("entry_event_id"))
                if entry_event is None or entry_event.get("episode_id") != event.get("episode_id"):
                    details["unresolved_episodes"].append(leg.get("leg_key"))
                if leg.get("parent_trade_id") and not leg_index.get((event.get("episode_id"), leg["parent_trade_id"])):
                    details["unresolved_episodes"].append(leg.get("leg_key"))
    for episode in episodes:
        episode_id = episode.get("episode_id")
        configuration_issue = _configuration_issue(episode, configurations)
        if not (episode.get("strategy_version") and re.fullmatch(r"[0-9a-f]{64}", str(episode.get("strategy_config_hash") or episode.get("config_fingerprint") or ""))) or configuration_issue == "MISSING":
            details["missing_configuration_fingerprints"].append(episode_id)
        if configuration_issue == "CONFLICTED":
            details["conflicting_configuration_snapshots"].append(episode_id)
        if any(any(event.get(field) != episode.get(field) for field in _SCOPE)
               for event in fills_by_episode[episode_id]):
            details["conflicting_episode_scope"].append(episode_id)
        if episode.get("identity_status") not in (None, "observed", "verified"):
            details["unverified_strategy_identity"].append(episode_id)
        mode, source = str(episode.get("execution_mode") or "").lower(), str(episode.get("source_kind") or "").lower()
        if not source or mode not in {"forward_paper", "backtest", "historical_backtest", "replay", "simulation", "simulated", "paper_replay", "paper"} or (mode == "paper" and source != "forward_paper"):
            details["unknown_execution_modes"].append(episode_id)
        if _number(episode.get("initial_risk")) is None or _number(episode.get("initial_risk")) <= 0:
            details["missing_initial_risk"].append(episode_id)
        if episode.get("status") == "closed" and any(
                _number(episode.get(field)) is None or str(episode.get(coverage) or "UNKNOWN").upper() not in _COSTS_KNOWN
                for field, coverage in (("fees", "fees_coverage"), ("funding", "funding_coverage"))):
            details["missing_cost_coverage"].append(episode_id)
        if episode.get("status") == "closed" and any(_number(episode.get(field)) is None for field in ("net_pnl", "gross_pnl")):
            details["missing_financial_components"].append(episode_id)

    with localcontext() as context:
        context.prec = 50
        financial = {}
        closed_trades = [row for row in scoped_trades if row.get("id") in relevant_trade_ids and row.get("status") == "closed"]
        closing_events = [row for row in relevant_events if (row.get("payload") or {}).get("action") in ("closed", "reduced")]
        for output, trade_field, event_field in (("net_pnl", "pnl", "net_pnl"), ("fees", "fees", "fees")):
            primary_values = [_number(row.get(trade_field)) for row in closed_trades]
            evidence_values = [_number((row.get("payload") or {}).get(event_field)) for row in closing_events]
            primary_total = sum((value for value in primary_values if value is not None), Decimal(0))
            evidence_total = sum((value for value in evidence_values if value is not None), Decimal(0))
            delta = evidence_total - primary_total
            amounts_known = all(v is not None for v in (*primary_values, *evidence_values))
            financial.update({"authoritative_" + output: _text(primary_total) if all(v is not None for v in primary_values) else None,
                              "evidence_" + output: _text(evidence_total) if all(v is not None for v in evidence_values) else None,
                              "known_authoritative_" + output: _text(primary_total), "known_evidence_" + output: _text(evidence_total),
                              output + "_delta": _text(delta) if amounts_known else None,
                              output + "_coverage_complete": amounts_known and len(closed_trades) == len(closing_events) and not details["missing_fill_events"]})
            if not financial[output + "_coverage_complete"]:
                details["missing_financial_components"].append(output)
            if amounts_known and abs(delta) > Decimal("1e-10") and not (details["missing_fill_events"] or details["missing_authoritative_closes"]):
                details["financial_conflicts"].append(output)
    unknown = any(details[name] for name in ("read_failures", "unreceipted_legacy_trades", "missing_authoritative_metadata",
                                            "missing_authoritative_closes", "unreceipted_positions"))
    conflict_types = ("duplicate_fill_events", "conflicting_fill_events", "conflicting_authoritative_receipts",
                      "unexpected_fill_events", "conflicting_journal_economics", "financial_conflicts",
                      "conflicting_authoritative_metadata", "duplicate_authoritative_metadata",
                      "conflicting_episode_scope", "conflicting_configuration_snapshots", "conflicting_episode_references")
    conflicts = any(details[name] for name in conflict_types)
    gaps = any(details.values())
    status = "CONFLICTED" if conflicts else "UNKNOWN" if unknown else "RECOVERING" if recovering and gaps else "PARTIAL" if gaps else "COMPLETE"
    primary_material = {key: snapshot.get(key) for key in (*collections, "source_complete", "outbox_supported", "source_kind")}
    journal_material = {key: journal.get(key) for key in ("events", "episodes", "journals", "legs", "configurations", "source_complete")}
    authoritative_watermark, evidence_watermark = _hash(primary_material), _hash(journal_material)
    counts = {name: len(items) for name, items in details.items()}
    counts.update(expected_executions=len(expected), persisted_fill_events=len(relevant_events), known_other_cohort_executions=known_other)
    aliases = {"expected_authoritative_events": "expected_executions", "persisted_evidence_events": "persisted_fill_events",
               "missing_events": "missing_fill_events", "duplicate_events": "duplicate_fill_events", "conflicting_events": "conflicting_fill_events",
               "unresolved_episode_references": "unresolved_episodes", "missing_cost_components": "missing_cost_coverage"}
    for alias, name in aliases.items(): counts[alias] = counts.get(name, 0)
    counts["conflicting_events"] = sum(counts.get(name, 0) for name in conflict_types)
    for name in ("missing_configuration_fingerprints", "unknown_execution_modes", "missing_journals", "missing_close_events"):
        counts.setdefault(name, 0)
    return {"report_version": REPORT_VERSION, "status": status, "cohort": exact_cohort, "scope": dict(scope) if scope is not None else None,
            "calculated_at": now, "last_successful_reconciliation": last_successful_reconciliation,
            "history_complete": status == "COMPLETE", "counts": counts, "details": dict(details),
            "financial_totals": financial, "financial": financial,
            "authoritative_watermark": authoritative_watermark, "evidence_watermark": evidence_watermark,
            "source_watermark": _hash([authoritative_watermark, evidence_watermark]),
            "metrics_evidence_watermark": metrics_episode_watermark(all_episodes, exact_cohort),
            "reconciliation_status": status,
            "delivery_contract": ("at_least_once_with_idempotent_logical_effects" if snapshot.get("outbox_supported")
                                  else "idempotent_cross_store_capture_with_detectable_gaps")}


def _matches_cohort(row, cohort):
    actual = dict(row)
    actual["config_fingerprint"] = row.get("config_fingerprint", row.get("strategy_config_hash"))
    return all(actual.get(key) == value for key, value in cohort.items() if key != "symbol") and (
        cohort.get("symbol") is None or actual.get("symbol") == cohort["symbol"])
