"""Observation-only durable lineage. This module has no order submission API.

Accounting commits remain authoritative. Exit preparation becomes a fill only
when its exact execution ID is present in primary committed receipts.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import uuid
from decimal import Decimal
from types import SimpleNamespace

from services.strategy_evidence import StrategyEvidence, evidence_json

log = logging.getLogger(__name__)
_SECRET_KEYS = {"secret", "password", "token", "access_token", "refresh_token",
                "api_key", "apikey", "authorization", "private_key", "webhook_secret"}


def _frozen(value):
    """Retain causal evidence without copying webhook credentials to the journal."""
    if isinstance(value, dict):
        return {key: _frozen(item) for key, item in value.items()
                if str(key).lower() not in _SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [_frozen(item) for item in value]
    return copy.deepcopy(value)


class StrategyEvidenceCapture:
    def __init__(self, journal, *, decisions=None, trade_memory=None):
        self.journal = journal
        self.store = journal.store
        self.decisions = decisions
        self.trade_memory = trade_memory
        self.episodes = StrategyEvidence(self.store)
        self._exit_context = {}
        self.evidence_changed_listener = None

    @staticmethod
    def _scope(context):
        provenance = context.get("journal_execution") or context
        return {**{key: provenance.get(key) for key in (
            "strategy_id", "strategy_version", "strategy_config_hash", "instance_id",
            "simulation_session_id", "execution_mode", "owner_id", "account_id",
            "lab_id", "source_kind")},
            "symbol": context.get("symbol"),
            "signal_id": context.get("decision_identity"),
            "decision_id": context.get("journal_decision_id"),
            "order_id": context.get("alert_id")}

    @classmethod
    def _key(cls, kind, identity, context):
        scope = cls._scope(context)
        tenant = [scope.get(key) for key in
                  ("owner_id", "account_id", "instance_id", "simulation_session_id")]
        return kind + ":" + hashlib.sha256(evidence_json(tenant).encode()).hexdigest() + ":" + str(identity)

    def event(self, event_id, kind, context, **extra):
        self.store.record_evidence_event(event_id, kind=kind, payload=_frozen(context),
                                         **{**self._scope(context), **extra})

    def observe_signal(self, signal, identity, context):
        frozen = _frozen(context)
        frozen["strategy_identity"] = copy.deepcopy(identity)
        if identity.get("strategy_config_hash"):
            self.store.save_strategy_identity(identity)
        self.event(self._key("signal", frozen["decision_identity"], frozen), "SIGNAL", frozen,
                   observed_at=frozen.get("timestamp"))

    def observe_decision(self, decision, context):
        frozen = _frozen(context)
        frozen["decision"] = copy.deepcopy(decision)
        self.event(self._key("decision", frozen["decision_identity"], frozen), "DECISION", frozen,
                   observed_at=frozen.get("decision_observed_at"))

    def order_context(self, payload, steps, equity):
        """Freeze producer facts independently of evidence-store availability."""
        context = _frozen(payload)
        self._validate_decision_reference(context)
        context["capture_steps"] = [
            {"rule": step.rule, "passed": step.passed, "detail": step.detail} for step in steps]
        context["capture_equity"] = equity
        # The execution key is reused after a rejected/rolled-back attempt.
        # A distinct frozen attempt captures its actual sizing/gate context;
        # exact persistence retries retain this deterministic attempt ID.
        attempt = self._key("order", context["alert_id"], context) + ":" + hashlib.sha256(
            evidence_json(context).encode()).hexdigest()
        context["evidence_order_attempt_id"] = attempt
        return context

    def persist_order_context(self, context):
        identity = context.get("strategy_identity")
        if identity and identity.get("strategy_config_hash"):
            self.store.save_strategy_identity(identity)
        attempt = context["evidence_order_attempt_id"]
        self.event(attempt, "ORDER_INTENT", context, observed_at=context.get("order_observed_at"))
        return context

    def prepare_order(self, payload, steps, equity):
        return self.persist_order_context(self.order_context(payload, steps, equity))

    def _validate_decision_reference(self, context):
        """Validate cross-store causal IDs before crediting an execution."""
        reference = context.get("journal_decision_id")
        context["decision_reference_status"] = "UNVERIFIED"
        if reference is None or self.decisions is None:
            return
        try:
            decision = self.decisions.get(reference)
        except Exception:
            context["decision_reference_status"] = "UNAVAILABLE"
            return
        expected = self._scope(context)
        expected["decision_identity"] = context.get("decision_identity")
        expected.pop("signal_id", None)
        expected.pop("decision_id", None)
        expected.pop("order_id", None)
        sides = {"buy": "long", "long": "long", "sell": "short", "short": "short"}
        actual_side = sides.get(str(context.get("side", "")).lower())
        decision_side = sides.get(str((decision or {}).get("side", "")).lower())
        conflicts = decision is None or decision.get("decision") != "accepted" or (
            actual_side is not None and decision_side is not None and actual_side != decision_side) or any(
            value is not None and decision.get(key) not in (None, "") and decision[key] != value
            for key, value in expected.items())
        if conflicts:
            context["claimed_decision_reference"] = reference
            context["journal_decision_id"] = None
            context["decision_reference_status"] = "CONFLICT" if decision else "UNRESOLVED"
        elif actual_side is not None and decision_side == actual_side and all(
                decision.get(key) not in (None, "") for key, value in expected.items() if value is not None):
            context["decision_reference_status"] = "VERIFIED"

    def observe_deferred_order(self, context, fill):
        if fill.execution_id != context["alert_id"]:
            self.event("deferred:" + context["evidence_order_attempt_id"], "PENDING_ORDER_ALREADY_EXISTS",
                       {**context, "existing_order_id": fill.execution_id})
            return
        self.event("deferred:" + context["evidence_order_attempt_id"], "DEFERRED_ORDER", context)

    def terminal(self, payload, state, reason):
        identity = (payload.get("evidence_order_attempt_id") or payload.get("alert_id")
                    or payload.get("decision_identity"))
        if not identity:
            return
        self.event(self._key("terminal", str(identity) + ":" + state, payload), state,
                   {**_frozen(payload), "terminal_reason": reason})

    def _entry_context(self, trade_id):
        event = self.store.get_evidence_event("entry:" + trade_id)
        return copy.deepcopy(event["payload"]) if event else None

    def _intent(self, order_id, **scope):
        events = self.store.evidence_events(kind="ORDER_INTENT", order_id=order_id, **scope)
        # A rejected/cancelled attempt may reuse its released execution key.
        # Without a captured committed fill, its older context is not proof
        # of the configuration or economics of a subsequent retry.
        terminals = [event for kind in ("REJECTED", "CANCELLED", "EXPIRED")
                     for event in self.store.evidence_events(kind=kind, order_id=order_id, **scope)]
        if any(not event["payload"].get("evidence_order_attempt_id") for event in terminals):
            return None
        rejected = {event["payload"]["evidence_order_attempt_id"] for event in terminals}
        eligible = [event for event in events if event["event_id"] not in rejected]
        return copy.deepcopy(eligible[0]["payload"]) if len(eligible) == 1 else None

    def _context(self, fill):
        receipt = getattr(fill, "receipt", {}) or {}
        context = (receipt.get("sizing_context") or {}).get("evidence_context")
        if context:
            return copy.deepcopy(context)
        if fill.action in ("opened", "recovered"):
            # Runtime opens must carry their original frozen intent. Explicit
            # restart reconciliation alone joins primary committed receipts.
            return None
        context = self._entry_context(fill.trade_id)
        if context:
            preparation = self.store.get_evidence_event(self._key("exit", fill.execution_id, context))
            if preparation:
                context["capture_exit"] = preparation["payload"].get("exit_context") or {}
        return context

    def prepare_close_context(self, trade_id, exit_context):
        # Metadata only, carried through the existing serialized pipeline call.
        self._exit_context[trade_id] = _frozen(exit_context)

    def cancel_pending(self, ledger, *, instance_id, simulation_session_id, reason):
        """Observe an already committed account cancellation; no state mutation."""
        scope = {"instance_id": instance_id, "simulation_session_id": simulation_session_id}
        committed = {row["execution_id"] for row in ledger.get_execution_receipts()
                     if row["action"] == "OPEN"}
        events = (self.store.evidence_events(kind="DEFERRED_ORDER", **scope)
                  + self.store.evidence_events(kind="PENDING_INTENT", **scope))
        seen = set()
        for event in events:
            context = event["payload"]
            if context.get("alert_id") in committed:
                continue
            decision_id = context.get("journal_decision_id")
            key = ("decision", decision_id) if decision_id is not None else (
                "order", context.get("alert_id") or context.get("decision_identity"))
            if key in seen:
                continue
            seen.add(key)
            if self.decisions is not None and decision_id is not None:
                decision = self.decisions.get(decision_id)
                if decision is not None and decision.get("executed"):
                    continue
            self.terminal(context, "CANCELLED", reason)

    def prepare_exit(self, action, execution_id, receipt):
        exit_context = self._exit_context.pop(receipt["trade_id"], {})
        context = None
        try:
            context = self._entry_context(receipt["trade_id"])
            if context:
                payload = {**context, "prepared_action": action, "prepared_receipt": _frozen(receipt),
                           "exit_context": exit_context}
                self.event(self._key("exit", execution_id, context), "EXIT_INTENT", payload,
                           trade_id=receipt["trade_id"], position_id=receipt["position_id"])
        except Exception:
            log.exception("strategy_evidence_exit_preparation_unavailable execution_id=%s", execution_id)
        if context:
            context = {**context, "capture_exit": exit_context}
        return {"context": context, "receipt": {**_frozen(receipt), "capture_exit": exit_context}}

    def observe_fill(self, fill):
        if self.evidence_changed_listener is not None:
            try:
                self.evidence_changed_listener()
            except Exception:
                log.exception("strategy_intelligence_cache_invalidation_failed")
        context = self._context(fill)
        if not context:
            log.warning("strategy_evidence_missing_context execution_id=%s trade_id=%s",
                        fill.execution_id, fill.trade_id)
            return
        previous = self.store.get_evidence_event(self._key("fill", fill.execution_id, context))
        if previous:
            original = previous["payload"].get("receipt") or {}
            # The public execution result retains producer arithmetic. An exact
            # repeat of that observation must reuse its immutable booked capture;
            # changed producer facts still reach the conflict checks below.
            incoming = {key: value for key, value in vars(fill).items() if key != "receipt"}
            if (original.get("producer_observation") == incoming
                    and original.get("producer_receipt") == (getattr(fill, "receipt", {}) or {})):
                fill = self._captured_fill(previous)
                context = self._context(fill) or context
        receipt = getattr(fill, "receipt", {}) or {}
        provenance = context.get("journal_execution") or {}
        envelope = {**context, **provenance,
                    "initial_risk_amount": receipt.get("initial_risk_amount"),
                    "risk_amount_at_entry": receipt.get("risk_amount_at_entry"),
                    "evidence_class": provenance.get("evidence_class", "UNKNOWN"),
                    "source_class": provenance.get("evidence_class", "UNKNOWN"),
                    "decision_id": context.get("journal_decision_id"),
                    "signal_id": context.get("decision_identity"), "order_id": context.get("alert_id")}
        self.episodes.observe_fill(fill, envelope)
        if fill.action in ("opened", "recovered"):
            self._record_entry(fill, context)
            # Retry a previously unavailable canonical decision read without
            # revising the immutable facts captured during that outage.
            credit = copy.deepcopy(context)
            self._validate_decision_reference(credit)
            if (self.decisions is not None and credit.get("journal_decision_id") is not None
                    and credit.get("decision_reference_status") != "UNAVAILABLE"):
                self.decisions.mark_executed(credit["journal_decision_id"])
        elif fill.action in ("closed", "reduced"):
            exit_context = context.get("capture_exit") or {}
            self.journal.record_exit(
                trade_id=fill.trade_id, exit_price=fill.price, pnl=fill.pnl,
                exit_reason=exit_context.get("exit_reason") or
                ("partial-exit" if fill.action == "reduced" else "executed-close"),
                mfe_r=exit_context.get("mfe_r"), mae_r=exit_context.get("mae_r"),
                instance_id=provenance.get("instance_id") or "",
                execution_receipt=receipt, exit_timestamp=fill.executed_at, event_id=fill.execution_id)
            if self.trade_memory is not None:
                try:
                    self.trade_memory.remember(fill.trade_id)
                except Exception:
                    log.exception("strategy_evidence_trade_memory_failed")
            if fill.action == "reduced" and fill.remainder_trade_id:
                child = SimpleNamespace(
                    trade_id=fill.remainder_trade_id, position_id=fill.remainder_position_id,
                    execution_id=fill.execution_id + ":remainder", action="opened",
                    symbol=fill.symbol, side=fill.side, size=receipt["remainder_size"],
                    price=receipt["entry"], receipt=receipt, executed_at=fill.executed_at)
                child_context = copy.deepcopy(context)
                child_context.pop("capture_exit", None)
                self._record_entry(child, child_context)

    def _record_entry(self, fill, context):
        receipt = getattr(fill, "receipt", {}) or {}
        payload = copy.deepcopy(context)
        payload.update(entry_timestamp=fill.executed_at, execution_id=fill.execution_id,
                       execution_receipt=receipt)
        payload["journal_sizing"] = {**(payload.get("journal_sizing") or {}),
                                     "filled_entry": fill.price, "filled_size": fill.size}
        self.event("entry:" + fill.trade_id, "TRADE_ENTRY_CONTEXT", context,
                   trade_id=fill.trade_id, position_id=fill.position_id)
        steps = [SimpleNamespace(**step) for step in context.get("capture_steps", [])]
        self.journal.record_entry(
            trade_id=fill.trade_id, position_id=fill.position_id, mode=context.get("mode", "paper"),
            symbol=fill.symbol, side=fill.side, strategy=context.get("strategy", "Unknown"),
            timeframe=context.get("timeframe", ""), entry=fill.price,
            stop=receipt.get("stop", context.get("stop")), target=receipt.get("target", context.get("target")),
            size=fill.size, equity=context.get("capture_equity", 0),
            confidence=context.get("confidence", 1), brain_score=context.get("brain_score"),
            regime=context.get("regime", ""), steps=steps, payload=payload)

    @staticmethod
    def _captured_fill(event):
        facts = event["payload"]
        return SimpleNamespace(
            action=facts["action"], symbol=event["symbol"], side=facts["side"],
            price=float(facts["price"]), size=float(facts["size"]), trade_id=event["trade_id"],
            position_id=event["position_id"], execution_id=facts["execution_id"],
            parent_trade_id=facts.get("parent_trade_id"),
            remainder_trade_id=facts.get("remainder_trade_id"),
            remainder_position_id=facts.get("remainder_position_id"),
            executed_at=event["observed_at"],
            pnl=float(facts["net_pnl"]) if facts.get("net_pnl") is not None else 0,
            fee=float(facts["fees"]) if facts.get("fees") is not None else None,
            receipt=copy.deepcopy(facts.get("receipt") or {}))

    def reconcile(self, ledger):
        """At-least-once metadata replay; accounting is never replayed."""
        reader = getattr(ledger, "get_authoritative_evidence_snapshot", None)
        if callable(reader):
            snapshot = reader()
            count, errors = self._replay_outbox(snapshot)
            if errors:
                log.warning("strategy_evidence_recovery_incomplete errors=%s", errors)
            return count + self._legacy_reconcile(ledger)
        return self._legacy_reconcile(ledger)

    def _restore_context(self, context, row):
        context = _frozen(context)
        identity = context.get("strategy_identity") or context.get("recovery_strategy_identity")
        if identity and identity.get("strategy_config_hash"):
            self.store.save_strategy_identity(identity)
            context["strategy_identity"] = identity
            context["journal_execution"] = {
                **(context.get("journal_execution") or {}),
                **{key: identity.get(key) for key in (
                    "strategy_id", "strategy_version", "strategy_config_hash", "source_hash", "identity_status")},
            }
        self._validate_decision_reference(context)
        return context

    @staticmethod
    def _unknown_execution_context(row):
        receipt = row["receipt"]
        return {
            "symbol": receipt["symbol"], "side": receipt["side"],
            "entry": receipt["entry"], "stop": receipt.get("stop"), "target": receipt.get("target"),
            "strategy": "Unknown historical strategy", "recovered_execution_only": True,
            "reason": "Recovered committed execution; original decision context unavailable.",
            "journal_execution": {"instance_id": row.get("instance_id") or None,
                "simulation_session_id": row.get("simulation_session_id") or None,
                "identity_status": "unavailable"},
        }

    def _replay_outbox(self, snapshot):
        trades = {row["id"]: row for row in snapshot.get("trades", [])}
        executions = {row["execution_id"]: row for row in snapshot.get("executions", [])}
        outbox = snapshot.get("outbox", [])
        roots = {row["trade_id"]: row for row in outbox if row["action"] == "OPEN"}
        parents = {row["remainder_trade_id"]: row["trade_id"]
                   for row in outbox if row.get("remainder_trade_id")}
        contexts = {}
        errors, count = [], 0
        for row in outbox:
            try:
                execution = executions.get(row["execution_id"])
                expected_ids = ((row.get("remainder_trade_id"), row.get("remainder_position_id"))
                    if row["action"] == "REDUCE" else (row["trade_id"], row["position_id"]))
                if (execution is None or execution["action"] != row["action"] or
                        (execution["trade_id"], execution["position_id"]) != expected_ids):
                    raise ValueError("outbox conflicts with committed execution IDs/action")
                receipt = copy.deepcopy(row["receipt"])
                trade = trades.get(row["trade_id"])
                if trade is None:
                    raise ValueError("outbox references missing authoritative trade")
                if any((row.get(key) or "") != (trade.get(key) or "")
                       for key in ("instance_id", "simulation_session_id")):
                    raise ValueError("outbox conflicts with authoritative account/session scope")
                if row["action"] == "OPEN":
                    position = next((item for item in snapshot.get("positions", [])
                                     if item["id"] == row["position_id"]), None)
                    if (position is None or Decimal(str(receipt["price"])) != Decimal(str(trade["entry"]))
                            or Decimal(str(receipt["size"])) != Decimal(str(position["size"]))):
                        raise ValueError("outbox conflicts with committed entry economics")
                if row["action"] != "OPEN":
                    if trade.get("status") != "closed" or any(
                            Decimal(str(receipt[left])) != Decimal(str(trade[right])) for left, right in
                            (("price", "exit"), ("size", "size"), ("net_pnl", "pnl"), ("booked_fees", "fees"))):
                        raise ValueError("outbox conflicts with committed exit economics")
                captured = self.store.execution_event(row["execution_id"],
                    instance_id=row.get("instance_id") or None,
                    simulation_session_id=row.get("simulation_session_id") or None)
                if captured:
                    previous = self.store.get(captured["trade_id"])
                    self.observe_fill(self._captured_fill(captured))
                    if (previous is None or (captured["payload"].get("action") in ("closed", "reduced")
                                              and previous.get("status") != "closed")):
                        count += 1
                    continue
                context = row.get("context")
                if not context:
                    context = contexts.get(row["trade_id"]) or self._entry_context(row["trade_id"])
                if not context:
                    root_id, visited = row["trade_id"], set()
                    while root_id in parents:
                        if root_id in visited:
                            raise ValueError("cyclic authoritative episode parent references")
                        visited.add(root_id)
                        root_id = parents[root_id]
                    root = roots.get(root_id)
                    context = root.get("context") if root else None
                context = self._restore_context(context or self._unknown_execution_context(row), row)
                context.pop("capture_exit", None)
                if receipt.get("capture_exit"):
                    context["capture_exit"] = receipt["capture_exit"]
                contexts[row["trade_id"]] = context
                if row.get("remainder_trade_id"):
                    contexts[row["remainder_trade_id"]] = context
                receipt.update(trade_id=row["trade_id"], position_id=row["position_id"])
                if row["action"] != "OPEN":
                    receipt.update(exit_price=receipt["price"], closed_size=receipt["size"])
                receipt["sizing_context"] = {**(receipt.get("sizing_context") or {}), "evidence_context": context}
                fill = SimpleNamespace(
                    action={"OPEN": "recovered", "REDUCE": "reduced", "CLOSE": "closed"}[row["action"]],
                    symbol=receipt["symbol"], side=receipt["side"], price=receipt["price"], size=receipt["size"],
                    trade_id=row["trade_id"], position_id=row["position_id"], execution_id=row["execution_id"],
                    parent_trade_id=row.get("parent_trade_id"), remainder_trade_id=row.get("remainder_trade_id"),
                    remainder_position_id=row.get("remainder_position_id"),
                    executed_at=row.get("observed_at") or row["created_at"],
                    pnl=receipt.get("net_pnl") or 0, fee=receipt.get("booked_fees"), receipt=receipt)
                self.observe_fill(fill)
                count += 1
            except Exception as exc:
                errors.append({"execution_id": row.get("execution_id"), "error": type(exc).__name__, "reason": str(exc)})
                log.exception("strategy_evidence_outbox_replay_failed execution_id=%s", row.get("execution_id"))
        return count, errors

    def reconcile_report(self, ledger, *, cohort=None):
        """Reconcile metadata and report remaining gaps, without order authority."""
        from services.strategy_evidence_completeness import assess_evidence_completeness
        scope = {key: getattr(ledger, key) for key in ("instance_id", "simulation_session_id")
                 if hasattr(ledger, key)}
        exact_cohort = cohort.as_dict() if hasattr(cohort, "as_dict") else cohort
        if exact_cohort is not None:
            scope.update({key: exact_cohort[key] for key in (
                "instance_id", "simulation_session_id", "owner_id", "account_id")})
        last = None
        try:
            last = self.store.last_successful_reconciliation(scope=scope,
                cohort=exact_cohort)
        except Exception:
            pass
        errors, recovered = [], 0
        try:
            before = ledger.get_authoritative_evidence_snapshot()
            recovered, errors = self._replay_outbox(before)
            self._legacy_reconcile(ledger)
            snapshot = ledger.get_authoritative_evidence_snapshot()
        except Exception as exc:
            snapshot = {"source_complete": False, "read_error": type(exc).__name__}
            errors.append({"error": type(exc).__name__, "reason": str(exc)})
        report = assess_evidence_completeness(snapshot, self.store, scope=scope, cohort=cohort,
            last_successful_reconciliation=last["calculated_at"] if last else None)
        if snapshot.get("source_complete"):
            try:
                latest = ledger.get_authoritative_evidence_snapshot()
                if latest.get("source_watermark") != snapshot.get("source_watermark"):
                    errors.append({"error": "SourceChangedDuringReconciliation",
                                   "reason": "Authoritative source changed during evidence assessment"})
            except Exception as exc:
                errors.append({"error": type(exc).__name__, "reason": "Authoritative source recheck unavailable"})
            if errors and report["status"] != "CONFLICTED":
                report["status"] = report["reconciliation_status"] = "UNKNOWN"
                report["history_complete"] = False
        report.update(run_id=uuid.uuid4().hex, recovered_events=recovered, recovery_errors=errors)
        if errors and any(item["error"] in ("ValueError", "IntegrityError") for item in errors):
            report["status"] = report["reconciliation_status"] = "CONFLICTED"
            report["history_complete"] = False
        try:
            self.store.record_reconciliation_run(report)
        except Exception:
            log.exception("strategy_evidence_reconciliation_report_unavailable")
            report["status"] = report["reconciliation_status"] = "UNKNOWN"
            report["history_complete"] = False
        return report

    def _legacy_reconcile(self, ledger):
        """Repair journal evidence from exact primary receipts; never write accounting.

        Unknown historical lineage and missing preparations are left incomplete.
        A REDUCE receipt references the child, so its persisted preparation is
        required to prove the parent link. Symbol coincidence is never a join.
        """
        reader = getattr(ledger, "get_execution_receipts", None)
        if not callable(reader):
            return 0
        scope = {key: getattr(ledger, key) for key in ("instance_id", "simulation_session_id")
                 if hasattr(ledger, key)}
        trades = {row["id"]: row for row in ledger.get_paper_trades()}
        positions = {row["id"]: row for row in ledger.get_positions()}
        count = 0
        for committed in reader():
            execution_id = committed["execution_id"]
            captured = self.store.execution_event(execution_id, **scope)
            if captured:
                fill = self._captured_fill(captured)
            elif committed["action"] == "OPEN":
                context = self._intent(execution_id, **scope)
                row, position = trades.get(committed["trade_id"]), positions.get(committed["position_id"])
                if not context or row is None or position is None:
                    continue
                fill = SimpleNamespace(
                    action="recovered", symbol=row["symbol"], side=row["side"],
                    price=row["entry"], size=position["size"], trade_id=row["id"],
                    position_id=position["id"], execution_id=execution_id,
                    executed_at=committed["created_at"], pnl=0, fee=None,
                    receipt={"entry": row["entry"], "stop": context.get("stop"),
                             "target": context.get("target"),
                             "initial_risk_amount": row.get("risk_amount_at_entry"),
                             "sizing_context": {"evidence_context": context},
                             "recovery_coverage": "primary_committed_open"})
            else:
                preparations = [event for event in self.store.evidence_events(kind="EXIT_INTENT", **scope)
                                if event["event_id"].endswith(":" + execution_id)]
                if len(preparations) != 1:
                    continue
                preparation = preparations[0]["payload"]
                receipt = copy.deepcopy(preparation["prepared_receipt"])
                row = trades.get(receipt["trade_id"])
                expected = "REDUCE" if preparation["prepared_action"] == "reduced" else "CLOSE"
                if (committed["action"] != expected or row is None or row.get("status") != "closed"
                        or (expected == "CLOSE" and committed["trade_id"] != row["id"])):
                    continue
                # Compare preparation with authoritative stored exit economics.
                if any(Decimal(str(receipt[left])) != Decimal(str(row[right])) for left, right in
                       (("exit_price", "exit"), ("net_pnl", "pnl"), ("booked_fees", "fees"), ("closed_size", "size"))):
                    log.warning("strategy_evidence_recovery_receipt_conflict execution_id=%s", execution_id)
                    continue
                child = trades.get(committed["trade_id"]) if expected == "REDUCE" else None
                child_position = positions.get(committed["position_id"]) if child else None
                if expected == "REDUCE" and (child is None or child_position is None):
                    continue
                fill = SimpleNamespace(
                    action=preparation["prepared_action"], symbol=row["symbol"], side=row["side"],
                    price=row["exit"], size=row["size"], trade_id=row["id"],
                    position_id=receipt["position_id"], execution_id=execution_id,
                    parent_trade_id=row["id"] if child else None,
                    remainder_trade_id=child["id"] if child else None,
                    remainder_position_id=child_position["id"] if child else None,
                    executed_at=row.get("closed_at"), pnl=row["pnl"], fee=row.get("fees"), receipt=receipt)
            previous = self.store.get(fill.trade_id)
            self.observe_fill(fill)
            if not captured or previous is None:
                count += 1
        return count
