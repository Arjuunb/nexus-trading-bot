"""Wire the SMC agent into the live lab tick without touching the strategy.

The lab already has a complete, reviewed decision path: a closed candle
becomes an evaluation, an ENTRY_READY evaluation becomes a staged candidate,
and a staged candidate becomes a paper order. This module adds one step at
the END of that path and changes nothing before it.

    closed candle -> SMC strategy -> lab decision + staged candidate
                  -> AGENT validation -> paper order -> journal -> review

Two properties are the whole point of doing it this way:

**The strategy is not modified, or even reached.** Everything here is a
subclass of the lab runtime. ``services/smc_strategy_lab.py`` and the three
decision-path modules behind it are byte-for-byte untouched, which is what
``services/smc_strategy_freeze.py`` and ``data/pr6_real_paper_freeze.json``
both check. The agent reads the decision the strategy already made; it has no
way to alter, re-run, or second-guess it.

**Execution stays inside the lab's own path.** The agent never calls the
broker. It approves — or does not approve — a candidate the lab has already
staged, through ``approve_candidate``, the same call the manual button makes.
So every protection, idempotency check and audit row the lab writes for a
human approval is written for an agent approval too.

The agent gates orders only when the session's operating mode is
``manual_approval``: that is the one mode where the lab stages a candidate and
waits for an approver instead of placing the order itself. Attaching an agent
is what makes it the approver. In ``automatic`` mode the lab places the order
before any agent could see it, so the agent would not be a gate at all and
this module deliberately stands down and says so rather than pretending to
have approved something. In ``signals_only`` no order is possible.

Failure is closed in every direction, structurally rather than by promise. In
``manual_approval`` the lab never places an order on its own, so an absent
agent, a raising agent, an unwritable journal, a declined gate or an
unreliable feed all end identically: the candidate stays staged and no order
exists. There is no branch here that places an order when the agent is
unavailable.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Optional

from services.smc_agent import SizingInputs
from services.smc_strategy_lab import SMCPaperAccount, SMCStrategyLabRuntime

#: The one saved operating mode in which an approver decides. See the module
#: docstring: this is a property of the lab, not a new mode.
AGENT_APPROVAL_MODE = "manual_approval"


class AgentGatedSMCPaperAccount(SMCPaperAccount):
    """The lab's paper account, able to place a size the agent decided.

    The lab sizes a strategy order from the saved risk percentage and the
    account's equity. That is the right behaviour for the lab and it is left
    exactly as it is. But the agent has a position bound of its own, and a
    journal that records "capped to 0.9" while the book holds 8.14 would be
    worse than no journal at all. So when — and only when — the agent is
    placing its own decision, it stages the size it committed to and the entry
    order is created at that size instead of a re-derived one.

    The staging is per-thread and lasts one call. Nothing else changes: a
    human approval, an automatic-mode placement, the scale-out order and every
    other caller go through the unmodified path and size exactly as before.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._staged_quantity = threading.local()

    @contextmanager
    def agent_quantity(self, quantity: float):
        """Place the next strategy entry on this thread at this exact size."""
        previous = getattr(self._staged_quantity, "value", None)
        self._staged_quantity.value = float(quantity)
        try:
            yield
        finally:
            self._staged_quantity.value = previous

    def submit_order(self, **kwargs):
        quantity = getattr(self._staged_quantity, "value", None)
        if (quantity is not None and kwargs.get("ownership") == "strategy"
                and kwargs.get("risk_pct") is not None):
            # Replacing risk_pct rather than adding to it: the parent sizes
            # from risk_pct when it is present, and the agent has already done
            # that arithmetic against its own bounds.
            kwargs = {**kwargs, "quantity": float(quantity), "risk_pct": None}
        return super().submit_order(**kwargs)


class AgentSMCStrategyLabRuntime(SMCStrategyLabRuntime):
    """The lab runtime with the independent SMC agent attached downstream."""

    def __init__(self, market, account: SMCPaperAccount, *, agent=None, **kwargs):
        super().__init__(market, account, **kwargs)
        #: The independent agent. ``None`` means the runtime behaves exactly
        #: like the lab runtime it inherits from.
        self.agent = agent
        # The agent step runs after the parent tick has released its own lock,
        # so it needs its own. Non-blocking for the same reason the parent's
        # is: a slow tick must not queue up the poll loop behind it.
        self._agent_lock = threading.Lock()
        # Written only by the thread currently inside this class's tick(), read
        # only by that same thread. A concurrent read of the visual state from
        # an HTTP request runs on another thread and cannot reach it.
        self._capture = threading.local()
        self.last_agent_result: dict = {
            "enabled": agent is not None, "executed": False,
            "reason": "the agent has not observed a tick yet"}

    # ------------------------------------------------------------- capture
    def reconcile_visual(self, visual: dict, **kwargs):
        """Unchanged behaviour; notes what the tick is about to act on.

        The evaluation the agent must see is the one the lab passes to
        ``synchronize_candidate``, which is this call's output. Reading it here
        means the agent judges exactly what the lab judged, rather than a
        re-derived or re-read copy that could differ.
        """
        reconciled, quote = super().reconcile_visual(visual, **kwargs)
        if getattr(self._capture, "armed", False):
            self._capture.visual = reconciled
        return reconciled, quote

    @staticmethod
    def _closed_candle_time(visual: dict) -> str:
        """The closed candle this tick decided on, as the lab names it.

        Deliberately the same derivation the lab runs before journalling its
        own decision, because the agent's dedupe key has to be the lab's
        candle and not a differently-rounded reading of it. The integration
        suite asserts the two agree on a real tick, so a change to either side
        fails rather than silently splitting one candle into two.
        """
        candles = (visual or {}).get("candles") or []
        if not candles:
            return ""
        closed = candles[-1]
        stamp = getattr(closed, "timestamp", None)
        if stamp is None and isinstance(closed, dict):
            stamp = closed.get("timestamp")
        return stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp or "")

    # ---------------------------------------------------------------- tick
    def tick(self) -> dict:
        if not self._agent_lock.acquire(blocking=False):
            return {"skipped": True, "reason": "tick already running",
                    "real_execution_allowed": False}
        try:
            self._capture.armed = True
            self._capture.visual = None
            try:
                result = super().tick()
            finally:
                self._capture.armed = False
            if result.get("skipped"):
                return result
            agent_result = self._run_agent(result, self._capture.visual)
            self.last_agent_result = agent_result
            return {**result, "agent": agent_result}
        finally:
            self._capture.visual = None
            self._agent_lock.release()

    # --------------------------------------------------------------- agent
    @staticmethod
    def _candidate_status(candidate: dict) -> str:
        """The staged candidate's status, whichever shape the lab returned."""
        status = (candidate or {}).get("candidate_status")
        if not status:
            status = ((candidate or {}).get("candidate") or {}).get("status")
        return str(status or "")

    def _sizing_inputs(self, session: dict) -> Optional[SizingInputs]:
        """The live numbers the agent must size from, or None if unknown.

        Unknown means the agent does not trade this tick. A size derived from
        a stale equity or an assumed step is a size the journal could not
        honestly claim was placed.
        """
        try:
            equity = float(self.account.broker.account()["equity"])
            rules = self.market.usdm_contract_rules(session["symbol"])
            step = float(rules["quantity_step"])
            risk_percent = float(session["risk_pct"])
        except Exception:  # noqa: BLE001 — absence is the answer, not a crash
            return None
        if equity <= 0 or risk_percent <= 0:
            return None
        return SizingInputs(equity=equity, risk_percent=risk_percent,
                            quantity_step=step)

    def _run_agent(self, result: dict, visual: Optional[dict]) -> dict:
        """Let the agent decide on what the strategy already decided.

        Strictly downstream and strictly a veto. By the time this runs the
        strategy has decided, the lab has journalled that decision and staged
        or refused its candidate, and none of that can be changed from here.
        """
        if self.agent is None:
            return {"enabled": False, "executed": False,
                    "reason": "no agent is attached to this runtime"}

        session = self.account.session() or {}
        mode = str(session.get("operating_mode") or "")
        if mode != AGENT_APPROVAL_MODE:
            return {"enabled": False, "executed": False, "mode": mode,
                    "reason": (f"the agent gates orders in {AGENT_APPROVAL_MODE} "
                               f"mode; this session is in {mode or 'no mode'}")}
        evaluation = (visual or {}).get("source_strategy")
        if not isinstance(evaluation, dict) or not evaluation:
            return {"enabled": True, "executed": False,
                    "reason": "the tick produced no strategy evaluation to judge"}

        candidate = result.get("candidate") or {}
        health = result.get("market_data_health") or {}
        status = self._candidate_status(candidate)
        staged = status == "PENDING_APPROVAL"
        reliable = bool(health.get("reliable"))
        proposal_id = str(evaluation.get("proposal_id") or "")
        inputs = self._sizing_inputs(session)
        stage = getattr(self.account, "agent_quantity", None)

        def execute(sizing):
            # The lab's own placement path, with the lab's own idempotency:
            # approve_candidate requires the candidate to still be
            # PENDING_APPROVAL and flips it to ORDER_CREATED, so a second
            # attempt raises instead of placing a second order. The only thing
            # the agent adds is the size it committed to in the journal.
            if not staged or not proposal_id:
                raise RuntimeError(
                    "no candidate is staged for approval — the agent will not "
                    "place an order outside the lab's own execution path")
            if stage is None:
                raise RuntimeError(
                    "this paper account cannot place the agent's own size; "
                    "refusing to place a size the journal would not match")
            with stage(sizing.executed):
                return self.account.approve_candidate(proposal_id)

        # Why the agent could not act, if it cannot. A signal the strategy
        # produced and the agent then failed to take is a MISSED trade, and
        # the journal is only useful if it names which of these it was.
        if not reliable:
            blocked = "market data is not reliable"
        elif not staged:
            blocked = f"the lab did not stage a candidate to approve ({status or 'none'})"
        elif inputs is None:
            blocked = "the live sizing inputs (account equity and venue step) were unavailable"
        elif stage is None:
            blocked = "this paper account cannot place a size the agent decided"
        else:
            blocked = ""
        can_trade = not blocked
        try:
            outcome = self.agent.observe(
                evaluation,
                market={"feed": dict(health), "candidate_status": status,
                        "session_id": session.get("id"), "operating_mode": mode},
                can_trade=can_trade,
                blocked_reason=blocked,
                candle_time=self._closed_candle_time(visual or {}),
                sizing_inputs=inputs,
                executor=execute)
        except Exception as exc:  # noqa: BLE001 — a broken agent must not trade
            # Nothing was placed: in this mode the lab does not place orders,
            # and the only path to an order is the executor above, which only
            # runs once the agent has committed to taking the trade.
            return {"enabled": True, "executed": False, "failed": True,
                    "error": f"{type(exc).__name__}: {exc}",
                    "reason": (f"the agent failed: {type(exc).__name__}: {exc}. "
                               "No order was placed.")}

        return {"enabled": True, "outcome": outcome.get("outcome"),
                "executed": outcome.get("outcome") == "TAKEN",
                "decision_id": outcome.get("decision_id", ""),
                "trade_id": outcome.get("trade_id", ""),
                "order_id": outcome.get("order_id", ""),
                "size": outcome.get("size"),
                "requested_size": outcome.get("requested_size"),
                "size_capped": outcome.get("size_capped"),
                "previous_outcome": outcome.get("previous_outcome"),
                "candidate_status": status}
