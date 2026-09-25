"""Settings endpoints — split from webhook_api.py.

Endpoint bodies are unchanged except that references to shared state resolve via
``_wa.<name>`` so singletons (pipeline, ledger, paper, engine, …) are read from
webhook_api at request time. That keeps the test suite's fixture rebinding
(``webhook_api.pipeline = <fresh>``) working exactly as before the split.
"""
import webhook_api as _wa
from fastapi import APIRouter, Header, HTTPException, Body, Query, Depends, Request  # noqa: F401
from typing import Optional, List, Dict  # noqa: F401

# Fallback: expose every webhook_api global by name so references the qualifier
# intentionally left bare (e.g. inside f-strings) still resolve. Qualified
# `_wa.<name>` uses stay dynamic; these copies are only a safety net.
globals().update({k: v for k, v in vars(_wa).items()
                  if not k.startswith("__") and k != "router"})

router = APIRouter()


@router.get("/alerts/channels")
def alerts_channels():
    """Which alert channels are connected (Telegram / Discord / Email) — never
    exposes a secret value."""
    return {"channels": _wa.alert_channels.status()}

@router.post("/alerts/channels")
def alerts_channels_save(body: _wa.AlertChannelSave, x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    """Save alert-channel credentials (Discord webhook / SMTP). Secret-gated."""
    _wa._check_secret(x_webhook_secret)
    return {"channels": _wa.alert_channels.save(body.model_dump(exclude_none=True))}

@router.get("/alerts/check")
def alerts_check():
    """Evaluate live alert conditions from real state (drawdown, sentiment,
    funding, strategy health) and return the alerts that would fire."""
    from services.alerts import evaluate_alerts
    from services.recovery import drawdown_recovery, health_scorecard
    # drawdown from the paper equity curve
    trades = sorted((t for t in _wa.paper.history() if t.get("closed_at")), key=lambda t: t["closed_at"])
    eq = peak = _wa.paper.starting_balance
    for t in trades:
        eq += (t.get("pnl") or 0.0); peak = max(peak, eq)
    dd = drawdown_recovery(peak, _wa.paper.balance())["drawdown_pct"]
    ctx = {"drawdown_pct": dd, "underperforming": [], "events": []}
    # market context (best-effort; None when offline so no alert fires)
    try:
        from services.sentiment import fetch_fear_greed
        fg = fetch_fear_greed()
        if fg:
            ctx["fear_greed"] = fg["value"]
    except Exception:  # noqa: BLE001
        pass
    try:
        from services.market_context import fetch_funding_rate
        fr = fetch_funding_rate("BTCUSDT")
        if fr.get("available"):
            ctx["funding_rate_pct"] = fr["value"]
    except Exception:  # noqa: BLE001
        pass
    # strategy health on recent paper trades
    rtrades = [{"pnl": t.get("pnl", 0.0), "r": t.get("rr", 0.0)} for t in trades]
    if len(rtrades) >= 8:
        card = health_scorecard(rtrades)
        if card["unhealthy"]:
            ctx["underperforming"] = [{"strategy": _wa.settings.auto_strategy,
                                       "reason": f"PF {card['profit_factor']}, confidence {card['confidence_score']}"}]
    return {"alerts": evaluate_alerts(ctx), "context": {"drawdown_pct": dd}}

@router.post("/alerts/test")
def alerts_test(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    """Send a test alert to every configured channel."""
    _wa._check_secret(x_webhook_secret)
    from services.alerts import dispatch_alert
    alert = {"type": "test", "severity": "info", "title": "Test alert",
             "detail": "Automation Hub alerts are wired up correctly."}
    return dispatch_alert(alert, _wa.alert_channels)

@router.get("/econ/protection")
def econ_protection():
    """Economic-event protection — halt / reduce-size / widen-stops around the
    next high-impact macro event (#7)."""
    from services.econ_guard import evaluate, EVENT_TYPES
    # The same window the engine enforces, so this never reads "normal" while
    # the pipeline is still refusing entries after a release.
    after_min = int(getattr(_wa.pipeline, "econ_after_min", 0) or 0)
    out = evaluate(_wa.econ_calendar.events(), after_min=after_min)
    out["blackout_after_min"] = after_min
    out["connected"] = _wa.econ_calendar.connected
    out["tracked_event_types"] = EVENT_TYPES
    out["feed"] = _wa.econ_feed.status()
    out["manual_events"] = len(_wa.econ_calendar.manual_events())
    if not _wa.econ_calendar.connected:
        feed = out["feed"]
        why = ("the calendar feed is turned off (HUB_ECON_FEED=off)" if not feed["enabled"]
               else f"the calendar feed has not fetched yet ({feed['last_error']})" if feed["last_error"]
               else "the calendar feed has not fetched yet")
        out["note"] = (f"No economic calendar connected: {why}, and no events were added by hand. "
                       "Event protection stays off rather than guessing dates.")
    return out


@router.post("/econ/feed/sync")
def econ_feed_sync(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    """Fetch the economic calendar now instead of waiting for the next refresh."""
    _wa._check_secret(x_webhook_secret)
    return _wa.econ_feed.sync()

@router.post("/econ/events")
def econ_set_events(body: _wa.EconEvents, x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    """Set the upcoming economic events the guard watches."""
    _wa._check_secret(x_webhook_secret)
    return {"events": _wa.econ_calendar.set_events(body.events)}

@router.get("/execution/fill-model")
def execution_fill_model():
    """The live paper engine's fill model (perfect vs realistic friction)."""
    return _wa.paper.fill_model.status()

@router.post("/execution/fill-model")
def set_execution_fill_model(body: _wa.FillModelBody, x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    """Switch the live paper engine between ideal and realistic fills (#9)."""
    _wa._check_secret(x_webhook_secret)
    from services.fill_model import PerfectFill, RealisticFill
    if body.model == "realistic":
        _wa.paper.fill_model = RealisticFill(spread_pct=body.spread_pct, slippage_pct=body.slippage_pct,
                                         partial_fill_prob=body.partial_fill_prob, reject_prob=body.reject_prob)
    else:
        _wa.paper.fill_model = PerfectFill()
    _wa.save_overrides(_wa.settings.settings_path, _wa._settings_snapshot())
    _wa.ledger.log(level="info", stage="execution", message=f"Fill model set to {_wa.paper.fill_model.name}")
    return _wa.paper.fill_model.status()

@router.get("/report/daily")
def report_daily_preview():
    """The daily digest, on demand (same content Telegram receives)."""
    from services.daily_report import format_report
    data = _wa._daily_report_data()
    return {"report": data, "text": format_report(data),
            "telegram_configured": _wa.notifier.configured,
            "scheduled_hour_utc": _wa.daily_tasks.hour,
            "last_sent_day": _wa.daily_tasks.last_sent_day}

@router.post("/report/daily/send")
def report_daily_send(x_webhook_secret: str = _wa.Header(default="")):
    """Send the daily report to Telegram right now (also runs the backup)."""
    _wa._check_secret(x_webhook_secret)
    return {"sent": _wa.notifier.configured, "report": _wa.daily_tasks.run_once()}

@router.get("/execution/quality")
def execution_quality_report():
    """Fill-by-fill execution grade: measured slippage (maker vs taker, per
    symbol) and whether the fill model's assumptions match reality."""
    return _wa.exec_quality.report(_wa.paper.fill_model)

@router.get("/execution/readiness")
def execution_readiness(symbols: str = "BTCUSDT,ETHUSDT"):
    """Live-exchange readiness checklist (keys, ccxt, testnet mode, connectivity,
    symbol filters) + startup reconciliation of ledger vs exchange positions.
    Anything missing reports as not ready — no pretending."""
    from execution.live_readiness import live_readiness, make_live_broker, reconcile_startup
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    report = live_readiness(symbols=syms)
    if report["ready"]:
        try:
            broker = make_live_broker()
            report["reconciliation"] = reconcile_startup(
                _wa.ledger.get_positions("open"), broker.get_account().positions)
        except Exception as e:  # noqa: BLE001
            report["reconciliation"] = {"error": str(e)}
    return report

@router.get("/execution/realism")
def execution_realism(symbol: str = "BTCUSDT", strategy: str = "Decision Brain",
                      timeframe: str = "15m", limit: int = 800,
                      spread_pct: float = 0.0002, slippage_pct: float = 0.0003,
                      partial_fill_prob: float = 0.1, reject_prob: float = 0.02):
    """Re-price a real run with spread / slippage / latency / partial fills /
    rejections — ideal vs realistic (#9)."""
    from services.replay import build_replay
    from services.execution_sim import apply_execution_realism
    rep = build_replay(symbol, timeframe, limit, strategy=strategy)
    if rep["meta"]["bars"] == 0:
        return {"available": False, "error": rep["meta"].get("data_warning", "No data.")}
    out = apply_execution_realism(rep["trades"], spread_pct=spread_pct, slippage_pct=slippage_pct,
                                  partial_fill_prob=partial_fill_prob, reject_prob=reject_prob)
    out["available"] = True; out["symbol"] = symbol; out["strategy"] = strategy
    return out

@router.get("/settings")
def get_settings():
    """Real current configuration. `editable` persists; `readonly` is env-set."""
    return {
        "editable": {
            "risk_per_trade_pct": _wa.pipeline.risk_per_trade_pct,
            "exposure_limit_pct": _wa.pipeline.exposure_limit_pct,
            "max_drawdown_pct": _wa.pipeline.max_drawdown_pct,
            "max_open_positions": _wa.pipeline.max_open_positions,
            "dedup_window_s": _wa.pipeline.dedup.window_seconds,
            "max_daily_loss_pct": _wa.pipeline.max_daily_loss_pct,
            "session_start": _wa.pipeline.session_start,
            "session_end": _wa.pipeline.session_end,
            "max_weekly_loss_pct": _wa.pipeline.max_weekly_loss_pct,
            "max_trades_per_day": _wa.pipeline.max_trades_per_day,
            "max_consecutive_losses": _wa.pipeline.max_consecutive_losses,
            "cooldown_after_loss_min": _wa.pipeline.cooldown_after_loss_min,
            "trading_days_mask": _wa.pipeline.trading_days_mask,
            "entry_mode": _wa.engine.entry_mode,
            "daily_report_hour": _wa.daily_tasks.hour,
            "min_quality_score": _wa.engine.min_quality_score,
            "streak_risk_scaling": _wa.pipeline.streak_risk_scaling,
            "position_sizing_mode": _wa.pipeline.position_sizing_mode,
            "fixed_position_size": _wa.pipeline.fixed_position_size,
        },
        "readonly": {
            "strategy": _wa.engine.strategy_label,
            "strategy_key": _wa.settings.auto_strategy,
            "timeframe": _wa.engine.timeframe,
            "symbols": _wa.engine.symbols,
            "starting_cash": _wa.paper.starting_balance,
            "data_source": "live (ccxt)" if _wa.engine.live else "synthetic / replay",
            "poll_seconds": _wa.engine.live_poll_s if _wa.engine.live else None,
            "mode": "paper",
            "broker_connected": False,
            "webhook_secret_set": bool(_wa.settings.webhook_secret),
            "telegram_configured": bool(_wa.settings.telegram_token),
        },
        "metadata": {
            "editable": {"scope": "legacy", "source": "runtime override",
                         "editable": True, "restart_required": False},
            "readonly": {"scope": "environment", "source": "environment",
                         "editable": False, "restart_required": True},
            "warning": ("These settings control the legacy autonomous engine "
                        "and do not configure Trading Instances."),
        },
    }

@router.post("/settings")
def update_settings(body: _wa.SettingsUpdate, request: Request,
                    x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    before = _wa._settings_snapshot()
    changed = {}
    if body.risk_per_trade_pct is not None:
        if not (0 < body.risk_per_trade_pct <= 0.5):
            raise _wa.HTTPException(400, "risk_per_trade_pct must be in (0, 0.5]")
        _wa.pipeline.risk_per_trade_pct = body.risk_per_trade_pct
        changed["risk_per_trade_pct"] = body.risk_per_trade_pct
    if body.exposure_limit_pct is not None:
        if not (0 < body.exposure_limit_pct <= 1):
            raise _wa.HTTPException(400, "exposure_limit_pct must be in (0, 1]")
        _wa.pipeline.exposure_limit_pct = body.exposure_limit_pct
        changed["exposure_limit_pct"] = body.exposure_limit_pct
    if body.max_drawdown_pct is not None:
        if not (0 < body.max_drawdown_pct <= 1):
            raise _wa.HTTPException(400, "max_drawdown_pct must be in (0, 1]")
        _wa.pipeline.max_drawdown_pct = body.max_drawdown_pct
        changed["max_drawdown_pct"] = body.max_drawdown_pct
    if body.max_open_positions is not None:
        if not (1 <= body.max_open_positions <= 50):
            raise _wa.HTTPException(400, "max_open_positions must be in [1, 50]")
        _wa.pipeline.max_open_positions = int(body.max_open_positions)
        changed["max_open_positions"] = int(body.max_open_positions)
    if body.dedup_window_s is not None:
        if not (0 <= body.dedup_window_s <= 86400):
            raise _wa.HTTPException(400, "dedup_window_s must be in [0, 86400]")
        _wa.pipeline.dedup.window_seconds = int(body.dedup_window_s)
        changed["dedup_window_s"] = int(body.dedup_window_s)
    if body.max_daily_loss_pct is not None:
        if not (0 <= body.max_daily_loss_pct <= 1):
            raise _wa.HTTPException(400, "max_daily_loss_pct must be in [0, 1]")
        _wa.pipeline.max_daily_loss_pct = float(body.max_daily_loss_pct)
        changed["max_daily_loss_pct"] = float(body.max_daily_loss_pct)
    if body.session_start is not None:
        if not (0 <= body.session_start <= 24):
            raise _wa.HTTPException(400, "session_start must be in [0, 24]")
        _wa.pipeline.session_start = int(body.session_start)
        changed["session_start"] = int(body.session_start)
    if body.session_end is not None:
        if not (0 <= body.session_end <= 24):
            raise _wa.HTTPException(400, "session_end must be in [0, 24]")
        _wa.pipeline.session_end = int(body.session_end)
        changed["session_end"] = int(body.session_end)
    if body.max_weekly_loss_pct is not None:
        if not (0 <= body.max_weekly_loss_pct <= 1):
            raise _wa.HTTPException(400, "max_weekly_loss_pct must be in [0, 1]")
        _wa.pipeline.max_weekly_loss_pct = float(body.max_weekly_loss_pct)
        changed["max_weekly_loss_pct"] = float(body.max_weekly_loss_pct)
    for k in ("max_trades_per_day", "max_consecutive_losses", "cooldown_after_loss_min"):
        v = getattr(body, k)
        if v is not None:
            if not (0 <= v <= 1000):
                raise _wa.HTTPException(400, f"{k} must be in [0, 1000]")
            setattr(_wa.pipeline, k, int(v))
            changed[k] = int(v)
    if body.trading_days_mask is not None:
        if not (0 <= body.trading_days_mask <= 127):
            raise _wa.HTTPException(400, "trading_days_mask must be in [0, 127]")
        _wa.pipeline.trading_days_mask = int(body.trading_days_mask)
        changed["trading_days_mask"] = int(body.trading_days_mask)
    if body.entry_mode is not None:
        if body.entry_mode not in ("limit", "market"):
            raise _wa.HTTPException(400, "entry_mode must be 'limit' or 'market'")
        _wa.engine.entry_mode = body.entry_mode
        changed["entry_mode"] = body.entry_mode
    if body.daily_report_hour is not None:
        if not (-1 <= body.daily_report_hour <= 23):
            raise _wa.HTTPException(400, "daily_report_hour must be -1 (off) .. 23 (UTC)")
        _wa.daily_tasks.hour = int(body.daily_report_hour)
        changed["daily_report_hour"] = int(body.daily_report_hour)
    if body.min_quality_score is not None:
        if not (0 <= body.min_quality_score <= 100):
            raise _wa.HTTPException(400, "min_quality_score must be in [0, 100] (0 disables)")
        _wa.engine.min_quality_score = int(body.min_quality_score)
        changed["min_quality_score"] = int(body.min_quality_score)
    if body.streak_risk_scaling is not None:
        _wa.pipeline.streak_risk_scaling = bool(body.streak_risk_scaling)
        changed["streak_risk_scaling"] = bool(body.streak_risk_scaling)
    proposed_sizing_mode = (body.position_sizing_mode if body.position_sizing_mode is not None
                            else _wa.pipeline.position_sizing_mode)
    proposed_fixed_size = (body.fixed_position_size if body.fixed_position_size is not None
                           else _wa.pipeline.fixed_position_size)
    if proposed_sizing_mode not in ("auto", "fixed"):
        raise _wa.HTTPException(400, "position_sizing_mode must be 'auto' or 'fixed'")
    if not (0 <= float(proposed_fixed_size) <= 1_000_000_000):
        raise _wa.HTTPException(400, "fixed_position_size must be in [0, 1000000000]")
    if proposed_sizing_mode == "fixed" and float(proposed_fixed_size) <= 0:
        raise _wa.HTTPException(400, "fixed_position_size must be greater than zero in manual mode")
    if body.position_sizing_mode is not None:
        _wa.pipeline.position_sizing_mode = proposed_sizing_mode
        changed["position_sizing_mode"] = proposed_sizing_mode
    if body.fixed_position_size is not None:
        _wa.pipeline.fixed_position_size = float(proposed_fixed_size)
        changed["fixed_position_size"] = float(proposed_fixed_size)

    snap = _wa._settings_snapshot()
    _wa.save_overrides(_wa.settings.settings_path, snap)
    _wa.ledger.log(level="info", stage="audit", message=f"Settings updated: {changed}")
    if changed:
        from services.audit_log import record_change
        record_change(action="settings.update", actor=_wa.request_user(request),
                      before={k: before.get(k) for k in changed}, after=changed,
                      ip=request.client.host if request.client else "")
    return {"saved": True, "editable": snap}

@router.get("/notifications/status")
def notifications_status():
    return {
        "telegram_configured": _wa.notifier.configured,
        "notify_trades": _wa.notifier.notify_trades,
        "notify_risk": _wa.notifier.notify_risk,
        "email": "not configured", "discord": "not configured",
    }

@router.post("/notifications/test")
def notifications_test(x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    sent = _wa.notifier.send("✅ Automation Hub — test notification")
    return {"sent": sent, "configured": _wa.notifier.configured}

@router.post("/notifications")
def notifications_update(body: _wa.NotifUpdate, x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    if body.notify_trades is not None:
        _wa.notifier.notify_trades = bool(body.notify_trades)
    if body.notify_risk is not None:
        _wa.notifier.notify_risk = bool(body.notify_risk)
    _wa.save_overrides(_wa.settings.settings_path, _wa._settings_snapshot())
    return {"notify_trades": _wa.notifier.notify_trades, "notify_risk": _wa.notifier.notify_risk}

@router.get("/data/coverage")
def data_coverage():
    """What real Binance history is cached locally (symbol/timeframe matrix)."""
    from data.historical import SYMBOLS, TIMEFRAMES
    return {"symbols": list(SYMBOLS), "timeframes": list(TIMEFRAMES),
            "coverage": _wa.market_store.all_coverage()}

@router.post("/data/backfill")
def data_backfill(years: float = 3.0, timeframes: str = "1h,4h,1d", candles: int = 0,
                  x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    """Backfill real Binance candles for every symbol on a background thread.
    ``candles`` (flat per-timeframe count) is the QUICK mode the dashboard
    uses; ``years`` is the deep mode for validation. Progress (including a
    live candle counter) at GET /data/backfill/status."""
    _wa._check_secret(x_webhook_secret)
    tfs = tuple(t.strip() for t in timeframes.split(",") if t.strip())
    res = _wa.backfill_job.start(years=years, candles=candles, timeframes=tfs)
    scope = f"{candles} candles" if candles else f"{years}y"
    _wa.ledger.log(level="info", stage="data",
               message=f"Backfill {'started' if res.get('started') else 'skipped'} "
                       f"({scope} × {','.join(tfs)})")
    return res

@router.get("/data/backfill/status")
def data_backfill_status():
    return _wa.backfill_job.status()

@router.get("/data/integrity")
def data_integrity(timeframes: str = "1h,4h,1d"):
    """Verify every cached candle series: gaps, duplicates, corrupt candles.
    A backtest on bad data silently overstates the edge."""
    from data.historical import SYMBOLS
    from data.integrity import verify_store
    tfs = tuple(t.strip() for t in timeframes.split(",") if t.strip())
    return verify_store(_wa.market_store, SYMBOLS, tfs)

@router.post("/data/sync")
def data_sync(symbol: str = "BTCUSDT", timeframe: str = "4h", target_candles: int = 3000,
              x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    """Fetch REAL Binance candles and cache them locally (no synthetic data)."""
    _wa._check_secret(x_webhook_secret)
    from data.historical import sync
    res = sync(_wa.market_store, symbol, timeframe, target_candles=target_candles)
    if "error" in res:
        _wa.ledger.log(level="warning", stage="data", message=f"Sync {symbol} {timeframe}: {res['error']}")
    else:
        _wa.ledger.log(level="info", stage="data",
                   message=f"Synced {symbol} {timeframe}: {res.get('stored')} candles cached")
    return res

@router.post("/data/sync-all")
def data_sync_all(target_candles: int = 2000, x_webhook_secret: _wa.Optional[str] = _wa.Header(default=None)):
    """Sync every supported symbol × timeframe (run once with network to populate)."""
    _wa._check_secret(x_webhook_secret)
    from data.historical import sync, SYMBOLS, TIMEFRAMES
    out = []
    for s in SYMBOLS:
        for tf in TIMEFRAMES:
            out.append(sync(_wa.market_store, s, tf, target_candles=target_candles))
    ok = sum(1 for r in out if "error" not in r)
    return {"synced": ok, "total": len(out), "results": out}
