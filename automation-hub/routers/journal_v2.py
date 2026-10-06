"""Canonical Trade Journal API (``/journal/v2/*``).

Every statistic is computed server-side by ``services.journal_analytics``; the
frontend renders, it does not calculate. Trading modes are never mixed unless
the caller explicitly asks for several modes or ``ALL`` — when no mode is
given the server picks ONE mode and says which in ``modes_applied``.

Reads are open to any authenticated session (the app's auth wall applies);
writes (notes, corrections, reviews, sync, weekly-review generation) require
the control credential like every other journal write.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import webhook_api as _wa
from data.trade_journal_store import (
    CORRECTABLE_FIELDS, OPERATIONAL_RESULTS, TRADE_COLUMNS, TRADING_MODES, TRADING_RESULTS,
)
from services import journal_analytics as analytics
from services.journal_review import validate_external_review
from services.journal_sessions import (
    SESSION_LABELS, SESSIONS, duration_display, iso_week_key, london_display, week_bounds,
)
from services.trade_journal import EXIT_REASONS

router = APIRouter(prefix="/journal/v2", tags=["journal-v2"])

#: Default and optional table columns. The UI lets the operator choose.
COLUMNS = (
    {"key": "entry_filled_at", "label": "Date", "default": True},
    {"key": "trade_ref", "label": "Trade ID", "default": False},
    {"key": "strategy_name", "label": "Strategy", "default": True},
    {"key": "instance", "label": "Instance / Lab", "default": True},
    {"key": "symbol", "label": "Symbol", "default": True},
    {"key": "direction", "label": "Side", "default": True},
    {"key": "timeframe", "label": "Timeframe", "default": True},
    {"key": "entry_session", "label": "Session", "default": True},
    {"key": "entry_price", "label": "Entry", "default": True},
    {"key": "exit_price", "label": "Exit", "default": True},
    {"key": "quantity", "label": "Qty", "default": True},
    {"key": "leverage", "label": "Leverage", "default": True},
    {"key": "risk_pct", "label": "Risk %", "default": True},
    {"key": "planned_rr", "label": "Planned RR", "default": True},
    {"key": "realised_r", "label": "Realised R", "default": True},
    {"key": "net_pnl", "label": "Net P&L", "default": True},
    {"key": "result", "label": "Result", "default": True},
    {"key": "duration_s", "label": "Duration", "default": True},
    {"key": "trading_mode", "label": "Mode", "default": False},
    {"key": "exit_reason", "label": "Exit reason", "default": False},
    {"key": "notional_value", "label": "Notional", "default": False},
    {"key": "margin_used", "label": "Margin", "default": False},
    {"key": "risk_amount", "label": "Risk $", "default": False},
    {"key": "initial_stop", "label": "Stop", "default": False},
    {"key": "initial_target", "label": "Target", "default": False},
    {"key": "gross_pnl", "label": "Gross P&L", "default": False},
    {"key": "fees_total", "label": "Fees", "default": False},
    {"key": "mfe_r", "label": "MFE R", "default": False},
    {"key": "mae_r", "label": "MAE R", "default": False},
    {"key": "rule_violation", "label": "Rule check", "default": False},
    {"key": "status", "label": "Status", "default": False},
    {"key": "exchange", "label": "Venue", "default": False},
)

#: Fields a complete record should have; anything missing is listed honestly.
_IMPORTANT = (
    ("strategy_name", "Strategy"), ("strategy_version", "Strategy version"), ("timeframe", "Timeframe"),
    ("htf_timeframe", "HTF timeframe"), ("exchange", "Exchange"), ("signal_at", "Signal time"),
    ("order_created_at", "Order time"), ("entry_filled_at", "Fill time"),
    ("requested_entry_price", "Requested entry"), ("entry_price", "Entry price"),
    ("quantity", "Quantity"), ("notional_value", "Notional"), ("leverage", "Leverage"),
    ("margin_used", "Margin used"), ("account_balance_before", "Balance before"),
    ("account_equity_before", "Equity before"), ("available_margin_before", "Available margin before"),
    ("initial_stop", "Stop loss"), ("initial_target", "Take profit"), ("risk_amount", "Risk amount"),
    ("risk_pct", "Risk %"), ("planned_rr", "Planned RR"), ("max_allowed_risk_pct", "Max allowed risk"),
)
_IMPORTANT_CLOSED = (("exit_price", "Exit price"), ("exit_reason", "Exit reason"), ("gross_pnl", "Gross P&L"),
                     ("net_pnl", "Net P&L"), ("realised_r", "Realised R"), ("mfe_r", "MFE"), ("mae_r", "MAE"))


def _store():
    return _wa.trade_journal_store


def _recorder():
    return _wa.trade_journal


def _split(value: Optional[str]) -> list[str]:
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def resolve_modes(modes: Optional[str]) -> tuple[list[str], bool]:
    """Explicit modes win. With none, ONE mode is chosen: forward paper when
    it has trades, else the busiest non-backtest mode. Never a blend."""
    requested = [m.upper() for m in _split(modes)]
    if requested:
        if "ALL" in requested:
            return ["ALL"], True
        unknown = [m for m in requested if m not in TRADING_MODES]
        if unknown:
            raise HTTPException(400, f"unknown trading mode(s): {', '.join(unknown)}")
        return requested, len(requested) > 1
    counts = _store().mode_counts()
    if counts.get("FORWARD_PAPER"):
        return ["FORWARD_PAPER"], False
    ranked = sorted(((n, m) for m, n in counts.items() if m != "BACKTEST" and n), reverse=True)
    return [ranked[0][1] if ranked else "FORWARD_PAPER"], False


def _filters(request: Request) -> dict:
    q = request.query_params

    def num(name):
        value = q.get(name)
        if value in (None, ""):
            return None
        try:
            return float(value)
        except ValueError:
            raise HTTPException(400, f"{name} must be a number")

    def day(name):
        value = q.get(name) or None
        if value and len(value) == 10:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(400, f"{name} must look like 2026-10-05")
        return value

    rule = q.get("rule_violation")
    modes, mixed = resolve_modes(q.get("modes") or q.get("mode"))
    return {
        "modes": modes, "_mixed": mixed,
        "date_from": day("date_from"), "date_to": day("date_to"),
        "strategy": q.get("strategy") or None, "instance_id": q.get("instance_id") or None,
        "lab": q.get("lab") or None, "trade_source": q.get("trade_source") or None,
        "symbol": q.get("symbol") or None, "direction": q.get("direction") or q.get("side") or None,
        "result": q.get("result") or None, "session": q.get("session") or None,
        "timeframe": q.get("timeframe") or None,
        "leverage_min": num("leverage_min"), "leverage_max": num("leverage_max"),
        "rr_min": num("rr_min"), "rr_max": num("rr_max"),
        "realised_r_min": num("realised_r_min"), "realised_r_max": num("realised_r_max"),
        "pnl_min": num("pnl_min"), "pnl_max": num("pnl_max"),
        "rule_violation": (None if rule in (None, "", "all") else rule.lower() in ("1", "true", "yes")),
        "exit_reason": q.get("exit_reason") or None, "status": q.get("status") or None,
    }


def _query(filters: dict, **extra) -> list[dict]:
    args = {k: v for k, v in filters.items() if not k.startswith("_")}
    args.update(extra)
    return _store().list_trades(**args)


def _scope(filters: dict) -> dict:
    return {"modes_applied": filters["modes"], "mixed_modes": filters["_mixed"],
            "mode_warning": ("Several trading modes are combined in these figures." if filters["_mixed"] else None)}


def _row(t: dict) -> dict:
    """Table row: every column plus display helpers computed server-side."""
    out = {k: t.get(k) for k in TRADE_COLUMNS if k != "provenance"}
    out["instance"] = (t.get("instance_name") or (t.get("strategy_name") if t.get("lab_id") else None)
                       or t.get("bot_id"))
    out["entry_at_display"] = london_display(t.get("entry_filled_at") or t.get("order_created_at"))
    out["exit_at_display"] = london_display(t.get("exit_at"))
    out["duration_display"] = duration_display(t.get("duration_s"))
    out["session_label"] = SESSION_LABELS.get(t.get("entry_session") or "", t.get("entry_session"))
    return out


# ------------------------------------------------------------------ catalogue
@router.get("/meta")
def journal_meta():
    """Modes with counts, the default mode, facets and the column catalogue."""
    counts = _store().mode_counts()
    default, _ = resolve_modes(None)
    return {
        "modes": [{"mode": m, "trades": counts.get(m, 0)} for m in TRADING_MODES],
        "default_modes": default, "facets": _store().facets(),
        "sessions": [{"key": s, "label": SESSION_LABELS[s]} for s in SESSIONS],
        "results": list(TRADING_RESULTS) + list(OPERATIONAL_RESULTS), "exit_reasons": list(EXIT_REASONS),
        "columns": list(COLUMNS), "correctable_fields": list(CORRECTABLE_FIELDS),
    }


# ------------------------------------------------------------------ trades
@router.get("/trades")
def journal_trades(request: Request, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                   order: str = "desc"):
    filters = _filters(request)
    rows = _query(filters, limit=limit, offset=offset, order=order)
    total = _store().count_trades(**{k: v for k, v in filters.items() if not k.startswith("_")})
    return {"trades": [_row(t) for t in rows], "total": total, "limit": limit, "offset": offset,
            **_scope(filters)}


@router.get("/trades.csv")
def journal_trades_csv(request: Request):
    filters = _filters(request)
    rows = _query(filters)                    # every matching trade: an export is never cut short
    buf = io.StringIO()
    columns = [c for c in TRADE_COLUMNS if c != "provenance"]
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for t in rows:
        writer.writerow({k: t.get(k) for k in columns})
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=trade-journal.csv"})


def _trade_or_404(ref: str) -> dict:
    trade = _store().find(ref)
    if trade is None:
        raise HTTPException(404, "No journal trade for that id")
    return trade


def _strategy_context(trade: dict) -> dict:
    """How this trade's strategy performed overall, in the SAME trading mode."""
    mode = trade.get("trading_mode") or "UNKNOWN"
    rows = _store().list_trades(modes=[mode], strategy=trade.get("strategy_name") or trade.get("strategy_id"))
    m = analytics.metrics(rows)
    sessions = analytics.group_by(analytics.stat_trades(rows), analytics.session_key, analytics._session_label)
    symbols = analytics.group_by(analytics.stat_trades(rows), lambda t: t.get("symbol"))
    return {"mode": mode, "strategy": trade.get("strategy_name"),
            "metrics": {k: m[k] for k in ("total_trades", "win_rate", "net_pnl", "profit_factor", "avg_r",
                                          "expectancy", "max_drawdown", "sample_warning")},
            "sessions": sessions, "symbols": symbols[:10],
            "trend": analytics.trend(rows)["overall"]}


def _facts(trade: dict, snapshot: Optional[dict]) -> list[dict]:
    """The questions the journal must answer at a glance, from structured data."""
    def money(v):
        return None if v is None else round(float(v), 2)

    def plain(v):
        """A stored number as recorded, without float noise."""
        return None if v is None else f"{round(float(v), 10):.10f}".rstrip("0").rstrip(".")
    passed = [c.get("name") for c in (snapshot or {}).get("conditions_passed") or []]
    return [
        {"q": "Strategy", "a": " ".join(str(v) for v in (trade.get("strategy_name"), trade.get("strategy_version"))
                                        if v) or None},
        {"q": "Bot / instance", "a": trade.get("instance_name") or trade.get("lab_id") or trade.get("bot_id")},
        {"q": "Why it entered", "a": (snapshot or {}).get("decision_reason")},
        {"q": "Conditions present", "a": ", ".join(passed) if passed else None},
        {"q": "Symbol / timeframe", "a": f"{trade.get('symbol')} · {trade.get('timeframe') or '—'}"
                                        + (f" (HTF {trade['htf_timeframe']})" if trade.get("htf_timeframe") else "")},
        {"q": "Direction", "a": trade.get("direction")},
        {"q": "Session", "a": SESSION_LABELS.get(trade.get("entry_session") or "", trade.get("entry_session"))},
        {"q": "Entry time", "a": london_display(trade.get("entry_filled_at"))},
        {"q": "Exit time", "a": london_display(trade.get("exit_at"))},
        {"q": "Entry / exit price", "a": (None if trade.get("entry_price") is None and trade.get("exit_price") is None
                                          else f"{plain(trade.get('entry_price')) or '—'} → "
                                               f"{plain(trade.get('exit_price')) or '—'}")},
        {"q": "Quantity", "a": plain(trade.get("quantity"))},
        {"q": "Position / notional", "a": money(trade.get("notional_value"))},
        {"q": "Leverage", "a": (f"{trade['leverage']:g}x" if trade.get("leverage") is not None else None)},
        {"q": "Margin", "a": money(trade.get("margin_used"))},
        {"q": "Stop loss", "a": plain(trade.get("initial_stop"))},
        {"q": "Take profit", "a": plain(trade.get("initial_target"))},
        {"q": "Risked", "a": money(trade.get("risk_amount"))},
        {"q": "Risk %", "a": (round(trade["risk_pct"], 3) if trade.get("risk_pct") is not None else None)},
        {"q": "Planned RR", "a": (f"1:{trade['planned_rr']:.2f}" if trade.get("planned_rr") is not None else None)},
        {"q": "Realised R", "a": (f"{trade['realised_r']:+.2f}R" if trade.get("realised_r") is not None else None)},
        {"q": "P&L before fees", "a": money(trade.get("gross_pnl"))},
        {"q": "P&L after fees", "a": money(trade.get("net_pnl"))},
        {"q": "Time in trade", "a": duration_display(trade.get("duration_s"))},
        {"q": "Exit reason", "a": trade.get("exit_reason")},
        {"q": "Followed the rules", "a": (None if trade.get("rule_violation") is None
                                          else "No — see rule violations" if trade["rule_violation"] else "Yes")},
    ]


@router.get("/trades/{ref}")
def journal_trade_detail(ref: str):
    """Everything about one trade, organised into sections."""
    store = _store()
    trade = _trade_or_404(ref)
    trade_id = trade["trade_id"]
    snapshot = store.snapshot(trade_id)
    fees = store.fees(trade_id)
    fee_totals: dict[str, float] = {}
    for fee in fees:
        fee_totals[fee["fee_type"]] = round(fee_totals.get(fee["fee_type"], 0.0) + float(fee["amount"]), 10)
    links = store.links(trade_id)
    legacy = None
    for link in links:
        if link["link_type"] in ("LEGACY_JOURNAL", "LEDGER_TRADE"):
            try:
                legacy = _wa.decision_journal_store.get(link["ref"])
            except Exception:  # noqa: BLE001 — legacy view is optional
                legacy = None
            if legacy:
                break
    important = _IMPORTANT + (_IMPORTANT_CLOSED if trade.get("finalised_at") else ())
    missing = [{"field": k, "label": label} for k, label in important if trade.get(k) is None]
    return {
        "trade": _row(trade) | {"provenance": trade.get("provenance")},
        "display": {
            "entry": london_display(trade.get("entry_filled_at")), "exit": london_display(trade.get("exit_at")),
            "signal": london_display(trade.get("signal_at")), "order": london_display(trade.get("order_created_at")),
            "duration": duration_display(trade.get("duration_s")),
            "session": SESSION_LABELS.get(trade.get("entry_session") or "", trade.get("entry_session")),
        },
        "facts": _facts(trade, snapshot),
        "snapshot": snapshot,
        "executions": store.executions(trade_id),
        "fees": {"items": fees, "totals": fee_totals},
        "modifications": store.modifications(trade_id),
        "timeline": store.events(trade_id),
        "reviews": store.reviews(trade_id),
        "notes": store.notes(trade_id),
        "corrections": store.corrections(trade_id),
        "links": links,
        "missing_fields": missing,
        "strategy_context": _strategy_context(trade),
        "legacy_decision_journal": legacy,
    }


@router.get("/trades/{ref}/timeline")
def journal_trade_timeline(ref: str):
    trade = _trade_or_404(ref)
    return {"trade_id": trade["trade_id"], "trade_ref": trade["trade_ref"],
            "timeline": _store().events(trade["trade_id"])}


@router.get("/trades/{ref}/reviews")
def journal_trade_reviews(ref: str):
    trade = _trade_or_404(ref)
    return {"trade_id": trade["trade_id"], "reviews": _store().reviews(trade["trade_id"])}


@router.post("/trades/{ref}/reviews")
def journal_trade_review_run(ref: str, x_webhook_secret: Optional[str] = Header(default=None)):
    """Run the trade review agent now. The review is stored separately and
    cannot change the trade or its snapshot."""
    _wa._check_secret(x_webhook_secret)
    trade = _trade_or_404(ref)
    if not trade.get("finalised_at") or trade.get("is_operational"):
        raise HTTPException(409, "Only closed trades are reviewed")
    return _recorder().review(trade["trade_id"])


@router.post("/trades/{ref}/reviews/external")
def journal_trade_review_external(ref: str, body: dict = Body(...),
                                  x_webhook_secret: Optional[str] = Header(default=None)):
    """Store a structured review produced by another agent."""
    _wa._check_secret(x_webhook_secret)
    trade = _trade_or_404(ref)
    try:
        review = validate_external_review(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    stored = _store().add_review(trade["trade_id"], review)
    _store().add_event(trade["trade_id"], "review-submitted", f"{review['reviewer']} submitted a review",
                       actor=review["reviewer"])
    return stored


@router.post("/trades/{ref}/notes")
def journal_trade_note(ref: str, body: dict = Body(...), x_webhook_secret: Optional[str] = Header(default=None)):
    """Manual commentary. Notes are additional; they never edit trade facts."""
    _wa._check_secret(x_webhook_secret)
    trade = _trade_or_404(ref)
    try:
        note = _store().add_note(trade["trade_id"], str(body.get("note") or ""),
                                 str(body.get("author") or "operator")[:80])
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _store().add_event(trade["trade_id"], "note-added", note["note"][:120], actor=note["author"])
    return note


@router.get("/trades/{ref}/corrections")
def journal_trade_corrections(ref: str):
    trade = _trade_or_404(ref)
    return {"trade_id": trade["trade_id"], "corrections": _store().corrections(trade["trade_id"])}


@router.post("/trades/{ref}/corrections")
def journal_trade_correct(ref: str, body: dict = Body(...), x_webhook_secret: Optional[str] = Header(default=None)):
    """Correct recorded facts through the audit trail: previous value, new
    value, timestamp, reason and actor are all kept."""
    _wa._check_secret(x_webhook_secret)
    trade = _trade_or_404(ref)
    changes = body.get("changes") or ({body["field"]: body.get("new_value")} if body.get("field") else {})
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(400, "changes must be a non-empty object of field -> new value")
    try:
        return _store().correct(trade["trade_id"], changes, reason=str(body.get("reason") or ""),
                                actor=str(body.get("actor") or "operator"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ------------------------------------------------------------------ analytics
@router.get("/summary")
def journal_summary(request: Request):
    filters = _filters(request)
    return {**analytics.dashboard(_query(filters)), **_scope(filters)}


@router.get("/performance/strategies")
def journal_strategy_performance(request: Request, group_by: str = "strategy"):
    if group_by not in ("strategy", "family", "instance", "source"):
        raise HTTPException(400, "group_by must be strategy|family|instance|source")
    filters = _filters(request)
    return {"group_by": group_by, "rows": analytics.strategy_performance(_query(filters), by=group_by),
            **_scope(filters)}


@router.get("/performance/comparison")
def journal_strategy_comparison(request: Request):
    filters = _filters(request)
    return {"rows": analytics.strategy_comparison(_query(filters)), **_scope(filters)}


@router.get("/performance/sessions")
def journal_session_performance(request: Request):
    filters = _filters(request)
    rows = _query(filters)
    return {**analytics.session_performance(rows), "hours": analytics.hour_performance(rows),
            "weekdays": analytics.weekday_performance(rows), **_scope(filters)}


@router.get("/performance/symbols")
def journal_symbol_performance(request: Request):
    filters = _filters(request)
    return {**analytics.symbol_performance(_query(filters)), **_scope(filters)}


@router.get("/performance/directions")
def journal_direction_performance(request: Request):
    filters = _filters(request)
    return {**analytics.direction_performance(_query(filters)), **_scope(filters)}


@router.get("/performance/leverage")
def journal_leverage_performance(request: Request):
    filters = _filters(request)
    return {"rows": analytics.leverage_performance(_query(filters)), **_scope(filters)}


@router.get("/performance/rr")
def journal_rr_performance(request: Request):
    filters = _filters(request)
    return {**analytics.rr_analysis(_query(filters)), **_scope(filters)}


@router.get("/performance/excursions")
def journal_excursions(request: Request):
    filters = _filters(request)
    return {**analytics.excursion_analysis(_query(filters)), **_scope(filters)}


@router.get("/performance/trend")
def journal_trend(request: Request):
    filters = _filters(request)
    return {**analytics.trend(_query(filters)), **_scope(filters)}


@router.get("/analytics")
def journal_analytics(request: Request):
    """Everything the analytics tab needs, in one server-side computation."""
    filters = _filters(request)
    return {**analytics.full_analytics(_query(filters)), **_scope(filters)}


# ------------------------------------------------------------------ weekly review
def _week_key(week: Optional[str]) -> str:
    if week:
        try:
            week_bounds(week)
        except (ValueError, IndexError):
            raise HTTPException(400, "week must look like 2026-W40")
        return week.upper()
    return iso_week_key(datetime.now(timezone.utc).isoformat())


def _review_time(trade: dict) -> str:
    """A trade belongs to the week it closed in; an order that never filled,
    to the week it was placed in."""
    return (trade.get("exit_at") or trade.get("entry_filled_at") or trade.get("order_created_at")
            or trade.get("signal_at") or trade.get("created_at") or "")


def _weekly(request: Request, week: Optional[str]) -> tuple[dict, dict]:
    key = _week_key(week)
    start, end = week_bounds(key)
    filters = _filters(request)
    base = {k: v for k, v in filters.items() if not k.startswith("_") and k not in ("date_from", "date_to")}
    prior_start = week_bounds(iso_week_key(datetime.fromisoformat(start) - timedelta(days=1)))[0]
    trades = _store().list_trades(**base)
    in_week = [t for t in trades if start <= _review_time(t) < end]
    prior = [t for t in trades if prior_start <= _review_time(t) < start]
    reviews = _store().reviews_for(t["trade_id"] for t in in_week)
    report = analytics.weekly_review(in_week, prior, reviews, week_key=key)
    scope = {"week": key, "modes": filters["modes"], **{k: v for k, v in base.items() if v and k != "modes"}}
    return report, {**scope, **_scope(filters)}


@router.get("/weekly-review")
def journal_weekly_review(request: Request, week: Optional[str] = None):
    """Compute the weekly strategy review from the journal (not persisted)."""
    report, scope = _weekly(request, week)
    return {**report, "scope": scope, "persisted": False}


@router.post("/weekly-review")
def journal_weekly_review_save(request: Request, week: Optional[str] = None,
                               x_webhook_secret: Optional[str] = Header(default=None)):
    """Generate and persist the weekly review (history is kept)."""
    _wa._check_secret(x_webhook_secret)
    report, scope = _weekly(request, week)
    return _store().save_weekly_review(report["week"], scope, report, generated_by="weekly-review-agent")


@router.get("/weekly-reviews")
def journal_weekly_reviews(week: Optional[str] = None, limit: int = Query(20, ge=1, le=100)):
    return {"reviews": _store().weekly_reviews(week.upper() if week else None, limit)}


# ------------------------------------------------------------------ maintenance
@router.post("/sync")
def journal_sync(x_webhook_secret: Optional[str] = Header(default=None)):
    """Migrate, reconcile and ingest now (normally runs at boot and on a timer)."""
    _wa._check_secret(x_webhook_secret)
    return _wa.journal_sync.run_once()


@router.get("/sync/status")
def journal_sync_status():
    return _wa.journal_sync.status()
