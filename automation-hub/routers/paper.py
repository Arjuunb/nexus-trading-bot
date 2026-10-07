"""Paper endpoints — split from webhook_api.py.

Endpoint bodies are unchanged except that references to shared state resolve via
``_wa.<name>`` so singletons (pipeline, ledger, paper, engine, …) are read from
webhook_api at request time. That keeps the test suite's fixture rebinding
(``webhook_api.pipeline = <fresh>``) working exactly as before the split.
"""
import math as _math
import webhook_api as _wa
from fastapi import APIRouter, Header, HTTPException, Body, Query, Depends  # noqa: F401
from typing import Optional, List, Dict  # noqa: F401

# Fallback: expose every webhook_api global by name so references the qualifier
# intentionally left bare (e.g. inside f-strings) still resolve. Qualified
# `_wa.<name>` uses stay dynamic; these copies are only a safety net.
globals().update({k: v for k, v in vars(_wa).items()
                  if not k.startswith("__") and k != "router"})

router = APIRouter()


def _persistence_status() -> dict:
    """Is account/trade state actually persistent? Supabase counts only when it
    really CONNECTED at boot (probe passed) — configured-but-broken shows the
    exact error instead of silently claiming persistence."""
    from data import ledger as _ledger_mod
    env = _wa._os.environ.get
    on_cloud = bool(env("RENDER") or env("DYNO"))
    data_dir_set = bool(env("HUB_DATA_DIR"))
    sb = _ledger_mod.SUPABASE_STATUS
    supabase_ok = bool(sb.get("connected"))
    persistent = data_dir_set or supabase_ok or not on_cloud
    warning = None
    if sb.get("configured") and not supabase_ok:
        warning = ("Supabase is configured but NOT connected "
                   f"({sb.get('error') or 'unknown error'}) — using local SQLite. "
                   "Run automation-hub/data/ledger_schema.sql in the Supabase SQL "
                   "editor and verify SUPABASE_URL / SUPABASE_KEY (service_role), "
                   "then redeploy.")
        if not persistent:
            warning += " Until fixed, capital and trades may reset on redeploy."
    elif not persistent:
        warning = ("No persistent storage configured — capital and trades may "
                   "reset on redeploy. Free fix: set SUPABASE_URL + SUPABASE_KEY "
                   "(free Supabase Postgres), or attach a disk and set HUB_DATA_DIR.")
    return {"persistent": persistent, "supabase": supabase_ok,
            "data_dir": data_dir_set, "warning": warning}


@router.get("/paper/account")
def paper_account():
    """Paper account with initial_capital and current_equity kept SEPARATE and
    the current values persisted, so capital survives logout / refresh / restart
    (with HUB_DATA_DIR). Legacy keys (starting_balance / balance) are kept."""
    acct = _wa.account_store.get() or {}
    realized = _wa.paper.realized_pnl()
    initial = float(acct.get("initial_capital", _wa.paper.starting_balance))
    current_equity = _wa.paper.balance()          # initial + realized P&L
    available = _wa.paper.available_balance()
    persist = _persistence_status()
    return {
        # separated, persisted concepts
        "initial_capital": initial,
        "current_equity": current_equity,
        "available_balance": available,
        "realized_pnl": realized,
        "fees_paid": _wa.paper.fees_paid(),
        "unrealized_pnl": float(acct.get("unrealized_pnl", 0.0)),
        "last_updated": acct.get("last_updated"),
        "open_positions": len(_wa.paper.positions()),
        "persistent": persist["persistent"],
        "storage": ("supabase" if persist["supabase"] else "disk" if persist["data_dir"] else "ephemeral"),
        "warning": persist["warning"],
        # legacy keys (unchanged) so existing callers keep working
        "starting_balance": initial,
        "balance": current_equity,
    }


class InitialCapital(_wa.BaseModel):
    amount: float
    confirm: bool = False       # must be true — resets the paper account
    reset_trades: bool = True   # clear trade history so equity == new initial


@router.post("/paper/initial-capital")
def paper_set_initial_capital(body: InitialCapital,
                              x_webhook_secret: Optional[str] = Header(default=None)):
    """Change the initial capital. This RESETS the paper account, so it requires
    an explicit ``confirm: true``. Never touches live trading."""
    _wa._check_secret(x_webhook_secret)
    if not body.confirm:
        raise HTTPException(400, "Changing initial capital resets the paper account — "
                                 "resend with confirm=true to proceed.")
    if body.amount <= 0:
        raise HTTPException(400, "initial capital must be positive")
    if body.reset_trades:
        try:
            _wa.ledger.reset_paper()   # clears trades + positions if supported
        except Exception:  # noqa: BLE001 — some ledgers can't reset; snapshot still resets
            pass
        else:
            # The journal's open paper-engine trades just lost their ledger rows:
            # end them honestly (no fabricated fill). Never blocks the reset.
            journal = getattr(_wa, "trade_journal", None)
            if journal is not None:
                try:
                    journal.cancel_open_engine_trades(
                        reason="Paper account reset by an initial-capital change; "
                               "no execution fill was fabricated.")
                except Exception:  # noqa: BLE001
                    pass
    _wa.account_store.set_initial_capital(body.amount, reset_account=True)
    _wa.paper.starting_balance = body.amount
    _wa.paper._persist_account_snapshot()
    _wa.ledger.log(level="warning", stage="account",
                   message=f"Initial capital set to {body.amount} — paper account reset.")
    return {"ok": True, **(paper_account())}

@router.get("/paper/positions")
def paper_positions():
    poss = _wa.paper.positions()
    # Enrich each OPEN position with the take-profit the engine is actually
    # enforcing (held in memory — there is no target column), so the terminal can
    # draw the real SL/TP the bot manages. Never invents a target.
    try:
        snap = _wa.engine.managed_snapshot()
    except Exception:  # noqa: BLE001 — a missing/other engine must never break the list
        snap = {}
    for p in poss:
        lvl = snap.get(p.get("symbol"))
        if lvl and lvl.get("target") is not None:
            p["target"] = lvl["target"]
    return poss


class AdjustLevels(_wa.BaseModel):
    symbol: str
    stop: Optional[float] = None
    target: Optional[float] = None


@router.post("/paper/stop-target")
def paper_stop_target(body: AdjustLevels,
                      x_webhook_secret: Optional[str] = Header(default=None)):
    """Adjust the stop-loss and/or take-profit on an OPEN paper position — the
    backing endpoint for on-chart drag-to-move. The stop is persisted to the
    ledger and pushed into the engine's live managed state (enforced next bar);
    the target and management state are persisted too. Paper only — never live."""
    _wa._check_secret(x_webhook_secret)
    symbol = (body.symbol or "").upper().strip()
    if not symbol:
        raise HTTPException(400, "symbol is required")
    stop = None if body.stop is None else float(body.stop)
    target = None if body.target is None else float(body.target)
    if stop is None and target is None:
        raise HTTPException(400, "Provide a stop and/or target to update.")
    for label, v in (("stop", stop), ("target", target)):
        if v is not None and (not _math.isfinite(v) or v <= 0):
            raise HTTPException(400, f"{label} must be a positive price.")
    pos = _wa.paper.open_position(symbol)
    if pos is None:
        raise HTTPException(404, f"No open paper position for {symbol}.")
    side = pos.get("side")
    # Effective levels after this change — validate SL/TP stay on the right sides
    # so the operator can't cross them into an instantly-nonsensical bracket.
    try:
        managed = _wa.engine.managed_snapshot().get(symbol) or {}
    except Exception:  # noqa: BLE001
        managed = {}
    eff_stop = stop if stop is not None else pos.get("stop")
    eff_target = target if target is not None else (managed.get("target") or pos.get("target"))
    if eff_stop is not None and eff_target is not None:
        if side == "long" and not (eff_stop < eff_target):
            raise HTTPException(400, "For a long, the stop must sit below the target.")
        if side == "short" and not (eff_stop > eff_target):
            raise HTTPException(400, "For a short, the stop must sit above the target.")
    # Persist the stop (durable, through the paper engine's own ledger) then push
    # both into the live managed state.
    if stop is not None:
        _wa.paper.update_stop(symbol, stop)
    applied = _wa.engine.apply_manual_levels(symbol, stop=stop, target=target)
    _wa.ledger.log(level="info", stage="execution", symbol=symbol,
                   message=f"Manual level update {symbol}: "
                           + (f"SL→{stop} " if stop is not None else "")
                           + (f"TP→{target}" if target is not None else ""))
    return {"ok": True, "symbol": symbol, "side": side,
            "entry": pos.get("entry"),
            "stop": applied.get("stop") if applied.get("stop") is not None else pos.get("stop"),
            "target": applied.get("target")}


class ClosePosition(_wa.BaseModel):
    symbol: str
    # optional client-observed mark (the terminal already streams the live
    # price); the server prefers its OWN fetched price and only uses this if it
    # can't reach a data source — it is never used to fabricate a fill.
    price: Optional[float] = None


@router.post("/paper/close")
def paper_close(body: ClosePosition,
                x_webhook_secret: Optional[str] = Header(default=None)):
    """Manually close an open PAPER position at the current market price through
    the real paper execution engine (same close path the engine uses on a
    stop/target). Never touches live trading."""
    _wa._check_secret(x_webhook_secret)
    symbol = (body.symbol or "").upper().strip()
    if not symbol:
        raise HTTPException(400, "symbol is required")
    pos = _wa.paper.open_position(symbol)
    if pos is None:
        raise HTTPException(404, f"No open paper position for {symbol}.")
    # real current price: latest candle close (crypto/Yahoo). Fall back to the
    # client's observed mark only if the server can't fetch one right now.
    price = None
    try:
        from data.market_data import get_bars
        bars, _src = get_bars(symbol, n=3, timeframe="1h")
        if bars:
            price = float(bars[-1].close)
    except Exception:  # noqa: BLE001 — fetch failure falls through to client mark
        price = None
    if price is None and body.price and float(body.price) > 0:
        price = float(body.price)
    if price is None or price <= 0:
        raise HTTPException(503, "Could not determine a current market price to close at. "
                                 "Try again once market data is reachable.")
    res = _wa.paper.close(symbol=symbol, exit_price=price,
                          exit_context={"exit_reason": "MANUAL_CLOSE",
                                        "exit_reason_source": "OPERATOR", "actor": "operator"})
    if getattr(res, "action", "") != "closed":
        raise HTTPException(409, f"Could not close {symbol} — position not open.")
    _wa.ledger.log(level="info", stage="execution",
                   message=f"Manual close {symbol} @ {round(price, 6)} — "
                           f"realized {res.pnl:+.2f}")
    return {"ok": True, "symbol": symbol, "exit_price": round(price, 6),
            "pnl": round(res.pnl, 2), "size": res.size, "side": res.side,
            **paper_account()}

@router.get("/paper/trades")
def paper_trades():
    return _wa.paper.history()

@router.get("/ledger/logs")
def ledger_logs(limit: int = 200):
    return _wa.ledger.get_logs(limit)

@router.get("/ledger/alerts")
def ledger_alerts(limit: int = 100):
    return _wa.ledger.get_alerts(limit)

@router.get("/ledger/logs/export")
def export_logs(fmt: str = "csv", limit: int = 2000):
    return _wa._export(_wa.ledger.get_logs(limit), ["ts", "level", "stage", "symbol", "message"], fmt, "decision_logs")

@router.get("/ledger/alerts/export")
def export_alerts(fmt: str = "csv", limit: int = 1000):
    return _wa._export(_wa.ledger.get_alerts(limit), ["ts", "severity", "category", "title", "detail"], fmt, "alerts")

@router.get("/paper/trades/export")
def export_trades(fmt: str = "csv"):
    return _wa._export(_wa.paper.history(), ["symbol", "side", "size", "entry", "exit", "pnl", "rr",
                                     "opened_at", "closed_at"], fmt, "paper_trades")

@router.get("/paper/equity-curve")
def paper_equity_curve():
    """Realized-equity curve: starting balance + cumulative closed-trade P&L."""
    trades = sorted((t for t in _wa.paper.history() if t.get("closed_at")),
                    key=lambda t: t["closed_at"])
    eq = _wa.paper.starting_balance
    points = [{"t": None, "equity": round(eq, 2)}]
    for t in trades:
        eq += (t.get("pnl") or 0.0)
        points.append({"t": t.get("closed_at"), "equity": round(eq, 2)})
    return {"starting_balance": _wa.paper.starting_balance, "points": points}


# ── portfolio ───────────────────────────────────────────────────────────────
# One view of capital across every venue, computed by tradexa.portfolio. The
# arithmetic deliberately does not live here or in the execution engine: a
# second broker must mean a second VenueSnapshot, not a second copy of the
# equity, exposure and Sharpe calculations.
#
# The import below is deliberately UNGUARDED, unlike the pipeline's. A guarded
# import here would answer 200 with an empty portfolio if tradexa were missing
# from the deployment — which is precisely how the risk-engine veto went absent
# for a release without anyone noticing. These two endpoints failing loudly is
# the correct blast radius: it is scoped to them (the import is inside the
# handler, so boot is unaffected) and it is visible. tests/test_packaging.py
# guards the packaging side.

def _live_marks(symbols: list[str]) -> dict:
    """Last traded price per symbol, best effort.

    A symbol that cannot be priced is simply ABSENT from the result — never
    filled in from the entry price. The portfolio engine reports equity as
    unavailable when a position is unmarked, and that is the correct answer;
    substituting entry would report an open loss as break-even.

    Binance answers HTTP 451 from US datacenter IPs, which is where this
    deploys, so an empty result is a normal outcome rather than a bug.
    """
    out: dict = {}
    for symbol in dict.fromkeys(symbols):
        try:
            from data.live_data import fetch_ohlcv
            bars = fetch_ohlcv(symbol, timeframe="1h", limit=1)
            if bars:
                out[symbol] = float(bars[-1].close)
        except Exception:  # noqa: BLE001 — an unpriceable symbol stays unmarked
            continue
    return out


@router.get("/portfolio/snapshot")
def portfolio_snapshot(marks: bool = True):
    """Balance, equity, buying power, margin, exposure, P&L, returns, win rate,
    expectancy, Sharpe and max drawdown — per venue and in aggregate.

    ``notes`` carries everything the figures do NOT account for, and
    ``available`` is false whenever it is non-empty. They travel with the
    numbers rather than in a separate call, so there is no version of this
    payload that shows the figures without the caveats.
    """
    from services import portfolio_view
    live = _live_marks([p["symbol"] for p in _wa.paper.positions()]) if marks else {}
    return portfolio_view.snapshot(paper=_wa.paper, registry=_wa.broker_registry,
                                   marks=live)


@router.get("/portfolio/venues")
def portfolio_venues(marks: bool = True):
    """The same figures, per venue. Useful answer to "which account did that?"."""
    from services import portfolio_view
    live = _live_marks([p["symbol"] for p in _wa.paper.positions()]) if marks else {}
    data = portfolio_view.snapshot(paper=_wa.paper, registry=_wa.broker_registry,
                                   marks=live)
    return {"venues": data.get("per_venue", []), "base_currency": data.get("base_currency"),
            "notes": data.get("notes", [])}
