"""The SMC agent: it trades the existing strategy, it does not change it.

The SMC Lab strategy decides whether there is a trade. This module decides
whether the AGENT takes the trade the strategy offered, sizes it, and writes
down everything about that decision. The split matters and is enforced here:

  * **Signals come only from the strategy.** The agent acts on an evaluation
    that reached ENTRY_READY with a proposal, and on nothing else. It has no
    entry logic of its own, so there is no path by which it invents a trade the
    strategy did not offer.

  * **The agent's gates can only REFUSE.** Every check below is a veto. None of
    them can turn a WATCHING evaluation into a trade, relax an SMC condition,
    or widen what qualifies. The worst an agent gate can do is decline a trade
    the strategy was willing to take, which is a risk decision and belongs to
    the agent.

  * **Everything it sees is written down**, including the times it did nothing.
    A journal of taken trades cannot answer "what did I skip, and why?" or
    "what did I miss?", and those are the questions that change how a trader
    behaves.

The two agent-level risk rules are the operator's, not the strategy's:
a 1:3 minimum reward-to-risk, and a position size for BTC between 0.01 and
0.9. Neither is an entry condition; both are limits on what the agent is
willing to put at risk on a signal the strategy already validated.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from services.smc_agent_journal import (DECISION_APPROVED, EXECUTED,
                                        EXECUTION_COMPLETE, EXECUTION_FAILED,
                                        EXECUTION_PENDING, EXECUTION_UNCERTAIN,
                                        MISSED, NOT_READY, RECONCILED, REJECTED,
                                        TAKEN, SMCAgentJournal)

#: The operator's floor. A plan that cannot offer three times its risk is not
#: taken, however clean the setup looked.
MIN_REWARD_TO_RISK = 3.0
#: BTC position bounds, in BTC.
BTC_MIN_SIZE = 0.01
BTC_MAX_SIZE = 0.9
#: The state the strategy uses to say "there is a trade here".
ENTRY_READY = "ENTRY_READY"
#: Only this strategy's evaluations are actionable. An evaluation from
#: anywhere else is not an SMC signal and the agent will not trade it.
SOURCE_STRATEGY_ID = "SMC_SOURCE_V1"

_DECISION_FILES = ("services/native_smc.py", "services/smc_strategy_ladder.py",
                   "services/smc_strategy_v1.py")
_ROOT = Path(__file__).resolve().parents[1]
_FINGERPRINT: Optional[str] = None


def strategy_fingerprint() -> str:
    """A short digest of the decision path, recorded on every journal row.

    So a review months later can tell whether two trades were decided by the
    same strategy build. Computed once per process: it is provenance, not a
    guard — services.smc_strategy_freeze is the guard.
    """
    global _FINGERPRINT
    if _FINGERPRINT is None:
        h = hashlib.sha256()
        for rel in _DECISION_FILES:
            path = _ROOT / rel
            h.update(path.read_bytes() if path.exists() else b"MISSING")
        _FINGERPRINT = h.hexdigest()[:16]
    return _FINGERPRINT


@dataclass(frozen=True)
class Sizing:
    """What risk asked for, what the bound allowed, and whether it moved."""
    requested: float
    executed: float
    capped: bool


@dataclass(frozen=True)
class SizingInputs:
    """The live numbers a size is computed from, supplied by the caller.

    Passed by the runtime on every live observation so the agent sizes from
    the account that will actually carry the trade and the risk percentage
    the operator saved, rather than from a number captured when the agent was
    constructed. ``quantity_step`` is the venue's step: rounding to it here is
    what makes the size the agent journals a size that can really be placed.
    """
    equity: float
    risk_percent: float
    quantity_step: float = 0.0


def _rounded_down(value: float, step: float) -> float:
    """The largest multiple of ``step`` at or below ``value``."""
    return value if step <= 0 else math.floor(value / step + 1e-12) * step


#: Returned when the agent has already decided on this candle or proposal and
#: is deliberately doing nothing. Distinct from every real outcome so a caller
#: cannot mistake "already handled" for "declined".
ALREADY_DECIDED = "ALREADY_DECIDED"


@dataclass(frozen=True)
class Gate:
    """One agent-side veto, with the number it was judged on."""
    name: str
    passed: bool
    detail: str
    value: Optional[float] = None
    limit: Optional[float] = None


class _ExecutionDeclined(Exception):
    """The execution layer ran and chose not to place an order."""


class ExecutionFailed(Exception):
    """The caller proved that broker submission did not create an order."""


def _order_id_of(placed: object) -> str:
    """The broker's id for what was just placed, whatever shape it came in."""
    if isinstance(placed, dict):
        order = placed.get("order")
        if isinstance(order, dict) and order.get("id"):
            return str(order["id"])
        if placed.get("id"):
            return str(placed["id"])
    return str(getattr(placed, "id", "") or "")


def size_bounds_for(symbol: str) -> tuple[float, float]:
    """Position bounds per symbol. Only BTC has them defined."""
    return (BTC_MIN_SIZE, BTC_MAX_SIZE) if symbol.upper().startswith("BTC") else (0.0, float("inf"))


def reward_to_risk(entry: float, stop: float, target: float) -> Optional[float]:
    """Reward over risk from the plan's own prices.

    Computed from prices rather than read from the plan's ``target_2_r``: the
    agent's floor has to be judged on what the trade actually offers, and a
    disagreement between the two is something the journal should be able to
    show.
    """
    risk = abs(float(entry) - float(stop))
    if risk <= 0:
        return None
    return abs(float(target) - float(entry)) / risk


class SMCAgent:
    """Observes SMC evaluations, decides, sizes, and journals."""

    def __init__(self, journal: SMCAgentJournal, *,
                 min_reward_to_risk: float = MIN_REWARD_TO_RISK,
                 equity: float = 10_000.0,
                 clock: Optional[Callable[[], datetime]] = None):
        self.journal = journal
        self.min_reward_to_risk = float(min_reward_to_risk)
        self.equity = float(equity)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------- helpers
    def _now(self) -> str:
        return self._clock().isoformat()

    @staticmethod
    def _identity(evaluation: dict, candle_time: str = "") -> tuple[str, str, str]:
        """Which market, and which closed candle this observation belongs to.

        The candle is resolved the way the lab resolves it, and for the same
        reason: it is the dedupe key. The live path evaluates without naming a
        candle, so ``selected_candle`` is empty there and an unqualified
        reading would leave every poll looking like a new observation. The
        caller's ``candle_time`` — the closed candle the lab itself journalled
        — wins, then the evaluation's own selection, then the timestamp of the
        bar the proposal was signalled on.
        """
        identity = evaluation.get("data_identity") or {}
        proposal = evaluation.get("proposal") or {}
        candle = (candle_time or identity.get("selected_candle")
                  or proposal.get("signal_timestamp") or "")
        return (identity.get("symbol") or "", identity.get("timeframe") or "",
                str(candle) if candle else "")

    def _record(self, evaluation: dict, *, outcome: str, reason_code: str,
                reason: str, gates: list[Gate] | None = None,
                plan: dict | None = None, market: dict | None = None,
                trade_id: str = "", candle_time: str = "") -> str:
        symbol, timeframe, candle = self._identity(evaluation, candle_time)
        return self.journal.record_decision(
            symbol=symbol, timeframe=timeframe,
            smc_state=str(evaluation.get("state") or ""),
            outcome=outcome, reason_code=reason_code, reason=reason,
            candle_time=candle,
            setup_id=str(evaluation.get("setup_id") or ""),
            proposal_id=str(evaluation.get("proposal_id") or ""),
            conditions=evaluation.get("ordered_condition_results"),
            missing=evaluation.get("missing_conditions"),
            plan=plan if plan is not None else evaluation.get("trade_plan"),
            gates=[asdict(g) for g in (gates or [])],
            market=market, strategy_fingerprint=strategy_fingerprint(),
            trade_id=trade_id, at=self._now())

    @classmethod
    def execution_key_for(cls, evaluation: dict, candle_time: str = "") -> str:
        """Return the stable idempotency key for one approved decision."""
        symbol, timeframe, candle = cls._identity(evaluation, candle_time)
        proposal_id = str(evaluation.get("proposal_id") or
                          (evaluation.get("proposal") or {}).get("id") or "")
        material = "|".join(("SMC_AGENT", symbol, timeframe, candle, proposal_id))
        return "smc-agent:" + hashlib.sha256(material.encode()).hexdigest()[:32]

    @staticmethod
    def _gate_objects(raw: list[dict]) -> list[Gate]:
        return [Gate(name=str(row.get("name") or ""),
                     passed=bool(row.get("passed")),
                     detail=str(row.get("detail") or ""),
                     value=row.get("value"), limit=row.get("limit"))
                for row in raw or []]

    def _intent_payload(self, evaluation: dict, *, plan: dict,
                        market: dict | None, gates: list[Gate],
                        sizing: Sizing, why: str) -> dict:
        """Capture enough immutable context to finish after a restart."""
        return {
            "evaluation": evaluation,
            "plan": plan,
            "market": market,
            "gates": [asdict(gate) for gate in gates],
            "sizing": asdict(sizing),
            "why": why,
        }

    def _finalize_intent(self, intent: dict, *, order_id: str) -> dict:
        """Write the decision/trade and close the intent in one journal tx."""
        payload = intent.get("payload") or {}
        evaluation = payload.get("evaluation") or {}
        plan = payload.get("plan") or evaluation.get("trade_plan") or {}
        sizing = Sizing(**(payload.get("sizing") or {}))
        gates = self._gate_objects(payload.get("gates") or [])
        symbol, timeframe, candle = self._identity(
            evaluation, str(intent.get("candle_time") or ""))
        rr = reward_to_risk(float(plan["entry"]), float(plan["stop"]),
                            float(plan["target_2"]))
        with self.journal.transaction():
            decision_id = self._record(
                evaluation, outcome=TAKEN, reason_code="ALL_GATES_PASSED",
                reason=str(payload.get("why") or "all agent gates passed"),
                gates=gates, plan=plan, market=payload.get("market"),
                candle_time=candle)
            trade_id = self.journal.open_trade(
                decision_id=decision_id, symbol=symbol, timeframe=timeframe,
                direction=str((evaluation.get("proposal") or {}).get("direction") or ""),
                entry=float(plan["entry"]), stop=float(plan["stop"]),
                target=float(plan["target_2"]), planned_rr=float(rr or 0.0),
                size=float(sizing.executed), requested_size=float(sizing.requested),
                size_capped=bool(sizing.capped),
                risk_amount=abs(float(plan["entry"]) - float(plan["stop"])) *
                             float(sizing.executed),
                setup_id=str(evaluation.get("setup_id") or ""),
                proposal_id=str(evaluation.get("proposal_id") or ""),
                conditions=evaluation.get("ordered_condition_results"),
                market=payload.get("market"), why=str(payload.get("why") or ""),
                order_id=order_id, strategy_fingerprint=strategy_fingerprint(),
                opened_at=self._now())
            self.journal.transition_execution(
                str(intent["execution_key"]), EXECUTION_COMPLETE,
                decision_id=decision_id, broker_order_id=order_id,
                trade_id=trade_id)
        return {"outcome": TAKEN, "decision_id": decision_id,
                "trade_id": trade_id, "order_id": order_id,
                "size": sizing.executed, "requested_size": sizing.requested,
                "size_capped": sizing.capped,
                "execution_key": intent["execution_key"],
                "execution_state": EXECUTION_COMPLETE}

    def reconcile_execution_intents(self, lookup: Callable[[str], object]) -> list[dict]:
        """Recover durable intents before another tick can submit work.

        ``lookup`` must return broker evidence for a key, or ``None`` only
        when the broker can prove that no order exists.  An uncertain intent is
        never retried blindly.
        """
        recovered = []
        states = (DECISION_APPROVED, EXECUTION_PENDING, EXECUTED,
                  EXECUTION_UNCERTAIN, RECONCILED)
        for intent in self.journal.execution_intents(states=states):
            try:
                evidence = lookup(str(intent["execution_key"]))
            except Exception as exc:  # an unavailable broker is uncertainty
                self.journal.transition_execution(
                    intent["execution_key"], EXECUTION_UNCERTAIN,
                    error=f"reconciliation unavailable: {type(exc).__name__}: {exc}")
                recovered.append({"execution_key": intent["execution_key"],
                                  "state": EXECUTION_UNCERTAIN, "error": str(exc)})
                continue
            order_id = ""
            if isinstance(evidence, dict):
                order = evidence.get("order")
                order_id = str((order or {}).get("id") or evidence.get("id") or "")
            if not order_id:
                self.journal.transition_execution(
                    intent["execution_key"], EXECUTION_FAILED,
                    error="broker reconciliation proved no order exists")
                recovered.append({"execution_key": intent["execution_key"],
                                  "state": EXECUTION_FAILED})
                continue
            self.journal.transition_execution(
                intent["execution_key"], EXECUTED, broker_order_id=order_id)
            try:
                result = self._finalize_intent(
                    self.journal.execution_intent(intent["execution_key"]),
                    order_id=order_id)
            except Exception as exc:  # preserve the broker truth
                self.journal.transition_execution(
                    intent["execution_key"], EXECUTION_UNCERTAIN,
                    broker_order_id=order_id,
                    error=f"journal finalization pending: {type(exc).__name__}: {exc}")
                recovered.append({"execution_key": intent["execution_key"],
                                  "state": EXECUTION_UNCERTAIN,
                                  "order_id": order_id, "error": str(exc)})
            else:
                recovered.append({"execution_key": intent["execution_key"],
                                  "state": EXECUTION_COMPLETE,
                                  "order_id": order_id, **result})
        return recovered

    # --------------------------------------------------------------- gates
    def evaluate_gates(self, plan: dict, symbol: str,
                       inputs: Optional[SizingInputs] = None) -> tuple[list[Gate], Sizing]:
        """Every agent veto, and the sizing that survived them.

        Pure arithmetic over ``plan`` and ``inputs``, so a caller that needs to
        know the size before it commits to placing it gets the same answer
        this method will reach during ``observe``. Size is 0.0 when no allowed
        size exists, which a gate will already have recorded as a failure.
        """
        entry = float(plan["entry"])
        stop = float(plan["stop"])
        # The runner is the trade's objective. Judging the floor on the
        # scale-out would pass plans whose real target is nearer than 3R.
        target = float(plan["target_2"])
        rr = reward_to_risk(entry, stop, target)
        gates = [Gate(
            name="minimum_reward_to_risk",
            passed=rr is not None and rr >= self.min_reward_to_risk,
            detail=(f"plan offers {rr:.2f}R against a {self.min_reward_to_risk:.1f}R floor"
                    if rr is not None else "stop is at the entry — no risk to measure"),
            value=rr, limit=self.min_reward_to_risk)]

        low, high = size_bounds_for(symbol)
        risk_per_unit = abs(entry - stop)
        equity = self.equity if inputs is None else float(inputs.equity)
        risk_percent = (float(plan.get("risk_percent") or 0.0) if inputs is None
                        else float(inputs.risk_percent))
        step = 0.0 if inputs is None else float(inputs.quantity_step)
        raw = (_rounded_down((equity * risk_percent / 100.0) / risk_per_unit, step)
               if risk_per_unit > 0 and risk_percent > 0 and equity > 0 else 0.0)
        # Capping DOWN reduces risk and is what a position limit is for, so it
        # is allowed and recorded. Below the floor there is no size that can
        # express the trade without risking more than planned, so it is a veto.
        size = _rounded_down(min(raw, high), step)
        gates.append(Gate(
            name="position_size_within_bounds",
            passed=size >= low,
            detail=(f"risk-based size {raw:.4f} -> {size:.4f} within "
                    f"{low}-{high} {symbol.upper()[:3]}" if size >= low else
                    f"risk-based size {raw:.4f} is below the {low} minimum"),
            value=size, limit=low))
        capped = raw > high
        if capped:
            gates.append(Gate(
                name="position_size_capped",
                passed=True,
                detail=f"size capped from {raw:.4f} to the {high} maximum — "
                       "this trade risks less than planned",
                value=size, limit=high))
        allowed = size if size >= low else 0.0
        return gates, Sizing(requested=raw, executed=allowed, capped=capped)

    # ------------------------------------------------------------- observe
    def observe(self, evaluation: dict, *, market: Optional[dict] = None,
                can_trade: bool = True, blocked_reason: str = "",
                candle_time: str = "",
                sizing_inputs: Optional[SizingInputs] = None,
                executor: Optional[Callable[[Sizing], object]] = None) -> dict:
        """Decide once and execute through a durable intent state machine.

        The journal transaction cannot include the paper broker's separate
        SQLite connection.  Therefore an intent is committed first, the
        broker receives the same stable key, and finalization is allowed to
        remain ``EXECUTED``/``EXECUTION_UNCERTAIN`` until reconciliation proves
        that the journal and broker agree.  No post-execution journal error is
        ever relabelled as a missed trade.
        """
        state = str(evaluation.get("state") or "")
        source = str(evaluation.get("strategy_id") or "")
        symbol, timeframe, candle = self._identity(evaluation, candle_time)
        proposal_id = str(evaluation.get("proposal_id") or "")

        # The runtime polls far more often than candles close, and a restart or
        # a replay re-presents work already done. Deciding again would journal
        # the same observation repeatedly and, worse, act on one signal twice.
        seen = self.journal.decision_for_candle(
            symbol=symbol, timeframe=timeframe, candle_time=candle)
        if seen is None and proposal_id:
            seen = self.journal.decision_for_proposal(proposal_id)
        if seen is not None:
            return {"outcome": ALREADY_DECIDED, "decision_id": seen["id"],
                    "trade_id": seen.get("trade_id") or "", "gates": [],
                    "previous_outcome": seen["outcome"]}

        if source != SOURCE_STRATEGY_ID:
            decision_id = self._record(
                evaluation, outcome=NOT_READY, reason_code="NOT_AN_SMC_SIGNAL",
                reason=(f"evaluation came from {source or 'an unnamed strategy'}, "
                        f"not {SOURCE_STRATEGY_ID}; the agent trades SMC signals only"),
                market=market, candle_time=candle)
            return {"outcome": NOT_READY, "decision_id": decision_id,
                    "trade_id": "", "gates": []}

        plan = evaluation.get("trade_plan") or {}
        if state != ENTRY_READY or not plan:
            missing = evaluation.get("missing_conditions") or []
            reason = (f"SMC is {state or 'not ready'}"
                      + (f"; still missing: {', '.join(map(str, missing))}" if missing
                         else "; no trade was offered"))
            decision_id = self._record(
                evaluation, outcome=NOT_READY, reason_code="SMC_NOT_READY",
                reason=reason, market=market, candle_time=candle)
            return {"outcome": NOT_READY, "decision_id": decision_id,
                    "trade_id": "", "gates": []}

        # Resolve an already-created intent before feed/candidate gates. A
        # restart or persistent journal outage must surface the existing
        # uncertainty, never turn it into a fresh MISSED observation.
        execution_key = self.execution_key_for(evaluation, candle)
        existing_intent = self.journal.execution_intent(execution_key)
        if existing_intent is not None:
            state_now = str(existing_intent.get("state") or "")
            if state_now == EXECUTION_COMPLETE:
                return {"outcome": ALREADY_DECIDED,
                        "decision_id": existing_intent.get("decision_id") or "",
                        "trade_id": existing_intent.get("trade_id") or "",
                        "order_id": existing_intent.get("broker_order_id") or "",
                        "previous_outcome": TAKEN,
                        "execution_key": execution_key,
                        "execution_state": state_now, "gates": []}
            return {"outcome": (EXECUTION_UNCERTAIN if state_now in
                                 {DECISION_APPROVED, EXECUTION_PENDING, EXECUTED,
                                  EXECUTION_UNCERTAIN, RECONCILED}
                                 else EXECUTION_FAILED),
                    "decision_id": existing_intent.get("decision_id") or "",
                    "trade_id": existing_intent.get("trade_id") or "",
                    "order_id": existing_intent.get("broker_order_id") or "",
                    "execution_key": execution_key, "execution_state": state_now,
                    "error": existing_intent.get("error") or
                             "execution intent already exists; reconciliation required",
                    "gates": []}

        gates, sizing = self.evaluate_gates(plan, symbol, sizing_inputs)
        size = sizing.executed
        failed = [g for g in gates if not g.passed]

        if failed:
            first = failed[0]
            decision_id = self._record(
                evaluation, outcome=REJECTED,
                reason_code=first.name.upper(),
                reason=f"the agent declined a valid SMC setup: {first.detail}",
                gates=gates, plan=plan, market=market, candle_time=candle)
            return {"outcome": REJECTED, "decision_id": decision_id,
                    "trade_id": "", "gates": gates}

        if not can_trade:
            decision_id = self._record(
                evaluation, outcome=MISSED,
                reason_code="AGENT_COULD_NOT_ACT",
                reason=("a valid SMC setup passed every agent gate and was not "
                        f"taken: {blocked_reason or 'the agent was unable to act'}"),
                gates=gates, plan=plan, market=market, candle_time=candle)
            return {"outcome": MISSED, "decision_id": decision_id,
                    "trade_id": "", "gates": gates}

        rr = reward_to_risk(plan["entry"], plan["stop"], plan["target_2"])
        passed_labels = [row.get("label") for row in
                         (evaluation.get("ordered_condition_results") or [])
                         if row.get("status") == "PASS"]
        why = ("SMC reached ENTRY_READY with every condition met ("
               + ", ".join(str(x) for x in passed_labels) + "); the plan offers "
               f"{rr:.2f}R against a {self.min_reward_to_risk:.1f}R floor "
               f"at size {size:.4f}"
               + (f" (capped down from {sizing.requested:.4f}; this trade risks "
                  "less than planned)" if sizing.capped else ""))

        rr = reward_to_risk(plan["entry"], plan["stop"], plan["target_2"])
        passed_labels = [row.get("label") for row in
                         (evaluation.get("ordered_condition_results") or [])
                         if row.get("status") == "PASS"]
        why = ("SMC reached ENTRY_READY with every condition met ("
               + ", ".join(str(x) for x in passed_labels) + "); the plan offers "
               f"{rr:.2f}R against a {self.min_reward_to_risk:.1f}R floor "
               f"at size {size:.4f}"
               + (f" (capped down from {sizing.requested:.4f}; this trade risks "
                  "less than planned)" if sizing.capped else ""))
        payload = self._intent_payload(evaluation, plan=plan, market=market,
                                       gates=gates, sizing=sizing, why=why)
        try:
            intent = self.journal.create_execution_intent(
                execution_key=execution_key, symbol=symbol, timeframe=timeframe,
                candle_time=candle, proposal_id=proposal_id, payload=payload)
            self.journal.transition_execution(execution_key, EXECUTION_PENDING)
        except Exception as exc:
            # No intent means the broker must not be called. A journal outage
            # itself cannot be written into the journal, so expose it directly.
            try:
                decision_id = self._record(
                    evaluation, outcome=EXECUTION_FAILED,
                    reason_code="INTENT_PERSISTENCE_FAILED",
                    reason=f"execution intent was not persisted: {type(exc).__name__}: {exc}",
                    gates=gates, plan=plan, market=market, candle_time=candle)
            except Exception:
                decision_id = ""
            return {"outcome": EXECUTION_FAILED, "executed": False,
                    "failed": True, "decision_id": decision_id, "trade_id": "",
                    "execution_key": execution_key,
                    "execution_state": EXECUTION_FAILED,
                    "error": f"{type(exc).__name__}: {exc}", "gates": gates}

        order_id = ""
        try:
            placed = executor(sizing) if executor is not None else {"order": {}}
            if executor is not None and not placed:
                raise ExecutionFailed("execution layer declined the order")
            order_id = _order_id_of(placed)
        except ExecutionFailed as exc:
            self.journal.transition_execution(execution_key, EXECUTION_FAILED,
                                              error=str(exc))
            decision_id = self._record(
                evaluation, outcome=EXECUTION_FAILED,
                reason_code="EXECUTION_FAILED",
                reason=f"broker submission failed: {exc}",
                gates=gates, plan=plan, market=market, candle_time=candle)
            return {"outcome": EXECUTION_FAILED, "executed": False,
                    "decision_id": decision_id, "trade_id": "", "gates": gates,
                    "execution_key": execution_key,
                    "execution_state": EXECUTION_FAILED, "error": str(exc)}
        except Exception as exc:
            # A generic exception cannot prove whether the broker committed.
            # Keep the intent uncertain; the runtime will reconcile by key.
            self.journal.transition_execution(
                execution_key, EXECUTION_UNCERTAIN,
                error=f"broker response uncertain: {type(exc).__name__}: {exc}")
            return {"outcome": EXECUTION_UNCERTAIN, "executed": False,
                    "decision_id": "", "trade_id": "", "order_id": "",
                    "gates": gates, "execution_key": execution_key,
                    "execution_state": EXECUTION_UNCERTAIN,
                    "error": str(exc)}

        self.journal.transition_execution(execution_key, EXECUTED,
                                          broker_order_id=order_id)
        try:
            result = self._finalize_intent(
                self.journal.execution_intent(execution_key), order_id=order_id)
        except Exception as exc:
            # The broker truth is durable even when the final journal write is
            # not. Do not create a MISSED row or claim that no order exists.
            self.journal.transition_execution(
                execution_key, EXECUTION_UNCERTAIN,
                broker_order_id=order_id,
                error=f"journal finalization pending: {type(exc).__name__}: {exc}")
            return {"outcome": EXECUTION_UNCERTAIN, "executed": bool(order_id),
                    "decision_id": "", "trade_id": "", "order_id": order_id,
                    "gates": gates, "execution_key": execution_key,
                    "execution_state": EXECUTION_UNCERTAIN,
                    "error": str(exc)}

        return {**result, "gates": gates, "planned_rr": rr,
                "proposal_id": proposal_id}
