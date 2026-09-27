"""Trade reviews, weekly reviews and the weekly scheduler.

Facts and interpretation are kept apart. The reviewer reads finalized trade
records and writes to its own tables (trade_reviews, weekly_reviews,
improvement_proposals); it has no path to change a trade record.

Every statistic comes from services/journal_stats.py. The weekly review
turns those numbers into statements of four kinds, each carrying the
journal_record_ids it rests on:

  FACT            a number computed from the records ("4 of 7 losses were in LONDON")
  OBSERVATION     what the facts show together ("losses are concentrated in LONDON")
  HYPOTHESIS      a possible explanation, marked as untested
  RECOMMENDATION  what to watch or collect next -- never a strategy change

A strategy change is only ever a PROPOSAL with status PENDING_APPROVAL, and
only when the evidence behind it reaches a minimum sample. Approving one
records a person's decision; nothing here edits a strategy, a parameter, a
risk setting or any source file.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from data.trade_record_store import TradeRecordStore, utcnow
from services import journal_stats as stats

REVIEWER_ID = "journal_reviewer"
REVIEWER_VERSION = 1
WEEKLY_REVIEW_VERSION = 1
#: A pattern needs this many trades behind it before a proposal is raised.
PROPOSAL_MIN_SAMPLE = 20
#: How far back the scheduler recovers missed weeks.
MAX_CATCH_UP_WEEKS = 12


# ======================================================================
# per-trade review
# ======================================================================
def review_trade(rec: dict) -> dict:
    """A rule-based review of one finalized record. Reads facts, writes none."""
    violations, mistakes, positives, tags, observations, recommendations = [], [], [], [], [], []
    r = rec.get("realized_r")
    risk = rec.get("risk_amount")
    outcome = rec.get("outcome")
    evidence = rec.get("evidence") or {}
    setup = rec.get("setup") or {}
    gate = evidence.get("quality_gate") or {}
    score = gate.get("score") if isinstance(gate, dict) else None
    failed = (setup.get("conditions_failed") if isinstance(setup, dict) else None) or []

    if rec.get("planned_stop_loss") is None:
        violations.append({"rule": "STOP_REQUIRED", "detail": "no stop-loss on record at entry"})
    if r is not None and r < -1.2:
        violations.append({"rule": "LOSS_WITHIN_PLANNED_RISK",
                           "detail": f"realized {r:+.2f}R, more than 1.2R lost"})
        mistakes.append("loss exceeded the planned risk (gap, slippage or stop not honoured)")
    if failed:
        violations.append({"rule": "ALL_CONDITIONS_PASSED",
                           "detail": f"entered with failed conditions: {failed[:5]}"})

    slip, qty = rec.get("slippage"), rec.get("filled_quantity")
    slip_r = (float(slip) * float(qty) / float(risk)) if (slip is not None and qty and risk) else None
    if slip_r is None:
        execution = "UNKNOWN"
    elif slip_r > 0.1:
        execution = "POOR"
        mistakes.append(f"entry slippage cost {slip_r:.2f}R")
    elif slip_r > 0.05:
        execution = "ACCEPTABLE"
    else:
        execution = "GOOD"

    if score is None:
        setup_quality = "UNKNOWN" if not failed else "LOW"
    else:
        setup_quality = "HIGH" if score >= 75 else "MEDIUM" if score >= 60 else "LOW"

    reason = str(rec.get("exit_reason") or "").lower()
    if outcome == "WIN" and "take" in reason:
        positives.append("target reached as planned")
    if outcome == "LOSS" and r is not None and -1.2 <= r <= -0.8 and "stop" in reason:
        positives.append("stop honoured: loss held to the planned risk")
    if (rec.get("planned_rr") or 0) >= 2:
        positives.append(f"planned reward:risk {rec['planned_rr']:.2f}")
    if outcome == "BREAKEVEN":
        observations.append("closed at breakeven")

    tags.extend(t for t in (outcome, rec.get("exit_reason"), rec.get("trading_session"),
                            rec.get("side")) if t)
    if r is not None:
        observations.append(f"{outcome} {r:+.2f}R ({rec.get('exit_reason') or 'exit reason unknown'})")
    if execution == "POOR":
        recommendations.append("watch entry slippage on this symbol before drawing conclusions")

    risk_compliance = ("UNKNOWN" if r is None and rec.get("planned_stop_loss") is not None else
                       "VIOLATION" if any(v["rule"] in ("STOP_REQUIRED", "LOSS_WITHIN_PLANNED_RISK")
                                          for v in violations) else "COMPLIANT")
    strategy_compliance = ("VIOLATION" if any(v["rule"] == "ALL_CONDITIONS_PASSED" for v in violations)
                           else "COMPLIANT" if (setup or evidence) else "UNKNOWN")
    return {
        "journal_record_id": rec["journal_record_id"], "agent_id": REVIEWER_ID,
        "review_version": REVIEWER_VERSION,
        "setup_quality": setup_quality, "execution_quality": execution,
        "risk_compliance": risk_compliance, "strategy_compliance": strategy_compliance,
        "rule_violations": violations, "mistakes": mistakes, "positive_behaviours": positives,
        "review_tags": tags, "observations": observations, "recommendations": recommendations,
        "basis": {"reviewer": "deterministic rules v1", "realized_r": r,
                  "quality_score": score, "slippage_r": slip_r},
    }


def review_finalized(store: TradeRecordStore, *, limit: int = 2000) -> int:
    """Review every finalized record that has no review of this version yet."""
    rows = store.query_trades(
        where="finalized=1 AND status='CLOSED' AND journal_record_id NOT IN "
              "(SELECT journal_record_id FROM trade_reviews WHERE agent_id=? AND review_version=?)",
        params=(REVIEWER_ID, REVIEWER_VERSION), limit=limit)
    for rec in rows:
        store.add_review(review_trade(rec))
    return len(rows)


# ======================================================================
# weekly review
# ======================================================================
def week_start_dow() -> int:
    try:
        return max(0, min(6, int(os.environ.get("HUB_REVIEW_WEEK_START_DOW", "0"))))
    except ValueError:
        return 0


def week_bounds(moment: datetime, *, dow: Optional[int] = None) -> tuple[datetime, datetime]:
    dow = week_start_dow() if dow is None else dow
    moment = moment.astimezone(timezone.utc)
    start = (moment - timedelta(days=(moment.weekday() - dow) % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)


def _joined(values: Optional[str]) -> str:
    return ", ".join(sorted(v for v in (values or "").split(",") if v)) or "?"


def review_scopes(store: TradeRecordStore) -> list[dict]:
    """Each agent reviews only its own records; strategies are never pooled."""
    scopes = []
    with store._lock:
        rows = store._c.execute(
            "SELECT record_source, instance_id, COALESCE(strategy_id, strategy_name, '?') "
            "AS strategy, GROUP_CONCAT(DISTINCT symbol), GROUP_CONCAT(DISTINCT timeframe) "
            "FROM trade_records WHERE record_origin='FORWARD_PAPER' AND status='CLOSED' "
            "GROUP BY record_source, instance_id, strategy ORDER BY MIN(rowid)"
        ).fetchall()
    for source, instance_id, strategy, symbols, timeframes in rows:
        if source in ("SMC_LAB", "AGENT"):
            scopes.append({"agent_id": "smc_agent", "strategy_id": strategy,
                           "where": "record_source IN ('SMC_LAB','AGENT') AND "
                                    "COALESCE(strategy_id, strategy_name, '?')=?",
                           "params": (strategy,), "label": f"SMC · {strategy}"})
        elif source == "PA_LAB":
            scopes.append({"agent_id": "pa_agent", "strategy_id": strategy,
                           "where": "record_source='PA_LAB' AND COALESCE(strategy_id, strategy_name, '?')=?",
                           "params": (strategy,), "label": f"Price Action · {strategy}"})
        elif source in ("INSTANCE", "ADAPTIVE_LAB") and instance_id:
            scopes.append({"agent_id": f"instance_agent:{instance_id}", "strategy_id": strategy,
                           "where": "record_source=? AND instance_id=? AND "
                                    "COALESCE(strategy_id, strategy_name, '?')=?",
                           "params": (source, instance_id, strategy),
                           # Instances have no name. The id is kept whole: its first
                           # characters alone can make two instances look identical.
                           "label": f"Instance · {_joined(symbols)} {_joined(timeframes)} · "
                                    f"{strategy} · {instance_id}"})
    seen, unique = set(), []
    for s in scopes:
        key = (s["agent_id"], s["strategy_id"])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return unique


def _records(store, scope: dict, start: Optional[datetime] = None,
             end: Optional[datetime] = None) -> list[dict]:
    where = f"record_origin='FORWARD_PAPER' AND status='CLOSED' AND ({scope['where']})"
    params = list(scope["params"])
    if start is not None:
        where += " AND position_closed_at >= ?"
        params.append(start.isoformat())
    if end is not None:
        where += " AND position_closed_at < ?"
        params.append(end.isoformat())
    return store.query_trades(where=where, params=params, limit=100000,
                              order="position_closed_at ASC")


def _validate(records: list[dict]) -> dict:
    no_r = [r["journal_record_id"] for r in records if r.get("realized_r") is None]
    minimal = [r["journal_record_id"] for r in records if r.get("data_completeness") == "MINIMAL"]
    no_exit = [r["journal_record_id"] for r in records if not r.get("exit_reason")]
    return {"records": len(records), "missing_realized_r": no_r, "minimal_records": minimal,
            "missing_exit_reason": no_exit,
            "note": "records without realized R are excluded from R statistics only"}


def _ids(rows: list[dict]) -> list[str]:
    return [r["journal_record_id"] for r in rows]


def _findings(records: list[dict], report: dict, previous: Optional[dict],
              reviews: dict, all_time: list[dict], scope: dict) -> tuple[dict, list[dict]]:
    facts, observations, hypotheses, recommendations = [], [], [], []
    proposals: list[dict] = []
    done = stats.completed(records)
    overall = report["overall"]
    n = overall["trades"]
    if n == 0:
        facts.append({"text": "No completed trades in this period.", "journal_record_ids": []})
        return {"facts": facts, "observations": observations, "hypotheses": hypotheses,
                "recommendations": recommendations}, proposals
    facts.append({"text": f"{n} completed trade(s): {overall['wins']} win(s), "
                          f"{overall['losses']} loss(es), {overall['breakevens']} breakeven.",
                  "journal_record_ids": _ids(done)})
    if overall["total_r"] is not None:
        facts.append({"text": f"Total {overall['total_r']:+.2f}R over {overall['r_known']} trade(s) "
                              "with known risk.", "journal_record_ids": _ids(done)})
    losses = [r for r in done if r["outcome"] == "LOSS"]
    if len(losses) >= 3:
        by_session = Counter(r.get("trading_session") or "UNKNOWN" for r in losses)
        session, count = by_session.most_common(1)[0]
        ids = _ids([r for r in losses if (r.get("trading_session") or "UNKNOWN") == session])
        facts.append({"text": f"{count} of {len(losses)} losses occurred in {session}.",
                      "journal_record_ids": ids})
        if count / len(losses) >= 0.6:
            observations.append({"text": f"Losses are concentrated in {session}.",
                                 "journal_record_ids": ids})
            hypotheses.append({"text": f"Conditions during {session} may reduce setup quality. "
                                       "Untested.", "journal_record_ids": ids})
            recommendations.append({
                "text": f"Collect at least {PROPOSAL_MIN_SAMPLE} qualifying trades in {session} "
                        "before considering a session filter.", "journal_record_ids": ids})
    longs, shorts = report["long"], report["short"]
    if longs["trades"] and shorts["trades"] and longs["total_r"] is not None \
            and shorts["total_r"] is not None and (longs["total_r"] > 0) != (shorts["total_r"] > 0):
        better = "LONG" if longs["total_r"] > shorts["total_r"] else "SHORT"
        facts.append({"text": f"Long {longs['total_r']:+.2f}R ({longs['trades']}), short "
                              f"{shorts['total_r']:+.2f}R ({shorts['trades']}).",
                      "journal_record_ids": _ids(done)})
        observations.append({"text": f"{better} trades carried the result this period.",
                             "journal_record_ids": _ids([r for r in done
                                                         if r.get("side") == better.lower()])})
    stops = [r for r in done if "stop" in str(r.get("exit_reason") or "").lower()]
    if n >= 3:
        facts.append({"text": f"{len(stops)} of {n} trades exited at the stop.",
                      "journal_record_ids": _ids(stops)})
    mistake_counts = Counter()
    mistake_ids: dict = {}
    for r in done:
        review = reviews.get(r["journal_record_id"]) or {}
        for m in review.get("mistakes") or []:
            mistake_counts[m] += 1
            mistake_ids.setdefault(m, []).append(r["journal_record_id"])
    for m, count in mistake_counts.most_common(3):
        if count >= 2:
            observations.append({"text": f"Recurring mistake ({count}×): {m}.",
                                 "journal_record_ids": mistake_ids[m]})
    if previous is not None:
        prev = (previous.get("stats") or {}).get("overall") or {}
        if prev.get("total_r") is not None and overall["total_r"] is not None:
            delta = overall["total_r"] - prev["total_r"]
            facts.append({"text": f"Total R changed by {delta:+.2f}R versus the previous week "
                                  f"({prev['total_r']:+.2f}R over {prev.get('trades', 0)} trade(s)).",
                          "journal_record_ids": _ids(done)})
    if n < stats.MIN_SAMPLE:
        recommendations.append({
            "text": f"Sample is {n} trade(s); {stats.MIN_SAMPLE}+ are needed before any figure "
                    "here is treated as evidence.", "journal_record_ids": _ids(done)})
    # Proposals use the whole forward-paper history of this scope, not one week.
    history = stats.completed(all_time)
    if len(history) >= PROPOSAL_MIN_SAMPLE:
        by_session = stats.breakdown(history, lambda r: r.get("trading_session"))
        for session, s in by_session.items():
            if s["trades"] >= PROPOSAL_MIN_SAMPLE and s["total_r"] is not None and s["total_r"] < 0 \
                    and (s["profit_factor"] or 0) < 0.8:
                proposals.append({
                    "title": f"Consider not taking {scope['label']} entries during {session}",
                    "affected_strategy": scope["strategy_id"],
                    "evidence": {"journal_record_ids": s["journal_record_ids"],
                                 "trades": s["trades"], "total_r": s["total_r"],
                                 "profit_factor": s["profit_factor"],
                                 "period": s["period"]},
                    "expected_benefit": f"removes a segment that returned {s['total_r']:+.2f}R over "
                                        f"{s['trades']} forward-paper trades",
                    "risk": "a session filter also removes that session's future winners; the "
                            "segment may be noise at this sample size",
                    "sample_size": s["trades"],
                })
    return {"facts": facts, "observations": observations, "hypotheses": hypotheses,
            "recommendations": recommendations}, proposals


def build_weekly_review(store: TradeRecordStore, scope: dict, start: datetime,
                        end: datetime, *, version: int = WEEKLY_REVIEW_VERSION,
                        revision: int = 1, supersedes: Optional[str] = None,
                        revision_reason: Optional[str] = None) -> dict:
    records = _records(store, scope, start, end)
    validation = _validate(records)
    reviews = store.reviews_for(_ids(records))
    report = stats.full_report(records, reviews=reviews)
    previous = store.weekly_reviews(agent_id=scope["agent_id"], strategy_id=scope["strategy_id"],
                                    limit=60)
    previous = next((p for p in previous if p["period_end"] == start.isoformat()), None)
    comparison = None
    if previous is not None:
        prev = (previous.get("stats") or {}).get("overall") or {}
        cur = report["overall"]
        comparison = {k: {"this": cur.get(k), "previous": prev.get(k)} for k in (
            "trades", "win_rate", "net_pnl", "total_r", "profit_factor", "average_r",
            "max_drawdown", "rule_compliance")}
        comparison["previous_review_id"] = previous["review_id"]
    all_time = _records(store, scope, None, end)
    findings, proposals = _findings(records, report, previous, reviews, all_time, scope)
    identity = f"{scope['agent_id']}|{scope['strategy_id']}|{start.isoformat()}|{end.isoformat()}|{version}"
    if revision > 1:                     # revision 1 keeps the id it always had
        identity += f"|r{revision}"
    review_id = "wr_" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    for p in proposals:
        p["proposal_id"] = "pp_" + hashlib.sha256(f"{review_id}|{p['title']}".encode()).hexdigest()[:24]
    return {
        "review_id": review_id, "agent_id": scope["agent_id"], "strategy_id": scope["strategy_id"],
        "scope": {"label": scope["label"], "where": scope["where"],
                  "params": list(scope["params"]), "record_origin": "FORWARD_PAPER"},
        "period_start": start.isoformat(), "period_end": end.isoformat(),
        "review_version": version, "generated_at": utcnow(),
        "journal_record_ids": _ids(records), "stats": report, "comparison": comparison,
        "findings": findings, "validation": validation, "proposals": proposals,
        "revision": revision, "supersedes": supersedes, "revision_reason": revision_reason,
    }


def run_weekly(store: TradeRecordStore, *, now: Optional[datetime] = None,
               catch_up_weeks: int = MAX_CATCH_UP_WEEKS) -> dict:
    """Write every missing completed-week review, and revise any whose week
    has gained or lost records since it was written. Idempotent and
    restart-safe.

    A trade can reach the journal after its week was reviewed -- it closed in
    the last seconds of the week, or its source was reconciled after a
    restart. A review written once and never revisited would then leave it out
    for good, and say nothing. Within the catch-up window, each reviewed week's
    record ids are compared with the records it has now; a difference writes
    the next revision, which supersedes the one before.
    """
    now = now or datetime.now(timezone.utc)
    current_start, _ = week_bounds(now)
    done, skipped, revised = [], 0, []
    for scope in review_scopes(store):
        for back in range(catch_up_weeks, 0, -1):
            start = current_start - timedelta(days=7 * back)
            end = start + timedelta(days=7)
            records = _records(store, scope, start, end)
            if not records:
                continue                                 # no empty reviews
            current = store.current_weekly_review(scope["agent_id"], scope["strategy_id"],
                                                  start.isoformat(), end.isoformat())
            if current is not None:
                then, now_ids = set(current["journal_record_ids"]), set(_ids(records))
                if then == now_ids:
                    skipped += 1
                    continue
                added, gone = sorted(now_ids - then), sorted(then - now_ids)
                reason = (f"revision {current['revision']} covered {len(then)} record(s); "
                          f"{len(added)} closed in this week reached the journal after it was "
                          f"written" + (f" and {len(gone)} no longer qualify" if gone else "")
                          + f". Added: {', '.join(added) or 'none'}.")
                review = build_weekly_review(store, scope, start, end,
                                             revision=int(current["revision"]) + 1,
                                             supersedes=current["review_id"],
                                             revision_reason=reason)
                saved = store.save_weekly_review(review)
                store.set_state(f"memory_last_reviewed:{scope['agent_id']}:{scope['strategy_id']}",
                                {"review_id": saved["review_id"], "at": saved["generated_at"]})
                done.append(saved["review_id"])
                revised.append(saved["review_id"])
                continue
            if not store.claim_review_run(scope["agent_id"], scope["strategy_id"],
                                          start.isoformat(), end.isoformat()):
                skipped += 1
                continue
            try:
                review = build_weekly_review(store, scope, start, end)
                saved = store.save_weekly_review(review)
                store.finish_review_run(scope["agent_id"], scope["strategy_id"],
                                        start.isoformat(), end.isoformat(),
                                        review_id=saved["review_id"])
                store.set_state(f"memory_last_reviewed:{scope['agent_id']}:{scope['strategy_id']}",
                                {"review_id": saved["review_id"], "at": saved["generated_at"]})
                done.append(saved["review_id"])
            except Exception as exc:  # noqa: BLE001 -- recorded; retried next pass
                store.finish_review_run(scope["agent_id"], scope["strategy_id"],
                                        start.isoformat(), end.isoformat(),
                                        error=f"{type(exc).__name__}: {exc}")
    return {"written": done, "already_done": skipped, "revised": revised}


class WeeklyReviewScheduler:
    """A durable weekly scheduler: state lives in review_runs, not in a timer."""

    def __init__(self, store: TradeRecordStore, *, interval_s: float = 900.0):
        self.store = store
        self.interval_s = max(30.0, float(interval_s))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None

    def tick(self, now: Optional[datetime] = None) -> dict:
        review_finalized(self.store)
        self.last_result = run_weekly(self.store, now=now)
        return self.last_result

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="weekly-reviews", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.interval_s)

    def status(self) -> dict:
        return {"running": self._thread is not None and self._thread.is_alive(),
                "interval_s": self.interval_s, "last_result": self.last_result,
                "last_error": self.last_error, "week_start_dow": week_start_dow(),
                "runs": self.store.review_runs(limit=20)}
