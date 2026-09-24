"""The public HTTP API: ``/v1``, authenticated with personal API keys.

JSON over HTTPS. Every request carries ``Authorization: Bearer nxs_...``
(services/api_keys.py); ``Nexus-Version`` pins the response shape to a date
and defaults to the version the key was created against. Every response
carries ``Nexus-Version`` and ``X-RateLimit-*`` headers; errors are
``{"error": {"code", "message"}}`` with a stable code.

Scopes: ``read`` for every GET, ``control`` for anything that changes state
(closing a paper position, queuing a backtest). Nothing here can place a live
order: live order routing is locked for every caller, and asking to promote a
strategy to live says so rather than pretending.

Every state-changing call passes through the audit log like any other
request, attributed to the key's name.
"""
from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

import webhook_api as _wa
from services.api_keys import API_VERSIONS, default_store

router = APIRouter(prefix="/v1", tags=["public-api"])

RATE_PER_MINUTE = 600
RATE_BURST_PER_SECOND = 20


class PublicApiError(Exception):
    def __init__(self, status: int, code: str, message: str, *, headers: Optional[dict] = None, **extra):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.headers = headers or {}
        self.extra = extra


def error_response(exc: PublicApiError) -> JSONResponse:
    return JSONResponse({"error": {"code": exc.code, "message": exc.message, **exc.extra}},
                        status_code=exc.status, headers=exc.headers)


async def public_api_error_handler(request, exc: PublicApiError):  # registered on the app
    return error_response(exc)


# --------------------------------------------------------------- auth + limits
def _limit(key_id: str) -> tuple[int, int]:
    """(remaining this minute, retry-after seconds or 0)."""
    from services.ratelimit import limiter
    if not limiter.allow(f"v1:{key_id}:s", RATE_BURST_PER_SECOND, 1.0):
        return 0, max(1, limiter.retry_after(f"v1:{key_id}:s", 1.0))
    if not limiter.allow(f"v1:{key_id}:m", RATE_PER_MINUTE, 60.0):
        return 0, max(1, limiter.retry_after(f"v1:{key_id}:m", 60.0))
    return max(0, RATE_PER_MINUTE - limiter.used(f"v1:{key_id}:m", 60.0)), 0


def api_key(request: Request, response: Response) -> dict:
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    key = default_store().authenticate(token) if token else None
    if key is None:
        raise PublicApiError(401, "unauthenticated", "Missing, malformed or revoked API key.")
    version = request.headers.get("nexus-version") or key["version"]
    if version not in API_VERSIONS:
        raise PublicApiError(400, "invalid_request",
                             f"Unknown Nexus-Version {version}. Supported: {', '.join(API_VERSIONS)}.")
    remaining, retry = _limit(key["id"])
    rate_headers = {"X-RateLimit-Limit": str(RATE_PER_MINUTE), "X-RateLimit-Remaining": str(remaining),
                    "Nexus-Version": version}
    if retry:
        raise PublicApiError(429, "rate_limited", "Too many requests for this key.",
                             headers={**rate_headers, "Retry-After": str(retry)})
    response.headers.update(rate_headers)
    request.state.api_key = {"id": key["id"], "name": key["name"]}  # read by the audit log
    return key


def control(key: dict = Depends(api_key)) -> dict:
    if "control" not in key["scopes"]:
        raise PublicApiError(403, "insufficient_scope",
                             "This key is read-only. Create a key with the control scope.")
    return key


def _iso(value) -> Optional[str]:
    return value if isinstance(value, str) or value is None else str(value)


# ------------------------------------------------------------------ strategies
@router.get("/strategies")
def strategies(key: dict = Depends(api_key)):
    """Every strategy the platform can run, with its immutable version."""
    from services.strategy_registry import all_entries
    return {"data": [{
        "id": e.strategy_id, "name": e.display_name, "version": e.version,
        "lifecycle": e.lifecycle.lower(), "mode": "paper",
        "timeframes": list(e.supported_timeframes), "markets": list(e.supported_markets),
        "description": e.description,
    } for e in all_entries()], "live_routing": "locked"}


@router.post("/strategies/{strategy_id}/promote")
def promote(strategy_id: str, body: dict = Body(default={}), key: dict = Depends(control)):
    """Change a strategy's execution mode. Paper is the only mode available:
    promotion to live is refused while live order routing is locked."""
    from services.strategy_registry import entry
    if entry(strategy_id) is None:
        raise PublicApiError(404, "not_found", f"No strategy {strategy_id}.")
    mode = str((body or {}).get("mode", "")).lower()
    if mode == "live":
        raise PublicApiError(409, "live_routing_locked",
                             "Live order routing is locked on this platform; every strategy runs in paper mode.")
    if mode != "paper":
        raise PublicApiError(400, "invalid_request", 'Body must be {"mode": "paper"} or {"mode": "live"}.')
    return {"id": strategy_id, "mode": "paper", "changed": False}


# ------------------------------------------------------------------- decisions
def _decision(d: dict) -> dict:
    return {
        "id": f"dec_{d['id']}", "ts": d.get("ts"), "symbol": d.get("symbol"),
        "timeframe": d.get("timeframe"), "strategy": d.get("strategy"), "side": d.get("side"),
        "regime": d.get("regime"), "verdict": d.get("decision"),
        "quality_score": d.get("setup_quality_score"),
        "blocked_by": d.get("blocker") or d.get("gate_stage") or None,
        "reason": d.get("reason"), "rules_passed": d.get("passed_rules") or [],
        "rules_failed": d.get("failed_rules") or [], "executed": bool(d.get("executed")),
        "instance_id": d.get("instance_id") or None, "components": d.get("components") or {},
    }


def _decision_id(raw: str) -> int:
    try:
        return int(str(raw).removeprefix("dec_"))
    except ValueError:
        raise PublicApiError(404, "not_found", f"No decision {raw}.") from None


@router.get("/decisions")
def decisions(key: dict = Depends(api_key), verdict: Optional[str] = None, symbol: Optional[str] = None,
              since: Optional[str] = None, limit: int = Query(50, ge=1, le=200),
              cursor: Optional[str] = None):
    """Every evaluation, accepted and rejected, newest first. Page with
    ``cursor`` = the previous response's ``next_cursor``."""
    if verdict not in (None, "accepted", "rejected"):
        raise PublicApiError(400, "invalid_request", "verdict must be accepted or rejected.")
    if since:
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise PublicApiError(400, "invalid_request", "since must be an ISO date or timestamp.") from None
    before = _decision_id(cursor) if cursor else None
    rows = _wa.decision_store.page(limit=limit, before_id=before, decision=verdict,
                                   symbol=symbol, since=since)
    return {"data": [_decision(r) for r in rows],
            "next_cursor": f"dec_{rows[-1]['id']}" if len(rows) == limit else None}


@router.get("/decisions/{decision_id}")
def decision(decision_id: str, key: dict = Depends(api_key)):
    row = _wa.decision_store.get(_decision_id(decision_id))
    if row is None:
        raise PublicApiError(404, "not_found", f"No decision {decision_id}.")
    return _decision(row)


@router.get("/decisions/{decision_id}/replay")
def decision_replay(decision_id: str, key: dict = Depends(api_key)):
    if _wa.decision_store.get(_decision_id(decision_id)) is None:
        raise PublicApiError(404, "not_found", f"No decision {decision_id}.")
    raise PublicApiError(501, "not_available",
                         "Decisions are stored with their scores and rules, not the full market inputs "
                         "they were made from, so they cannot be re-run yet.")


# ------------------------------------------------------------------- positions
def _open_positions() -> list[dict]:
    manager = _wa.instance_manager
    out = []
    for inst in list(manager._instances.values()):  # noqa: SLF001 -- read-only walk
        disposition = manager.open_position_disposition(inst.id, None)
        runtime = manager._runtime.get(inst.id)  # noqa: SLF001
        targets = getattr(runtime[0], "_targets", {}) if runtime else {}
        for row in disposition["open_positions"]:
            entry, stop, mark = row["entry"], row.get("stop"), row.get("mark")
            r_multiple = None
            if mark is not None and stop not in (None, entry):
                direction = 1 if row.get("side") == "long" else -1
                r_multiple = round(direction * (float(mark) - entry) / abs(entry - float(stop)), 3)
            out.append({
                "id": row["position_id"], "instance_id": inst.id, "symbol": row["symbol"],
                "side": row.get("side"), "size": row["size"], "entry": entry,
                "mark": mark, "mark_available": row["mark_available"],
                "unrealized_pnl": row.get("unrealized_pnl"), "r_multiple": r_multiple,
                "protective": {"stop": stop, "target": targets.get(row["symbol"]), "managed_by": "engine"},
                "opened_at": _iso(row.get("opened_at")), "mode": "paper",
            })
    return out


@router.get("/positions")
def positions(key: dict = Depends(api_key)):
    """Open paper positions with their mark and R multiple. Stops and targets
    are managed by the engine, not held at an exchange."""
    return {"data": _open_positions()}


@router.post("/positions/{position_id}/close")
def close_position(position_id: str, body: dict = Body(default={}), key: dict = Depends(control)):
    """Close one paper position at the current observed mark."""
    matches = [p for p in _open_positions() if str(p["id"]) == position_id]
    if not matches:
        raise PublicApiError(404, "not_found", f"No open position {position_id}.")
    target = matches[0]
    siblings = [p for p in _open_positions() if p["instance_id"] == target["instance_id"]]
    if len(siblings) > 1:
        raise PublicApiError(409, "ambiguous_close",
                             "This instance holds several positions; close them from the dashboard.")
    if not target["mark_available"]:
        raise PublicApiError(409, "no_mark", "No fresh price for this position, so it cannot be closed "
                                             "at a real mark yet.")
    result = _wa.instance_manager.close_open_positions(
        target["instance_id"], initiated_by=f"api-key:{key['name']}")
    closed = [c for c in result.get("closed", []) if str(c.get("position_id")) == position_id]
    if not closed:
        raise PublicApiError(409, "not_closed", "The position could not be closed.",
                             remaining=result.get("remaining", []))
    return {"id": position_id, "status": "closed", "close": closed[0],
            "reason": str((body or {}).get("reason", ""))[:200]}


# ------------------------------------------------------------------- backtests
_JOBS: "OrderedDict[str, dict]" = OrderedDict()
_JOBS_LOCK = threading.Lock()
_MAX_KEPT, _MAX_WAITING = 50, 5


def _run_backtest(job_id: str, strategy_name: str, symbol: str, timeframe: str, bars: int) -> None:
    from services.fill_model import RealisticFill
    from services.replay import build_replay
    with _JOBS_LOCK:
        _JOBS[job_id]["status"] = "running"
    try:
        cost_pct = RealisticFill().cost_pct
        gross = build_replay(symbol, timeframe, bars, strategy=strategy_name)
        net = build_replay(symbol, timeframe, bars, strategy=strategy_name, fill_cost_pct=cost_pct)
        meta = net.get("meta", {})
        if not meta.get("bars"):
            # No candles means no backtest -- not a backtest that found nothing.
            raise LookupError(meta.get("data_warning")
                              or f"No historical data for {symbol} {timeframe} on this server.")
        g, n = gross.get("stats", {}), net.get("stats", {})
        result = {
            "gross": {k: g.get(k) for k in ("trades", "win_rate", "expectancy_r", "net_r", "profit_factor",
                                            "max_drawdown_r")},
            "net": {k: n.get(k) for k in ("trades", "win_rate", "expectancy_r", "net_r", "profit_factor",
                                          "max_drawdown_r")},
            "costs": {"cost_pct_per_side": round(cost_pct, 6),
                      "net_r_drag": round((g.get("net_r") or 0) - (n.get("net_r") or 0), 3)},
            "data": {"bars": meta.get("bars"), "start": meta.get("start"), "end": meta.get("end"),
                     "source": meta.get("data_source_label"), "is_real": meta.get("data_is_real"),
                     "warning": meta.get("data_warning"), "timeframe": meta.get("timeframe"),
                     "timeframe_note": meta.get("timeframe_note")},
        }
        status = "complete"
    except LookupError as exc:
        result, status = {"error": str(exc)[:300]}, "failed"
    except Exception as exc:  # noqa: BLE001 -- a failed job is reported, never hidden
        result, status = {"error": f"{type(exc).__name__}: {exc}"[:300]}, "failed"
    with _JOBS_LOCK:
        _JOBS[job_id].update(status=status, result=result,
                             finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


@router.post("/backtests", status_code=202)
def queue_backtest(body: dict = Body(...), key: dict = Depends(control)):
    """Queue a backtest of one built-in strategy on cached Binance candles. The
    result reports gross and net-of-costs figures separately."""
    from services.strategy_registry import entry
    strategy = entry(str(body.get("strategy", "")))
    if strategy is None:
        raise PublicApiError(400, "invalid_request", "strategy must be a strategy id from GET /v1/strategies.")
    if body.get("sweep"):
        raise PublicApiError(400, "invalid_request", "Parameter sweeps are not available through the API yet.")
    symbol = str(body.get("symbol", "BTCUSDT")).upper().replace("/", "")
    timeframe = str(body.get("timeframe", "15m"))
    try:
        bars = max(300, min(int(body.get("bars", 800)), 1500))
    except (TypeError, ValueError):
        raise PublicApiError(400, "invalid_request", "bars must be a number between 300 and 1500.") from None
    with _JOBS_LOCK:
        waiting = sum(1 for j in _JOBS.values() if j["status"] in ("queued", "running"))
        if waiting >= _MAX_WAITING:
            raise PublicApiError(429, "rate_limited", "Too many backtests waiting; try again shortly.",
                                 headers={"Retry-After": "30"})
        job_id = f"bt_{secrets.token_hex(6)}"
        _JOBS[job_id] = {"id": job_id, "status": "queued", "key_id": key["id"],
                         "request": {"strategy": strategy.strategy_id, "symbol": symbol,
                                     "timeframe": timeframe, "bars": bars},
                         "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "result": None}
        while len(_JOBS) > _MAX_KEPT:
            _JOBS.popitem(last=False)
    threading.Thread(target=_run_backtest, name=f"api-{job_id}", daemon=True,
                     args=(job_id, strategy.display_name, symbol, timeframe, bars)).start()
    return {"id": job_id, "status": "queued"}


@router.get("/backtests/{job_id}")
def backtest(job_id: str, key: dict = Depends(api_key)):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["key_id"] != key["id"]:
            raise PublicApiError(404, "not_found", f"No backtest {job_id} for this key.")
        return {k: v for k, v in job.items() if k != "key_id"}
