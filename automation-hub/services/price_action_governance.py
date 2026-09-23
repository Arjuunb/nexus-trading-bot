"""Immutable Price Action journal and governed PAPER-only learning records.

The store is deliberately separate from the generic trade journal.  It records
every native Price Action setup, including rejected/unfilled outcomes, and only
permits learning through append-only evidence and explicitly approved shadow
candidates.  Nothing in this module can submit an order or mutate an active
strategy configuration.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from data.sqlite_runtime import is_sqlite_busy, runtime_connection


STRATEGY_FAMILY = "PURE_PRICE_ACTION"
STRATEGY_VERSION = "1.1.0"
ALLOWED_CANDIDATE_RULES = {
    "first_touch_only", "zone_expiry_bars", "trigger_filter",
    "confusion_candles", "entry_model", "stop_model",
    "zone_timeframe_scope",
}
LEARNING_CLASSES = {
    "STRATEGY_LOSS", "EXECUTION_LOSS", "RULE_VIOLATION",
    "DATA_QUALITY_FAILURE", "MODEL_SPECIFICATION_WEAKNESS",
    "RANDOM_OR_INCONCLUSIVE", "VALID_NON_LOSS",
}


class PersistenceBlocked(RuntimeError):
    """A retryable journal lock that must not be reported as feed failure."""

    code = "PERSISTENCE_BLOCKED"

    def __init__(self, operation: str, exc: BaseException):
        super().__init__(f"Price Action journal {operation} is blocked: {exc}")
        self.operation = operation
        self.retryable = True


def _validate_rule_value(rule_key: str, value: object) -> None:
    if rule_key == "first_touch_only" and not isinstance(value, bool):
        raise ValueError("first_touch_only must be boolean")
    if rule_key == "zone_expiry_bars" and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0):
        raise ValueError(f"{rule_key} must be a positive integer")
    if rule_key == "confusion_candles" and (
            not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3):
        raise ValueError("confusion_candles must be an integer from zero to three")
    allowed = {
        "trigger_filter": {"generic_rejection", "pin_bar_only"},
        "entry_model": {"confirmation", "close", "retracement_50"},
        "stop_model": {"rejection_extreme", "pattern", "structural_zone"},
        "zone_timeframe_scope": {"same_timeframe", "higher_timeframe"},
    }
    if rule_key in allowed and value not in allowed[rule_key]:
        raise ValueError(f"invalid governed value for {rule_key}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> object:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _fingerprint(value: object, prefix: str) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()}"


def _transition_key(row: dict) -> tuple:
    """Identify a state transition by the event, not by its recorded values.

    The id is a fingerprint of the setup, the status and the moment, so it is
    the right key when it is present. A legacy row without one falls back to
    the same four facts, so it still matches itself across captures instead of
    being appended again on every poll.
    """
    identifier = row.get("id")
    if identifier:
        return ("id", str(identifier))
    return ("event", str(row.get("setup_id")), str(row.get("from_phase")),
            str(row.get("to_phase")), str(row.get("timestamp")))


def _latest_by(rows: list[dict], key: str, value: str) -> dict | None:
    return next((row for row in reversed(rows) if str(row.get(key) or "") == value), None)


class PriceActionJournalStore:
    """SQLite-backed append-only PA evidence, revisions and research governance."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 10_000):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._db = runtime_connection(
                self.path, autocommit=True, busy_timeout_ms=busy_timeout_ms)
            self._schema()
        except sqlite3.OperationalError as exc:
            if is_sqlite_busy(exc):
                raise PersistenceBlocked("startup", exc) from exc
            raise

    def _schema(self) -> None:
        with self._lock:
            self._db.executescript("""
              CREATE TABLE IF NOT EXISTS pa_journal_entries(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, experiment_id TEXT,
                setup_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL, configuration_fingerprint TEXT NOT NULL,
                engine_fingerprint TEXT NOT NULL, dataset_fingerprint TEXT NOT NULL,
                symbol TEXT NOT NULL, timeframe TEXT NOT NULL, direction TEXT NOT NULL,
                origin TEXT NOT NULL, partition_label TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT NOT NULL,
                opened_at TEXT NOT NULL, closed_at TEXT,
                base_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(session_id,setup_id));
              CREATE TABLE IF NOT EXISTS pa_journal_revisions(
                id TEXT PRIMARY KEY, journal_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
                reason_code TEXT NOT NULL, created_at TEXT NOT NULL,
                initiated_by TEXT NOT NULL, payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(journal_id,revision_no), UNIQUE(journal_id,payload_hash));
              CREATE TABLE IF NOT EXISTS pa_learning_candidates(
                id TEXT PRIMARY KEY, parent_strategy_version TEXT NOT NULL,
                rule_key TEXT NOT NULL, rule_value_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL, contradicting_evidence_json TEXT NOT NULL,
                development_period_json TEXT NOT NULL, validation_period_json TEXT NOT NULL,
                code_fingerprint TEXT NOT NULL, dataset_fingerprint TEXT NOT NULL,
                expected_benefit TEXT NOT NULL, risks_json TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL,
                live_execution_allowed INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS pa_candidate_events(
                id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, from_status TEXT NOT NULL,
                to_status TEXT NOT NULL, reason TEXT NOT NULL, initiated_by TEXT NOT NULL,
                created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS pa_shadow_observations(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                session_id TEXT NOT NULL, candle_identity TEXT NOT NULL,
                baseline_json TEXT NOT NULL, candidate_json TEXT NOT NULL,
                created_at TEXT NOT NULL, execution_mode TEXT NOT NULL DEFAULT 'PAPER',
                account_affected INTEGER NOT NULL DEFAULT 0,
                UNIQUE(run_id,candle_identity));
              CREATE INDEX IF NOT EXISTS idx_pa_journal_filters ON pa_journal_entries(
                session_id,strategy_id,symbol,timeframe,status,partition_label,opened_at);
              CREATE INDEX IF NOT EXISTS idx_pa_journal_revisions ON pa_journal_revisions(
                journal_id,revision_no);
            """)
            self._create_immutability_triggers()

    def _create_immutability_triggers(self) -> None:
        self._db.executescript("""
          CREATE TRIGGER IF NOT EXISTS pa_journal_entries_no_update
          BEFORE UPDATE ON pa_journal_entries BEGIN
            SELECT RAISE(ABORT,'Price Action journal entries are immutable');
          END;
          CREATE TRIGGER IF NOT EXISTS pa_journal_entries_no_delete
          BEFORE DELETE ON pa_journal_entries BEGIN
            SELECT RAISE(ABORT,'Price Action journal entries are immutable');
          END;
          CREATE TRIGGER IF NOT EXISTS pa_journal_revisions_no_update
          BEFORE UPDATE ON pa_journal_revisions BEGIN
            SELECT RAISE(ABORT,'Price Action journal revisions are immutable');
          END;
          CREATE TRIGGER IF NOT EXISTS pa_journal_revisions_no_delete
          BEFORE DELETE ON pa_journal_revisions BEGIN
            SELECT RAISE(ABORT,'Price Action journal revisions are immutable');
          END;
        """)

    @staticmethod
    def _classification(payload: dict) -> tuple[str, str]:
        health = str(payload["market_context"].get("data_health_state") or "UNKNOWN")
        compliance = payload["review"].get("rule_compliance")
        outcome = payload["outcome"]
        net_r, gross_r = outcome.get("net_r"), outcome.get("gross_r")
        transition_health = {
            str(row["market_data_health"])
            for row in payload["setup"].get("state_transitions", [])
            if row.get("market_data_health")
        }
        if (outcome.get("status") == "DATA_PAUSED" or
                health not in {"SYNCHRONIZED", "REPLAY", "HISTORICAL_REPLAY",
                               "HISTORICAL_RECONCILED", "LIVE_BOOTSTRAP_RECONCILED"} or
                any(row not in {"SYNCHRONIZED", "REPLAY", "HISTORICAL_REPLAY",
                                "HISTORICAL_RECONCILED", "LIVE_BOOTSTRAP_RECONCILED"}
                    for row in transition_health)):
            return "DATA_QUALITY_FAILURE", "decision or execution evidence was not fully reconciled"
        if compliance is False:
            return "RULE_VIOLATION", "saved setup evidence does not satisfy its immutable rule snapshot"
        if net_r is None:
            return "RANDOM_OR_INCONCLUSIVE", "the setup has no completed outcome"
        if float(net_r) < 0 and float(gross_r or 0) > 0:
            return "EXECUTION_LOSS", "execution costs changed a positive gross result into a net loss"
        if float(net_r) < 0:
            return "STRATEGY_LOSS", "the valid setup lost under the saved conservative execution model"
        return "VALID_NON_LOSS", "the completed rule-compliant setup did not lose"

    @staticmethod
    def _record(*, visual_state: dict, session: dict, paper_state: dict,
                feed_status: dict, setup: dict, partition_label: str) -> dict:
        from services.mtf_policy import material_evidence

        setup_id = str(setup["id"])
        proposals = [row for row in visual_state.get("proposals", [])
                     if row.get("setup_id") == setup_id]
        research = [row for row in visual_state.get("trades", []) + visual_state.get("orders", [])
                    if row.get("setup_id") == setup_id]
        proposal = proposals[-1] if proposals else None
        paper_candidate = next((row for row in paper_state.get("candidates", [])
                                if row.get("source_proposal_id") == (proposal or {}).get("id")), None)
        research_trade = research[-1] if research else None
        frozen_context = setup.get("context_snapshot") or {}
        zone = frozen_context.get("zone") or next((
            row for row in visual_state.get("zones", [])
            if row.get("id") == setup.get("zone_id")), None)
        event = frozen_context.get("trigger_event") or next((
            row for row in visual_state.get("events", [])
            if row.get("id") == setup.get("trigger_event_id")), None)
        order_meta = next((row for row in paper_state.get("order_metadata", [])
                           if row.get("config", {}).get("proposal", {}).get("setup_id") == setup_id), None)
        order_id = order_meta.get("order_id") if order_meta else None
        broker_order = next((row for row in paper_state.get("orders", [])
                             if row.get("id") == order_id), None)
        fills = [row for row in paper_state.get("trades", []) if row.get("order_id") == order_id]
        execution_events = [row for row in paper_state.get("activity", [])
                            if row.get("object_id") == order_id and
                            row.get("kind") in {"paper_order_filled", "paper_position_completed"}]
        entry_event = next((row for row in reversed(execution_events)
                            if row.get("kind") == "paper_order_filled"), None)
        fill_quote = ((entry_event or {}).get("payload") or {}).get("execution_quote")
        funding_events = [row for row in paper_state.get("funding_events", [])
                          if row.get("order_id") == order_id and bool(row.get("applied"))]
        execution = (order_meta or {}).get("config", {})
        candidate_status = str((paper_candidate or {}).get("status") or "")
        outcome_status = str(
            candidate_status if candidate_status in {"REJECTED", "DATA_PAUSED"} else
            (research_trade or {}).get("status") or setup.get("phase") or "WATCHING_LOCATION")
        result = ("won" if outcome_status in {"WON", "TARGET_HIT"} else
                  "lost" if outcome_status in {"LOST", "STOPPED", "LIQUIDATED_PAPER"} else
                  "rejected" if outcome_status == "REJECTED" else
                  "cancelled" if outcome_status in {"CANCELLED", "INVALIDATED", "DATA_PAUSED"} else
                  "unfilled" if outcome_status == "EXPIRED" else "open")
        config = execution.get("strategy_configuration") or session.get("strategy_config") or {}
        config_fingerprint = (visual_state.get("metrics_scope", {}).get("configuration_id") or
                              _fingerprint(config, "config"))
        engine_fingerprint = str(visual_state.get("metrics_scope", {}).get("engine_fingerprint") or "UNVERIFIED")
        trigger_patterns = setup.get("pattern_metadata") or []
        requested = float((proposal or {}).get("entry") or execution.get("entry") or 0) or None
        stop = float((proposal or {}).get("stop") or execution.get("stop") or 0) or None
        target = float((proposal or {}).get("target") or execution.get("target") or 0) or None
        actual_fill = (broker_order or {}).get("average_price") or (research_trade or {}).get("fill_price")
        initial_risk = abs(requested - stop) if requested is not None and stop is not None else None
        latest_fill = fills[-1] if fills else None
        quantity = execution.get("quantity") or (broker_order or {}).get("quantity")
        actual_risk = (float(quantity) * abs(float(actual_fill or requested) - stop)
                       if quantity and (actual_fill is not None or requested is not None) and stop is not None
                       else None)
        leverage = execution.get("leverage")
        margin = (float(quantity) * float(actual_fill or requested) / float(leverage)
                  if quantity and (actual_fill is not None or requested is not None) and leverage else None)
        display_quote = visual_state.get("live_display") or {}
        decision_bid = display_quote.get("bid")
        decision_ask = display_quote.get("ask")
        spread = (float(decision_ask) - float(decision_bid)
                  if decision_bid is not None and decision_ask is not None else None)
        fill_spread = (float(fill_quote["ask"]) - float(fill_quote["bid"])
                       if fill_quote and fill_quote.get("bid") is not None and
                       fill_quote.get("ask") is not None else None)
        expected_risk = execution.get("risk_amount")
        funding_amount = sum(float(row.get("amount") or 0) for row in funding_events)
        funding_evidence = ({
            "amount_usdt": funding_amount,
            "normalized_r": (funding_amount / float(expected_risk)
                             if expected_risk else None),
            "event_count": len(funding_events),
            "coverage": "APPLIED_EVENTS_JOINED_TO_ORDER",
            "events": [{"funding_time": row.get("funding_time"),
                        "funding_rate": row.get("funding_rate"),
                        "mark_price": row.get("mark_price"),
                        "amount_usdt": row.get("amount")} for row in funding_events],
        } if funding_events else {
            "amount_usdt": None, "normalized_r": None, "event_count": 0,
            "coverage": "NO_ORDER_SCOPED_FUNDING_EVIDENCE",
        })
        transitions = list(setup.get("transitions", []))
        # This fingerprint is setup-scoped evidence captured at decision time.
        # Never fingerprint the runtime rolling candle window: its membership
        # changes on every closed candle even when this setup does not.
        decision_evidence = {
            "setup_id": setup_id,
            "created_at": setup.get("created_at"),
            "strategy_id": setup.get("strategy_id"),
            "direction": setup.get("direction"),
            "zone_id": setup.get("zone_id"),
            "trigger_event_id": setup.get("trigger_event_id"),
            "context_snapshot": frozen_context,
            "pattern_metadata": setup.get("pattern_metadata") or [],
            "mtf_evidence": material_evidence(visual_state.get("mtf_evidence")),
            "proposal": ({
                "id": proposal.get("id"), "entry": proposal.get("entry"),
                "stop": proposal.get("stop"), "target": proposal.get("target"),
                "signal_at": proposal.get("signal_at"),
                "entry_model": proposal.get("entry_model"),
                # valid_until_index is deliberately absent. It is len(bars) - 1
                # + entry_expiry_bars, so it identifies where the rolling
                # buffer happened to end, not the data the decision was made
                # on: two replays over identical candles would fingerprint the
                # same decision differently. entry_expiry_bars is part of the
                # configuration fingerprint, which is where it belongs.
            } if proposal else None),
        }
        dataset_fingerprint = _fingerprint(decision_evidence, "decision-evidence")
        if paper_candidate and candidate_status in {"REJECTED", "DATA_PAUSED"}:
            payload = paper_candidate.get("payload") or {}
            transitions.append({
                "id": _fingerprint([setup_id, candidate_status, paper_candidate.get("created_at")], "transition"),
                "setup_id": setup_id, "from_phase": setup.get("phase"),
                "to_phase": candidate_status, "timestamp": paper_candidate.get("created_at"),
                "candle_identity": setup.get("created_at"),
                "market_data_health": feed_status.get("state", "UNKNOWN"),
                "reason_code": f"PAPER_{candidate_status}",
                "reason": payload.get("reason") or "paper execution eligibility rejected",
                "zone_id": setup.get("zone_id"), "order_id": None, "position_id": None,
                "strategy_version": STRATEGY_VERSION,
                "configuration_fingerprint": config_fingerprint,
                "relevant_prices": {"entry": requested, "stop": stop, "target": target},
            })
        record = {
            "identity": {
                "journal_entry_id": _fingerprint([session.get("id"), setup_id], "pa-journal"),
                "correlation_id": (paper_candidate or {}).get("correlation_id") or
                    ((paper_candidate or {}).get("payload") or {}).get("correlation_id"),
                "session_id": session.get("id"), "experiment_id": visual_state.get("metrics_scope", {}).get("experiment_id"),
                "strategy_family": STRATEGY_FAMILY, "strategy_id": setup.get("strategy_id"),
                "strategy_name": str(setup.get("strategy_id") or "").replace("_", " "),
                "strategy_version": STRATEGY_VERSION, "configuration_fingerprint": config_fingerprint,
                "engine_fingerprint": engine_fingerprint, "dataset_fingerprint": dataset_fingerprint,
                "symbol": (visual_state.get("symbol") or session.get("symbol") or "UNKNOWN").upper(),
                "timeframe": visual_state.get("timeframe") or session.get("timeframe") or "UNKNOWN",
                "direction": setup.get("direction"), "origin": execution.get("source", "research_engine"),
                "research_partition": partition_label, "execution_mode": "PAPER",
                "market_data_mode": visual_state.get("data_provenance", {}).get("market_data_mode"),
                "market_data_source": visual_state.get("data_provenance", {}).get("market_data_source"),
                "exchange": visual_state.get("data_provenance", {}).get("exchange", "Binance USDⓈ-M Futures"),
                "opened_at": setup.get("created_at"),
                "closed_at": (research_trade or {}).get("closed_at") or (latest_fill or {}).get("timestamp"),
            },
            "market_context": {
                "structure_state": frozen_context.get("structure_state",
                                                       visual_state.get("snapshot", {}).get("structure_bias")),
                "relevant_swings": frozen_context.get("relevant_swings", []),
                "zone_id": setup.get("zone_id"), "zone_role": (zone or {}).get("role"),
                "original_zone_role": (zone or {}).get("original_role"), "flipped_zone": (zone or {}).get("flipped"),
                "zone_low": (zone or {}).get("low"), "zone_high": (zone or {}).get("high"),
                "zone_age": frozen_context.get("zone_age"),
                "touch_count": (zone or {}).get("touch_count"),
                "higher_timeframe_context": (zone or {}).get("timeframe_scope"),
                "mtf_evidence": material_evidence(visual_state.get("mtf_evidence")),
                "market_regime": frozen_context.get(
                    "structure_state", visual_state.get("snapshot", {}).get("structure_bias", "neutral")),
                "data_health_state": frozen_context.get(
                    "market_data_health", feed_status.get("state", "UNKNOWN")),
                "data_health_reason": feed_status.get("health_reason"),
            },
            "setup": {
                "setup_id": setup_id, "state": setup.get("phase"),
                "location_reached_candle": setup.get("created_at"),
                "rejection_reclaim_candle": (event or {}).get("occurred_at"),
                "trigger_classification": (event or {}).get("event_type"),
                "pattern_metadata": trigger_patterns,
                "confusion_candle_count": config.get("confusion_candles"),
                "confirmation_boundary": {
                    "high": (proposal or {}).get("trigger_high"), "low": (proposal or {}).get("trigger_low")},
                "confirmation_candle": (proposal or {}).get("signal_at"),
                "invalidation_price": stop, "state_transitions": transitions,
                "acceptance_reasons": setup.get("reasons", []),
                "rejection_reasons": setup.get("missing_conditions", []),
            },
            "order_risk": {
                "order_id": order_id, "position_id": (broker_order or {}).get("symbol") if actual_fill else None,
                "entry_model": (proposal or {}).get("entry_model") or (research_trade or {}).get("entry_model"),
                "requested_entry": requested, "actual_simulated_fill": actual_fill,
                "stop": stop, "target": target, "initial_risk_price": initial_risk,
                "expected_risk_usdt": execution.get("risk_amount"), "expected_risk_r": 1.0 if initial_risk else None,
                "actual_risk_usdt": actual_risk, "quantity": quantity,
                "leverage": leverage, "margin": margin,
                "expiry_index": (proposal or {}).get("valid_until_index"),
                "bid_ask_decision": {"bid": decision_bid, "ask": decision_ask},
                "bid_ask_fill": fill_quote, "spread": spread,
                "fill_spread": fill_spread,
                "fill_quote_evidence_status": (
                    "RECONCILED_PUBLIC_STREAM_QUOTE" if fill_quote else "NOT_AVAILABLE"),
                "slippage": ((research_trade or {}).get("config_snapshot") or {}).get("slippage_bps"),
                "commission": sum(float(row.get("fee") or 0) for row in fills) +
                    sum(float((row.get("payload") or {}).get("fee") or 0)
                        for row in execution_events if row.get("kind") == "paper_position_completed"),
                "funding": funding_evidence, "contract_rounding": execution.get("contract_rules"),
            },
            "outcome": {
                "status": outcome_status, "result": result,
                "exit_price": (research_trade or {}).get("exit_price") or (latest_fill or {}).get("price"),
                "exit_timestamp": (research_trade or {}).get("closed_at") or (latest_fill or {}).get("timestamp"),
                "exit_reason": (research_trade or {}).get("reason"),
                "gross_r": (research_trade or {}).get("gross_r"),
                "net_r": (research_trade or {}).get("net_r"),
                "costs_r": (research_trade or {}).get("costs_r"),
                "gross_result_usdt": (latest_fill or {}).get("realized_pnl"),
                "net_result_usdt": None,
                "maximum_favourable_excursion": (research_trade or {}).get(
                    "maximum_favourable_excursion_r"),
                "maximum_adverse_excursion": (research_trade or {}).get(
                    "maximum_adverse_excursion_r"),
                "excursion_unit": "R",
                "excursion_model": (research_trade or {}).get("excursion_model"),
                "bars_to_entry": (research_trade or {}).get("bars_to_entry"),
                "bars_in_trade": (research_trade or {}).get("bars_in_trade"),
                "intrabar_ambiguity": (research_trade or {}).get("intrabar_ambiguous"),
                "premise_remained_valid": outcome_status not in {"INVALIDATED", "RULE_VIOLATION"},
                "execution_materially_different": (
                    requested is not None and actual_fill is not None and initial_risk
                    and abs(float(actual_fill) - requested) / initial_risk > .1),
            },
            "review": {
                "what_happened": (research_trade or {}).get("reason") or "setup remains under observation",
                "what_worked": [], "what_failed": [],
                "rule_compliance": not bool(setup.get("missing_conditions")),
                "include_in_research_statistics": feed_status.get("state") in {
                    "SYNCHRONIZED", "REPLAY", "HISTORICAL_REPLAY"},
                "machine_reason_codes": [outcome_status], "researcher_notes": "", "tags": [],
            },
            "chart_state": {
                "candle_timestamp": setup.get("created_at"), "zone_id": setup.get("zone_id"),
                "event_id": setup.get("trigger_event_id"), "entry": requested,
                "stop": stop, "target": target,
            },
        }
        classification, explanation = PriceActionJournalStore._classification(record)
        record["review"]["learning_classification"] = classification
        record["review"]["classification_explanation"] = explanation
        record["review"]["include_in_research_statistics"] = classification not in {
            "DATA_QUALITY_FAILURE", "RULE_VIOLATION"}
        return record

    @staticmethod
    def _material_projection(record: dict) -> dict:
        """Return immutable lifecycle/evidence fields that justify a revision.

        Quotes shown by the UI and current feed explanations are useful in a
        response, but are not new evidence about an already-created setup.

        The bar counters and the entry expiry belong here for a sharper reason:
        they are not facts about the setup at all, they are positions in the
        runtime's rolling candle buffer. ``bars_in_trade`` is ``index -
        filled_index``, and ``expiry_index`` is ``len(bars) - 1 +
        entry_expiry_bars`` (native_price_action.py). Both shift as the window
        slides and jump outright when it is re-seeded by a restart or a
        re-sync -- observed going *down*, 1254 to 1253 for the counter and 1242
        to 235 for the expiry. Since the hash is what decides whether to append
        another copy of the record, a number that changes on its own writes a
        revision on its own.

        Between them they cost 59,560 of 72,443 revisions on the production
        lab -- 82% of them recording no lifecycle event at all, at ~9.5 KB each
        -- and grew the journal until writes began timing out and the lab
        refused to place orders it could not durably record.

        The split between the two was measured, not assumed, by
        scripts/pa_journal_churn.py, after excluding the counters alone turned
        out not to be enough. Of the 7,833 revisions still being written on the
        counters-only build, ``expiry_index`` was the ONLY field that differed
        in 5,212 of them (50.0 MB of 75.0 MB) and appeared in 99.8% of all
        differing pairs. The excursions, which were the standing hypothesis at
        the time, accounted for 10 revisions and 0.1 MB and are deliberately
        still hashed: a real move in MFE or MAE is evidence.

        Any rate quoted for this is an average over a bursty process, not a
        steady drip: a revision is only written per open setup, so an active
        trade with a drifting number produced hundreds in minutes while a quiet
        book produced almost none.
        An early estimate of ~860 MB/day came from extrapolating one burst
        across a gap that had not been measured, and a later observation of 36
        revisions in 46 minutes disproved it.

        Excluding them here does not hide them: every stored payload still
        carries its counters and its expiry index, and any revision written
        for a real reason
        captures whatever they are at that moment.
        """
        material = _canonical(record)
        material.get("market_context", {}).pop("data_health_reason", None)
        order_risk = material.get("order_risk", {})
        order_risk.pop("bid_ask_decision", None)
        order_risk.pop("spread", None)
        order_risk.pop("expiry_index", None)
        outcome = material.get("outcome", {})
        outcome.pop("bars_in_trade", None)
        outcome.pop("bars_to_entry", None)
        return material

    @classmethod
    def _material_hash(cls, record: dict) -> str:
        return _fingerprint(cls._material_projection(record), "material-revision")

    @staticmethod
    def _reason_code(prior: dict | None, record: dict) -> str:
        if prior is None:
            return "SETUP_CREATED"
        if (prior.get("outcome") or {}).get("status") != record["outcome"].get("status"):
            if record["outcome"].get("exit_timestamp") or record["outcome"].get("result") not in {None, "open"}:
                return "OUTCOME_CLOSED"
            return "LIFECYCLE_TRANSITION"
        prior_risk, current_risk = prior.get("order_risk", {}), record.get("order_risk", {})
        if (prior_risk.get("actual_simulated_fill"), prior_risk.get("order_id")) != (
                current_risk.get("actual_simulated_fill"), current_risk.get("order_id")):
            return "EXECUTION_FILL"
        return "MATERIAL_EVIDENCE_CHANGED"

    def capture(self, *, visual_state: dict, session: dict, paper_state: dict,
                feed_status: dict, partition_label: str) -> list[str]:
        """Capture changed setup snapshots as append-only revisions."""
        if partition_label not in {"development", "validation", "untouched_oos", "paper_forward"}:
            raise ValueError("invalid research partition label")
        captured: list[str] = []
        try:
            with self._lock:
                for setup in visual_state.get("setups", []):
                    record = self._record(
                        visual_state=visual_state, session=session,
                        paper_state=paper_state, feed_status=feed_status,
                        setup=setup, partition_label=partition_label)
                    record = _canonical(record)
                    identity = record["identity"]
                    journal_id = identity["journal_entry_id"]
                    existing = self._db.execute(
                        "SELECT id FROM pa_journal_entries WHERE id=?", (journal_id,)).fetchone()
                    latest = self._db.execute(
                        "SELECT revision_no,payload_hash,payload_json FROM pa_journal_revisions "
                        "WHERE journal_id=? ORDER BY revision_no DESC LIMIT 1", (journal_id,)).fetchone()
                    prior = None
                    if latest:
                        # Decision-time identity/context are evidence, not
                        # mutable projections of the rolling runtime window.
                        prior = json.loads(latest["payload_json"])
                        prior_identity = prior["identity"]
                        closed_at = record["identity"].get("closed_at")
                        record["identity"] = prior_identity
                        record["identity"]["closed_at"] = closed_at or prior_identity.get("closed_at")
                        record["market_context"] = prior["market_context"]
                        for key in (
                            "location_reached_candle", "rejection_reclaim_candle",
                            "trigger_classification", "pattern_metadata",
                            "confusion_candle_count", "confirmation_boundary",
                            "confirmation_candle", "invalidation_price",
                            "acceptance_reasons", "rejection_reasons"):
                            record["setup"][key] = prior["setup"].get(key)
                        # A transition records something that happened. Rebuilt
                        # every capture, it stamps the feed state and the
                        # proposal prices of *now* onto a past event, so the
                        # same transition keeps changing while the event does
                        # not -- and _classification reads that health back,
                        # flipping the setup's learning classification with the
                        # feed. Keep what was recorded, append only what is new.
                        recorded = prior["setup"].get("state_transitions") or []
                        seen = {_transition_key(row) for row in recorded}
                        record["setup"]["state_transitions"] = list(recorded) + [
                            row for row in (record["setup"].get("state_transitions") or [])
                            if _transition_key(row) not in seen]
                        record["chart_state"] = prior["chart_state"]
                        # Runtime capture may append lifecycle evidence but may
                        # never erase an immutable researcher annotation.
                        record["review"]["researcher_notes"] = prior["review"].get(
                            "researcher_notes", "")
                        record["review"]["tags"] = prior["review"].get("tags", [])
                        classification, explanation = self._classification(record)
                        record["review"]["learning_classification"] = classification
                        record["review"]["classification_explanation"] = explanation
                        record["review"]["include_in_research_statistics"] = classification not in {
                            "DATA_QUALITY_FAILURE", "RULE_VIOLATION"}
                    payload_hash = self._material_hash(record)
                    # Historical rows used a full-payload hash. Comparing the
                    # projection avoids a one-time migration revision storm.
                    if prior is not None and self._material_hash(prior) == payload_hash:
                        continue
                    revision_no = int(latest["revision_no"] if latest else 0) + 1
                    self._db.execute("BEGIN IMMEDIATE")
                    try:
                        if not existing:
                            self._db.execute(
                                "INSERT INTO pa_journal_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (journal_id, identity["session_id"], identity["experiment_id"],
                                 setup["id"], identity["strategy_id"], identity["strategy_version"],
                                 identity["configuration_fingerprint"], identity["engine_fingerprint"],
                                 identity["dataset_fingerprint"], identity["symbol"], identity["timeframe"],
                                 identity["direction"], identity["origin"], identity["research_partition"],
                                 record["outcome"]["status"], record["outcome"]["result"],
                                 str(setup.get("created_at")), record["outcome"].get("exit_timestamp"),
                                 json.dumps(record, sort_keys=True), _now(),))
                        self._db.execute(
                            "INSERT OR IGNORE INTO pa_journal_revisions VALUES (?,?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, journal_id, revision_no,
                             self._reason_code(prior, record), _now(),
                             "price_action_runtime", payload_hash,
                             json.dumps(record, sort_keys=True)))
                        self._db.execute("COMMIT")
                    except Exception:
                        self._db.execute("ROLLBACK")
                        raise
                    captured.append(journal_id)
        except sqlite3.OperationalError as exc:
            if is_sqlite_busy(exc):
                raise PersistenceBlocked("capture", exc) from exc
            raise
        return captured

    def record_legacy_remediation(
            self, *, session: dict, original_position: dict,
            close_result: dict, market_evidence: dict,
            remediation_id: str, initiated_by: str) -> str:
        """Append immutable evidence for a user-confirmed legacy PAPER close.

        Unknown historical protection and risk fields remain explicitly null.
        The remediation is operational cleanup, not a reconstructed strategy
        trade and is excluded from research/performance statistics.
        """
        symbol = str(original_position["symbol"]).upper()
        direction = str(original_position["side"])
        fill = close_result.get("fill") or close_result.get("event") or {}
        closed_at = str(fill.get("timestamp") or _now())
        journal_id = _fingerprint(
            [session.get("id"), remediation_id, symbol], "pa-journal-remediation"
        )
        setup_id = f"LEGACY_REMEDIATION:{remediation_id}"
        record = _canonical({
            "identity": {
                "journal_entry_id": journal_id,
                "correlation_id": remediation_id,
                "session_id": session.get("id"), "experiment_id": None,
                "strategy_family": STRATEGY_FAMILY,
                "strategy_id": "LEGACY_UNVERIFIED",
                "strategy_name": "Legacy / Unverified",
                "strategy_version": "LEGACY / UNVERIFIED",
                "configuration_fingerprint": "LEGACY_UNVERIFIED",
                "engine_fingerprint": "LEGACY_UNVERIFIED",
                "dataset_fingerprint": "LEGACY_UNVERIFIED",
                "symbol": symbol, "timeframe": session.get("timeframe") or "UNKNOWN",
                "direction": direction, "origin": "legacy_position_remediation",
                "research_partition": "paper_forward", "execution_mode": "PAPER",
                "market_data_mode": "LIVE",
                "market_data_source": "Binance USDⓈ-M reconciled public mark",
                "exchange": "Binance USDⓈ-M Futures",
                "opened_at": original_position.get("opened_at"),
                "closed_at": closed_at,
            },
            "market_context": {
                "structure_state": None, "relevant_swings": [], "zone_id": None,
                "zone_role": None, "original_zone_role": None,
                "flipped_zone": None, "zone_low": None, "zone_high": None,
                "zone_age": None, "touch_count": None,
                "higher_timeframe_context": None, "market_regime": None,
                "data_health_state": market_evidence.get("state"),
                "data_health_reason": market_evidence.get("health_reason"),
            },
            "setup": {
                "setup_id": setup_id, "state": "REMEDIATED",
                "location_reached_candle": None,
                "rejection_reclaim_candle": None,
                "trigger_classification": "LEGACY_POSITION_REMEDIATION",
                "pattern_metadata": [], "confusion_candle_count": None,
                "confirmation_boundary": {"high": None, "low": None},
                "confirmation_candle": None, "invalidation_price": None,
                "state_transitions": [{
                    "id": remediation_id, "setup_id": setup_id,
                    "from_phase": "LEGACY_UNPROTECTED", "to_phase": "REMEDIATED",
                    "timestamp": closed_at, "candle_identity": None,
                    "market_data_health": market_evidence.get("state"),
                    "reason_code": "LEGACY_POSITION_REMEDIATION",
                    "reason": "user-confirmed paper-account cleanup",
                    "zone_id": None, "order_id": fill.get("order_id"),
                    "position_id": symbol,
                    "strategy_version": "LEGACY / UNVERIFIED",
                    "configuration_fingerprint": "LEGACY_UNVERIFIED",
                    "relevant_prices": {
                        "entry": original_position.get("entry_price"),
                        "stop": None, "target": None,
                    },
                }],
                "acceptance_reasons": ["explicit authenticated user confirmation"],
                "rejection_reasons": [
                    "historical stop, target, risk and R:R provenance unavailable"
                ],
            },
            "order_risk": {
                "order_id": fill.get("order_id"), "position_id": symbol,
                "entry_model": "LEGACY_UNVERIFIED",
                "requested_entry": original_position.get("entry_price"),
                "actual_simulated_fill": fill.get("price"),
                "stop": None, "target": None, "initial_risk_price": None,
                "expected_risk_usdt": None, "expected_risk_r": None,
                "actual_risk_usdt": None,
                "quantity": fill.get("quantity") or original_position.get("size"),
                "leverage": None, "margin": None, "expiry_index": None,
                "bid_ask_decision": {
                    "bid": market_evidence.get("bid"),
                    "ask": market_evidence.get("ask"),
                },
                "bid_ask_fill": market_evidence,
                "spread": None, "fill_spread": None,
                "fill_quote_evidence_status": "RECONCILED_PUBLIC_STREAM_MARK",
                "slippage": None, "commission": fill.get("fee"),
                "funding": {"amount_usdt": None, "normalized_r": None,
                            "event_count": 0,
                            "coverage": "LEGACY_ORDER_SCOPE_UNAVAILABLE"},
                "contract_rounding": None,
            },
            "outcome": {
                "status": "REMEDIATED", "result": "closed",
                "exit_price": fill.get("price"), "exit_timestamp": closed_at,
                "exit_reason": "LEGACY_POSITION_REMEDIATION",
                "gross_r": None, "net_r": None, "costs_r": None,
                "gross_result_usdt": fill.get("realized_pnl"),
                "net_result_usdt": (
                    float(fill.get("realized_pnl") or 0) - float(fill.get("fee") or 0)
                ),
                "maximum_favourable_excursion": None,
                "maximum_adverse_excursion": None, "excursion_unit": "R",
                "excursion_model": "UNAVAILABLE_LEGACY_PROVENANCE",
                "bars_to_entry": None, "bars_in_trade": None,
                "intrabar_ambiguity": None, "premise_remained_valid": None,
                "execution_materially_different": None,
            },
            "review": {
                "what_happened": "Legacy unprotected PAPER position closed by explicit remediation",
                "what_worked": ["original position and reconciled close evidence preserved"],
                "what_failed": ["original protection and risk provenance unavailable"],
                "learning_classification": "RANDOM_OR_INCONCLUSIVE",
                "classification_explanation": "operational cleanup is not strategy evidence",
                "include_in_research_statistics": False,
                "rule_compliance": None, "researcher_notes": "",
                "tags": ["legacy", "paper", "remediation"],
                "initiated_by": initiated_by,
            },
            "chart_state": {
                "candle_timestamp": None, "zone_id": None, "event_id": None,
                "entry": original_position.get("entry_price"),
                "stop": None, "target": None,
            },
            "audit": {
                "reason_code": "LEGACY_POSITION_REMEDIATION",
                "remediation_id": remediation_id,
                "reference_mark_price": close_result.get("reference_mark_price"),
                "original_position": original_position,
                "close_result": close_result,
                "market_evidence": market_evidence,
                "real_execution_allowed": False,
            },
        })
        with self._lock:
            self._db.execute(
                "INSERT INTO pa_journal_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (journal_id, session.get("id"), None, setup_id,
                 "LEGACY_UNVERIFIED", "LEGACY / UNVERIFIED",
                 "LEGACY_UNVERIFIED", "LEGACY_UNVERIFIED", "LEGACY_UNVERIFIED",
                 symbol, session.get("timeframe") or "UNKNOWN", direction,
                 "legacy_position_remediation", "paper_forward", "REMEDIATED",
                 "closed", str(original_position.get("opened_at") or closed_at),
                 closed_at, json.dumps(record, sort_keys=True), _now()),
            )
            payload_hash = _fingerprint(record, "revision")
            self._db.execute(
                "INSERT INTO pa_journal_revisions VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, journal_id, 1, "LEGACY_POSITION_REMEDIATION",
                 _now(), initiated_by, payload_hash,
                 json.dumps(record, sort_keys=True)),
            )
        return journal_id

    def _latest_records(self, *, session_id: str | None = None,
                        strategy_id: str | None = None, symbol: str | None = None,
                        timeframe: str | None = None, direction: str | None = None,
                        partition: str | None = None, date_from: str | None = None,
                        date_to: str | None = None) -> list[dict]:
        """Load the newest revision of each matching entry.

        The filters are applied here rather than after loading. Without them
        this read every journal entry ever written, joined every revision, and
        json.loads()'d every payload, and bot_status calls it three times per
        poll. On a journal with real history that took longer than nginx's
        ninety-second ceiling, so the Price Action lab answered 504 and the
        dashboard showed nothing at all.

        Every column filtered here is in idx_pa_journal_filters, and each one
        is equivalent to the Python check it replaces: the insert writes
        identity["research_partition"] into partition_label, and the remaining
        columns are the same ones list() compared against row[...]. Filters
        that read the JSON payload stay in Python, since only the payload can
        answer them.
        """
        clauses, params = [], []
        for column, value in (("session_id", session_id), ("strategy_id", strategy_id),
                              ("symbol", symbol.upper() if symbol else None),
                              ("timeframe", timeframe), ("direction", direction),
                              ("partition_label", partition)):
            if value:
                clauses.append(f"e.{column}=?")
                params.append(value)
        if date_from:
            clauses.append("e.opened_at>=?")
            params.append(date_from)
        if date_to:
            clauses.append("e.opened_at<=?")
            params.append(date_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._db.execute(f"""
          SELECT e.*,r.revision_no,r.payload_json FROM pa_journal_entries e
          JOIN pa_journal_revisions r ON r.journal_id=e.id
          JOIN (SELECT journal_id,MAX(revision_no) revision_no FROM pa_journal_revisions GROUP BY journal_id) latest
            ON latest.journal_id=r.journal_id AND latest.revision_no=r.revision_no
          {where}
          ORDER BY e.opened_at DESC
        """, params).fetchall()
        return [{"index": dict(row), "record": json.loads(row["payload_json"])} for row in rows]

    def list(self, *, session_id: str | None = None, strategy_id: str | None = None,
             symbol: str | None = None, timeframe: str | None = None,
             direction: str | None = None, result: str | None = None,
             trigger_type: str | None = None, partition: str | None = None,
             data_quality: str | None = None, strategy_version: str | None = None,
             zone_type: str | None = None, touch_count: int | None = None,
             regime: str | None = None, rule_compliance: bool | None = None,
             entry_model: str | None = None,
             date_from: str | None = None, date_to: str | None = None) -> dict:
        records = self._latest_records(
            session_id=session_id, strategy_id=strategy_id, symbol=symbol,
            timeframe=timeframe, direction=direction, partition=partition,
            date_from=date_from, date_to=date_to)
        # keep() still re-checks these. They are now redundant rather than
        # wrong, and leaving them means the payload-only filters below read
        # exactly as before.
        def keep(item: dict) -> bool:
            row, record = item["index"], item["record"]
            ident, setup, review = record["identity"], record["setup"], record["review"]
            checks = (
                not session_id or row["session_id"] == session_id,
                not strategy_id or row["strategy_id"] == strategy_id,
                not symbol or row["symbol"] == symbol.upper(),
                not timeframe or row["timeframe"] == timeframe,
                not direction or row["direction"] == direction,
                not result or record["outcome"]["result"] == result,
                not trigger_type or setup.get("trigger_classification") == trigger_type,
                not partition or ident["research_partition"] == partition,
                not data_quality or record["market_context"]["data_health_state"] == data_quality,
                not strategy_version or ident["strategy_version"] == strategy_version,
                not zone_type or record["market_context"].get("zone_role") == zone_type,
                touch_count is None or record["market_context"].get("touch_count") == touch_count,
                not regime or record["market_context"].get("market_regime") == regime,
                rule_compliance is None or review.get("rule_compliance") is rule_compliance,
                not entry_model or record["order_risk"].get("entry_model") == entry_model,
                not date_from or row["opened_at"] >= date_from,
                not date_to or row["opened_at"] <= date_to,
            )
            return all(checks)
        filtered = [item for item in records if keep(item)]
        nets = [float(item["record"]["outcome"]["net_r"])
                for item in filtered if item["record"]["outcome"].get("net_r") is not None]
        return {
            "entries": [{**item["record"], "revision_no": item["index"]["revision_no"]}
                        for item in filtered],
            "statistics": {
                "setups": len(filtered), "completed": len(nets),
                "wins": sum(value > 0 for value in nets), "losses": sum(value < 0 for value in nets),
                "net_r": sum(nets), "expectancy_r": sum(nets) / len(nets) if nets else 0,
            },
            "filters_are_configuration_scoped": True,
            "real_execution_allowed": False,
        }

    def get(self, journal_id: str) -> dict:
        base = self._db.execute("SELECT * FROM pa_journal_entries WHERE id=?", (journal_id,)).fetchone()
        if not base:
            raise KeyError(journal_id)
        revisions = [dict(row) for row in self._db.execute(
            "SELECT * FROM pa_journal_revisions WHERE journal_id=? ORDER BY revision_no", (journal_id,))]
        for row in revisions:
            row["payload"] = json.loads(row.pop("payload_json"))
        return {"base": json.loads(base["base_json"]), "revisions": revisions,
                "latest": revisions[-1]["payload"], "immutable": True,
                "real_execution_allowed": False}

    def revise(self, journal_id: str, *, notes: str, tags: list[str], initiated_by: str) -> dict:
        current = self.get(journal_id)["latest"]
        payload = _canonical(current)
        payload["review"]["researcher_notes"] = str(notes)[:4000]
        payload["review"]["tags"] = sorted({str(tag).strip()[:60] for tag in tags if str(tag).strip()})
        latest = self._db.execute(
            "SELECT MAX(revision_no) FROM pa_journal_revisions WHERE journal_id=?", (journal_id,)).fetchone()[0]
        payload_hash = _fingerprint(payload, "revision")
        if self._db.execute(
                "SELECT 1 FROM pa_journal_revisions WHERE journal_id=? AND payload_hash=?",
                (journal_id, payload_hash)).fetchone():
            return self.get(journal_id)
        self._db.execute(
            "INSERT INTO pa_journal_revisions VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, journal_id, int(latest) + 1, "RESEARCHER_ANNOTATION",
             _now(), initiated_by or "authenticated_user", payload_hash,
             json.dumps(payload, sort_keys=True)))
        return self.get(journal_id)

    def analyze(self, *, minimum_pattern_sample: int = 30) -> dict:
        entries = [item["record"] for item in self._latest_records()]
        classifications = Counter(row["review"]["learning_classification"] for row in entries)
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in entries:
            identity, context = row["identity"], row["market_context"]
            order, outcome = row["order_risk"], row["outcome"]
            touch = context.get("touch_count")
            stop_distance = order.get("initial_risk_price")
            cost_driven = (outcome.get("net_r") is not None and outcome.get("gross_r") is not None
                           and float(outcome["gross_r"]) > 0 > float(outcome["net_r"]))
            dimensions = {
                "strategy_direction_market": (
                    f"{identity['strategy_id']}|{identity['direction']}|"
                    f"{identity['symbol']}|{identity['timeframe']}"),
                "regime": str(context.get("market_regime") or "unknown"),
                "zone_touch": "first_touch" if touch == 1 else "repeated_touch" if touch else "unknown",
                "zone_lifecycle": "flipped" if context.get("flipped_zone") else "original",
                "trigger": str(row["setup"].get("trigger_classification") or "unknown"),
                "entry_model": str(order.get("entry_model") or "unknown"),
                "data_health": str(context.get("data_health_state") or "unknown"),
                "stop_distance": ("tight" if stop_distance is not None and float(stop_distance) < .002 *
                                  float(order.get("requested_entry") or 1) else "wide_or_unknown"),
                "cost_effect": "cost_flipped_result" if cost_driven else "not_cost_flipped",
            }
            for dimension, segment in dimensions.items():
                groups[(dimension, segment)].append(row)
        patterns = []
        for key, rows in sorted(groups.items()):
            closed = [float(row["outcome"]["net_r"]) for row in rows
                      if row["outcome"].get("net_r") is not None]
            sample = len(closed)
            expectancy = sum(closed) / sample if sample else 0
            development = [float(row["outcome"]["net_r"]) for row in rows
                           if row["outcome"].get("net_r") is not None and
                           row["identity"]["research_partition"] == "development"]
            validation = [float(row["outcome"]["net_r"]) for row in rows
                          if row["outcome"].get("net_r") is not None and
                          row["identity"]["research_partition"] == "validation"]
            first_identity = rows[0]["identity"]
            patterns.append({
                "dimension": key[0], "segment": key[1],
                "strategy_id": first_identity["strategy_id"],
                "direction": first_identity["direction"],
                "symbol": first_identity["symbol"], "timeframe": first_identity["timeframe"],
                "setups": len(rows), "completed": sample,
                "net_expectancy_r": expectancy, "uncertainty": (
                    "HIGH" if sample < minimum_pattern_sample else "MEDIUM" if sample < 100 else "LOW"),
                "candidate_eligible": (key[0] != "data_health" and sample >= minimum_pattern_sample
                                       and expectancy < 0),
                "development_result": {
                    "sample": len(development),
                    "expectancy_r": sum(development) / len(development) if development else None,
                },
                "validation_result": {
                    "sample": len(validation),
                    "expectancy_r": sum(validation) / len(validation) if validation else None,
                },
                "cost_sensitivity_r": sum(float(row["outcome"].get("costs_r") or 0) for row in rows),
                "assets": sorted({row["identity"]["symbol"] for row in rows}),
                "timeframes": sorted({row["identity"]["timeframe"] for row in rows}),
                "confounding_factors": ["execution costs", "regime mix", "rolling-window selection"],
            })
        return {
            "classifications": {name: classifications.get(name, 0)
                                for name in sorted(LEARNING_CLASSES)},
            "patterns": patterns,
            "minimum_pattern_sample": minimum_pattern_sample,
            "warning": "Patterns are hypotheses, not approved strategy changes.",
            "active_strategy_mutated": False, "real_execution_allowed": False,
        }

    def propose_candidate(self, *, parent_strategy_version: str, rule_difference: dict,
                          evidence_ids: list[str], contradicting_evidence: list[str],
                          development_period: dict, validation_period: dict,
                          code_fingerprint: str, dataset_fingerprint: str,
                          expected_benefit: str, risks: list[str], source_partition: str) -> dict:
        if source_partition != "development":
            raise ValueError("candidate hypotheses may only be generated from development evidence")
        if not evidence_ids:
            raise ValueError("candidate requires immutable development journal evidence")
        if not development_period or not validation_period:
            raise ValueError("candidate requires explicit development and validation periods")
        if not str(expected_benefit).strip() or not risks:
            raise ValueError("candidate requires an expected benefit and explicit risks")
        if len(rule_difference) != 1:
            raise ValueError("a candidate must contain exactly one isolated rule difference")
        rule_key, rule_value = next(iter(rule_difference.items()))
        if rule_key not in ALLOWED_CANDIDATE_RULES:
            raise ValueError("candidate rule is outside the governed Price Action allowlist")
        _validate_rule_value(rule_key, rule_value)
        evidence = []
        for journal_id in evidence_ids:
            row = self.get(journal_id)["latest"]
            if row["identity"]["research_partition"] != "development":
                raise ValueError("validation, untouched OOS and paper-forward evidence cannot tune a candidate")
            evidence.append(journal_id)
        material = [parent_strategy_version, rule_key, rule_value, evidence,
                    code_fingerprint, dataset_fingerprint]
        candidate_id = _fingerprint(material, "pa-candidate")
        existing = self._db.execute(
            "SELECT * FROM pa_learning_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not existing:
            self._db.execute(
                "INSERT INTO pa_learning_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (candidate_id, parent_strategy_version, rule_key,
                 json.dumps(_canonical(rule_value)), json.dumps(evidence),
                 json.dumps(_canonical(contradicting_evidence)),
                 json.dumps(_canonical(development_period)), json.dumps(_canonical(validation_period)),
                 code_fingerprint, dataset_fingerprint, expected_benefit[:2000],
                 json.dumps(_canonical(risks)), "DRAFT", _now()))
            self._db.execute(
                "INSERT INTO pa_candidate_events VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, candidate_id, "NONE", "DRAFT",
                 "isolated development hypothesis created", "research_system", _now()))
        return self.candidate(candidate_id)

    def candidate(self, candidate_id: str) -> dict:
        row = self._db.execute(
            "SELECT * FROM pa_learning_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise KeyError(candidate_id)
        result = dict(row)
        for key in ("rule_value_json", "evidence_json", "contradicting_evidence_json",
                    "development_period_json", "validation_period_json", "risks_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        result["events"] = [dict(event) for event in self._db.execute(
            "SELECT * FROM pa_candidate_events WHERE candidate_id=? ORDER BY created_at", (candidate_id,))]
        result["live_execution_allowed"] = False
        return result

    def candidates(self) -> list[dict]:
        return [self.candidate(row[0]) for row in self._db.execute(
            "SELECT id FROM pa_learning_candidates ORDER BY created_at DESC")]

    def candidate_transition(self, candidate_id: str, *, action: str,
                             reason: str, initiated_by: str) -> dict:
        candidate = self.candidate(candidate_id)
        transitions = {
            ("DRAFT", "record_development"): "DEVELOPMENT_TESTED",
            ("DEVELOPMENT_TESTED", "pass_validation"): "VALIDATION_PASSED",
            ("VALIDATION_PASSED", "pass_robustness"): "ROBUSTNESS_PASSED",
            ("ROBUSTNESS_PASSED", "approve_shadow"): "APPROVED_FOR_SHADOW",
            ("DRAFT", "reject"): "REJECTED",
            ("DEVELOPMENT_TESTED", "reject"): "REJECTED",
            ("VALIDATION_PASSED", "reject"): "REJECTED",
            ("ROBUSTNESS_PASSED", "reject"): "REJECTED",
            ("APPROVED_FOR_SHADOW", "start_shadow"): "SHADOW_ACTIVE",
            ("SHADOW_ACTIVE", "stop_shadow"): "SHADOW_COMPLETE",
        }
        target = transitions.get((candidate["status"], action))
        if not target:
            raise ValueError("candidate transition is not allowed")
        self._db.execute("UPDATE pa_learning_candidates SET status=? WHERE id=?", (target, candidate_id))
        self._db.execute(
            "INSERT INTO pa_candidate_events VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, candidate_id, candidate["status"], target,
             reason[:2000], initiated_by or "authenticated_user", _now()))
        return self.candidate(candidate_id)

    def record_shadow(self, *, run_id: str, candidate_id: str, session_id: str,
                      candle_identity: str, baseline: dict, candidate: dict) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO pa_shadow_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, run_id, candidate_id, session_id, candle_identity,
             json.dumps(_canonical(baseline), sort_keys=True),
             json.dumps(_canonical(candidate), sort_keys=True), _now(), "PAPER", 0))

    def shadow_report(self, candidate_id: str) -> dict:
        rows = [dict(row) for row in self._db.execute(
            "SELECT * FROM pa_shadow_observations WHERE candidate_id=? ORDER BY created_at",
            (candidate_id,))]
        shared = baseline_only = candidate_only = changed_entries = changed_stops = 0
        for row in rows:
            baseline, candidate = json.loads(row["baseline_json"]), json.loads(row["candidate_json"])
            baseline_proposals = {item["setup_id"]: item for item in baseline.get("proposals", [])}
            candidate_proposals = {item["setup_id"]: item for item in candidate.get("proposals", [])}
            if not baseline_proposals and baseline.get("proposal_ids"):
                baseline_proposals = {str(item): {} for item in baseline["proposal_ids"]}
            if not candidate_proposals and candidate.get("proposal_ids"):
                candidate_proposals = {str(item): {} for item in candidate["proposal_ids"]}
            baseline_setups, candidate_setups = set(baseline_proposals), set(candidate_proposals)
            shared_setups = baseline_setups & candidate_setups
            shared += len(shared_setups)
            baseline_only += len(baseline_setups - candidate_setups)
            candidate_only += len(candidate_setups - baseline_setups)
            changed_entries += sum(
                baseline_proposals[key].get("entry") != candidate_proposals[key].get("entry")
                for key in shared_setups)
            changed_stops += sum(
                baseline_proposals[key].get("stop") != candidate_proposals[key].get("stop")
                for key in shared_setups)
        latest_baseline = json.loads(rows[-1]["baseline_json"]) if rows else {}
        latest_candidate = json.loads(rows[-1]["candidate_json"]) if rows else {}
        baseline_trades = {item["setup_id"]: item for item in latest_baseline.get("trades", [])}
        candidate_trades = {item["setup_id"]: item for item in latest_candidate.get("trades", [])}
        avoided_losses = sum(
            row.get("status") == "LOST" and candidate_trades.get(key, {}).get("status") != "LOST"
            for key, row in baseline_trades.items())
        missed_winners = sum(
            row.get("status") == "WON" and candidate_trades.get(key, {}).get("status") != "WON"
            for key, row in baseline_trades.items())
        baseline_completed = [row for row in baseline_trades.values() if row.get("status") in {"WON", "LOST"}]
        candidate_completed = [row for row in candidate_trades.values() if row.get("status") in {"WON", "LOST"}]

        def drawdown(trades: list[dict]) -> float:
            equity = peak = maximum = 0.0
            for trade in sorted(trades, key=lambda item: str(item.get("closed_at") or "")):
                equity += float(trade.get("net_r") or 0)
                peak = max(peak, equity)
                maximum = max(maximum, peak - equity)
            return maximum

        baseline_net = sum(float(row.get("net_r") or 0) for row in baseline_completed)
        candidate_net = sum(float(row.get("net_r") or 0) for row in candidate_completed)
        baseline_costs = sum(float(row.get("costs_r") or 0) for row in baseline_completed)
        candidate_costs = sum(float(row.get("costs_r") or 0) for row in candidate_completed)
        return {
            "candidate_id": candidate_id, "observations": len(rows),
            "shared_signals": shared, "baseline_only_signals": baseline_only,
            "candidate_only_signals": candidate_only,
            "changed_entries": changed_entries, "changed_stops": changed_stops,
            "avoided_losses": avoided_losses, "missed_winners": missed_winners,
            "additional_costs_r": candidate_costs - baseline_costs,
            "net_effect_r": candidate_net - baseline_net,
            "net_oos_effect": None,
            "net_oos_effect_status": "NOT_OOS_PAPER_SHADOW",
            "baseline_drawdown_r": drawdown(baseline_completed),
            "candidate_drawdown_r": drawdown(candidate_completed),
            "drawdown_effect_r": drawdown(candidate_completed) - drawdown(baseline_completed),
            "baseline_completed_trades": len(baseline_completed),
            "candidate_completed_trades": len(candidate_completed),
            "trade_count_change": len(candidate_completed) - len(baseline_completed),
            "official_paper_account_affected": False,
            "promotion_automatic": False, "real_execution_allowed": False,
        }

    def factory_reset(self) -> None:
        """Authorized global reset hook; normal callers cannot delete journal rows."""
        with self._lock:
            self._db.executescript("""
              DROP TRIGGER IF EXISTS pa_journal_entries_no_update;
              DROP TRIGGER IF EXISTS pa_journal_entries_no_delete;
              DROP TRIGGER IF EXISTS pa_journal_revisions_no_update;
              DROP TRIGGER IF EXISTS pa_journal_revisions_no_delete;
              DELETE FROM pa_shadow_observations;
              DELETE FROM pa_candidate_events;
              DELETE FROM pa_learning_candidates;
              DELETE FROM pa_journal_revisions;
              DELETE FROM pa_journal_entries;
            """)
            self._create_immutability_triggers()
