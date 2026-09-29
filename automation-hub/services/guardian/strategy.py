"""Strategy telemetry: decision traces, rejection reasons and almost-trades
(PRD §8, §9, §37; Phase 2).

Three producers, one trace shape:

* **Trading Instances** publish a trace per closed candle from the engine
  (``services/strategy_trace.py``).
* **SMC lab** -- the agent journal records one decision per closed candle
  with the SMC strategy's own ordered condition results
  (``agent_decisions``). Guardian reads them.
* **Price Action lab** -- the lab records one evaluation per closed candle
  with the frozen engine's trace, and advances it through risk and the
  order lifecycle (``pa_evaluations``). Guardian reads them.

Guardian reads the lab databases through SQLite read-only connections
(``mode=ro``): the database itself refuses a write, so nothing here can alter a
lab's records. Reads are incremental and every event gets an id derived from
the row it came from, so re-reading after a restart adds nothing twice.

An **almost-trade** is decided from structured states only:

* the strategy's own conditions were all evaluated, all but exactly one
  passed, at least two passed, and the one that failed was a setup condition
  (not warm-up or data); or
* the strategy's setup was complete and exactly one gate refused it.

It is recorded as a MISSED OPPORTUNITY CANDIDATE with the PRD's warning
attached. It is never a verdict that a rule is wrong (PRD §9).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Iterable, Optional

from services.guardian.schema import GuardianEvent, make_event, utcnow

STRATEGY_EVENTS = ("evaluation_completed", "setup_detected", "setup_rejected")
PASS, FAIL = "PASS", "FAIL"
NOT_REQUIRED, NOT_REACHED, UNATTRIBUTED = "NOT_REQUIRED", "NOT_REACHED", "UNATTRIBUTED"

CLASSIFICATION = "MISSED_OPPORTUNITY_CANDIDATE"
NOTE = ("Most conditions passed and one prevented the trade. This does NOT mean the "
        "rule was wrong. It is a candidate for research analysis only; production "
        "rules are never changed from it.")
#: Refusals that are not a missed opportunity: the trade already exists, or
#: the signal repeats one already acted on.
_NOT_AN_OPPORTUNITY = frozenset({"DUPLICATE_SIGNAL", "ORDER_PENDING", "POSITION_ALREADY_ALIGNED",
                                 "POSITION_MANAGED", "EXISTING_EXPOSURE"})
#: A condition that failed this way cannot come back: not "one away".
_TERMINAL = frozenset({"INVALIDATED", "EXPIRED"})
_EARLY_STAGES = frozenset({"MARKET_DATA", "FEATURES"})

_NAMESPACE = uuid.UUID("4f1c9a52-6a8b-4f6e-9d57-7c0f3e1a9b21")


def event_key(*parts: Any) -> str:
    """A stable event id for an observation of a stored row."""
    return uuid.uuid5(_NAMESPACE, "|".join(str(p) for p in parts)).hex


def keyed(event: GuardianEvent, *parts: Any) -> GuardianEvent:
    from dataclasses import replace
    return replace(event, event_id=event_key(*parts))


# ------------------------------------------------------------ almost-trades
def almost_trade(trace: dict) -> Optional[dict]:
    """The almost-trade classification of one trace, or None."""
    if not isinstance(trace, dict):
        return None
    final = str(trace.get("final") or "")
    conditions = [c for c in trace.get("conditions") or [] if c.get("kind") == "strategy"]
    counted = [c for c in conditions if c.get("state") in (PASS, FAIL)]
    passed = [c for c in counted if c["state"] == PASS]
    failed = [c for c in counted if c["state"] == FAIL]
    blocking = [b for b in trace.get("blocking") or [] if isinstance(b, dict)]
    if final == "NO_SETUP":
        if len(failed) != 1 or len(passed) < 2:
            return None
        if any(c.get("state") in (NOT_REACHED, UNATTRIBUTED) for c in conditions):
            return None                      # something after the failure was never judged
        miss = failed[0]
        if miss.get("stage") in _EARLY_STAGES or miss.get("code") in _TERMINAL:
            return None
        kind = "ONE_CONDITION_SHORT"
        prevented = {"condition": miss.get("label") or miss.get("id"), "code": miss.get("code"),
                     "detail": miss.get("detail")}
    elif final in ("REJECTED", "MISSED"):
        if failed or any(c.get("state") not in (PASS, NOT_REQUIRED) for c in conditions):
            return None                      # the setup itself was not complete
        if not passed or len(blocking) != 1:
            return None
        block = blocking[0]
        if str(block.get("code") or "") in _NOT_AN_OPPORTUNITY:
            return None
        kind = "VALID_SETUP_NOT_ACTED_ON" if final == "MISSED" else "VALID_SETUP_REFUSED"
        prevented = {"condition": block.get("id"), "code": block.get("code"),
                     "detail": block.get("detail")}
    else:
        return None
    return {"classification": CLASSIFICATION, "kind": kind, "passed": len(passed),
            "evaluated": len(counted) + (1 if final in ("REJECTED", "MISSED") else 0),
            "prevented_by": prevented, "note": NOTE}


def setup_identity(event: dict, trace: dict) -> str:
    """One almost-trade per setup, not one per candle it stayed one away."""
    ref = trace.get("setup_ref") or trace.get("candle_time") or event.get("timestamp")
    return event_key("almost", event.get("source_component"), event.get("strategy_id"),
                     event.get("symbol"), trace.get("direction"), ref)


# ------------------------------------------------------------------- SMC lab
_SMC_FINAL = {"TAKEN": "ENTERED", "REJECTED": "REJECTED", "NOT_READY": "NO_SETUP",
              "MISSED": "MISSED", "EXECUTION_FAILED": "ERROR",
              "EXECUTION_UNCERTAIN": "EXECUTION_UNCERTAIN", "RECONCILED": "RECONCILED"}


def _load(value: Any) -> Any:
    if isinstance(value, (str, bytes)) and value:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _missing_code(condition: dict) -> str:
    """MISSING_REJECTION, INVALIDATED_POI: the state and the condition."""
    key = "".join(ch if ch.isalnum() else "_" for ch in str(condition.get("id") or "").upper()).strip("_")
    return f"{condition.get('code') or 'MISSING'}_{key}" if key else str(condition.get("code") or "MISSING")


def smc_trace(row: dict) -> dict:
    """The trace of one SMC agent decision (services/smc_agent_journal.py)."""
    conditions = []
    for c in _load(row.get("conditions_json")) or []:
        if not isinstance(c, dict):
            continue
        status = str(c.get("status") or "").upper()
        state = PASS if status == PASS else NOT_REQUIRED if status == NOT_REQUIRED else FAIL
        conditions.append({"id": c.get("key") or c.get("label"), "label": c.get("label") or c.get("key"),
                           "stage": "SETUP", "kind": "strategy", "state": state,
                           "code": None if state != FAIL else status, "detail": c.get("detail")})
    gates = [g for g in _load(row.get("gates_json")) or [] if isinstance(g, dict)]
    for g in gates:
        conditions.append({"id": g.get("name"), "label": g.get("name"), "stage": "AGENT_GATE",
                           "kind": "gate", "state": PASS if g.get("passed") else FAIL,
                           "code": None if g.get("passed") else str(g.get("name") or "").upper(),
                           "detail": g.get("detail")})
    outcome = str(row.get("outcome") or "")
    final = _SMC_FINAL.get(outcome, outcome or "UNKNOWN")
    if outcome == "REJECTED":
        blocking = [{"id": g.get("name"), "code": str(g.get("name") or "").upper(),
                     "detail": g.get("detail")} for g in gates if not g.get("passed")]
    elif outcome in ("MISSED", "EXECUTION_FAILED", "EXECUTION_UNCERTAIN"):
        blocking = [{"id": "agent", "code": row.get("reason_code"), "detail": row.get("reason")}]
    elif outcome == "NOT_READY":
        blocking = [{"id": c["id"], "code": _missing_code(c), "detail": c.get("detail")}
                    for c in conditions if c["state"] == FAIL and c["kind"] == "strategy"]
    else:
        blocking = []
    plan = _load(row.get("plan_json")) or {}
    # The reason a setup did not form is the first condition it was missing;
    # "SMC_NOT_READY" alone says nothing about which.
    code = blocking[0]["code"] if outcome == "NOT_READY" and blocking else row.get("reason_code")
    return {"v": 1, "final": final, "direction": plan.get("direction") if isinstance(plan, dict) else None,
            "blocker_code": code, "reason": row.get("reason"),
            "smc_state": row.get("smc_state"), "candle_time": row.get("candle_time") or None,
            "setup_ref": row.get("setup_id") or row.get("proposal_id") or row.get("candle_time"),
            "conditions": conditions, "blocking": blocking,
            "strategy_fingerprint": row.get("strategy_fingerprint") or None,
            "source": {"kind": "smc_agent_journal", "decision_id": row.get("id"),
                       "trade_id": row.get("trade_id") or None}}


# ----------------------------------------------------------- Price Action lab
_PA_FINAL = {"WATCHING": "NO_SETUP", "NO_ZONE": "NO_SETUP", "SIGNAL_FOUND": "SIGNAL",
             "RISK_REJECTED": "REJECTED", "ORDER_SUBMITTED": "ORDER_PENDING",
             "FILLED": "ENTERED", "POSITION_OPEN": "ENTERED", "EXITED": "EXITED"}


def pa_traces(row: dict) -> list[tuple[str, str, dict]]:
    """Every state one PA evaluation has been through: its first decision and
    each later lifecycle step (price_action_lab.py keeps them in the payload).
    Returns (state key, timestamp, trace)."""
    payload = _load(row.get("payload_json")) or {}
    trace_in = payload.get("trace") or {}
    conditions = []
    for c in trace_in.get("conditions") or []:
        if not isinstance(c, dict):
            continue
        ok = str(c.get("status") or "").upper() == PASS
        conditions.append({"id": c.get("key"), "label": str(c.get("key") or "").replace("_", " "),
                           "stage": "SETUP", "kind": "strategy", "state": PASS if ok else FAIL,
                           "code": None if ok else str(c.get("status") or "MISSING").upper(),
                           "detail": c.get("detail")})
    direction = trace_in.get("direction")
    first_state = "SIGNAL_FOUND" if payload.get("proposal_ids") else "WATCHING"
    setup_ref = trace_in.get("setup_id") or event_key(
        "pa-setup", row.get("strategy_id"), direction,
        *sorted(str(x) for x in trace_in.get("supporting_object_ids") or []))
    base = {"v": 1, "direction": direction, "candle_time": row.get("candle_time"),
            "setup_ref": setup_ref, "conditions": conditions,
            "pa_trace_state": trace_in.get("state"),
            "next_required_event": trace_in.get("next_required_event"),
            "feed_state": payload.get("feed_state"),
            "source": {"kind": "pa_evaluations", "correlation_id": row.get("correlation_id"),
                       "proposal_ids": payload.get("proposal_ids") or []}}

    def one(state: str, reason: Optional[str]) -> dict:
        final = _PA_FINAL.get(state, state)
        if final == "NO_SETUP" and trace_in.get("state") == "ORDER_PENDING":
            final = "SETUP_PENDING"          # formed; waiting for the confirmation stop
        missing = [c for c in conditions if c["state"] == FAIL]
        blocking = ([{"id": "risk", "code": "RISK_REJECTED", "detail": reason}] if final == "REJECTED"
                    else [{"id": c["id"], "code": _missing_code(c), "detail": c.get("detail")} for c in missing]
                    if final == "NO_SETUP" else [])
        return {**base, "final": final, "state": state, "reason": reason,
                "blocker_code": blocking[0]["code"] if blocking else None, "blocking": blocking}

    out = [(f"{first_state}#0", str(row.get("created_at") or row.get("candle_time")),
            one(first_state, row.get("reason") if not payload.get("lifecycle") else None))]
    for i, step in enumerate(payload.get("lifecycle") or [], start=1):
        if isinstance(step, dict) and step.get("state"):
            out.append((f"{step['state']}#{i}", str(step.get("at") or row.get("updated_at")),
                        one(str(step["state"]), step.get("reason"))))
    return out


_EVENT_FOR = {"ENTERED": "setup_detected", "ORDER_PENDING": "setup_detected",
              "SIGNAL": "setup_detected", "SETUP_PENDING": "setup_detected",
              "REJECTED": "setup_rejected", "MISSED": "setup_rejected"}


def trace_event(trace: dict, *, source_service: str, source_component: str, timestamp: str,
                **fields: Any) -> GuardianEvent:
    final = trace.get("final")
    return make_event(_EVENT_FOR.get(final, "evaluation_completed"), source_service=source_service,
                      source_component=source_component, timestamp=timestamp,
                      severity="WARNING" if final in ("ERROR", "EXECUTION_UNCERTAIN") else "INFO",
                      decision=final, reason=trace.get("reason") or trace.get("blocker_code"),
                      evidence=trace, metadata={"blocker_code": trace.get("blocker_code"),
                                                "trace_source": (trace.get("source") or {}).get("kind")},
                      **fields)


# ------------------------------------------------------------------- readers
def read_only(path: str) -> sqlite3.Connection:
    """A connection the database itself will refuse to write through."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


class LabTraceReader:
    """Incremental, read-only reader over one lab's decision table."""

    def __init__(self, lab_id: str, path: str, *, table: str):
        self.lab_id, self.path, self.table = lab_id, str(path), table

    def _rows(self, sql: str, args: Iterable) -> list[dict]:
        conn = read_only(self.path)
        try:
            return [dict(r) for r in conn.execute(sql, tuple(args))]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []                    # the lab has not written its first row yet
            raise
        finally:
            conn.close()


class SMCDecisionReader(LabTraceReader):
    """``agent_decisions`` rows are only ever inserted, so the rowid is a cursor."""

    def __init__(self, path: str, *, lab_id: str = "smc"):
        super().__init__(lab_id, path, table="agent_decisions")

    def read(self, mark: Optional[dict], limit: int) -> tuple[list[GuardianEvent], dict]:
        after = int((mark or {}).get("rowid") or 0)
        rows = self._rows("SELECT rowid AS _rowid, * FROM agent_decisions WHERE rowid > ? "
                          "ORDER BY rowid LIMIT ?", (after, int(limit)))
        events = []
        for row in rows:
            trace = smc_trace(row)
            event = trace_event(trace, source_service="smc_lab", source_component=f"lab:{self.lab_id}",
                                timestamp=row.get("at"), lab_id=self.lab_id, symbol=row.get("symbol"),
                                timeframe=row.get("timeframe"),
                                # The agent acts only on SMC_SOURCE_V1 (services/smc_agent.py);
                                # a row refusing any other source names no strategy.
                                strategy_id=(None if row.get("reason_code") == "NOT_AN_SMC_SIGNAL"
                                             else "SMC_SOURCE_V1"),
                                strategy_version=row.get("strategy_fingerprint") or None,
                                correlation_id=row.get("proposal_id") or row.get("id"))
            events.append(keyed(event, "smc", self.path, row["id"]))
        return events, {"rowid": rows[-1]["_rowid"] if rows else after}


class PAEvaluationReader(LabTraceReader):
    """``pa_evaluations`` rows advance in place, so the cursor is the
    (updated_at, rowid) pair and each state they pass through is one event."""

    def __init__(self, path: str, *, lab_id: str = "pa"):
        super().__init__(lab_id, path, table="pa_evaluations")

    def read(self, mark: Optional[dict], limit: int) -> tuple[list[GuardianEvent], dict]:
        at, rid = (mark or {}).get("updated_at") or "", int((mark or {}).get("rowid") or 0)
        rows = self._rows(
            "SELECT rowid AS _rowid, * FROM pa_evaluations WHERE updated_at > ? "
            "OR (updated_at = ? AND rowid > ?) ORDER BY updated_at, rowid LIMIT ?",
            (at, at, rid, int(limit)))
        events = []
        for row in rows:
            for key, stamp, trace in pa_traces(row):
                event = trace_event(trace, source_service="price_action_lab",
                                    source_component=f"lab:{self.lab_id}", timestamp=stamp,
                                    lab_id=self.lab_id, symbol=row.get("symbol"),
                                    timeframe=row.get("timeframe"), strategy_id=row.get("strategy_id"),
                                    strategy_version=row.get("strategy_version"),
                                    session_id=row.get("session_id"),
                                    correlation_id=row.get("correlation_id"))
                events.append(keyed(event, "pa", self.path, row["correlation_id"], key))
        mark_out = ({"updated_at": rows[-1]["updated_at"], "rowid": rows[-1]["_rowid"]} if rows
                    else {"updated_at": at, "rowid": rid})
        return events, mark_out


class StrategyTelemetry:
    """Runs on Guardian's own thread: reads each lab's new decisions and
    writes them straight to Guardian's store (no queue to overflow on a
    backlog). A lab that cannot be read is reported, never guessed at."""

    def __init__(self, store, readers: Iterable[LabTraceReader], *, batch: int = 500):
        self.store = store
        self.readers = list(readers)
        self.batch = int(batch)
        self.last: dict[str, dict] = {}

    def poll(self) -> dict[str, dict]:
        out = {}
        for reader in self.readers:
            key = f"telemetry.{reader.lab_id}.{reader.table}"
            mark = self.store.meta(key)
            try:
                events, new_mark = reader.read(mark, self.batch)
                written = self.store.append_events(events) if events else 0
                self.store.set_meta(key, new_mark)   # only after the write succeeded
                out[reader.lab_id] = {"ok": True, "read": len(events), "written": written,
                                      "backlog": len(events) >= self.batch, "at": utcnow()}
            except Exception as exc:  # noqa: BLE001 -- one lab cannot blind the others
                out[reader.lab_id] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                                      "at": utcnow()}
        self.last = out
        return out


# --------------------------------------------------------------- performance
def strategy_performance(path: Optional[str]) -> list[dict]:
    """Closed-trade results per strategy from the journal (PRD §37), read-only.
    Every figure is computed from finished trade records; nothing is estimated."""
    if not path:
        return []
    conn = read_only(path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT record_source, record_origin, strategy_id, strategy_version, instance_id, lab_id, "
            "net_pnl, realized_r, outcome, position_closed_at FROM trade_records "
            "WHERE status='CLOSED' ORDER BY position_closed_at")]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    finally:
        conn.close()
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["record_source"], row["record_origin"], row["strategy_id"] or "",
               row["instance_id"] or "", row["lab_id"] or "")
        groups.setdefault(key, []).append(row)
    out = []
    for (source, origin, strategy, instance, lab), trades in groups.items():
        pnl = [float(t["net_pnl"]) for t in trades if t["net_pnl"] is not None]
        r = [float(t["realized_r"]) for t in trades if t["realized_r"] is not None]
        wins = sum(1 for x in pnl if x > 0)
        losses = sum(1 for x in pnl if x < 0)
        gains, pains = sum(x for x in pnl if x > 0), -sum(x for x in pnl if x < 0)
        peak = cum = worst = 0.0
        for x in r:
            cum += x
            peak = max(peak, cum)
            worst = min(worst, cum - peak)
        out.append({"record_source": source, "record_origin": origin, "strategy_id": strategy or None,
                    "instance_id": instance or None, "lab_id": lab or None,
                    "trades": len(trades), "wins": wins, "losses": losses,
                    "net_pnl": round(sum(pnl), 2) if pnl else None,
                    # per trade, in the account currency and in R
                    "expectancy": round(sum(pnl) / len(pnl), 2) if pnl else None,
                    "average_r": round(sum(r) / len(r), 3) if r else None,
                    "profit_factor": round(gains / pains, 2) if pains > 0 else None,
                    "max_drawdown_r": round(worst, 3) if r else None,
                    "r_measured": len(r), "last_closed_at": trades[-1]["position_closed_at"]})
    return out


# ------------------------------------------------------------------ overview
_SETUP_FINALS = frozenset({"ENTERED", "ORDER_PENDING", "APPROVAL_REQUIRED", "SIGNAL_ONLY",
                           "SIGNAL", "SETUP_PENDING"})
_REFUSED_FINALS = frozenset({"REJECTED", "MISSED"})
_LAB_SOURCE = {"lab:smc": "SMC_LAB", "lab:pa": "PA_LAB"}


def _performance_for(scope: str, instance_id: Optional[str], rows: list[dict]) -> list[dict]:
    if instance_id:
        return [r for r in rows if r.get("instance_id") == instance_id]
    source = _LAB_SOURCE.get(scope)
    return [r for r in rows if source and r.get("record_source") == source]


def strategy_overview(store, *, since_day: str, performance: Optional[list[dict]] = None,
                      telemetry: Optional[dict] = None) -> dict:
    """What every strategy attempted, and why it did or did not trade (§37)."""
    cards: dict[tuple, dict] = {}
    for row in store.strategy_rollup(since_day=since_day):
        key = (row["source_component"], row["strategy_id"], row["symbol"], row["timeframe"])
        card = cards.setdefault(key, {
            "scope": row["source_component"], "lab_id": row["lab_id"],
            "instance_id": row["instance_id"], "strategy_id": row["strategy_id"] or None,
            "strategy_version": row["strategy_version"], "symbol": row["symbol"] or None,
            "timeframe": row["timeframe"] or None, "evaluations": 0, "setups": 0, "entries": 0,
            "refused": 0, "no_setup": 0, "decisions": {}, "_reasons": {},
            "last_evaluation_at": None})
        n = int(row["count"])
        final = row["decision"]
        card["evaluations"] += n
        card["decisions"][final] = card["decisions"].get(final, 0) + n
        card["setups"] += n if final in _SETUP_FINALS else 0
        card["entries"] += n if final == "ENTERED" else 0
        card["refused"] += n if final in _REFUSED_FINALS else 0
        card["no_setup"] += n if final == "NO_SETUP" else 0
        if row["blocker_code"] and (final in _REFUSED_FINALS or final == "NO_SETUP"):
            reason = (final, row["blocker_code"])
            card["_reasons"][reason] = card["_reasons"].get(reason, 0) + n
        card["last_evaluation_at"] = max(filter(None, (card["last_evaluation_at"], row["last_at"])))
        if row["strategy_version"]:
            card["strategy_version"] = row["strategy_version"]
    since = f"{since_day}T00:00:00"
    almost_rows = store.almost_trades(since=since, limit=500)
    perf = performance or []
    out = []
    for card in cards.values():
        reasons = sorted(card.pop("_reasons").items(), key=lambda kv: -kv[1])
        card["top_rejection_reasons"] = [{"decision": d, "code": c, "count": n}
                                         for (d, c), n in reasons[:6]]
        card["almost_trades"] = sum(1 for a in almost_rows
                                    if a["source_component"] == card["scope"]
                                    and (a["strategy_id"] or None) == card["strategy_id"]
                                    and (a["symbol"] or None) == card["symbol"])
        card["performance"] = _performance_for(card["scope"], card["instance_id"], perf)
        out.append(card)
    out.sort(key=lambda c: (c["scope"], c["strategy_id"] or "", c["symbol"] or ""))
    return {"since_day": since_day, "strategies": out, "telemetry": telemetry or {},
            "almost_trades_total": len(almost_rows),
            "research": {"built": True,
                         "note": "Research hypotheses from finished forward-paper trades are on the "
                                 "Research tab. A hypothesis is an idea under test: it never changes "
                                 "a strategy."}}
