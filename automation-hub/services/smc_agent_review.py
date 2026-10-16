"""What the agent makes of its own trading: reviews, mistakes, lessons.

A trader who treats every loss as a mistake learns to avoid good trades, and
one who treats every win as validation learns nothing at all. This module keeps
those two things apart:

    RESULT is what the market did.  VERDICT is what the agent did.

They are judged separately and then combined:

  * followed the rules and won   -> CORRECT
  * followed the rules and lost  -> CORRECT_BUT_LOST, which is NOT a mistake.
    The setup was valid, the size was right, the floor was respected, and the
    market went the other way. Recording that as an error would teach the agent
    to stop taking the trades it is supposed to take.
  * broke a rule and lost        -> MISTAKE
  * broke a rule and won         -> LUCKY, which IS a mistake. A profit earned
    by ignoring the rules is the most expensive kind of feedback, because it
    rewards the behaviour that will eventually cost the account.

Only rules the AGENT controls can be violated: the reward-to-risk floor, the
position bounds, and whether the signal was actually a completed SMC setup. The
SMC strategy's own conditions are never second-guessed here — if SMC said the
setup was valid, this module treats that as given. It reviews the agent.

Nothing here modifies the strategy. Where a review suggests the STRATEGY itself
could be different, that is filed as a proposed improvement and left alone.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from services.smc_agent import (BTC_MAX_SIZE, BTC_MIN_SIZE, MIN_REWARD_TO_RISK,
                                reward_to_risk, size_bounds_for)
from services.smc_agent_journal import (CORRECT, CORRECT_BUT_LOST, LUCKY,
                                        MISSED, MISTAKE, NOT_READY, REJECTED,
                                        TAKEN, SMCAgentJournal)

#: Verdicts that count against the agent. CORRECT_BUT_LOST is deliberately
#: absent: a disciplined loss is not an error.
MISTAKE_VERDICTS = (MISTAKE, LUCKY)

#: How often a pattern has to recur before it is called a habit rather than an
#: incident. Below this it is one event and naming it a habit would be noise.
REPEAT_THRESHOLD = 3


def is_mistake(verdict: str) -> bool:
    return verdict in MISTAKE_VERDICTS


@dataclass(frozen=True)
class Violation:
    """One rule the agent broke, with the number that proves it."""
    rule: str
    detail: str
    value: Optional[float] = None
    limit: Optional[float] = None


def rule_violations(trade: dict, decision: Optional[dict] = None, *,
                    min_reward_to_risk: float = MIN_REWARD_TO_RISK) -> list[Violation]:
    """Re-check the agent's own rules against the recorded trade.

    Re-derived from what was stored rather than trusted from the decision, so a
    gate that passed at the time because of a bug is still caught in review.
    """
    out: list[Violation] = []
    rr = reward_to_risk(trade["entry"], trade["stop"], trade["target"])
    if rr is None or rr < min_reward_to_risk:
        out.append(Violation(
            rule="minimum_reward_to_risk",
            detail=(f"took a {rr:.2f}R plan under the {min_reward_to_risk:.1f}R floor"
                    if rr is not None else "took a plan with no measurable risk"),
            value=rr, limit=min_reward_to_risk))

    low, high = size_bounds_for(trade["symbol"])
    size = float(trade["size"])
    if not (low <= size <= high):
        out.append(Violation(
            rule="position_size_within_bounds",
            detail=f"sized {size:.4f}, outside the {low}-{high} bound",
            value=size, limit=high if size > high else low))

    if decision is not None:
        if decision.get("smc_state") != "ENTRY_READY":
            out.append(Violation(
                rule="signal_was_a_completed_smc_setup",
                detail=(f"opened on an SMC state of {decision.get('smc_state')!r} "
                        "— the strategy had not completed its sequence")))
        if decision.get("missing"):
            out.append(Violation(
                rule="no_missing_smc_conditions",
                detail=("opened while SMC conditions were still missing: "
                        + ", ".join(map(str, decision["missing"])))))
    return out


def _won(trade: dict) -> bool:
    realised = trade.get("realised_r")
    return realised is not None and float(realised) > 0


def classify(trade: dict, violations: Iterable[Violation]) -> str:
    violations = list(violations)
    won = _won(trade)
    if not violations:
        return CORRECT if won else CORRECT_BUT_LOST
    return LUCKY if won else MISTAKE


def review_trade(journal: SMCAgentJournal, trade_id: str, *,
                 min_reward_to_risk: float = MIN_REWARD_TO_RISK,
                 at: Optional[str] = None) -> dict:
    """Review one closed trade and write the verdict to the journal."""
    trade = journal.trade(trade_id)
    if trade.get("closed_at") is None:
        raise ValueError("a trade cannot be reviewed before it closes — the "
                         "result is half of the judgement")
    decisions = [d for d in journal.decisions(limit=5000)
                 if d["id"] == trade["decision_id"]]
    decision = decisions[0] if decisions else None

    violations = rule_violations(trade, decision, min_reward_to_risk=min_reward_to_risk)
    verdict = classify(trade, violations)
    realised = trade.get("realised_r")

    did_well, did_badly = [], []
    if not violations:
        did_well.append(f"followed every agent rule: {reward_to_risk(trade['entry'], trade['stop'], trade['target']):.2f}R "
                        f"plan at size {float(trade['size']):.4f}")
        if decision and not decision.get("missing"):
            did_well.append("entered only after SMC completed its sequence")
    did_badly.extend(v.detail for v in violations)

    if verdict == CORRECT:
        why = ("The rules were followed and the trade worked. Nothing to change: "
               "repeat this.")
    elif verdict == CORRECT_BUT_LOST:
        why = ("The rules were followed and the market went the other way. This "
               "is NOT a mistake — the setup was valid, the size was right and "
               "the floor was respected. Changing behaviour because of it would "
               "mean avoiding the trades the strategy exists to take.")
    elif verdict == LUCKY:
        why = ("This trade made money and should not have been taken. "
               + "; ".join(v.detail for v in violations)
               + ". A profit earned by ignoring the rules is the most expensive "
                 "kind of feedback, because it rewards what will eventually "
                 "cost the account. It counts as a mistake.")
    else:
        why = ("The rules were broken and the trade lost. "
               + "; ".join(v.detail for v in violations) + ".")

    journal.record_review(
        trade_id=trade_id, verdict=verdict, followed_rules=not violations,
        why=why, did_well=did_well, did_badly=did_badly,
        violations=[asdict(v) for v in violations],
        result=trade.get("result") or "", realised_r=realised, at=at)
    return {"trade_id": trade_id, "verdict": verdict,
            "followed_rules": not violations, "is_mistake": is_mistake(verdict),
            "violations": [asdict(v) for v in violations],
            "did_well": did_well, "did_badly": did_badly, "why": why}


def review_closed_trades(journal: SMCAgentJournal, *,
                         min_reward_to_risk: float = MIN_REWARD_TO_RISK) -> list[dict]:
    """Review every closed trade that has not been reviewed yet."""
    reviewed = {r["trade_id"] for r in journal.reviews(limit=5000)}
    out = []
    for trade in journal.trades(closed_only=True, limit=5000):
        if trade["id"] not in reviewed:
            out.append(review_trade(journal, trade["id"],
                                    min_reward_to_risk=min_reward_to_risk))
    return out


# ────────────────────────────── weekly review ──────────────────────────────

def _period(end: Optional[datetime], days: int = 7) -> tuple[str, str]:
    end = end or datetime.now(timezone.utc)
    return (end - timedelta(days=days)).isoformat(), end.isoformat()


def weekly_review(journal: SMCAgentJournal, *, end: Optional[datetime] = None,
                  days: int = 7,
                  min_reward_to_risk: float = MIN_REWARD_TO_RISK) -> dict:
    """Look back over the week: what was taken, skipped, missed, and repeated.

    Findings about the AGENT's own process are returned and stored. Anything
    that would change the SMC strategy is filed through
    ``journal.propose_improvement`` and goes no further — recording it is the
    entire action.
    """
    start, finish = _period(end, days)
    decisions = [d for d in journal.decisions(since=start, limit=5000)
                 if d["at"] <= finish]
    # Trades are counted by WHEN THEY HAPPENED, not by when the paperwork was
    # done. Taking is an opening event and a result is a closing one, so they
    # are selected on different timestamps; counting both on opened_at drops a
    # trade carried over from last week, and counting reviews on their own
    # timestamp drops a trade reviewed a minute after the period ended.
    every = journal.trades(limit=5000)
    trades = [t for t in every if start <= t["opened_at"] <= finish]
    closed = [t for t in every
              if t.get("closed_at") and start <= t["closed_at"] <= finish]
    closed_ids = {t["id"] for t in closed}
    reviews = [r for r in journal.reviews(limit=5000) if r["trade_id"] in closed_ids]

    by_outcome = Counter(d["outcome"] for d in decisions)
    realised = [float(t["realised_r"]) for t in closed
                if t.get("realised_r") is not None]
    wins = [r for r in realised if r > 0]
    mistakes = [r for r in reviews if is_mistake(r["verdict"])]
    disciplined_losses = [r for r in reviews if r["verdict"] == CORRECT_BUT_LOST]

    summary = {
        "period_start": start, "period_end": finish,
        "observations": len(decisions),
        "taken": by_outcome.get(TAKEN, 0),
        "skipped": by_outcome.get(REJECTED, 0),
        "not_ready": by_outcome.get(NOT_READY, 0),
        "missed": by_outcome.get(MISSED, 0),
        "closed": len(closed),
        "open": sum(1 for t in trades if not t.get("closed_at")),
        "wins": len(wins), "losses": len(realised) - len(wins),
        "win_rate_pct": round(100.0 * len(wins) / len(realised), 1) if realised else None,
        "net_r": round(sum(realised), 2) if realised else 0.0,
        "expectancy_r": round(sum(realised) / len(realised), 3) if realised else None,
        "average_planned_rr": (round(sum(float(t["planned_rr"]) for t in trades) / len(trades), 2)
                               if trades else None),
        "mistakes": len(mistakes),
        "disciplined_losses": len(disciplined_losses),
        "reviewed": len(reviews),
    }

    repeated = _repeated_patterns(decisions, reviews)
    findings = _agent_findings(summary, repeated, decisions)

    for pattern in repeated:
        journal.record_lesson(
            pattern=pattern["pattern"], occurrences=pattern["occurrences"],
            detail=pattern["detail"], evidence=pattern.get("evidence"),
            first_seen=pattern.get("first_seen", ""),
            last_seen=pattern.get("last_seen", ""))

    journal.record_weekly_review(
        period_start=start, period_end=finish, summary=summary,
        agent_findings=findings, lessons=repeated)
    return {"summary": summary, "repeated": repeated, "agent_findings": findings}


def _repeated_patterns(decisions: list[dict], reviews: list[dict]) -> list[dict]:
    """Behaviour that happened often enough to be a habit, not an incident."""
    out = []

    skips = Counter(d["reason_code"] for d in decisions if d["outcome"] == REJECTED)
    for code, count in skips.most_common():
        if count >= REPEAT_THRESHOLD:
            rows = [d for d in decisions if d["reason_code"] == code]
            out.append({
                "pattern": f"REPEATED_SKIP::{code}", "occurrences": count,
                "detail": (f"{count} valid SMC setups were declined for the same "
                           f"reason ({code}). Worth understanding — it is either "
                           "the risk floor doing its job or the agent refusing "
                           "trades it should be taking."),
                "evidence": [r["id"] for r in rows[:10]],
                "first_seen": min(r["at"] for r in rows),
                "last_seen": max(r["at"] for r in rows)})

    misses = [d for d in decisions if d["outcome"] == MISSED]
    if len(misses) >= REPEAT_THRESHOLD:
        codes = Counter(d["reason_code"] for d in misses)
        out.append({
            "pattern": "REPEATED_MISS", "occurrences": len(misses),
            "detail": (f"{len(misses)} valid setups passed every gate and were "
                       f"still not taken ({dict(codes)}). These are the agent's "
                       "own failures, not the strategy's."),
            "evidence": [d["id"] for d in misses[:10]],
            "first_seen": min(d["at"] for d in misses),
            "last_seen": max(d["at"] for d in misses)})

    broken = Counter(v["rule"] for r in reviews for v in r.get("violations") or [])
    for rule, count in broken.most_common():
        if count >= REPEAT_THRESHOLD:
            out.append({
                "pattern": f"REPEATED_VIOLATION::{rule}", "occurrences": count,
                "detail": (f"the agent broke its own {rule} rule {count} times. "
                           "A rule broken this often is either not understood or "
                           "not enforced."),
                "evidence": [r["id"] for r in reviews
                             if any(v["rule"] == rule for v in r.get("violations") or [])][:10]})
    return out


def _agent_findings(summary: dict, repeated: list[dict],
                    decisions: list[dict]) -> list[dict]:
    """Improvements to the AGENT's process. Never to the strategy."""
    findings = []
    if summary["missed"]:
        findings.append({
            "area": "execution",
            "finding": (f"{summary['missed']} valid setups were not acted on. "
                        "Every one is a trade the strategy found and the agent "
                        "failed to take."),
            "action": "Investigate what blocked execution and make it not block."})
    if summary["taken"] and summary["reviewed"] < summary["closed"]:
        findings.append({
            "area": "journalling",
            "finding": (f"{summary['closed'] - summary['reviewed']} closed trades "
                        "have no review."),
            "action": "Review every closed trade before the next session."})
    if any(p["pattern"].startswith("REPEATED_VIOLATION") for p in repeated):
        findings.append({
            "area": "risk management",
            "finding": "The agent repeatedly broke its own rules.",
            "action": ("Treat the gates as hard stops rather than advisory — a "
                       "rule broken repeatedly is not enforced.")})
    if summary["observations"] and not summary["taken"]:
        findings.append({
            "area": "detection",
            "finding": (f"{summary['observations']} observations produced no "
                        "trade at all this week."),
            "action": ("Confirm this is the market and not the agent: check the "
                       "skip reasons before assuming there were no setups.")})
    if summary["disciplined_losses"]:
        findings.append({
            "area": "discipline",
            "finding": (f"{summary['disciplined_losses']} losses followed the "
                        "rules exactly. These are the cost of doing business, "
                        "not errors."),
            "action": "No change. Do not tune anything in response to these."})
    return findings
