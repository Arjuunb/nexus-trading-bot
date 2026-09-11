"""Read-only observability surface for the native SMC research model."""
from __future__ import annotations

import hashlib
import csv
import io
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from services.native_smc import VisualReview, VisualReviewLedger, research_engine
from services.native_smc_live_visual import (
    NativeSMCLiveDataUnavailable,
    live_visual_history,
    live_visual_state,
)
from services.native_smc_visual_verification import deterministic_review_sample
from services.smc_strategy_ladder import evaluate_ladder, manifest_payload
from services.smc_strategy_v1 import (
    evaluate as evaluate_source_strategy,
    manifest as source_strategy_manifest,
    strategy_models as source_strategy_models,
)
from services.smc_strategy_lab import SMCPaperConfig
from services.lab_read_view import lab_read_view, saved_session, saved_status
from data.sqlite_runtime import is_sqlite_busy

router = APIRouter(prefix="/research/smc", tags=["research-smc"])


def _persistence_blocked(exc: BaseException):
    """Report a contended SQLite read as a retryable 503, matching the
    Price Action lab. Without this the same fault surfaced here as an opaque
    500, so an operator reading two lab statuses got two different codes for
    one condition and had no signal that retrying was the right response."""
    if is_sqlite_busy(exc):
        raise HTTPException(status_code=503, detail={
            "state": "PERSISTENCE_BLOCKED", "code": "PERSISTENCE_BLOCKED",
            "retryable": True, "message": str(exc),
            "real_execution_allowed": False,
        }) from exc
    raise exc
reviews = VisualReviewLedger()
_REFERENCE_PINE_PATH = Path(__file__).resolve().parents[1] / "research_references" / "smc_pro_v2_reference.pine"


class VisualReviewInput(BaseModel):
    object_id: str = Field(min_length=1, max_length=160)
    component: str = Field(min_length=1, max_length=80)
    classification: Literal["CORRECT", "INCORRECT", "AMBIGUOUS"]
    expected_structure: Optional[str] = Field(default=None, max_length=2000)
    actual_structure: Optional[str] = Field(default=None, max_length=2000)
    reason: Optional[str] = Field(default=None, max_length=4000)
    notes: Optional[str] = Field(default=None, max_length=4000)
    screenshot_timestamp: Optional[str] = Field(default=None, max_length=80)
    visible_range_start: Optional[str] = Field(default=None, max_length=80)
    visible_range_end: Optional[str] = Field(default=None, max_length=80)
    selected_candle_timestamp: Optional[str] = Field(default=None, max_length=80)


class SMCSessionBody(BaseModel):
    mode: str = "LIVE_PAPER"
    symbol: str = "BTCUSDT"
    timeframe: str = "5m"
    starting_balance: float = Field(default=10_000, gt=0)
    operating_mode: str = "automatic"
    model_id: str = "SMC_M1_SWEEP_REVERSAL"
    risk_pct: float = Field(default=0.5, gt=0, le=1)


class SMCSessionConfigBody(BaseModel):
    mode: Optional[str] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    replay_cursor: Optional[int] = Field(default=None, ge=0)
    operating_mode: str = "automatic"
    model_id: str = "SMC_M1_SWEEP_REVERSAL"
    risk_pct: float = Field(default=0.5, gt=0, le=1)


class SMCPaperOrderBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    symbol: str
    side: str
    order_type: str = Field(alias="type")
    quantity: Optional[float] = Field(default=None, gt=0)
    risk_pct: Optional[float] = Field(default=None, gt=0, le=1)
    limit_price: Optional[float] = Field(default=None, gt=0)
    trigger_price: Optional[float] = Field(default=None, gt=0)
    stop_loss: Optional[float] = Field(default=None, gt=0)
    target_1: Optional[float] = Field(default=None, gt=0)
    target_2: Optional[float] = Field(default=None, gt=0)


class SMCPaperResetBody(BaseModel):
    confirmation: str


class SMCLeverageBody(BaseModel):
    leverage: float = Field(ge=1, le=10)


class SMCProtectionBody(BaseModel):
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)


class SMCReplayStepBody(BaseModel):
    steps: int = Field(default=1, ge=1, le=500)


class SMCJournalNoteBody(BaseModel):
    note: str = Field(min_length=1, max_length=4000)


def _smc_runtime():
    import webhook_api as runtime
    return runtime


def _smc_auth(secret: Optional[str]) -> None:
    _smc_runtime()._check_secret(secret)


def _smc_bad(exc: Exception, status: int = 400):
    raise HTTPException(status, str(exc)) from exc


def _at_timestamp(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(422, "'at' must be an ISO-8601 timestamp") from exc


def _strategy_engine(symbol: str, timeframe: str):
    normalized_symbol = symbol.upper().strip()
    normalized_timeframe = timeframe.lower().strip()
    if normalized_symbol not in {"BTCUSDT", "ETHUSDT", "SOLUSDT"}:
        raise HTTPException(422, "symbol must be one of BTCUSDT, ETHUSDT, or SOLUSDT")
    if normalized_timeframe not in {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}:
        raise HTTPException(422, "unsupported SMC timeframe")
    return research_engine(normalized_symbol, normalized_timeframe)


@router.get("/state")
def state(symbol: str = "BTCUSDT", timeframe: str = "5m", at: Optional[str] = None,
          window: int = 400, model_id: str = "SMC_M1_SWEEP_REVERSAL"):
    selected_at = _at_timestamp(at)
    engine = _strategy_engine(symbol, timeframe)
    payload = engine.visual_state(candle_at=selected_at, candle_window=window)
    payload["strategy_ladder"] = evaluate_ladder(engine, candle_at=selected_at)
    try:
        payload["source_strategy"] = evaluate_source_strategy(engine, model_id, candle_at=selected_at)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return payload


@router.get("/chart")
def chart(symbol: str = "BTCUSDT", timeframe: str = "5m", at: Optional[str] = None,
          window: int = 400, model_id: str = "SMC_M1_SWEEP_REVERSAL"):
    """Chart contract made only from native engine objects and raw candles."""
    selected_at = _at_timestamp(at)
    engine = _strategy_engine(symbol, timeframe)
    payload = engine.visual_state(candle_at=selected_at, candle_window=window)
    payload["strategy_ladder"] = evaluate_ladder(engine, candle_at=selected_at)
    try:
        payload["source_strategy"] = evaluate_source_strategy(engine, model_id, candle_at=selected_at)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return payload


@router.get("/strategy-ladder")
def strategy_ladder(symbol: str = "BTCUSDT", timeframe: str = "5m", at: Optional[str] = None):
    """Frozen SMC research traces; it has no order or execution authority."""
    engine = research_engine(symbol, timeframe)
    return {
        "research_only": True,
        "execution_allowed": False,
        "definition": manifest_payload(),
        "evaluation": evaluate_ladder(engine, candle_at=_at_timestamp(at)),
    }


@router.get("/strategy-v1/manifest")
def strategy_v1_manifest():
    """Versioned source-informed strategy rules; paper-only and non-executable."""
    return source_strategy_manifest()


@router.get("/strategy-models")
def strategy_models():
    """List active and parked source-informed models without activating them."""
    return source_strategy_models()


@router.get("/strategy-v1/evaluate")
def strategy_v1_evaluate(symbol: str = "BTCUSDT", timeframe: str = "5m",
                         model_id: str = "SMC_M1_SWEEP_REVERSAL", at: Optional[str] = None):
    engine = _strategy_engine(symbol, timeframe)
    try:
        return evaluate_source_strategy(engine, model_id, candle_at=_at_timestamp(at))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _smc_paper_state() -> dict:
    runtime = _smc_runtime()
    with lab_read_view(runtime.smc_paper) as account:
        return account.state()


@router.get("/session")
def session_identity():
    try:
        return saved_session(_smc_runtime().smc_paper)
    except sqlite3.OperationalError as exc:
        _persistence_blocked(exc)


@router.get("/paper")
def smc_paper_account():
    return _smc_paper_state()


@router.get("/bot-status")
def smc_bot_status():
    """Dashboard-safe status for the isolated SMC paper system."""
    try:
        return saved_status(_smc_runtime().smc_runtime)
    except sqlite3.OperationalError as exc:
        _persistence_blocked(exc)


@router.get("/paper/export")
def smc_paper_export():
    return _smc_runtime().smc_paper.export_session()


@router.get("/sessions")
def smc_sessions():
    with lab_read_view(_smc_runtime().smc_paper) as account:
        return {"sessions": account.sessions(), "paper_only": True, "real_execution_allowed": False}


@router.post("/sessions")
def smc_session_start(body: SMCSessionBody, x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_paper.start(
            mode=body.mode, symbol=body.symbol, timeframe=body.timeframe,
            starting_balance=body.starting_balance,
            config=SMCPaperConfig(operating_mode=body.operating_mode,
                                  model_id=body.model_id, risk_pct=body.risk_pct).validated())
    except ValueError as exc:
        _smc_bad(exc)


@router.post("/sessions/current/configuration")
def smc_session_configure(body: SMCSessionConfigBody,
                          x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_paper.configure(
            mode=body.mode, symbol=body.symbol, timeframe=body.timeframe,
            replay_cursor=body.replay_cursor,
            config=SMCPaperConfig(operating_mode=body.operating_mode,
                                  model_id=body.model_id, risk_pct=body.risk_pct).validated())
    except ValueError as exc:
        _smc_bad(exc, 409)


@router.post("/sessions/current/end")
def smc_session_end(x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_paper.end()
    except ValueError as exc:
        _smc_bad(exc, 409)


@router.post("/sessions/current/replay/step")
def smc_session_replay_step(body: SMCReplayStepBody,
                            x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_runtime.replay_step(steps=body.steps)
    except ValueError as exc:
        _smc_bad(exc, 409)
    except RuntimeError as exc:
        _smc_bad(exc, 503)


@router.post("/sessions/{session_id}/resume")
def smc_session_resume(session_id: str, x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_paper.resume(session_id)
    except KeyError as exc:
        _smc_bad(exc, 404)
    except ValueError as exc:
        _smc_bad(exc, 409)


@router.post("/sessions/{session_id}/duplicate")
def smc_session_duplicate(session_id: str, x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_paper.duplicate(session_id)
    except KeyError as exc:
        _smc_bad(exc, 404)
    except ValueError as exc:
        _smc_bad(exc, 409)


@router.get("/sessions/{session_id}/export")
def smc_session_export(session_id: str):
    try:
        return _smc_runtime().smc_paper.export_session(session_id)
    except KeyError as exc:
        _smc_bad(exc, 404)


@router.post("/paper/reset")
def smc_paper_reset(body: SMCPaperResetBody,
                    x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_paper.reset(body.confirmation)
    except ValueError as exc:
        _smc_bad(exc, 409)


@router.post("/paper/leverage")
def smc_paper_leverage(body: SMCLeverageBody,
                       x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_paper.set_leverage(body.leverage)
    except ValueError as exc:
        _smc_bad(exc)


@router.post("/paper/orders")
def smc_paper_order(body: SMCPaperOrderBody,
                    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
                    x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    if body.order_type == "market" and (body.limit_price is not None or body.trigger_price is not None):
        raise HTTPException(422, "market paper orders cannot include a limit or trigger price")
    if body.order_type in {"limit", "stop_limit"} and body.limit_price is None:
        raise HTTPException(422, "limit paper orders require a limit price")
    if body.order_type in {"stop", "stop_limit"} and body.trigger_price is None:
        raise HTTPException(422, "stop paper orders require a trigger price")
    if body.quantity is None and body.risk_pct is None:
        raise HTTPException(422, "provide quantity or risk_pct")
    if not idempotency_key:
        raise HTTPException(422, "Idempotency-Key header is required")
    runtime = _smc_runtime()
    try:
        rules = runtime.v2_market_data.usdm_contract_rules(body.symbol)
        quote = runtime.v2_market_data.public_usdm_quote(body.symbol)
        return runtime.smc_paper.submit_order(
            symbol=body.symbol, side=body.side, order_type=body.order_type,
            rules=rules, reference_price=float(quote["mark"]), quantity=body.quantity,
            risk_pct=body.risk_pct, limit_price=body.limit_price,
            trigger_price=body.trigger_price, stop_loss=body.stop_loss,
            target_1=body.target_1, target_2=body.target_2,
            idempotency_key=idempotency_key, ownership="manual")
    except (ValueError, RuntimeError) as exc:
        _smc_bad(exc)


@router.post("/paper/orders/{order_id}/cancel")
def smc_paper_cancel(order_id: str, x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_paper.cancel_order(order_id)
    except KeyError as exc:
        _smc_bad(exc, 404)
    except ValueError as exc:
        _smc_bad(exc, 409)


@router.post("/paper/orders/reconcile")
def smc_paper_reconcile(x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    runtime = _smc_runtime()
    try:
        tick = runtime.smc_runtime.tick()
        return {**tick,
                "reconciliation": runtime.smc_paper.reconcile_orders(),
                "paper_only": True, "real_execution_allowed": False}
    except (NativeSMCLiveDataUnavailable, ValueError, RuntimeError) as exc:
        _smc_bad(exc, 503)


@router.post("/paper/candidates/{proposal_id}/approve")
def smc_paper_candidate_approve(proposal_id: str,
                                x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_paper.approve_candidate(proposal_id)
    except KeyError as exc:
        _smc_bad(exc, 404)
    except ValueError as exc:
        _smc_bad(exc, 409)


@router.post("/paper/positions/{symbol}/protection")
def smc_paper_protection(symbol: str, body: SMCProtectionBody,
                         x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return _smc_runtime().smc_paper.set_protection(
            symbol, stop_loss=body.stop_loss, take_profit=body.take_profit)
    except ValueError as exc:
        _smc_bad(exc, 409)


@router.get("/journal")
def smc_journal(symbol: Optional[str] = None, timeframe: Optional[str] = None,
                model_id: Optional[str] = None, status: Optional[str] = None,
                session_id: Optional[str] = None, direction: Optional[str] = None,
                rule_compliance: Optional[str] = None, result: Optional[str] = None,
                date_from: Optional[str] = None, date_to: Optional[str] = None):
    payload = _smc_runtime().smc_paper.journal(session_id)
    rows = payload["journal"]
    filters = {"symbol": symbol, "timeframe": timeframe, "model_id": model_id,
               "status": status, "session_id": session_id, "direction": direction,
               "rule_compliance": rule_compliance}
    for key, value in filters.items():
        if value:
            rows = [row for row in rows if str(row.get(key, "")).lower() == value.lower()]
    if result:
        wanted = result.lower()
        rows = [row for row in rows if ("win" if row["net_pnl"] > 0 else
                                        "loss" if row["net_pnl"] < 0 else "flat") == wanted]
    if date_from:
        rows = [row for row in rows if row["created_at"] >= date_from]
    if date_to:
        rows = [row for row in rows if row["created_at"] <= date_to]
    filters.update({"result": result, "date_from": date_from, "date_to": date_to})
    return {**payload, "journal": rows, "filters": filters}


@router.get("/journal/export")
def smc_journal_export(format: str = "csv"):
    payload = _smc_runtime().smc_paper.journal()
    if format.lower() == "json":
        return {"format_version": "SMC_JOURNAL_1",
                "exported_at": datetime.now(timezone.utc).isoformat(), **payload}
    output = io.StringIO()
    fields = ["journal_id", "session_id", "symbol", "timeframe", "strategy_id", "model_id",
              "direction", "status", "proposal_id", "setup_id", "order_id", "net_pnl",
              "data_quality", "rule_compliance", "created_at", "updated_at"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(payload["journal"])
    return Response(output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=smc-journal.csv"})


@router.get("/journal/{journal_id}")
def smc_journal_item(journal_id: str):
    row = next((item for item in _smc_runtime().smc_paper.journal()["journal"]
                if item["journal_id"] == journal_id), None)
    if not row:
        raise HTTPException(404, "unknown SMC journal record")
    return {"journal": row, "paper_only": True, "real_execution_allowed": False}


@router.post("/journal/{journal_id}/notes")
def smc_journal_note(journal_id: str, body: SMCJournalNoteBody,
                     x_webhook_secret: Optional[str] = Header(default=None)):
    _smc_auth(x_webhook_secret)
    try:
        return {"revision": _smc_runtime().smc_paper.add_journal_note(journal_id, body.note),
                "paper_only": True, "real_execution_allowed": False}
    except KeyError as exc:
        _smc_bad(exc, 404)
    except ValueError as exc:
        _smc_bad(exc, 409)


@router.get("/metrics")
def smc_metrics():
    return _smc_runtime().smc_paper.metrics()


@router.get("/live-chart")
def live_chart(symbol: str = "BTCUSDT", timeframe: str = "5m", venue: str = "binance_usdm",
               window: int = 800, visible: int = 240,
               model_id: str = "SMC_M1_SWEEP_REVERSAL"):
    """Read-only live-exchange visualisation; never a trading data path."""
    try:
        runtime = _smc_runtime().smc_runtime
        if venue == "binance_usdm":
            from copy import copy
            with lab_read_view(_smc_runtime().smc_paper) as account:
                view = copy(runtime)
                view.account = account
                return view.live_state(symbol, timeframe, visible=visible, window=window, model_id=model_id)
        session = _smc_runtime().smc_paper.session() or {}
        evidence = (runtime.last_mtf_evidence
                    if session.get("symbol") == symbol.upper() and
                    session.get("timeframe") == timeframe else {})
        state = live_visual_state(symbol, timeframe, venue, limit=window, visible=visible,
                                  model_id=model_id, mtf_evidence=evidence,
                                  mtf_context=(runtime.last_mtf_context if evidence else None))
        if venue == "binance_usdm":
            state, _ = runtime.reconcile_visual(
                state, symbol=symbol, timeframe=timeframe)
        else:
            state.setdefault("live_display", {}).update({
                "connection_state": "VIEW_ONLY_ALTERNATE", "reliable": False,
                "new_entries_paused": True,
                "health_reason": "alternate venues are display-only and cannot authorize SMC paper entries",
            })
        return state
    except (NativeSMCLiveDataUnavailable, ValueError) as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/live-history")
def live_history(symbol: str = "BTCUSDT", timeframe: str = "5m", venue: str = "binance_usdm",
                 before: Optional[str] = None, limit: int = 400):
    """One paginated, closed-candle exchange page for Visual Lab browsing."""
    if before is None:
        raise HTTPException(422, "'before' is required for historical paging")
    parsed_before = _at_timestamp(before)
    # Guarded above; keeping this explicit makes the non-optional boundary of
    # the service contract clear without changing timestamp semantics.
    assert parsed_before is not None
    try:
        return live_visual_history(symbol, timeframe, venue, before=parsed_before, limit=limit)
    except (NativeSMCLiveDataUnavailable, ValueError) as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/pine-reference")
def pine_reference():
    """Return the immutable reference source for visual comparison only.

    The reference is deliberately not compiled, evaluated, or wired into the
    native model.  Its status remains a parity-audit artefact, never execution
    authority.
    """
    if not _REFERENCE_PINE_PATH.is_file():
        raise HTTPException(503, "Native SMC Pine reference is unavailable in this deployment")
    content = _REFERENCE_PINE_PATH.read_text(encoding="utf-8")
    return {
        "reference_id": "SMC_PRO_V2_REFERENCE",
        "status": "PARITY_AUDIT",
        "language": "pine",
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "execution_allowed": False,
        "notice": "Reference source only. It is not executed by TradeLogX and native SMC parity is not yet claimed.",
        "content": content,
    }


@router.get("/review-sample")
def review_sample(symbol: str = "BTCUSDT", timeframe: str = "5m"):
    """Fixed deterministic review list; classification can never select samples."""
    engine = research_engine(symbol, timeframe)
    return {
        "research_only": True,
        "execution_allowed": False,
        "sample": [asdict(row) for row in deterministic_review_sample(engine)],
    }


@router.get("/setups")
def setups(symbol: str = "BTCUSDT", timeframe: str = "5m"):
    engine = research_engine(symbol, timeframe)
    return {"research_only": True, "execution_allowed": False,
            "setups": engine.visual_state()["setups"]}


@router.get("/setups/{setup_id}")
def setup(setup_id: str, symbol: str = "BTCUSDT", timeframe: str = "5m"):
    engine = research_engine(symbol, timeframe)
    row = engine.setups.get(setup_id)
    if row is None:
        raise HTTPException(404, "Unknown native SMC research setup")
    # This is the engine's factual state history, not a generated explanation.
    return {"research_only": True, "execution_allowed": False, "setup": asdict(row)}


@router.get("/events")
def events(symbol: str = "BTCUSDT", timeframe: str = "5m"):
    engine = research_engine(symbol, timeframe)
    state = engine.visual_state()
    return {"research_only": True, "execution_allowed": False,
            "events": state["events"], "chart_objects": state["chart_objects"],
            "pivots": state["pivots"], "fair_value_gaps": state["fair_value_gaps"],
            "order_blocks": state["order_blocks"]}


@router.get("/reviews")
def list_reviews(symbol: str = "BTCUSDT", timeframe: str = "5m"):
    rows = [asdict(row) for row in reviews.records()
            if row.symbol == symbol.upper() and row.timeframe == timeframe]
    return {"research_only": True, "execution_allowed": False, "reviews": rows}


@router.post("/reviews")
def record_review(body: VisualReviewInput, symbol: str = "BTCUSDT", timeframe: str = "5m"):
    """Persist human interpretation evidence; this never modifies SMC rules."""
    engine = research_engine(symbol, timeframe)
    if body.object_id not in engine.known_object_ids():
        raise HTTPException(404, "The selected native SMC object is not present in this research state")
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(f"{engine.config.symbol}|{timeframe}|{body.object_id}|{body.component}|{now.isoformat()}".encode()).hexdigest()[:16]
    review = VisualReview(
        id=f"smc-review-{digest}", research_id="SMC_NATIVE_V1_RESEARCH",
        symbol=engine.config.symbol, timeframe=timeframe, object_id=body.object_id,
        component=body.component, classification=body.classification,
        expected_structure=body.expected_structure, actual_structure=body.actual_structure,
        reason=body.reason, screenshot_timestamp=body.screenshot_timestamp, created_at=now,
        notes=body.notes, visible_range_start=body.visible_range_start,
        visible_range_end=body.visible_range_end,
        selected_candle_timestamp=body.selected_candle_timestamp,
    )
    return {"research_only": True, "execution_allowed": False, "review": asdict(reviews.append(review))}
