"""Journal API over the canonical trade and decision records.

Registered before routers/journal.py, whose ``GET /journal/{trade_id}`` would
otherwise capture these paths. Reads are open to a signed-in session like the
rest of the Journal; every write (notes, proposal decisions, a manual
reconcile) needs the control credential the session bridge supplies, and a
proposal decision records who made it.

Statistics come from services/journal_stats.py. Forward-paper records are the
default view: backtest, research, simulation and legacy records appear only
when a request names their origin.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

import webhook_api as _wa
from services import journal_stats as stats
from services.journal_memory import evidence as memory_evidence
from services.journal_memory import memory as memory_view
from services.journal_reviews import review_scopes, week_bounds, _records as scope_records

router = APIRouter()

_LIST_FIELDS = (
    "journal_record_id", "execution_key", "record_source", "record_origin", "verification",
    "operating_mode", "data_completeness", "status", "outcome", "trade_id", "instance_id",
    "lab_id", "agent_id", "strategy_id", "strategy_name", "strategy_version", "symbol",
    "timeframe", "side", "trading_session", "setup_type", "market_regime",
    "signal_detected_at", "position_opened_at", "position_closed_at",
    "planned_entry", "planned_stop_loss", "planned_take_profit", "planned_rr",
    "risk_amount", "risk_percent", "actual_entry", "actual_exit", "exit_reason",
    "net_pnl", "fees", "realized_r", "achieved_rr", "trade_duration_s", "execution_status",
)


def _store():
    store = getattr(_wa, "trade_records", None)
    if store is None:
        raise HTTPException(503, "trade record store is not available")
    return store


def _where(*, origin: Optional[str], source: Optional[str], instance_id: Optional[str],
           lab_id: Optional[str], strategy: Optional[str], symbol: Optional[str],
           timeframe: Optional[str], side: Optional[str], session: Optional[str],
           mode: Optional[str], outcome: Optional[str], status: Optional[str],
           reviewed: Optional[str], date_from: Optional[str], date_to: Optional[str],
           ids: Optional[str]) -> tuple[str, list]:
    cond, args = [], []
    if ids:
        wanted = [i for i in ids.split(",") if i][:1000]
        cond.append(f"journal_record_id IN ({','.join('?' * len(wanted))})")
        args.extend(wanted)
    elif origin and origin.lower() != "all":
        cond.append("record_origin=?"); args.append(origin.upper())
    elif not origin:
        cond.append("record_origin='FORWARD_PAPER'")
    for column, value in (("record_source", source), ("instance_id", instance_id),
                          ("lab_id", lab_id), ("timeframe", timeframe),
                          ("trading_session", session)):
        if value:
            cond.append(f"{column}=?"); args.append(value if column != "record_source" else value.upper())
    if strategy:
        cond.append("(strategy_id=? OR strategy_name=?)"); args.extend((strategy, strategy))
    if symbol:
        cond.append("symbol=?"); args.append(symbol.upper())
    if side:
        cond.append("side=?"); args.append(side.lower())
    if mode:
        cond.append("LOWER(operating_mode)=?"); args.append(mode.lower())
    if outcome:
        cond.append("outcome=?"); args.append(outcome.upper())
    if status:
        cond.append("status=?"); args.append(status.upper())
    if reviewed in ("yes", "true", "1"):
        cond.append("journal_record_id IN (SELECT journal_record_id FROM trade_reviews)")
    elif reviewed in ("no", "false", "0"):
        cond.append("journal_record_id NOT IN (SELECT journal_record_id FROM trade_reviews)")
    stamp = "COALESCE(position_opened_at, decision_created_at, created_at)"
    if date_from:
        cond.append(f"{stamp} >= ?"); args.append(date_from)
    if date_to:
        cond.append(f"{stamp} <= ?"); args.append(date_to)
    return " AND ".join(cond), args


@router.get("/journal/records")
def journal_records(
        limit: int = 100, offset: int = 0, origin: Optional[str] = None,
        source: Optional[str] = None, instance_id: Optional[str] = None,
        lab_id: Optional[str] = None, strategy: Optional[str] = None,
        symbol: Optional[str] = None, timeframe: Optional[str] = None,
        side: Optional[str] = None, session: Optional[str] = None,
        mode: Optional[str] = None, outcome: Optional[str] = None,
        status: Optional[str] = None, reviewed: Optional[str] = None,
        date_from: Optional[str] = None, date_to: Optional[str] = None,
        ids: Optional[str] = None):
    """Canonical trade records (newest first) and KPIs over every match."""
    store = _store()
    where, args = _where(origin=origin, source=source, instance_id=instance_id, lab_id=lab_id,
                         strategy=strategy, symbol=symbol, timeframe=timeframe, side=side,
                         session=session, mode=mode, outcome=outcome, status=status,
                         reviewed=reviewed, date_from=date_from, date_to=date_to, ids=ids)
    page = store.query_trades(where=where, params=args, limit=max(1, min(limit, 500)),
                              offset=max(0, offset))
    everything = store.query_trades(where=where, params=args, limit=200000)
    reviews = store.reviews_for([r["journal_record_id"] for r in everything])
    rows = []
    for r in page:
        row = {k: r.get(k) for k in _LIST_FIELDS}
        review = reviews.get(r["journal_record_id"])
        row["reviewed"] = review is not None
        row["compliance"] = (review or {}).get("strategy_compliance")
        rows.append(row)
    return {"records": rows, "total": len(everything),
            "kpis": stats.kpis(everything, reviews=reviews),
            "origin": (origin or "FORWARD_PAPER").upper(),
            "note": "Statistics use completed trades only; — means there is nothing to compute from."}


@router.get("/journal/records/facets")
def journal_record_facets():
    store = _store()
    out = {}
    with store._lock:
        for column in ("record_source", "record_origin", "strategy_name", "symbol", "timeframe",
                       "side", "trading_session", "instance_id", "lab_id", "operating_mode"):
            out[column] = [r[0] for r in store._c.execute(
                f"SELECT DISTINCT {column} FROM trade_records WHERE {column} IS NOT NULL "
                f"ORDER BY {column}")]
        out["counts"] = {r[0]: r[1] for r in store._c.execute(
            "SELECT record_origin, COUNT(*) FROM trade_records GROUP BY record_origin")}
    return out


@router.get("/journal/records/{record_id}")
def journal_record(record_id: str):
    record = _store().get(record_id)
    if record is None:
        raise HTTPException(404, "No trade record with that id")
    return record


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=20)


@router.post("/journal/records/{record_id}/notes")
def add_record_note(record_id: str, body: NoteBody, request: Request,
                    x_webhook_secret: Optional[str] = Header(default=None)):
    """A note sits beside the record; it never edits a fact."""
    _wa._check_secret(x_webhook_secret)
    store = _store()
    record = store.get(record_id)
    if record is None:
        raise HTTPException(404, "No trade record with that id")
    return store.add_note(record["journal_record_id"], body.text, author=_wa.request_user(request),
                          tags=body.tags)


@router.post("/journal/notes")
def add_note(body: NoteBody, request: Request,
             x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    return _store().add_note(None, body.text, author=_wa.request_user(request), tags=body.tags)


@router.get("/journal/notes")
def list_notes(limit: int = 200):
    return {"notes": _store().notes(limit=max(1, min(limit, 1000)))}


# ------------------------------------------------------------------ decisions
@router.get("/journal/decision-records")
def decision_records(limit: int = 100, offset: int = 0, source: Optional[str] = None,
                     decision_type: Optional[str] = None, symbol: Optional[str] = None,
                     instance_id: Optional[str] = None, strategy: Optional[str] = None,
                     traded: Optional[str] = None, date_from: Optional[str] = None,
                     date_to: Optional[str] = None):
    """Material decisions: signals and why they did or did not become trades."""
    cond, args = [], []
    if source:
        cond.append("record_source=?"); args.append(source.upper())
    if decision_type:
        cond.append("decision_type=?"); args.append(decision_type.upper())
    if symbol:
        cond.append("symbol=?"); args.append(symbol.upper())
    if instance_id:
        cond.append("instance_id=?"); args.append(instance_id)
    if strategy:
        cond.append("(strategy_id=? OR strategy_name=?)"); args.extend((strategy, strategy))
    if traded in ("yes", "true"):
        cond.append("journal_record_id IS NOT NULL")
    elif traded in ("no", "false"):
        cond.append("journal_record_id IS NULL")
    if date_from:
        cond.append("decided_at >= ?"); args.append(date_from)
    if date_to:
        cond.append("decided_at <= ?"); args.append(date_to)
    where = " AND ".join(cond)
    store = _store()
    rows = store.query_decisions(where=where, params=args, limit=max(1, min(limit, 500)),
                                 offset=max(0, offset))
    with store._lock:
        types = {r[0]: r[1] for r in store._c.execute(
            "SELECT decision_type, COUNT(*) FROM decision_records"
            + (f" WHERE {where}" if where else "") + " GROUP BY decision_type", args)}
    return {"decisions": rows, "total": store.count_decisions(where=where, params=args),
            "by_type": types}


@router.get("/journal/decision-records/{decision_id}")
def decision_record(decision_id: str):
    store = _store()
    row = store.get_decision(decision_id)
    if row is None:
        raise HTTPException(404, "No decision record with that id")
    if row.get("journal_record_id"):
        row["trade"] = store.get(row["journal_record_id"])
    return row


# --------------------------------------------------------------------- memory
@router.get("/journal/memory")
def journal_memory():
    return memory_view(_store(), getattr(_wa, "decision_journal_store", None))


@router.get("/journal/memory/evidence")
def journal_memory_evidence(setup_key: str):
    found = memory_evidence(_store(), setup_key, getattr(_wa, "decision_journal_store", None))
    if found is None:
        raise HTTPException(404, "No memory entry with that key")
    return found


# -------------------------------------------------------------- weekly reviews
@router.get("/journal/weekly")
def weekly_reviews(agent_id: Optional[str] = None, strategy_id: Optional[str] = None,
                   limit: int = 52):
    store = _store()
    scopes = [{k: s[k] for k in ("agent_id", "strategy_id", "label")} for s in review_scopes(store)]
    reviews = store.weekly_reviews(agent_id=agent_id, strategy_id=strategy_id,
                                   limit=max(1, min(limit, 200)))
    for r in reviews:
        r.pop("stats", None)          # the list stays light; the detail has everything
    scheduler = getattr(_wa, "weekly_review_scheduler", None)
    return {"scopes": scopes, "reviews": reviews,
            "scheduler": scheduler.status() if scheduler is not None else None,
            "pending_proposals": store.proposals(status="PENDING_APPROVAL", limit=50)}


@router.get("/journal/weekly/overview")
def weekly_overview(agent_id: str, strategy_id: str):
    """This week so far, the previous week and a four-week trend, computed now."""
    store = _store()
    scope = next((s for s in review_scopes(store)
                  if s["agent_id"] == agent_id and s["strategy_id"] == strategy_id), None)
    if scope is None:
        raise HTTPException(404, "No review scope with that agent and strategy")
    start, end = week_bounds(datetime.now(timezone.utc))
    weeks = []
    for back in range(0, 4):
        ws, we = start - timedelta(days=7 * back), end - timedelta(days=7 * back)
        rows = scope_records(store, scope, ws, we)
        reviews = store.reviews_for([r["journal_record_id"] for r in rows])
        weeks.append({"period_start": ws.isoformat(), "period_end": we.isoformat(),
                      "in_progress": back == 0,
                      "stats": stats.full_report(rows, reviews=reviews)})
    return {"scope": {k: scope[k] for k in ("agent_id", "strategy_id", "label")},
            "this_week": weeks[0], "previous_week": weeks[1], "trend": list(reversed(weeks))}


@router.get("/journal/weekly/{review_id}")
def weekly_review(review_id: str):
    review = _store().weekly_review(review_id)
    if review is None:
        raise HTTPException(404, "No weekly review with that id")
    return review


@router.post("/journal/weekly/run")
def run_weekly_now(x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    scheduler = getattr(_wa, "weekly_review_scheduler", None)
    if scheduler is None:
        raise HTTPException(503, "weekly review scheduler is not available")
    return scheduler.tick()


# ------------------------------------------------------------------ proposals
@router.get("/journal/proposals")
def proposals(status: Optional[str] = None):
    return {"proposals": _store().proposals(status=status.upper() if status else None)}


class ProposalDecision(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=2000)


@router.post("/journal/proposals/{proposal_id}/decision")
def decide_proposal(proposal_id: str, body: ProposalDecision, request: Request,
                    x_webhook_secret: Optional[str] = Header(default=None)):
    """A person approves or rejects. Approval records the decision; it applies nothing."""
    _wa._check_secret(x_webhook_secret)
    try:
        return _store().decide_proposal(proposal_id, approve=body.approve,
                                        actor=_wa.request_user(request), note=body.note)
    except KeyError:
        raise HTTPException(404, "No proposal with that id") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


# ------------------------------------------------------------------- recorder
@router.get("/journal/recorder")
def recorder_status():
    recorder = getattr(_wa, "journal_recorder", None)
    if recorder is None:
        raise HTTPException(503, "journal recorder is not available")
    return {**recorder.status(), "last_report": recorder.last_report}


@router.post("/journal/recorder/reconcile")
def recorder_reconcile(x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    recorder = getattr(_wa, "journal_recorder", None)
    if recorder is None:
        raise HTTPException(503, "journal recorder is not available")
    return recorder.reconcile()
