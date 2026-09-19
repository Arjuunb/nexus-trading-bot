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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from services.smc_agent_journal import (MISSED, NOT_READY, REJECTED, TAKEN,
                                        SMCAgentJournal)

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
class Gate:
    """One agent-side veto, with the number it was judged on."""
    name: str
    passed: bool
    detail: str
    value: Optional[float] = None
    limit: Optional[float] = None


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
    def _identity(evaluation: dict) -> tuple[str, str, str]:
        identity = evaluation.get("data_identity") or {}
        return (identity.get("symbol") or "", identity.get("timeframe") or "",
                identity.get("selected_candle") or "")

    def _record(self, evaluation: dict, *, outcome: str, reason_code: str,
                reason: str, gates: list[Gate] | None = None,
                plan: dict | None = None, market: dict | None = None,
                trade_id: str = "") -> str:
        symbol, timeframe, candle = self._identity(evaluation)
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

    # --------------------------------------------------------------- gates
    def evaluate_gates(self, plan: dict, symbol: str) -> tuple[list[Gate], float]:
        """Every agent veto, and the size that survived them.

        Returns the gates and the size to trade. Size is 0.0 when no allowed
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
        risk_percent = float(plan.get("risk_percent") or 0.0)
        raw = ((self.equity * risk_percent / 100.0) / risk_per_unit
               if risk_per_unit > 0 and risk_percent > 0 else 0.0)
        # Capping DOWN reduces risk and is what a position limit is for, so it
        # is allowed and recorded. Below the floor there is no size that can
        # express the trade without risking more than planned, so it is a veto.
        size = min(raw, high)
        gates.append(Gate(
            name="position_size_within_bounds",
            passed=size >= low,
            detail=(f"risk-based size {raw:.4f} -> {size:.4f} within "
                    f"{low}-{high} {symbol.upper()[:3]}" if size >= low else
                    f"risk-based size {raw:.4f} is below the {low} minimum"),
            value=size, limit=low))
        if raw > high:
            gates.append(Gate(
                name="position_size_capped",
                passed=True,
                detail=f"size capped from {raw:.4f} to the {high} maximum — "
                       "this trade risks less than planned",
                value=size, limit=high))
        return gates, (size if size >= low else 0.0)

    # ------------------------------------------------------------- observe
    def observe(self, evaluation: dict, *, market: Optional[dict] = None,
                can_trade: bool = True, blocked_reason: str = "") -> dict:
        """Look at one SMC evaluation and decide. Always leaves a journal row.

        ``can_trade`` is the agent's own readiness — a stale feed, an existing
        position, a paused session. When a signal was actionable and this is
        False the decision is recorded as MISSED rather than skipped, because
        the strategy did its job and the agent did not.
        """
        state = str(evaluation.get("state") or "")
        source = str(evaluation.get("strategy_id") or "")

        if source != SOURCE_STRATEGY_ID:
            decision_id = self._record(
                evaluation, outcome=NOT_READY, reason_code="NOT_AN_SMC_SIGNAL",
                reason=(f"evaluation came from {source or 'an unnamed strategy'}, "
                        f"not {SOURCE_STRATEGY_ID}; the agent trades SMC signals only"),
                market=market)
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
                reason=reason, market=market)
            return {"outcome": NOT_READY, "decision_id": decision_id,
                    "trade_id": "", "gates": []}

        symbol, timeframe, _candle = self._identity(evaluation)
        gates, size = self.evaluate_gates(plan, symbol)
        failed = [g for g in gates if not g.passed]

        if failed:
            first = failed[0]
            decision_id = self._record(
                evaluation, outcome=REJECTED,
                reason_code=first.name.upper(),
                reason=f"the agent declined a valid SMC setup: {first.detail}",
                gates=gates, plan=plan, market=market)
            return {"outcome": REJECTED, "decision_id": decision_id,
                    "trade_id": "", "gates": gates}

        if not can_trade:
            decision_id = self._record(
                evaluation, outcome=MISSED,
                reason_code="AGENT_COULD_NOT_ACT",
                reason=("a valid SMC setup passed every agent gate and was not "
                        f"taken: {blocked_reason or 'the agent was unable to act'}"),
                gates=gates, plan=plan, market=market)
            return {"outcome": MISSED, "decision_id": decision_id,
                    "trade_id": "", "gates": gates}

        rr = reward_to_risk(plan["entry"], plan["stop"], plan["target_2"])
        passed_labels = [row.get("label") for row in
                         (evaluation.get("ordered_condition_results") or [])
                         if row.get("status") == "PASS"]
        why = ("SMC reached ENTRY_READY with every condition met ("
               + ", ".join(str(x) for x in passed_labels) + "); the plan offers "
               f"{rr:.2f}R against a {self.min_reward_to_risk:.1f}R floor "
               f"at size {size:.4f}")
        decision_id = self._record(
            evaluation, outcome=TAKEN, reason_code="ALL_GATES_PASSED",
            reason=why, gates=gates, plan=plan, market=market)
        trade_id = self.journal.open_trade(
            decision_id=decision_id, symbol=symbol, timeframe=timeframe,
            direction=str((evaluation.get("proposal") or {}).get("direction") or ""),
            entry=float(plan["entry"]), stop=float(plan["stop"]),
            target=float(plan["target_2"]), planned_rr=float(rr or 0.0),
            size=size, risk_amount=abs(float(plan["entry"]) - float(plan["stop"])) * size,
            setup_id=str(evaluation.get("setup_id") or ""),
            proposal_id=str(evaluation.get("proposal_id") or ""),
            conditions=evaluation.get("ordered_condition_results"),
            market=market, why=why,
            strategy_fingerprint=strategy_fingerprint(), opened_at=self._now())
        return {"outcome": TAKEN, "decision_id": decision_id,
                "trade_id": trade_id, "gates": gates, "size": size,
                "planned_rr": rr}
