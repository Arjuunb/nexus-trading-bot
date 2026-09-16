"""Webhook + ledger API (Kyros Phase 1).

Public, secret-gated endpoint that receives TradingView alerts and runs the
signal pipeline (dedup -> risk -> sizing -> paper execution -> ledger). Plus
emergency controls (Pause/Stop/Resume) and read endpoints the dashboard uses.

Mounted on the existing FastAPI app via ``app.include_router(router)``.
"""
from __future__ import annotations

import hmac
import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from config import settings
from data.ledger import get_ledger
from execution.paper_engine import PaperExecutionEngine
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.market_quality import MarketQualityConfig, MarketQualityGate
from services.signal_pipeline import SignalPipeline
from strategies.builtin_versions import builtin_strategy_version

# --- Phase 1 singletons (one ledger / paper account / control switch) ---
_BOOT = time.time()
ledger = get_ledger(settings.ledger_path)
controls = TradingControl()
from services.fill_model import from_env as _fill_from_env  # noqa: E402
paper = PaperExecutionEngine(ledger, settings.starting_cash, fill_model=_fill_from_env())
from services.execution_quality import ExecutionQuality  # noqa: E402
exec_quality = ExecutionQuality()
paper.quality = exec_quality
quality = MarketQualityGate(MarketQualityConfig(
    min_stop_distance_pct=settings.quality_min_stop_pct,
    max_stop_distance_pct=settings.quality_max_stop_pct,
    max_signal_age_s=settings.quality_max_signal_age_s,
    max_spread_bps=settings.quality_max_spread_bps,
))
pipeline = SignalPipeline(
    ledger, paper, controls,
    equity=settings.starting_cash,
    risk_per_trade_pct=settings.risk_per_trade_pct,
    exposure_limit_pct=settings.exposure_limit_pct,
    dedup_window_s=settings.dedup_window_s,
    quality=quality,
    max_drawdown_pct=settings.max_drawdown_pct,
    max_open_positions=settings.max_open_positions,
    max_daily_loss_pct=settings.max_daily_loss_pct,
    session_start=settings.session_start,
    session_end=settings.session_end,
    max_weekly_loss_pct=settings.max_weekly_loss_pct,
    max_trades_per_day=settings.max_trades_per_day,
    max_consecutive_losses=settings.max_consecutive_losses,
    cooldown_after_loss_min=settings.cooldown_after_loss_min,
    trading_days_mask=settings.trading_days_mask,
)
# Telegram notifications (best-effort) -> routed from pipeline events.
from services.notifier import Notifier  # noqa: E402
notifier = Notifier(settings.telegram_token, settings.telegram_chat_id)
pipeline.notifier = notifier.dispatch

# Multi-channel alerts (Telegram / Discord / Email) — credentials in a local
# JSON store next to the provider settings (gitignored), or env vars.
from services.alerts import AlertChannels  # noqa: E402
import os as _os  # noqa: E402
alert_channels = AlertChannels(notifier, _os.path.join(_os.path.dirname(settings.providers_path), "alert_channels.json"))

# Economic-event calendar (user-set / provider-fed upcoming events).
from services.econ_guard import EconCalendar  # noqa: E402
econ_calendar = EconCalendar(_os.path.join(_os.path.dirname(settings.providers_path), "econ_events.json"))
# Event-risk gate: the pipeline halts new entries in the blackout window
# around high-impact events and halves size in the caution window.
pipeline.econ_events = econ_calendar.events
# Allocator tilt: size up symbols with a proven recent live record (bounded).
from services.allocator import risk_weights as _alloc_weights  # noqa: E402
pipeline.allocator = lambda sym: _alloc_weights(paper.history(), [sym]).get(sym.upper(), 1.0)

# Trade journal (auto-entries from closed trades, human-editable).
from services.journal import JournalStore  # noqa: E402
journal_store = JournalStore(_os.path.join(_os.path.dirname(settings.providers_path), "journal.json"))

# Persistent market memory (mined per-strategy stat snapshots).
from services.memory import MemoryStore  # noqa: E402
memory_store = MemoryStore(_os.path.join(_os.path.dirname(settings.providers_path), "memory.json"))

# Self-learning loop: the bot studies its own losing trades after every close
# and applies bounded, expiring corrections (see services/learning.py).
from services.learning import LearningBook  # noqa: E402
learning_book = LearningBook(_os.path.join(_os.path.dirname(settings.providers_path), "learning.json"))
pipeline.learning = learning_book

# Counterfactual tracker: every veto is followed as a virtual trade so each
# rule is graded by what it actually blocked; rules that block winners get
# falsified instead of surviving on their expiry timer.
from services.counterfactual import CounterfactualTracker  # noqa: E402
counterfactual = CounterfactualTracker(
    _os.path.join(_os.path.dirname(settings.providers_path), "counterfactual.json"))
pipeline.counterfactual = counterfactual

# Decision journal: the full explainable record of every bot trade — entry
# reasoning, rule checklist, market snapshot, risk check, exit, review and
# evolution notes, all from REAL decision data.
from data.journal_store import JournalStore as DecisionJournalStore  # noqa: E402
from services.decision_journal import DecisionJournal  # noqa: E402
decision_journal_store = DecisionJournalStore(settings.journal_db)
pipeline.journal = DecisionJournal(decision_journal_store)

# Live-trading readiness gate: an enforced checklist between paper and live.
# Live stays locked by default; this only reports real state, never fakes it.
from services.safety_gate import SafetyState  # noqa: E402
safety_state = SafetyState(settings.safety_state_path)

# Skipped-trade log: every rejected setup with its failed gate + market
# snapshot, so a quiet bot is explainable and searchable (not a black box).
from data.skipped_store import SkippedTradeStore  # noqa: E402
skipped_store = SkippedTradeStore(settings.skipped_db)
pipeline.skipped = skipped_store

# Unified decision store: every evaluated signal becomes a persisted decision
# object (accepted or rejected) BEFORE any trade is placed.
from data.decision_store import DecisionStore  # noqa: E402
decision_store = DecisionStore(settings.decisions_db)

# Persistent paper-account state: capital / equity survive logout, refresh and
# restart (with HUB_DATA_DIR). initial_capital is seeded once from the configured
# starting cash; the SAVED value wins over the default on every restart.
from data.account_store import AccountStore  # noqa: E402
account_store = AccountStore(settings.account_db)
account_store.seed_if_empty(settings.starting_cash)
# the persisted initial capital drives the paper account (not the env default)
paper.starting_balance = account_store.initial_capital()
paper.account_store = account_store
# reconcile the snapshot from the ledger's real closed trades on boot
paper._persist_account_snapshot()

# Persistent market prefs: favorites / pins / watchlists survive logout + restart.
from data.watchlist_store import WatchlistStore  # noqa: E402
watchlist_store = WatchlistStore(settings.watchlist_db)

# Permanent Trading Memory: every CLOSED trade is composed into an 8-category
# memory (trade info, market context, technicals, strategy, execution, emotion,
# outcome, AI reflection) and remembered forever unless explicitly deleted.
# Composed from REAL captured data — the decision journal, the decision object
# and the ledger; uncaptured fields are marked honestly, never invented.
from data.trade_memory_store import TradeMemoryStore  # noqa: E402
from services.trade_memory_manager import TradeMemoryManager  # noqa: E402
trade_memory_store = TradeMemoryStore(settings.trade_memory_db)
trade_memory = TradeMemoryManager(
    trade_memory_store, decision_journal_store, decision_store,
    exchange=_os.environ.get("HUB_EXCHANGE", "paper"),
    starting_balance=account_store.initial_capital())
# the pipeline calls this after the decision journal closes a trade
pipeline.trade_memory = trade_memory
pipeline.journal_context = {
    "instance_id": None,
    "strategy_version": builtin_strategy_version(settings.auto_strategy),
    "market_data_mode": "legacy_live" if settings.use_live_data else "legacy_replay",
    "fill_model": type(paper.fill_model).__name__,
    "execution_mode": "paper",
    "exchange": _os.environ.get("HUB_EXCHANGE", "paper"),
    "instrument_type": "spot",
}
# import already-closed journal trades so the memory isn't empty on first boot
trade_memory.backfill()

# Broker layer (#14) — one interface, paper executable, live locked.
from services.broker import BrokerRegistry  # noqa: E402
broker_registry = BrokerRegistry()

# Research lab (#15) — saved A/B experiments + reports.
from services.research import ResearchStore  # noqa: E402
research_store = ResearchStore(_os.path.join(_os.path.dirname(settings.providers_path), "research.json"))

# Bot OS — the service/event layer the engines communicate through.
from services.bot_os import BotOS  # noqa: E402
bot_os = BotOS()
bot_os.set_status_fn("Execution Engine", lambda: {"state": "up" if engine.status().get("running") else "idle",
                                                  "detail": "running" if engine.status().get("running") else "stopped"})
bot_os.set_status_fn("Strategy Engine", lambda: {"state": "up", "detail": settings.auto_strategy})
bot_os.bus.publish("system", "boot", {"msg": "Bot OS initialised"})

# Autonomous engine: real strategy signals -> the same pipeline (paper-only).
# Default brain is the multi-signal DecisionBrain; HUB_AUTO_STRATEGY=ema selects
# the simple EMA crossover instead.
def _make_strategy(symbol: str):
    s = settings.auto_strategy
    from services.strategy_factory import make_builtin_strategy
    if s == "adaptive":
        # multi-strategy allocation: per-symbol pick from market memory
        from services.allocator import adaptive_factory
        allocator = adaptive_factory(memory_store, settings.auto_timeframe)
        return make_builtin_strategy(s, symbol, adaptive=allocator)
    return make_builtin_strategy(s, symbol)


def _make_instance_strategy(key: str, symbol: str):
    """Factory without mutable global settings — one instance, one strategy."""
    from services.strategy_factory import make_builtin_strategy
    return make_builtin_strategy(key, symbol)


# WebSocket feed (live mode): push candles with REST fallback. Starts only if
# ccxt.pro is available; otherwise the fetcher is a pure REST pass-through and
# the watchdog/status endpoints report the degraded mode honestly.
from data.ws_feed import WebSocketFeed  # noqa: E402
from services.auto_engine import _default_fetcher  # noqa: E402
ws_feed = WebSocketFeed(list(settings.auto_symbols), timeframe=settings.auto_timeframe)
if settings.use_live_data:
    ws_feed.start()

engine = AutoStrategyEngine(
    pipeline, paper, ledger,
    symbols=list(settings.auto_symbols),
    timeframe=settings.auto_timeframe,
    interval=settings.auto_interval,
    strategy_factory=_make_strategy,
    live=settings.use_live_data,
    live_poll_s=settings.live_poll_s,
    fetcher=ws_feed.make_fetcher(_default_fetcher) if settings.use_live_data else None,
)
engine.counterfactual = counterfactual   # resolve vetoed trades on live bars
engine.decisions = decision_store        # persist every accept/reject decision

# Explainable Trading: one complete Decision Report per analysis cycle —
# including WAIT candles — so the bot never trades or skips silently.
from data.cycle_store import CycleStore  # noqa: E402
cycle_store = CycleStore(settings.cycles_db)
engine.reports = cycle_store

# Instance-first paper platform. It is additive during migration: legacy paper
# endpoints remain intact, while every new instance owns isolated execution and
# tagged ledger rows. Supabase instances activate after its additive SQL schema
# has been applied; a missing schema never blocks the established application.
from services.trading_instances import TradingInstanceManager  # noqa: E402
instance_manager = TradingInstanceManager(
    ledger, strategy_factory=_make_instance_strategy, live=settings.use_live_data,
    live_poll_s=settings.live_poll_s,
    fetcher=ws_feed.make_fetcher(_default_fetcher) if settings.use_live_data else None,
    decision_store=decision_store,
    decision_journal=pipeline.journal,
    trade_memory=trade_memory,
    skipped_store=skipped_store,
    cycle_store=cycle_store,
    max_drawdown_pct=settings.max_drawdown_pct,
    max_daily_loss_pct=settings.max_daily_loss_pct,
    max_consecutive_losses=settings.max_consecutive_losses,
    cooldown_after_loss_min=settings.cooldown_after_loss_min,
    session_start=settings.session_start,
    session_end=settings.session_end,
    max_weekly_loss_pct=settings.max_weekly_loss_pct,
    max_trades_per_day=settings.max_trades_per_day,
    trading_days_mask=settings.trading_days_mask,
)

def _instance_execution_status():
    rows = [instance_manager.status(item.id) for item in instance_manager._instances.values()
            if item.mode == "trading" and item.desired_running]
    ready = [row for row in rows if row.get("state") in ("ready", "running")]
    working = [row for row in rows if row.get("state") in
               ("starting", "bootstrapping", "warming", "syncing", "recovering", "data_stale")]
    if ready:
        return {"state": "up", "detail": f"{len(ready)} ready; waiting for valid closed-candle signals"}
    if working:
        return {"state": "warming", "detail": f"{len(working)} worker(s) bootstrapping/recovering"}
    return {"state": "idle", "detail": "no desired paper instance"}

bot_os.set_status_fn("Execution Engine", _instance_execution_status)

# Semi-auto / signal trading modes: the human-approval queue for entries.
from services.approvals import ApprovalStore  # noqa: E402
approvals = ApprovalStore(ttl_s=int(_os.environ.get("HUB_APPROVAL_TTL_S", "900")))
engine.approvals = approvals

# Watchdog: alerts (ledger + Telegram) when the feed stalls, the engine thread
# dies, or the stream degrades to REST. Heartbeat shown at /ops/watchdog.
from services.watchdog import Watchdog  # noqa: E402
watchdog = Watchdog(engine, ledger, notifier.dispatch, ws_feed=ws_feed)
watchdog.start()

# AI Monitoring Agent, on a timer: compares the DEPLOYED strategy's live
# behaviour against a backtest of that same spec and alerts on deviation. Reads
# and reports only — it never edits a strategy or touches risk.
from services.monitor_runner import MonitorRunner  # noqa: E402
monitor_runner = MonitorRunner(engine, paper, ledger, exec_quality=exec_quality,
                               notifier=notifier.dispatch)
# Opt-out for multi-worker deployments: each worker would otherwise run its own
# loop and alert the same deviation N times. The endpoints keep working either
# way — only the timer stops.
if _os.environ.get("HUB_MONITOR_AGENT", "1").strip().lower() not in ("0", "false", "no", "off"):
    monitor_runner.start()

# Daily report + nightly backup: one honest digest to Telegram per UTC day
# (HUB_DAILY_REPORT_HOUR, default 08:00 UTC; -1 disables) and a pruned
# snapshot of every db/json store under DATA_DIR/backups.
from services.backup import backup_now as _backup_now  # noqa: E402
from services.daily_report import DailyTasks, build_report  # noqa: E402
import config as _config  # noqa: E402


def _daily_report_data() -> dict:
    return build_report(history=paper.history(), positions=paper.positions(),
                        balance=paper.balance(), starting_balance=paper.starting_balance,
                        learning_report=learning_book.report(),
                        watchdog_status=watchdog.status(), engine_status=engine.status(),
                        counterfactual_report=counterfactual.report())


_last_retune: dict = {}


def _auto_retune_check() -> None:
    """Nightly: if the live record has diverged from the backtest promise and
    nothing is auditioning yet, search for a retuned brain and shadow it."""
    global _last_retune
    if engine.shadow is not None:
        return
    from services.retune import retune
    from services.track_record import track_record
    tr = track_record(paper.history(), strategy="Decision Brain",
                      symbol=engine.symbols[0], timeframe=engine.timeframe)
    res = retune(engine, notifier.dispatch, timeframe=engine.timeframe,
                 track_verdict=tr.get("verdict"))
    if res.get("ran"):
        _last_retune = res
        ledger.log(level="info", stage="research",
                   message=f"Auto-retune: {res.get('verdict')} — {res.get('detail', '')[:160]}")


def _memory_review() -> None:
    """Nightly pattern recognition over the permanent trade memory (also rolls
    up weekly/monthly/yearly reviews). Real stats only; never blocks."""
    try:
        res = trade_memory.run_reviews()
        ledger.log(level="info", stage="research",
                   message=f"Trade-memory review: {res.get('memories', 0)} memories, "
                           f"{len(res.get('ran', []))} periods refreshed")
    except Exception as e:  # noqa: BLE001
        ledger.log(level="warning", stage="research",
                   message=f"Trade-memory review failed: {type(e).__name__}")


# Retention pruning (M-6): cap the append-only tables each night so a
# persistent disk never fills. Trade rows are never pruned — they are the record.
def _retention_prune() -> None:
    keep = int(_os.environ.get("HUB_RETENTION_ROWS", "20000"))
    try:
        led = ledger.prune(keep_logs=keep * 2, keep_alerts=max(2000, keep // 2),
                           keep_events=keep)
        dec = decision_store.prune(keep=keep)
        skp = skipped_store.prune(keep=keep)
        ledger.log(level="info", stage="ops",
                   message=f"Retention prune: ledger={led} decisions={dec} skipped={skp}")
    except Exception as e:  # noqa: BLE001 — pruning must never crash the nightly run
        ledger.log(level="warning", stage="ops", message=f"Retention prune failed: {e}")


# Storage durability (H-1): assess whether state survives a redeploy and warn
# LOUDLY at boot if it does not, so no one runs on disposable storage silently.
from services.storage_health import assess as _assess_storage, boot_banner as _storage_banner  # noqa: E402
from data.ledger import SUPABASE_STATUS as _SUPA  # noqa: E402


def storage_assessment() -> dict:
    return _assess_storage(
        data_dir=str(_config.DATA_DIR),
        hub_data_dir_set=bool(_os.environ.get("HUB_DATA_DIR")),
        on_cloud=bool(_os.environ.get("RENDER") or _os.environ.get("DYNO")),
        supabase_connected=bool(_SUPA.get("connected")))


_boot_storage = storage_assessment()
_boot_banner = _storage_banner(_boot_storage)
if _boot_banner:
    import sys as _sys
    print(_boot_banner, file=_sys.stderr, flush=True)
    ledger.log(level="warning", stage="ops",
               message=(_boot_storage["warning"] or "Storage not fully durable"))


daily_tasks = DailyTasks(
    notifier.send_async, _daily_report_data,
    hour=int(_os.environ.get("HUB_DAILY_REPORT_HOUR", "8")),
    extra=[lambda: ledger.log(level="info", stage="ops",
                              message=f"Nightly backup: {_backup_now(str(_config.DATA_DIR))['snapshot']}"),
           _auto_retune_check,
           _memory_review,
           _retention_prune])
daily_tasks.start()

# Apply persisted runtime overrides on top of env defaults.
from services.runtime_settings import load_overrides, save_overrides  # noqa: E402


def _apply_setting(key: str, value) -> None:
    if key == "auto_strategy":
        settings.auto_strategy = str(value)
    elif key in ("notify_trades", "notify_risk"):
        setattr(notifier, key, bool(int(value)))
    elif key == "dedup_window_s":
        pipeline.dedup.window_seconds = int(value)
    elif key == "entry_mode":
        engine.entry_mode = "market" if str(value) == "market" else "limit"
    elif key == "daily_report_hour":
        daily_tasks.hour = int(value)
    elif key == "min_quality_score":
        engine.min_quality_score = int(value)
    elif key == "streak_risk_scaling":
        pipeline.streak_risk_scaling = bool(int(value))
    elif key == "position_sizing_mode":
        pipeline.position_sizing_mode = "fixed" if str(value) == "fixed" else "auto"
    elif key == "fixed_position_size":
        pipeline.fixed_position_size = max(0.0, float(value))
    elif key == "fill_model":
        from services.fill_model import from_name
        paper.fill_model = from_name(str(value))
    elif key == "symbol_selection_mode":
        engine.symbol_selection_mode = "manual" if str(value) == "manual" else "auto"
    elif key == "manual_symbol":
        symbol = str(value).strip().upper()
        if symbol:
            engine.manual_symbol = symbol
    elif key == "auto_symbols":
        syms = [x.strip().upper() for x in str(value).split(",") if x.strip()]
        if syms:
            engine.auto_symbols = syms
    elif key == "trading_mode":
        engine.trading_mode = str(value) if str(value) in ("full", "semi", "signal") else "full" 
    elif key == "engine_desired_running":
        engine.autostart_enabled = bool(int(value))
    elif key == "engine_timeframe":
        # applied before the startup event starts the engine, so a persisted
        # timeframe choice survives restarts/redeploys
        engine.timeframe = str(value)
    elif key == "engine_symbols":
        # persisted watchlist (comma-separated) — applied before engine start
        syms = [x.strip().upper() for x in str(value).split(",") if x.strip()]
        if syms:
            engine.symbols = syms
    elif key in ("max_open_positions", "session_start", "session_end",
                 "max_trades_per_day", "max_consecutive_losses", "cooldown_after_loss_min",
                 "trading_days_mask"):
        setattr(pipeline, key, int(value))
    else:  # *_pct float settings
        setattr(pipeline, key, float(value))


def _settings_snapshot() -> dict:
    return {
        "engine_timeframe": engine.timeframe,
        "risk_per_trade_pct": pipeline.risk_per_trade_pct,
        "exposure_limit_pct": pipeline.exposure_limit_pct,
        "max_drawdown_pct": pipeline.max_drawdown_pct,
        "max_open_positions": pipeline.max_open_positions,
        "dedup_window_s": pipeline.dedup.window_seconds,
        "max_daily_loss_pct": pipeline.max_daily_loss_pct,
        "session_start": pipeline.session_start,
        "session_end": pipeline.session_end,
        "max_weekly_loss_pct": pipeline.max_weekly_loss_pct,
        "max_trades_per_day": pipeline.max_trades_per_day,
        "max_consecutive_losses": pipeline.max_consecutive_losses,
        "cooldown_after_loss_min": pipeline.cooldown_after_loss_min,
        "trading_days_mask": pipeline.trading_days_mask,
        "notify_trades": 1 if notifier.notify_trades else 0,
        "notify_risk": 1 if notifier.notify_risk else 0,
        "auto_strategy": settings.auto_strategy,
        "entry_mode": engine.entry_mode,
        "daily_report_hour": daily_tasks.hour,
        "min_quality_score": engine.min_quality_score,
        "streak_risk_scaling": 1 if pipeline.streak_risk_scaling else 0,
        "position_sizing_mode": pipeline.position_sizing_mode,
        "fixed_position_size": pipeline.fixed_position_size,
        "fill_model": type(paper.fill_model).__name__,
        "engine_symbols": ",".join(engine.symbols),
        "symbol_selection_mode": engine.symbol_selection_mode,
        "manual_symbol": engine.manual_symbol,
        "auto_symbols": ",".join(engine.auto_symbols),
        "trading_mode": engine.trading_mode,
        "engine_desired_running": 1 if engine.autostart_enabled else 0,
    }


for _k, _v in load_overrides(settings.settings_path).items():
    _apply_setting(_k, _v)

# Legacy installations only have engine_symbols. New installs preserve the
# full auto watchlist and activate exactly one pair when manual mode is saved.
if engine.symbol_selection_mode == "manual" and engine.manual_symbol:
    engine.symbols = [engine.manual_symbol]
elif engine.auto_symbols:
    engine.symbols = list(engine.auto_symbols)

router = APIRouter()


class SettingsUpdate(BaseModel):
    risk_per_trade_pct: Optional[float] = None
    exposure_limit_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    max_open_positions: Optional[int] = None
    dedup_window_s: Optional[int] = None
    max_daily_loss_pct: Optional[float] = None
    session_start: Optional[int] = None
    session_end: Optional[int] = None
    max_weekly_loss_pct: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    max_consecutive_losses: Optional[int] = None
    cooldown_after_loss_min: Optional[int] = None
    trading_days_mask: Optional[int] = None
    entry_mode: Optional[str] = None
    daily_report_hour: Optional[int] = None
    min_quality_score: Optional[int] = None
    streak_risk_scaling: Optional[bool] = None
    position_sizing_mode: Optional[str] = None
    fixed_position_size: Optional[float] = None


class WebhookPayload(BaseModel):
    alert_id: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: str
    entry: float
    stop: Optional[float] = None
    timestamp: Optional[str] = None


def _check_webhook_secret(secret: Optional[str]) -> None:
    """Validate the TradingView webhook secret. Only used by /webhook/tradingview
    — this credential can post alerts but (when scoped) nothing else."""
    if not secret or not hmac.compare_digest(secret, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret")


def _check_secret(secret: Optional[str]) -> None:
    """Validate the dedicated admin/control credential for a control action.

    Webhook and exchange credentials are never aliases and are never accepted
    on control/configuration endpoints.
    """
    if secret and hmac.compare_digest(secret, settings.admin_key):
        return
    raise HTTPException(status_code=401, detail="Invalid or missing credential")


def request_user(request) -> str:
    """The author identity for a request — who is publishing / rating / commenting.

    Reads the same signed cookie ``app.py`` mints, via the same code
    (``services.session_auth``), then the Bearer JWT. Falls back to ``"owner"``
    when neither resolves: on a single-owner install there is exactly one person,
    and a stable label beats inventing an anonymous one. This is an AUTHOR name,
    not a tenant key — it must not be the tenancy sentinel, which is a storage
    concept and would leak ``__owner__`` into the UI as a byline."""
    from services.session_auth import verify as _verify_cookie
    try:
        token = request.cookies.get("hub_session", "")
    except Exception:  # noqa: BLE001 — a request without cookies is still valid
        token = ""
    user = _verify_cookie(token, settings.secret_key) if token else None
    if not user:
        try:
            auth = request.headers.get("authorization", "")
            if auth[:7].lower() == "bearer ":
                from services.jwt_tokens import decode
                user = (decode(auth[7:].strip(), settings.secret_key) or {}).get("sub")
        except Exception:  # noqa: BLE001 — an unreadable token is simply not an identity
            user = None
    return user or "owner"
































def _export(rows: list, fields: list, fmt: str, name: str):
    import csv as _csv
    import io
    import json as _json
    from fastapi.responses import Response
    if fmt == "json":
        return Response(_json.dumps(rows, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename={name}.json"})
    buf = io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in fields})
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}.csv"})












class PositionSizeRequest(BaseModel):
    equity: float = 10000.0
    entry: float
    stop: Optional[float] = None
    side: str = "long"
    method: str = "percent"          # fixed | percent | atr | vol_adjusted
    risk_pct: float = 0.01
    fixed_risk: Optional[float] = None
    atr: Optional[float] = None
    atr_mult: float = 1.5
    leverage: float = 10.0
    vol_target_pct: float = 0.02






























class AlertChannelSave(BaseModel):
    discord_webhook: Optional[str] = None
    email_to: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[str] = None
    smtp_user: Optional[str] = None
    smtp_pass: Optional[str] = None










class TagsBody(BaseModel):
    tags: list[str] = []






class CloneTemplateBody(BaseModel):
    template: str






class ResearchRunBody(BaseModel):
    name: str = "Experiment"
    spec_a: dict
    spec_b: dict
    bars: int = 4000
    label_a: str = "A"
    label_b: str = "B"
















class JournalEdit(BaseModel):
    notes: Optional[str] = None
    emotions: Optional[str] = None
    mistakes: Optional[list] = None
    lessons: Optional[list] = None
    tags: Optional[list] = None








def _broker_live_connected() -> bool:
    """True only if a real (non-paper) venue is actually connected."""
    try:
        return any(b.kind != "paper" and b.connected()
                   for b in broker_registry.brokers.values())
    except Exception:  # noqa: BLE001
        return False














class EconEvents(BaseModel):
    events: list = []
















class FillModelBody(BaseModel):
    model: str = "perfect"          # perfect | realistic
    spread_pct: float = 0.0004
    slippage_pct: float = 0.0003
    partial_fill_prob: float = 0.0
    reject_prob: float = 0.0


























































from services.native_price_action import STRATEGY_VERSION as _NATIVE_PA_VERSION  # noqa: F401
# Derived from services.strategy_registry, which is the single source of truth
# for what a Trading Instance may run and for the immutable version that
# attributes its paper records. The hand-maintained list this replaces had
# drifted: Donchian has a pinned 1.0.0 in strategies.builtin_versions, but the
# catalog omitted its ``version`` key, so the creation screen offered
# "unversioned" for a strategy that is fully reproducible.
from services.strategy_registry import catalog_rows as _strategy_catalog_rows  # noqa: E402
_STRATEGY_CATALOG = _strategy_catalog_rows()

# Reconcile the engine label with a persisted strategy choice: the overrides
# loop (which restores auto_strategy across restarts) runs before this catalog
# exists, so the label is corrected here — the factory itself already reads
# settings.auto_strategy live.
_persisted_strategy = next((s for s in _STRATEGY_CATALOG
                            if s["key"] == settings.auto_strategy), None)
if _persisted_strategy is not None:
    engine.strategy_label = _persisted_strategy["label"]






class NotifUpdate(BaseModel):
    notify_trades: Optional[bool] = None
    notify_risk: Optional[bool] = None








# ------------------------------------------------- custom strategy builder
from services.custom_store import CustomStore  # noqa: E402
custom_store = CustomStore(settings.custom_path)

# ------------------------------------------------- marketplace (publishing)
from services.strategy_publisher import PublishStore  # noqa: E402
publish_store = PublishStore(_os.path.join(_os.path.dirname(settings.custom_path),
                                           "marketplace.json"))

# ------------------------------------------------- collaboration (comments/shares)
from services.strategy_collab import CollabStore  # noqa: E402
collab_store = CollabStore(_os.path.join(_os.path.dirname(settings.custom_path),
                                         "collab.json"))

# ------------------------------------------------- evolution engine stores
from services.lessons import LessonStore  # noqa: E402
from services.evolution import UpgradeStore, StrategyVersionStore  # noqa: E402
lesson_store = LessonStore(settings.lessons_path)
upgrade_store = UpgradeStore(settings.upgrades_path)
version_store = StrategyVersionStore(settings.versions_path)

# ------------------------------------------------- historical data engine
from data.historical import HistoricalStore  # noqa: E402
market_store = HistoricalStore(settings.market_db)
from data.backfill import BackfillJob  # noqa: E402
backfill_job = BackfillJob(market_store)

# Paper Trading V2: strict provider-backed cache and persistent candle-driven
# broker.  This is additive; the established signal-driven ``paper`` engine
# remains the compatibility path until callers opt into /paper-v2.
from data.market_data_v2 import MarketDataService, MarketDataUpdateJob  # noqa: E402
from execution.paper_broker_v2 import PaperBrokerV2  # noqa: E402
from services.price_action_lab import PriceActionLabRuntime, PriceActionPaperAccount  # noqa: E402
from services.smc_strategy_lab import SMCPaperAccount, SMCStrategyLabRuntime  # noqa: E402
from services.forward_paper_hub import ForwardPaperMarketDataHub  # noqa: E402
from services.research_observer import ResearchObservationRuntime  # noqa: E402
from services.shadow_research import ShadowResearchStore  # noqa: E402
from services.price_action_research import PriceActionExperimentRunner, PriceActionExperimentStore  # noqa: E402
v2_market_data = MarketDataService(settings.market_data_v2_dir)
forward_paper_market_hub = ForwardPaperMarketDataHub(v2_market_data.public_usdm_window)
_shadow_path = _os.path.abspath(settings.shadow_research_db)
_execution_paths = {
    _os.path.abspath(path) for path in (
        settings.paper_broker_v2_db, settings.price_action_paper_db,
        settings.smc_paper_db, settings.price_action_research_db,
    )
}
if _shadow_path in _execution_paths:
    raise RuntimeError("HUB_SHADOW_RESEARCH_DB must be physically separate from every paper/research ledger")
shadow_research_store = ShadowResearchStore(settings.shadow_research_db)
research_observer = ResearchObservationRuntime(
    forward_paper_market_hub, shadow_research_store,
    symbol=settings.auto_symbols[0] if settings.auto_symbols else "BTCUSDT",
    timeframe="5m",
    # Supervised in the running service: a REST blip at boot must not end
    # shadow research for the lifetime of the process.
    supervise=True,
)
# Shadow research is observational and is not needed for the trading labs.
# Starting it unconditionally opens three independent Binance feeds (entry,
# primary HTF and secondary HTF) on every deployment, even when no research
# session is being used. Keep it opt-in so the production worker does not spend
# CPU on an unused workload; operators can enable it explicitly when needed.
if ("PYTEST_CURRENT_TEST" not in _os.environ and
        _os.environ.get("HUB_RESEARCH_AUTOSTART", "0").strip().lower()
        in ("1", "true", "yes", "on")):
    research_observer.start()
# Trading Instances are constructed before the research services for legacy
# import compatibility. Bind their production data/rules authority here so PA,
# SMC and instances all consume the same Binance USD-M hub.
instance_manager.market_hub = forward_paper_market_hub
instance_manager.symbol_rules_provider = v2_market_data.usdm_symbol_rules

# The process, not the browser, owns instance uptime. This supervisor is the
# component that makes that true after the first minute: startup restoration
# runs once, the legacy Watchdog only watches the singleton engine, and the
# dashboard is a read-only poller, so before it existed a worker that died at
# 02:00 stayed dead until a person opened the page and pressed Start.
from services.instance_supervisor import InstanceSupervisor  # noqa: E402
instance_supervisor = InstanceSupervisor(
    instance_manager,
    interval_s=float(_os.environ.get("HUB_INSTANCE_SUPERVISOR_INTERVAL", "20")))
paper_broker_v2 = PaperBrokerV2(settings.paper_broker_v2_db,
                                starting_balance=settings.starting_cash)
price_action_paper = PriceActionPaperAccount(settings.price_action_paper_db,
                                              starting_balance=10_000.0)
if _os.path.abspath(settings.smc_paper_db) == _os.path.abspath(settings.price_action_paper_db):
    raise RuntimeError("HUB_SMC_PAPER_DB must not share the Price Action paper database")
smc_paper = SMCPaperAccount(settings.smc_paper_db, starting_balance=10_000.0)
smc_runtime = SMCStrategyLabRuntime(
    v2_market_data, smc_paper, market_hub=forward_paper_market_hub,
    poll_seconds=settings.smc_poll_s)
# Price Action must remain autonomous after a server restart even when no
# browser has opened the lab page. The supervisor owns stream initialization;
# UI requests are read-only observers of the same server-side runtime.
price_action_runtime = PriceActionLabRuntime(
    v2_market_data, price_action_paper, autostart=True,
    market_hub=forward_paper_market_hub,
    poll_seconds=settings.price_action_poll_s)
price_action_experiments = PriceActionExperimentStore(settings.price_action_research_db)
price_action_research = PriceActionExperimentRunner(price_action_experiments)
v2_market_update_job = MarketDataUpdateJob(v2_market_data)

# ------------------------------------------------- market-context providers
from services.market_context import ProviderSettings  # noqa: E402
provider_settings = ProviderSettings(settings.providers_path)


class SimRequest(BaseModel):
    spec: dict
    bars: int = 3000
    # Optional named window (3M|6M|1Y|3Y|5Y). Resolved to candles server-side
    # from the spec's own timeframe, so "1Y" means a year on every timeframe and
    # the range->bars maths lives in exactly one place.
    range: Optional[str] = None






def _build_builtin(key: str, symbol: str):
    """Construct a built-in strategy object by catalog key."""
    if key == "smc":
        from strategies.smc_strategy import SMCStrategy
        return SMCStrategy(symbol)
    if key == "supertrend":
        from strategies.supertrend_strategy import SupertrendStrategy
        return SupertrendStrategy(symbol)
    if key == "donchian":
        from strategies.donchian_strategy import DonchianStrategy
        return DonchianStrategy(symbol)
    if key == "ensemble":
        from strategies.ensemble_strategy import ConfirmationEnsemble
        return ConfirmationEnsemble(symbol)
    if key == "ema":
        from strategies.ema_strategy import EMAStrategy
        return EMAStrategy(symbol)
    if key == "liquidity_sweep":
        from strategies.liquidity_sweep_strategy import LiquiditySweepStrategy
        return LiquiditySweepStrategy(symbol)
    if key == "adaptive_trend_pullback":
        from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackConfig, AdaptiveTrendPullbackStrategy
        return AdaptiveTrendPullbackStrategy(symbol, config=AdaptiveTrendPullbackConfig.from_env())
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)




class ControlSimRequest(BaseModel):
    strategy: str = "Decision Brain"
    symbol: str = "BTCUSDT"
    timeframe: str = "4h"
    tuning: dict = {}
    custom_spec: Optional[dict] = None
    bars: int = 4000
    macro: Optional[str] = None
    confirmation: Optional[str] = None
    realistic: bool = False






class ControlCompareRequest(BaseModel):
    a: dict
    b: dict
    bars: int = 4000




































def _replay_diag(rep: dict) -> dict:
    """Build a diagnosis-shaped dict from replay trades for the lessons engine."""
    from strategies.diagnosis import diagnose
    # replay trades use 'rr'; diagnose expects 'r'
    trades = [{**t, "r": t.get("rr")} for t in rep["trades"] if t.get("rr") is not None]
    return diagnose({"trades": trades, "total_trades": len(trades),
                     "win_rate": rep["stats"]["win_rate"], "profit_factor": rep["stats"]["profit_factor"],
                     "span_days": 30}, [])










class ExperimentRequest(BaseModel):
    base: dict
    variant: dict
    bars: int = 4000








def _default_base_spec(strategy: str, symbol: str = "BTCUSDT") -> dict:
    """A representative base spec to patch when no prior version exists."""
    return {"name": strategy, "symbol": symbol, "timeframe": "4h", "side": "long",
            "entry": {"op": "AND", "rules": [{"type": "ema_cross", "fast": 20, "slow": 50, "dir": "above"}]},
            "stop": {"type": "atr", "mult": 1.5, "period": 14},
            "target": {"type": "rr", "rr": 2.0}, "risk_per_trade_pct": 0.01,
            "min_score": 60, "quality_filter": True}




























class SymbolsUpdate(BaseModel):
    symbols: list[str]


class SymbolSelectionUpdate(BaseModel):
    mode: str
    manual_symbol: Optional[str] = None
    auto_symbols: Optional[list[str]] = None






class StrategySelect(BaseModel):
    strategy: str








def _session_of(hour: int) -> str:
    if 0 <= hour < 8:
        return "Asia"
    if 8 <= hour < 16:
        return "London"
    return "New York"


def _health_breakdown(history: list, blocked_by_sym) -> dict:
    """Per-symbol and per-session taken-trade performance (real P&L) + blocks."""
    def _hour(ts) -> int:
        try:
            return int(str(ts)[11:13])
        except (ValueError, TypeError):
            return -1

    sym: dict = {}
    sess: dict = {}
    for t in history:
        pnl = t.get("pnl") or 0.0
        s = sym.setdefault(t.get("symbol", "?"), {"trades": 0, "wins": 0, "net_pnl": 0.0})
        s["trades"] += 1; s["wins"] += 1 if pnl > 0 else 0; s["net_pnl"] += pnl
        h = _hour(t.get("opened_at"))
        if h >= 0:
            name = _session_of(h)
            g = sess.setdefault(name, {"trades": 0, "wins": 0, "net_pnl": 0.0})
            g["trades"] += 1; g["wins"] += 1 if pnl > 0 else 0; g["net_pnl"] += pnl

    def _rows(d, extra=None):
        out = []
        for name, v in d.items():
            row = {"name": name, "trades": v["trades"],
                   "win_rate": round(100 * v["wins"] / v["trades"], 0) if v["trades"] else 0.0,
                   "net_pnl": round(v["net_pnl"], 2)}
            if extra is not None:
                row["blocked"] = int(extra.get(name, 0))
            out.append(row)
        return sorted(out, key=lambda r: r["net_pnl"])

    # include symbols that were only ever blocked (never traded)
    by_symbol = _rows(sym, blocked_by_sym)
    seen = {r["name"] for r in by_symbol}
    for s, c in blocked_by_sym.items():
        if s not in seen:
            by_symbol.append({"name": s, "trades": 0, "win_rate": 0.0, "net_pnl": 0.0, "blocked": int(c)})
    return {"by_symbol": by_symbol, "by_session": _rows(sess)}


# ── domain routers (endpoints live in routers/<domain>.py) ──
import routers.analytics  # noqa: E402
import routers.bots  # noqa: E402
import routers.engine  # noqa: E402
import routers.health  # noqa: E402
import routers.journal  # noqa: E402
import routers.paper  # noqa: E402
import routers.paper_v2  # noqa: E402
import routers.risk  # noqa: E402
import routers.settings  # noqa: E402
import routers.symbols  # noqa: E402
import routers.ai  # noqa: E402
import routers.grid  # noqa: E402
import routers.market_data  # noqa: E402
import routers.instances  # noqa: E402
import routers.forward_validation  # noqa: E402
import routers.native_smc  # noqa: E402
import routers.price_action  # noqa: E402
import routers.pa_rulebook  # noqa: E402
import routers.instance_visual_lab  # noqa: E402
import routers.research_observatory  # noqa: E402
import routers.factory_reset  # noqa: E402
router.include_router(routers.analytics.router)
router.include_router(routers.bots.router)
router.include_router(routers.engine.router)
router.include_router(routers.health.router)
router.include_router(routers.journal.router)
router.include_router(routers.paper.router)
router.include_router(routers.paper_v2.router)
router.include_router(routers.risk.router)
router.include_router(routers.settings.router)
router.include_router(routers.symbols.router)
router.include_router(routers.ai.router)
router.include_router(routers.grid.router)
router.include_router(routers.market_data.router)
router.include_router(routers.instances.router)
router.include_router(routers.forward_validation.router)
router.include_router(routers.native_smc.router)
router.include_router(routers.price_action.router)
router.include_router(routers.pa_rulebook.router)
router.include_router(routers.instance_visual_lab.router)
router.include_router(routers.research_observatory.router)
router.include_router(routers.factory_reset.router)


# ───────────────────────────── server-side grid (paper, 24/7) ─────────────────
# A single active grid bot that keeps trading with no browser open, on the same
# market-data feed the engine uses. Persisted so it survives a restart.
grid_runner = None  # type: ignore[assignment]  # services.grid_engine.GridRunner | None


def _grid_fetcher(sym: str, tf: str, n: int):
    from data.market_data import get_bars
    return get_bars(sym, n=n, timeframe=tf)


def grid_start(cfg: dict) -> dict:
    """Create + start a server grid from a config dict."""
    global grid_runner
    from services.grid_engine import GridBot, GridRunner
    from data import grid_store
    if grid_runner is not None and grid_runner.running:
        grid_runner.stop()
    sym = str(cfg.get("symbol", "")).upper().strip()
    if not sym:
        raise HTTPException(400, "symbol is required")
    tf = str(cfg.get("timeframe", "5m"))
    bars, _src = _grid_fetcher(sym, tf, 3)
    start_price = cfg.get("start_price") or (bars[-1].close if bars else None)
    if not start_price:
        raise HTTPException(503, "Could not get a current price to start the grid (market data unavailable).")
    try:
        bot = GridBot(symbol=sym, timeframe=tf, lower=float(cfg["lower"]), upper=float(cfg["upper"]),
                      levels=int(cfg["levels"]), geometric=bool(cfg.get("geometric", False)),
                      investment=float(cfg["investment"]), leverage=float(cfg.get("leverage", 1)),
                      fee_pct=float(cfg.get("fee_pct", 0.04)), start_price=float(start_price))
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, f"Invalid grid config: {e}")
    grid_runner = GridRunner(bot, _grid_fetcher, ledger, interval=30.0, persist=grid_store.save)
    grid_runner.start()
    ledger.add_alert(severity="info", category="system", title="Grid started",
                     detail=f"Server grid running on {sym} {tf} ({bot.levels} levels, paper).")
    return {"started": True, "status": grid_runner.status()}


def grid_stop() -> dict:
    global grid_runner
    from data import grid_store
    stopped = False
    if grid_runner is not None and grid_runner.running:
        grid_runner.stop()
        stopped = True
    grid_store.save(None)          # clear persisted state so it doesn't resume on reboot
    return {"stopped": stopped, "running": False}


def grid_status() -> dict:
    if grid_runner is None:
        return {"active": False, "running": False}
    return {"active": True, **grid_runner.status()}


# resume a persisted grid on boot (survives restart / redeploy)
try:
    from data import grid_store as _grid_store  # noqa: E402
    _grid_snap = _grid_store.load()
    if _grid_snap and _grid_snap.get("running"):
        from services.grid_engine import GridRunner as _GR  # noqa: E402
        grid_runner = _GR.from_snapshot(_grid_snap, _grid_fetcher, ledger, interval=30.0, persist=_grid_store.save)
        grid_runner.start()
        print(f"[grid] resumed grid on {grid_runner.bot.symbol} — survives redeploys.", flush=True)
except Exception:  # noqa: BLE001 — never let grid resume break boot
    pass
