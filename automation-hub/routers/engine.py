"""Engine endpoints — split from webhook_api.py.

Endpoint bodies are unchanged except that references to shared state resolve via
``_wa.<name>`` so singletons (pipeline, ledger, paper, engine, …) are read from
webhook_api at request time. That keeps the test suite's fixture rebinding
(``webhook_api.pipeline = <fresh>``) working exactly as before the split.
"""
import datetime as _dt

import webhook_api as _wa
from fastapi import APIRouter, Header, HTTPException, Body, Query, Depends  # noqa: F401
from typing import Optional, List, Dict  # noqa: F401

# Fallback: expose every webhook_api global by name so references the qualifier
# intentionally left bare (e.g. inside f-strings) still resolve. Qualified
# `_wa.<name>` uses stay dynamic; these copies are only a safety net.
globals().update({k: v for k, v in vars(_wa).items()
                  if not k.startswith("__") and k != "router"})

router = APIRouter()


# ─────────────────────── Trading modes + approvals (§7, §11) ───────────────
_MODES = ("full", "semi", "signal")


@router.get("/engine/mode")
def get_mode():
    """Current trading mode: full (auto-execute), semi (approve each entry),
    signal (alert only). Exits always execute automatically in every mode."""
    return {"mode": _wa.engine.trading_mode, "modes": list(_MODES),
            "pending_approvals": _wa.approvals.counts()["pending"]}


@router.post("/engine/mode")
def set_mode(body: dict = Body(...), x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    mode = str(body.get("mode", "")).lower()
    if mode not in _MODES:
        raise HTTPException(400, f"mode must be one of {_MODES}")
    _wa.engine.trading_mode = mode
    _wa.save_overrides(_wa.settings.settings_path, _wa._settings_snapshot())
    _wa.ledger.log(level="info", stage="controls", message=f"Trading mode → {mode}")
    return {"mode": mode}


@router.get("/approvals")
def list_approvals(limit: int = 50):
    """Pending trade ideas awaiting approval (semi-auto) plus recent resolved
    ideas (approved / rejected / expired / signalled) for the audit trail."""
    return {"mode": _wa.engine.trading_mode,
            "pending": _wa.approvals.list_pending(),
            "recent": _wa.approvals.list_recent(max(1, min(limit, 200)))}


@router.post("/approvals/{iid}/approve")
def approve_idea(iid: int, x_webhook_secret: Optional[str] = Header(default=None)):
    """Approve a queued idea — routes it through the SAME risk pipeline (all
    gates still apply). 404 if the idea expired or was already resolved."""
    _wa._check_secret(x_webhook_secret)
    idea = _wa.approvals.approve(iid)
    if idea is None:
        raise HTTPException(404, "No pending idea with that id (it may have expired)")
    result = _wa.engine.execute_approved(idea)
    return {"approved": True, "id": iid, "result": result}


@router.post("/approvals/{iid}/reject")
def reject_idea(iid: int, body: dict = Body(default={}),
                x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    idea = _wa.approvals.reject(iid, str(body.get("reason", "")))
    if idea is None:
        raise HTTPException(404, "No pending idea with that id (it may have expired)")
    # grade the rejection like any veto: was the trade we passed on a winner?
    if _wa.counterfactual is not None:
        try:
            _wa.counterfactual.record_veto(
                symbol=idea.get("symbol"),
                side="long" if idea.get("side") == "BUY" else "short",
                entry=idea.get("entry"), stop=idea.get("stop"),
                target=idea.get("target"), rule="approval-reject",
                detail=idea.get("reject_reason", "manual"))
        except Exception:  # noqa: BLE001
            pass
    return {"rejected": True, "id": iid}


# ─────────────────────────── Risk profile presets (§9) ─────────────────────
# Each preset is a coherent bundle; the headline is risk-per-trade (0.5/1/2%),
# with drawdown / daily-loss / exposure / max-positions scaled to match. All
# values are FRACTIONS (0.01 = 1%), the same units POST /settings expects.
RISK_PRESETS = {
    "conservative": {"risk_per_trade_pct": 0.005, "max_open_positions": 2,
                     "max_daily_loss_pct": 0.02, "max_drawdown_pct": 0.10,
                     "exposure_limit_pct": 0.10},
    "balanced": {"risk_per_trade_pct": 0.01, "max_open_positions": 3,
                 "max_daily_loss_pct": 0.03, "max_drawdown_pct": 0.15,
                 "exposure_limit_pct": 0.15},
    "aggressive": {"risk_per_trade_pct": 0.02, "max_open_positions": 5,
                   "max_daily_loss_pct": 0.05, "max_drawdown_pct": 0.25,
                   "exposure_limit_pct": 0.30},
}


def _active_risk_preset() -> Optional[str]:
    """Which preset (if any) the live risk config currently matches exactly."""
    cur = {"risk_per_trade_pct": _wa.pipeline.risk_per_trade_pct,
           "max_open_positions": _wa.pipeline.max_open_positions,
           "max_daily_loss_pct": _wa.pipeline.max_daily_loss_pct,
           "max_drawdown_pct": _wa.pipeline.max_drawdown_pct,
           "exposure_limit_pct": _wa.pipeline.exposure_limit_pct}
    for name, vals in RISK_PRESETS.items():
        if all(abs(float(cur[k]) - float(v)) < 1e-9 for k, v in vals.items()):
            return name
    return None


@router.get("/risk/presets")
def risk_presets():
    return {"presets": RISK_PRESETS, "active": _active_risk_preset()}


@router.post("/risk/preset")
def apply_risk_preset(body: dict = Body(...), x_webhook_secret: Optional[str] = Header(default=None)):
    """Apply a named risk preset to the live engine (persisted). 'custom' is a
    no-op marker — edit individual fields under Settings for custom profiles."""
    _wa._check_secret(x_webhook_secret)
    name = str(body.get("name", "")).lower()
    if name == "custom":
        return {"applied": "custom", "note": "edit individual risk fields under Settings"}
    preset = RISK_PRESETS.get(name)
    if preset is None:
        raise HTTPException(400, f"Unknown preset {name!r}")
    for k, val in preset.items():
        if k == "max_open_positions":
            _wa.pipeline.max_open_positions = int(val)
        else:
            setattr(_wa.pipeline, k, float(val))
    _wa.save_overrides(_wa.settings.settings_path, _wa._settings_snapshot())
    _wa.ledger.log(level="info", stage="audit", message=f"Risk preset applied: {name}")
    return {"applied": name, "values": preset}




# ------------------------------------------------------------------- webhook
@router.post("/webhook/tradingview")
def tradingview_webhook(payload: _wa.WebhookPayload,
                        x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_webhook_secret(x_webhook_secret)
    result = _wa.pipeline.process(payload.model_dump())
    return {"status": "ok", **result.to_dict()}

# ------------------------------------------------------- emergency controls
@router.post("/controls/pause-all")
def pause_all(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    _wa.controls.pause_all()
    _wa.ledger.log(level="warning", stage="controls", message="PAUSE ALL — entries blocked")
    return {"state": _wa.controls.state}

@router.post("/controls/stop-all")
def stop_all(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    _wa.controls.stop_all()
    _wa.ledger.log(level="warning", stage="controls", message="STOP ALL — trading halted")
    return {"state": _wa.controls.state}

@router.post("/controls/resume")
def resume(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    _wa.controls.resume()
    _wa.pipeline.resume()          # also clear any auto-halt (drawdown breaker)
    _wa.ledger.log(level="info", stage="controls", message="RESUME — trading active")
    return {"state": _wa.controls.state, "auto_halted": _wa.pipeline.halted}

# ---------------------------------------------------- autonomous engine
def _engine_payload() -> dict:
    """One authoritative control-plane view for every dashboard surface."""
    from datetime import datetime, timezone

    st = _wa.engine.status()
    state = st.get("lifecycle_state", "stopped")
    reason = st.get("stop_reason")
    recommendation = "Start the paper engine to resume market scanning."
    if st.get("running"):
        if _wa.pipeline.halted:
            state = "paused"
            reason = _wa.pipeline.halt_reason or "Risk Manager paused new entries"
            recommendation = "Review the risk limit, then Resume when it is safe to trade."
        elif _wa.controls.state != "Active":
            state = "paused"
            reason = f"Trading controls are {_wa.controls.state.lower()}"
            recommendation = "Resume trading controls to allow new paper entries."
        elif state not in ("starting", "bootstrapping", "warming", "syncing",
                           "ready", "data_stale", "recovering"):
            state = "running"
            recommendation = "Scanning closed candles; no action is required."
    elif state == "error":
        recommendation = "Review the engine logs, verify exchange connectivity, then Restart Engine."
    elif reason and "Replay data completed" in reason:
        recommendation = "Enable live market data for continuous paper trading, or Start a new replay run."

    uptime_s = None
    try:
        started = datetime.fromisoformat(str(st.get("started_at")).replace("Z", "+00:00"))
        uptime_s = max(0, round((datetime.now(timezone.utc) - started).total_seconds()))
    except (TypeError, ValueError):
        pass
    hour = datetime.now(timezone.utc).hour
    session = "Asia" if hour < 8 else "London" if hour < 16 else "New York"
    st.update({
        "state": state,
        "reason": reason,
        "recommended_action": recommendation,
        "uptime_s": uptime_s,
        "trading_state": _wa.controls.state,
        "auto_halted": _wa.pipeline.halted,
        "halt_reason": _wa.pipeline.halt_reason,
        "connected_exchange": getattr(_wa.settings, "default_exchange", "configured"),
        "websocket_status": (
            "connected" if (st.get("websocket") or {}).get("connected")
            else "REST fallback" if (st.get("websocket") or {}).get("available") is False
            else "not configured"),
        "market_session": session,
        "position_sizing_mode": _wa.pipeline.position_sizing_mode,
        "fixed_position_size": _wa.pipeline.fixed_position_size,
    })
    return st


@router.post("/engine/start")
def engine_start(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    started = _wa.engine.start()
    _wa.save_overrides(_wa.settings.settings_path, _wa._settings_snapshot())
    _wa.ledger.log(level="info", stage="engine", message=f"Operator requested engine start (started={started})")
    return {"started": started, "status": _engine_payload()}


@router.post("/engine/symbol-selection")
def engine_symbol_selection(body: _wa.SymbolSelectionUpdate,
                            x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    """Persist and apply one-pair manual mode or the automatic watchlist."""
    _wa._check_secret(x_webhook_secret)
    mode = (body.mode or "").strip().lower()
    if mode not in ("manual", "auto"):
        raise _wa.HTTPException(400, "mode must be 'manual' or 'auto'")
    auto_symbols = ([s.strip().upper() for s in (body.auto_symbols or _wa.engine.auto_symbols) if s.strip()]
                    or list(_wa.engine.auto_symbols))
    if not auto_symbols:
        raise _wa.HTTPException(400, "At least one automatic watchlist symbol is required")
    manual_symbol = (body.manual_symbol or _wa.engine.manual_symbol or auto_symbols[0]).strip().upper()
    if not manual_symbol:
        raise _wa.HTTPException(400, "A manual symbol is required")
    target = [manual_symbol] if mode == "manual" else auto_symbols
    _wa.engine.symbol_selection_mode = mode
    _wa.engine.manual_symbol = manual_symbol
    _wa.engine.auto_symbols = auto_symbols
    if _wa.engine.running:
        _wa.engine.reconfigure(symbols=target, timeframe=_wa.engine.timeframe,
                               strategy_factory=_wa.engine.strategy_factory,
                               label=_wa.engine.strategy_label)
    else:
        _wa.engine.symbols = target
    _wa.save_overrides(_wa.settings.settings_path, _wa._settings_snapshot())
    descriptor = manual_symbol if mode == "manual" else ", ".join(auto_symbols)
    _wa.ledger.log(level="info", stage="audit",
                   message=f"Pair selection set to {mode}: {descriptor}")
    return {"applied": True, "status": _engine_payload()}

@router.post("/engine/pause")
def engine_pause(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    _wa.controls.pause_all()
    _wa.ledger.log(level="warning", stage="engine", message="Operator paused paper-engine entries")
    return {"paused": True, "status": _engine_payload()}

@router.post("/engine/resume")
def engine_resume(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    _wa.controls.resume()
    _wa.pipeline.resume()
    started = _wa.engine.start() if not _wa.engine.running else False
    _wa.save_overrides(_wa.settings.settings_path, _wa._settings_snapshot())
    _wa.ledger.log(level="info", stage="engine", message=f"Operator resumed paper engine (started={started})")
    return {"resumed": True, "started": started, "status": _engine_payload()}

@router.post("/engine/restart")
def engine_restart(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    started = _wa.engine.restart()
    _wa.save_overrides(_wa.settings.settings_path, _wa._settings_snapshot())
    _wa.ledger.log(level="info", stage="engine", message=f"Operator requested engine restart (started={started})")
    return {"restarted": started, "status": _engine_payload()}

@router.post("/engine/stop")
def engine_stop(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    stopped = _wa.engine.stop()
    _wa.save_overrides(_wa.settings.settings_path, _wa._settings_snapshot())
    _wa.ledger.log(level="warning", stage="engine", message=f"Operator requested engine stop (stopped={stopped})")
    return {"stopped": stopped, "status": _engine_payload()}

@router.get("/engine/status")
def engine_status():
    return _engine_payload()

@router.get("/system/threads")
def system_threads(frames: int = Query(14, ge=1, le=60)):
    """Every live thread and where it currently is.

    A request that hangs leaves no trace: the log shows no exception because
    none is raised, and the caller sees only a gateway timeout. That happened
    to the Price Action status route, which nginx cut off at ninety seconds
    while the app log stayed clean, and no amount of reading it could say which
    call was stuck. This answers that directly, by naming the frame each thread
    is sitting in right now.

    Read-only: it inspects stacks and touches no engine, account or broker
    state. Session-gated like every other control endpoint.
    """
    import sys
    import threading
    import traceback

    names = {thread.ident: thread.name for thread in threading.enumerate()}
    daemon = {thread.ident: thread.daemon for thread in threading.enumerate()}
    rows = []
    for ident, frame in sys._current_frames().items():
        stack = traceback.extract_stack(frame)[-int(frames):]
        rows.append({
            "thread_id": ident,
            "name": names.get(ident, "unknown"),
            "daemon": daemon.get(ident),
            # Innermost last, so the final line is where the thread actually is.
            "stack": ["%s:%d in %s | %s" % (entry.filename.split("/")[-1],
                                            entry.lineno, entry.name,
                                            (entry.line or "").strip())
                      for entry in stack],
        })
    rows.sort(key=lambda row: row["name"])
    return {
        "observed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "thread_count": len(rows),
        "read_only": True,
        "threads": rows,
    }


@router.get("/system/status")
def system_status():
    """Real bot/system health — no fabricated values. Paper-only until a live
    broker is wired (live execution is a future phase)."""
    st = _engine_payload()
    verdict = _inactivity_verdict(st)
    return {
        "mode": "paper",                     # the engine paper-executes; no live broker
        "broker_connected": False,           # honest: no live venue connected
        "data_source": "live (ccxt)" if _wa.engine.live else "synthetic / replay",
        "engine_running": st.get("running", False),
        "engine_state": st.get("state"),
        "engine_reason": st.get("reason"),
        "engine_mode": st.get("mode"),
        "strategy": _wa.engine.strategy_label,
        "symbols": _wa.engine.symbols,
        "timeframe": _wa.engine.timeframe,
        "bars_processed": st.get("bars", 0),
        # A bare "0" reads the same whether the engine is healthily waiting for
        # the next candle to close or the feed died hours ago. These carry the
        # distinction the diagnostics page already makes, so the status bar can
        # say which — same verdict, so the two can never disagree.
        "bars_status": verdict["status"],
        "bars_note": verdict["headline"],
        "bars_severity": verdict["severity"],
        "signals": st.get("signals", 0),
        "trades": st.get("trades", 0),
        "started_at": st.get("started_at"),
        "uptime_s": round(_wa.time.time() - _wa._BOOT, 0),
        "trading_state": _wa.controls.state,
        "auto_halted": _wa.pipeline.halted,
        "halt_reason": _wa.pipeline.halt_reason,
    }

def _last_activity_age_s(st: dict):
    """Seconds since the engine last processed a bar, or None if never/unparseable."""
    from datetime import datetime, timezone
    if not st.get("last_activity"):
        return None
    try:
        la = datetime.fromisoformat(str(st["last_activity"]).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - la).total_seconds()
    except (ValueError, TypeError):
        return None


def _inactivity_verdict(st: dict) -> dict:
    """The one place engine state becomes a plain-English verdict.

    Both /engine/diagnostics and /system/status need this. Assembling the
    arguments twice is how the status bar and the diagnostics page end up
    disagreeing about whether the bot is healthy.
    """
    from services.auto_engine import explain_inactivity
    return explain_inactivity(
        running=st.get("running", False), trading_state=_wa.controls.state,
        mode=st.get("mode", "replay"), timeframe=st.get("timeframe", "4h"),
        bars=st.get("bars", 0), signals=st.get("signals", 0),
        trades=st.get("trades", 0), rejections=st.get("rejections", 0),
        data_source=st.get("data_source"),
        last_activity_age_s=_last_activity_age_s(st),
        feed_error=st.get("feed_error"),
    )


@router.get("/engine/diagnostics")
def engine_diagnostics():
    """Plain-English answer to 'why isn't the bot trading?' — built from real
    engine activity (running state, data feed, bars/signals/rejections, and how
    long since the last new candle)."""
    st = _engine_payload()
    age = _last_activity_age_s(st)
    verdict = _inactivity_verdict(st)
    return {
        **verdict,
        "running": st.get("running", False),
        "state": st.get("state"), "reason": st.get("reason"),
        "recommended_action": st.get("recommended_action"),
        "mode": st.get("mode"),
        "timeframe": st.get("timeframe"),
        "data_source": st.get("data_source"),
        "feed_status": st.get("feed_status"),
        "feed_error": st.get("feed_error"),
        "bars": st.get("bars", 0), "signals": st.get("signals", 0),
        "trades": st.get("trades", 0), "rejections": st.get("rejections", 0),
        "last_bar_ts": st.get("last_bar_ts"),
        "last_activity_age_s": round(age, 0) if age is not None else None,
    }

# ------------------------------------------------------------- read (dashboard)
@router.get("/controls/state")
def control_state():
    return {"state": _wa.controls.state}


# ------------------------------------------------- Explainable Trading reports
@router.get("/engine/cycles")
def engine_cycles(limit: int = 100, symbol: Optional[str] = None,
                  decision: Optional[str] = None):
    """Per-cycle Decision Reports, newest first — EVERY analysis cycle is here,
    including WAIT candles. Filter by symbol or decision (BUY/SELL/WAIT/SKIP)."""
    return {"cycles": _wa.cycle_store.list(limit=max(1, min(limit, 500)),
                                           symbol=symbol, decision=decision),
            "total": _wa.cycle_store.count()}


@router.get("/engine/cycles/{cid}")
def engine_cycle(cid: int):
    """The COMPLETE Decision Report for one cycle: narrated market analysis,
    rule-by-rule checklist, five-category confidence score, decision, reasons
    and recommendation."""
    c = _wa.cycle_store.get(cid)
    if c is None:
        raise HTTPException(404, "No cycle report with that id")
    return c


# Timeframes the data layer supports end-to-end (live fetch, synthetic, engine).
_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d")


@router.post("/engine/timeframe")
def engine_set_timeframe(timeframe: str,
                         x_webhook_secret: Optional[str] = Header(default=None)):
    """Switch the engine's candle timeframe (1m/5m/15m/1h/4h/1d) and restart it.

    The choice is persisted so it survives restarts/redeploys. 4h remains the
    walk-forward-validated config; lower timeframes give faster activity and
    count as their own experiment. Paper only — live stays locked."""
    _wa._check_secret(x_webhook_secret)
    tf = (timeframe or "").strip().lower()
    if tf not in _TIMEFRAMES:
        raise HTTPException(400, f"Unsupported timeframe '{timeframe}'. "
                                 f"Choose one of: {', '.join(_TIMEFRAMES)}.")
    _wa.engine.reconfigure(symbols=_wa.engine.symbols, timeframe=tf,
                           strategy_factory=_wa.engine.strategy_factory,
                           label=_wa.engine.strategy_label)
    # persist alongside the other runtime settings (applied again at boot)
    _wa.save_overrides(_wa.settings.settings_path, _wa._settings_snapshot())
    _wa.ledger.log(level="info", stage="audit",
                   message=f"Engine timeframe set to {tf} (engine restarted)")
    return {"applied": True, "timeframe": _wa.engine.timeframe,
            "options": list(_TIMEFRAMES)}
