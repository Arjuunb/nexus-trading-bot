"""Instance-first paper trading.

Each TradingInstance owns its engine, strategy state, paper execution view and
ledger scope.  The legacy engine remains available for backwards compatibility;
instances never share its mutable strategy or trade history.
"""
from __future__ import annotations

from services.market_data_freshness import grace_for_interval

import json
import os
import inspect
import math
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from data.ledger import Ledger, SqliteLedger, remote_call_with_retry
from data.tenant_scope import ensure_column
from services.tenancy import OWNER_TENANT
from execution.paper_engine import FillResult, ForwardPaperExecutionEngine, PaperExecutionEngine
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.performance import summarize
from services.signal_pipeline import SignalPipeline
from services.strategy_health import StrategyHealthMonitor
from tradexa.risk.position_sizing import (
    FIXED_QUANTITY, FIXED_STARTING_EQUITY_PERCENT, PositionSizingService,
    SIZING_ENGINE_VERSION, SIZING_MODES, normalize_sizing_mode,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hostname() -> str:
    try:
        return socket.gethostname() or "unknown-host"
    except Exception:  # noqa: BLE001 — identity telemetry must not break a start
        return "unknown-host"


def _id() -> str:
    return uuid.uuid4().hex


_TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400, "1w": 604800,
}

_ACTIVE_INSTANCE_STATES = {
    "starting", "bootstrapping", "warming", "syncing", "ready", "running",
    "data_stale", "recovering",
}

_REBOOT_RUNNING = "running"
_REBOOT_TERMINAL = {"completed", "degraded", "failed"}

# How many trading workers may run at once. The shipped platform row defaulted
# to ONE and the manager additionally hard-capped the configured value at three,
# so the platform could only ever run a single Trading Instance without an
# operator first discovering and changing an undocumented setting. Three is the
# supported concurrent configuration; the ceiling leaves headroom to 5-10 for
# operators who have measured their own host. Each instance costs one entry
# channel plus its two native HTF channels, so raising this is a capacity
# decision, not a free one.
DEFAULT_ACTIVE_SLOTS = 3
MAX_ACTIVE_SLOTS_CEILING = 10

#: Marker for the one-time lift of the old single-slot default. Recorded so a
#: later deliberate choice of one slot is never reverted on the next restart.
_SLOT_CAPACITY_MIGRATION = "2026-09-13-active-slots-default-3"

#: Worker lifecycle state -> canonical telemetry event.
_LIFECYCLE_EVENTS = {
    "starting": "INSTANCE_STARTING",
    "bootstrapping": "MARKET_CONNECTING",
    "warming": "WARMUP_STARTED",
    "syncing": "WARMUP_COMPLETE",
    "ready": "STRATEGY_READY",
    "running": "MARKET_CONNECTED",
    "data_stale": "MARKET_STALE",
    "recovering": "MARKET_DISCONNECTED",
    "error": "INSTANCE_ERROR",
    "stopped": "INSTANCE_STOPPED",
}


class WorkerLeaseError(RuntimeError):
    """Another process already owns this instance's execution.

    Raised rather than started: two workers on one paper account duplicate
    every order, fill, position and journal entry it produces.
    """


class InstanceNotDesired(RuntimeError):
    """A repair start found the operator no longer wants this instance running.

    The supervisor reads desired state, then waits for the lifecycle lock an
    operator Stop, Pause or Delete may be holding. By the time it gets the lock
    that reading can be stale, so a repair re-checks it under the lock instead.
    """


def _parse_iso(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _iso_plus(stamp: str, seconds: float) -> str:
    base = _parse_iso(stamp) or datetime.now(timezone.utc)
    return (base + timedelta(seconds=float(seconds))).isoformat()


def _iso_before(left: object, right: object) -> bool:
    """True when ``left`` is strictly earlier than ``right``; False if unknown.

    Unknown deliberately means "not expired": an unreadable lease expiry must
    never be read as permission to start a second execution owner.
    """
    a, b = _parse_iso(left), _parse_iso(right)
    return bool(a and b and a < b)


def _age_seconds(value: object) -> Optional[int]:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - stamp).total_seconds()))
    except (TypeError, ValueError):
        return None


def _market_health(market: dict, *, timeframe: str, worker_state: str) -> dict:
    """Classify candle freshness on the server, from its actual timestamp.

    A transport being connected is deliberately not sufficient.  This makes
    the UI's Healthy/Stale/Disconnected legend truthful after browser refresh
    and also when the worker has stopped and only persisted state remains.
    """
    out = dict(market)
    # Provider OHLCV uses candle-open timestamps. Freshness starts when that
    # candle closes, not at its open, so a newly closed 5m candle reads 0s old.
    raw_age = _age_seconds(out.get("last_market_data_timestamp"))
    interval = _TIMEFRAME_SECONDS.get(timeframe, 300)
    age = max(0, raw_age - interval) if raw_age is not None else None
    out["market_data_age_seconds"] = age
    raw = str(out.get("market_data_status") or "").lower()
    if worker_state in ("error", "degraded") or raw == "error":
        state = "error"
    elif worker_state == "created":
        state = "stopped"
    elif worker_state in ("stopped",) and raw not in ("healthy", "stale", "disconnected"):
        state = "stopped"
    elif worker_state in ("starting", "bootstrapping", "warming", "syncing", "ready", "rebooting") or raw in ("warming_up", "bootstrapping", "warming", "syncing"):
        state = "warming_up"
    elif age is None:
        state = "error" if raw in ("failed", "error") else "disconnected"
    else:
        # "healthy" must mean the same thing here as it does at the gate, or
        # the legend says Healthy while the engine blocks the entry.
        healthy_under = interval + grace_for_interval(interval)
        state = ("healthy" if age < healthy_under
                 else "stale" if age <= interval * 3 else "disconnected")
    out["market_data_status"] = state
    out["freshness_thresholds_seconds"] = {
        "healthy_under": int(interval + grace_for_interval(interval)),
        "disconnected_over": int(_TIMEFRAME_SECONDS.get(timeframe, 300) * 3),
    }
    return out


@dataclass
class TradingInstance:
    id: str
    symbol: str
    strategy_key: str
    strategy_label: str
    strategy_version: str
    timeframe: str
    risk_per_trade_pct: float
    capital_allocation: float
    exchange: str = "inherit"
    instrument_type: str = "spot"
    max_open_positions: int = 3
    # These execution values are persisted with the worker; legacy autonomous
    # engine settings are never consulted when an instance starts or restores.
    sizing_mode: str = FIXED_STARTING_EQUITY_PERCENT
    fixed_position_size: float = 0.0
    fixed_quantity: float = 0.0
    profit_reinvestment: bool = False
    maximum_risk_amount: Optional[float] = None
    minimum_equity: Optional[float] = None
    starting_equity: float = 0.0
    current_realized_equity: float = 0.0
    risk_basis: float = 0.0
    sizing_engine_version: str = SIZING_ENGINE_VERSION
    entry_mode: str = "limit"
    fill_model: str = "RealisticFill"
    execution_mode: str = "paper"
    simulation_session_id: str = ""
    simulation_session_number: int = 0
    # Who owns this instance. Every instance-scoped read and control action
    # verifies it: an instance_id alone is a guessable string, and trusting it
    # would make one account's worker reachable from another's session the
    # moment this deployment stops being single-owner.
    owner_id: str = OWNER_TENANT
    # Bumped on every configuration change. The worker records the revision it
    # started with, so the UI can never show new settings while the running
    # worker is still using the old ones.
    config_revision: int = 1
    mode: str = "trading"              # trading | research (paper only)
    # Trading instances are always forward paper. Research remains the only
    # instance mode allowed to consume a historical replay.
    market_data_mode: str = "paper_forward"
    state: str = "created"             # created | running | paused | stopped | error
    desired_running: bool = False
    created_at: str = field(default_factory=_now)
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    updated_at: str = field(default_factory=_now)
    last_error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class InstanceLedger:
    """Tag every record and constrain every read to one instance."""
    def __init__(self, ledger: Ledger, instance_id: str, simulation_session_id: str = ""):
        self._ledger, self.instance_id = ledger, instance_id
        self.simulation_session_id = simulation_session_id

    def __getattr__(self, name):
        return getattr(self._ledger, name)

    def insert_webhook_event(self, **kw):
        return self._ledger.insert_webhook_event(**kw, instance_id=self.instance_id)

    def promote_webhook_event(self, **kw):
        return self._ledger.promote_webhook_event(**kw, instance_id=self.instance_id)

    def release_webhook_claim(self, alert_id: str):
        return self._ledger.release_webhook_claim(alert_id, self.instance_id)

    def webhook_seen(self, alert_id: str, since_iso: str) -> bool:
        return self._ledger.webhook_seen(alert_id, since_iso,
                                        instance_id=self.instance_id)

    def get_webhook_events(self, limit=500):
        return [e for e in self._ledger.get_webhook_events(max(limit * 5, 500))
                if e.get("instance_id") == self.instance_id][:limit]

    def open_position(self, **kw):
        return self._ledger.open_position(
            **kw, instance_id=self.instance_id,
            simulation_session_id=self.simulation_session_id)

    def open_position_and_trade(self, *, position, trade, execution_id):
        p, t = dict(position), dict(trade)
        p.update(instance_id=self.instance_id,
                 simulation_session_id=self.simulation_session_id)
        t.update(instance_id=self.instance_id,
                 simulation_session_id=self.simulation_session_id)
        return self._ledger.open_position_and_trade(
            position=p, trade=t, execution_id=execution_id)

    def get_positions(self, status=None):
        return self._ledger.get_positions(
            status, instance_id=self.instance_id,
            simulation_session_id=self.simulation_session_id)

    def update_position_stop(self, *, symbol, stop):
        return self._ledger.update_position_stop(symbol=symbol, stop=stop, instance_id=self.instance_id)

    def update_position_management(self, *, symbol, stop=None, target=None, management=None):
        return self._ledger.update_position_management(
            symbol=symbol, stop=stop, target=target, management=management,
            instance_id=self.instance_id)

    def close_position(self, position_id, *, exit_price, pnl):
        return self._ledger.close_position(
            position_id, exit_price=exit_price, pnl=pnl, instance_id=self.instance_id)

    def record_paper_trade(self, trade):
        row = dict(trade); row["instance_id"] = self.instance_id
        row["simulation_session_id"] = self.simulation_session_id
        return self._ledger.record_paper_trade(row)

    def get_paper_trades(self):
        return self._ledger.get_paper_trades(
            instance_id=self.instance_id,
            simulation_session_id=self.simulation_session_id)

    def close_paper_trade(self, trade_id, **kw):
        return self._ledger.close_paper_trade(trade_id, **kw, instance_id=self.instance_id)

    def close_position_and_trade(self, **kw):
        return self._ledger.close_position_and_trade(**kw, instance_id=self.instance_id)

    def reduce_position_and_trade(self, *, position, remainder_position,
                                  remainder_trade, **kw):
        p, rp, rt = dict(position), dict(remainder_position), dict(remainder_trade)
        p["instance_id"] = self.instance_id
        rp.update(instance_id=self.instance_id,
                  simulation_session_id=self.simulation_session_id)
        rt.update(instance_id=self.instance_id,
                  simulation_session_id=self.simulation_session_id)
        return self._ledger.reduce_position_and_trade(
            position=p, remainder_position=rp, remainder_trade=rt,
            **kw, instance_id=self.instance_id)

    def log(self, *, level, stage, message, symbol=""):
        return self._ledger.log(level=level, stage=stage, message=message, symbol=symbol,
                                instance_id=self.instance_id)

    def get_logs(self, limit=200):
        return self._ledger.get_logs(limit, instance_id=self.instance_id)

    def add_alert(self, *, severity, category, title, detail=""):
        return self._ledger.add_alert(severity=severity, category=category, title=title,
                                      detail=detail, instance_id=self.instance_id)


class ResearchExecutionEngine(PaperExecutionEngine):
    """Signal-only execution adapter used by research instances.

    Research workers must exercise the same strategy and risk path as trading
    workers, but they are not allowed to create positions or orders.  Returning
    a normal rejected fill makes that boundary explicit in the decision log
    without pretending that a simulated order was placed.
    """
    def open(self, *, symbol: str, side: str, size: float, entry: float,
             stop: Optional[float], target: Optional[float] = None,
             alert_id: str = "", maker: bool = False,
             sizing_context: Optional[dict] = None) -> FillResult:
        return FillResult("rejected", symbol, side.lower(), 0.0, entry)


_LOCAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS trading_instances (
 id TEXT PRIMARY KEY, symbol TEXT NOT NULL, strategy_key TEXT NOT NULL,
 strategy_label TEXT NOT NULL, strategy_version TEXT NOT NULL, timeframe TEXT NOT NULL,
 risk_per_trade_pct REAL NOT NULL, capital_allocation REAL NOT NULL,
 exchange TEXT NOT NULL DEFAULT 'inherit', instrument_type TEXT NOT NULL DEFAULT 'spot',
 max_open_positions INTEGER NOT NULL DEFAULT 3,
 sizing_mode TEXT NOT NULL DEFAULT 'fixed_starting_equity_percent', fixed_position_size REAL NOT NULL DEFAULT 0,
 fixed_quantity REAL NOT NULL DEFAULT 0, profit_reinvestment INTEGER NOT NULL DEFAULT 0,
 maximum_risk_amount REAL, minimum_equity REAL, starting_equity REAL,
 current_realized_equity REAL, risk_basis REAL, sizing_engine_version TEXT NOT NULL DEFAULT 'v2',
 entry_mode TEXT NOT NULL DEFAULT 'limit', fill_model TEXT NOT NULL DEFAULT 'RealisticFill',
 execution_mode TEXT NOT NULL DEFAULT 'paper',
 simulation_session_id TEXT NOT NULL DEFAULT '', simulation_session_number INTEGER NOT NULL DEFAULT 0,
 owner_id TEXT NOT NULL DEFAULT '__owner__', config_revision INTEGER NOT NULL DEFAULT 1,
 mode TEXT NOT NULL, market_data_mode TEXT NOT NULL DEFAULT 'paper_forward', state TEXT NOT NULL, desired_running INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, started_at TEXT, stopped_at TEXT, updated_at TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS instance_metrics (
 instance_id TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instance_engine_logs (
 id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, ts TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instance_market_state (
 instance_id TEXT PRIMARY KEY, last_processed_candle_timestamp TEXT,
 worker_heartbeat TEXT,
 market_data_mode TEXT NOT NULL, market_data_status TEXT NOT NULL DEFAULT 'stopped',
 last_market_data_timestamp TEXT, data_source TEXT, warmup_bars INTEGER NOT NULL DEFAULT 0,
 duplicate_candles INTEGER NOT NULL DEFAULT 0, missing_candles INTEGER NOT NULL DEFAULT 0,
 out_of_order_candles INTEGER NOT NULL DEFAULT 0,
 last_blocker TEXT, last_blocker_timestamp TEXT,
 pending_orders_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS simulation_sessions (
 id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, session_number INTEGER NOT NULL,
 starting_balance REAL NOT NULL, ending_balance REAL, realized_pnl REAL NOT NULL DEFAULT 0,
 trades_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
 started_at TEXT NOT NULL, ended_at TEXT, end_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_simulation_session_number
 ON simulation_sessions(instance_id, session_number);
CREATE TABLE IF NOT EXISTS simulation_account_audit (
 id TEXT PRIMARY KEY, action TEXT NOT NULL, instance_id TEXT NOT NULL,
 previous_session_id TEXT, new_session_id TEXT NOT NULL,
 previous_balance REAL NOT NULL, new_balance REAL NOT NULL,
 open_positions_cleared INTEGER NOT NULL, pending_orders_cleared INTEGER NOT NULL,
 timestamp TEXT NOT NULL, initiated_by TEXT NOT NULL, result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trading_instance_platform_settings (
 id TEXT PRIMARY KEY, max_active_slots INTEGER NOT NULL DEFAULT 1,
 max_global_risk_pct REAL NOT NULL DEFAULT 0.02,
 max_global_daily_loss_pct REAL NOT NULL DEFAULT 0.05,
 max_instance_risk_per_trade_pct REAL NOT NULL DEFAULT 0.05,
 paper_account_capital REAL NOT NULL DEFAULT 10000,
 default_symbol TEXT NOT NULL DEFAULT 'BTCUSDT', default_timeframe TEXT NOT NULL DEFAULT '5m',
 default_strategy TEXT NOT NULL DEFAULT 'brain', default_capital REAL NOT NULL DEFAULT 1000,
 default_risk_per_trade_pct REAL NOT NULL DEFAULT 0.005,
 default_max_open_positions INTEGER NOT NULL DEFAULT 3,
 default_entry_mode TEXT NOT NULL DEFAULT 'limit',
 default_fill_model TEXT NOT NULL DEFAULT 'RealisticFill', updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instance_symbol_strategy ON trading_instances(symbol, strategy_key, strategy_version);
-- Exactly one execution owner per instance, enforced by the database rather
-- than by hoping two processes never overlap. instance_id is the primary key,
-- so a second worker cannot insert a competing lease; it must either take over
-- an expired one or fail closed.
CREATE TABLE IF NOT EXISTS instance_worker_leases (
 instance_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, process_id INTEGER NOT NULL,
 host TEXT NOT NULL, started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
 lease_expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_worker_lease_expiry ON instance_worker_leases(lease_expires_at);
"""


class InstanceStore:
    """SQLite in development; Supabase tables in production after the additive migration."""
    _REMOTE_SCHEMA = {
        "paper_trades": (
            "id", "instance_id", "target", "sizing_mode", "sizing_engine_version",
            "risk_basis_at_entry", "risk_pct_at_entry", "risk_amount_at_entry",
            "equity_before_trade", "equity_after_close", "fees", "realized_pnl",
            "simulation_session_id",
        ),
        "positions": ("id", "instance_id", "target", "management_json", "simulation_session_id"),
        "trading_instances": (
            "id", "symbol", "strategy_key", "strategy_label", "strategy_version",
            "timeframe", "risk_per_trade_pct", "capital_allocation", "exchange",
            "instrument_type", "max_open_positions",
            "sizing_mode", "fixed_position_size", "fixed_quantity", "profit_reinvestment",
            "maximum_risk_amount", "minimum_equity", "starting_equity",
            "current_realized_equity", "risk_basis", "sizing_engine_version", "entry_mode", "fill_model",
            "execution_mode", "simulation_session_id", "simulation_session_number",
            "mode", "market_data_mode", "state", "desired_running",
            "created_at", "started_at", "stopped_at", "updated_at", "last_error",
        ),
        "instance_market_state": (
            "instance_id", "last_processed_candle_timestamp", "market_data_mode",
            "market_data_status", "last_market_data_timestamp", "data_source",
            "warmup_bars", "duplicate_candles", "missing_candles",
            "out_of_order_candles", "last_blocker", "last_blocker_timestamp",
            "pending_orders_json", "updated_at",
        ),
        "instance_metrics": ("instance_id", "data_json", "updated_at"),
        "instance_worker_leases": (
            "instance_id", "worker_id", "process_id", "host",
            "started_at", "heartbeat_at", "lease_expires_at",
        ),
        "instance_engine_logs": ("id", "instance_id", "ts", "level", "message"),
        "simulation_sessions": (
            "id", "instance_id", "session_number", "starting_balance", "ending_balance",
            "realized_pnl", "trades_count", "status", "started_at", "ended_at", "end_reason",
        ),
        "simulation_account_audit": (
            "id", "action", "instance_id", "previous_session_id", "new_session_id",
            "previous_balance", "new_balance", "open_positions_cleared",
            "pending_orders_cleared", "timestamp", "initiated_by", "result",
        ),
        "trading_instance_platform_settings": (
            "id", "max_active_slots", "max_global_risk_pct",
            "max_global_daily_loss_pct", "max_instance_risk_per_trade_pct",
            "paper_account_capital", "default_symbol", "default_timeframe",
            "default_strategy", "default_capital", "default_risk_per_trade_pct",
            "default_max_open_positions", "default_entry_mode", "default_fill_model",
            "updated_at",
        ),
    }

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.remote = not isinstance(ledger, SqliteLedger)
        self.available = True
        self.error = ""
        if not self.remote:
            with ledger._lock:
                ledger._c.executescript(_LOCAL_SCHEMA)
                ensure_column(ledger._c, "trading_instance_platform_settings",
                              "max_global_daily_loss_pct", "REAL NOT NULL DEFAULT 0.05")
                ensure_column(ledger._c, "trading_instances", "market_data_mode",
                              "TEXT NOT NULL DEFAULT 'paper_forward'")
                for name, definition in (
                    ("owner_id", "TEXT NOT NULL DEFAULT '__owner__'"),
                    ("config_revision", "INTEGER NOT NULL DEFAULT 1"),
                    ("exchange", "TEXT NOT NULL DEFAULT 'inherit'"),
                    ("instrument_type", "TEXT NOT NULL DEFAULT 'spot'"),
                    ("sizing_mode", "TEXT NOT NULL DEFAULT 'fixed_starting_equity_percent'"),
                    ("fixed_position_size", "REAL NOT NULL DEFAULT 0"),
                    ("fixed_quantity", "REAL NOT NULL DEFAULT 0"),
                    ("profit_reinvestment", "INTEGER NOT NULL DEFAULT 0"),
                    ("maximum_risk_amount", "REAL"),
                    ("minimum_equity", "REAL"),
                    ("starting_equity", "REAL"),
                    ("current_realized_equity", "REAL"),
                    ("risk_basis", "REAL"),
                    ("sizing_engine_version", "TEXT NOT NULL DEFAULT 'v2'"),
                    ("entry_mode", "TEXT NOT NULL DEFAULT 'limit'"),
                    ("fill_model", "TEXT NOT NULL DEFAULT 'RealisticFill'"),
                    ("execution_mode", "TEXT NOT NULL DEFAULT 'paper'"),
                    ("simulation_session_id", "TEXT NOT NULL DEFAULT ''"),
                    ("simulation_session_number", "INTEGER NOT NULL DEFAULT 0"),
                    ("max_open_positions", "INTEGER NOT NULL DEFAULT 3"),
                    ("started_at", "TEXT"),
                    ("stopped_at", "TEXT"),
                ):
                    ensure_column(ledger._c, "trading_instances", name, definition)
                ledger._c.execute("""UPDATE trading_instances SET sizing_mode=CASE
                    WHEN sizing_mode IN ('auto','fixed_starting_equity_pct') THEN 'fixed_starting_equity_percent'
                    WHEN sizing_mode IN ('fixed','fixed_position') THEN 'fixed_quantity'
                    ELSE sizing_mode END""")
                ledger._c.execute("UPDATE trading_instances SET fixed_quantity=fixed_position_size WHERE fixed_quantity=0 AND fixed_position_size>0")
                ledger._c.execute("UPDATE trading_instances SET starting_equity=capital_allocation WHERE starting_equity IS NULL OR starting_equity<=0")
                ledger._c.execute("UPDATE trading_instances SET current_realized_equity=capital_allocation WHERE current_realized_equity IS NULL")
                ledger._c.execute("UPDATE trading_instances SET risk_basis=capital_allocation WHERE risk_basis IS NULL")
                ensure_column(ledger._c, "trading_instance_platform_settings",
                              "paper_account_capital", "REAL NOT NULL DEFAULT 10000")
                for name, definition in (
                    ("max_instance_risk_per_trade_pct", "REAL NOT NULL DEFAULT 0.05"),
                    ("default_symbol", "TEXT NOT NULL DEFAULT 'BTCUSDT'"),
                    ("default_timeframe", "TEXT NOT NULL DEFAULT '5m'"),
                    ("default_strategy", "TEXT NOT NULL DEFAULT 'brain'"),
                    ("default_capital", "REAL NOT NULL DEFAULT 1000"),
                    ("default_risk_per_trade_pct", "REAL NOT NULL DEFAULT 0.005"),
                    ("default_max_open_positions", "INTEGER NOT NULL DEFAULT 3"),
                    ("default_entry_mode", "TEXT NOT NULL DEFAULT 'limit'"),
                    ("default_fill_model", "TEXT NOT NULL DEFAULT 'RealisticFill'"),
                ):
                    ensure_column(ledger._c, "trading_instance_platform_settings", name, definition)
                # One-time capacity migration, and genuinely once. The
                # shipped row default was a single active slot and the manager
                # additionally capped the configured value at three, so a
                # persisted 1 was never an operator risk decision -- it was the
                # only value the platform ever wrote.
                #
                # It has to be marked applied, not merely conditional: one is a
                # legal deliberate choice now (a single-core host), and an
                # unguarded "UPDATE ... WHERE max_active_slots<=1" would revert
                # that operator's decision on every single restart, forever.
                ledger._c.execute(
                    "CREATE TABLE IF NOT EXISTS instance_schema_migrations ("
                    " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
                already = ledger._c.execute(
                    "SELECT 1 FROM instance_schema_migrations WHERE name=?",
                    (_SLOT_CAPACITY_MIGRATION,)).fetchone()
                if not already:
                    ledger._c.execute(
                        "UPDATE trading_instance_platform_settings SET max_active_slots=? "
                        "WHERE max_active_slots<=1", (DEFAULT_ACTIVE_SLOTS,))
                    ledger._c.execute(
                        "INSERT INTO instance_schema_migrations(name, applied_at) VALUES (?,?)",
                        (_SLOT_CAPACITY_MIGRATION, _now()))
                ensure_column(ledger._c, "instance_market_state",
                              "pending_orders_json", "TEXT NOT NULL DEFAULT '{}'")
                ensure_column(ledger._c, "instance_market_state", "last_blocker", "TEXT")
                # The worker's last proof of life, durably. It lived only in
                # the engine object, so after a crash or restart nobody could
                # tell whether an instance had been working an hour ago or had
                # been dark since yesterday.
                ensure_column(ledger._c, "instance_market_state", "worker_heartbeat", "TEXT")
                # Indexes over columns added by migration must be created here,
                # not in _LOCAL_SCHEMA: executescript() runs before the
                # ensure_column pass, so an index naming a new column would
                # fail to open every pre-existing database.
                ledger._c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_instance_owner "
                    "ON trading_instances(owner_id, created_at)")
                ensure_column(ledger._c, "instance_market_state", "last_blocker_timestamp", "TEXT")
                ledger._c.commit()
        if getattr(ledger, "read_only_degraded", False):
            self.available = False
            self.error = (
                "Primary ledger unavailable; Trading Instances are read-only/degraded "
                "and all mutations are disabled"
            )

    def _table(self, name):
        return self.ledger._db.table(name)

    def assert_runtime_schema(self) -> None:
        """Fail before mutating a remote database whose migration is incomplete.

        PostgREST validates selected column names even when a table has no rows,
        so this catches both a missing table and a partially applied additive
        migration.  Creation must never insert a hidden ``trading_instances``
        row and only then discover that its durable forward cursor cannot be
        written.
        """
        if not self.remote:
            return
        for table, columns in self._REMOTE_SCHEMA.items():
            try:
                remote_call_with_retry(lambda table=table, columns=columns:
                                       self._table(table).select(",".join(columns)).limit(1).execute())
            except Exception as exc:
                self.available = False
                self.error = (
                    f"Trading Instance runtime schema is incomplete at '{table}'; "
                    "run data/trading_instances_schema.sql in Supabase, reload the "
                    "PostgREST schema cache, then restart the app"
                )
                raise RuntimeError(self.error) from exc
        self.available = True
        self.error = ""

    def create(self, instance: TradingInstance) -> None:
        if not self.available:
            raise RuntimeError(self.error or "Trading instance storage is not available")
        row = instance.to_dict()
        if self.remote:
            self._table("trading_instances").insert(row).execute()
        else:
            with self.ledger._lock:
                self.ledger._c.execute("""INSERT INTO trading_instances
                (id,symbol,strategy_key,strategy_label,strategy_version,timeframe,risk_per_trade_pct,capital_allocation,exchange,instrument_type,max_open_positions,sizing_mode,fixed_position_size,fixed_quantity,profit_reinvestment,maximum_risk_amount,minimum_equity,starting_equity,current_realized_equity,risk_basis,sizing_engine_version,entry_mode,fill_model,execution_mode,simulation_session_id,simulation_session_number,owner_id,config_revision,mode,market_data_mode,state,desired_running,created_at,started_at,stopped_at,updated_at,last_error)
                VALUES (:id,:symbol,:strategy_key,:strategy_label,:strategy_version,:timeframe,:risk_per_trade_pct,:capital_allocation,:exchange,:instrument_type,:max_open_positions,:sizing_mode,:fixed_position_size,:fixed_quantity,:profit_reinvestment,:maximum_risk_amount,:minimum_equity,:starting_equity,:current_realized_equity,:risk_basis,:sizing_engine_version,:entry_mode,:fill_model,:execution_mode,:simulation_session_id,:simulation_session_number,:owner_id,:config_revision,:mode,:market_data_mode,:state,:desired_running,:created_at,:started_at,:stopped_at,:updated_at,:last_error)""", row)
                self.ledger._c.commit()

    def ensure_simulation_session(self, instance: TradingInstance) -> None:
        """Backfill one active session for pre-session Trading Instances."""
        if instance.simulation_session_id and instance.simulation_session_number > 0:
            return
        session_id = _id()
        row = {
            "id": session_id, "instance_id": instance.id, "session_number": 1,
            "starting_balance": instance.starting_equity,
            "ending_balance": None, "realized_pnl": 0.0, "trades_count": 0,
            "status": "active", "started_at": instance.created_at,
            "ended_at": None, "end_reason": None,
        }
        if self.remote:
            remote_call_with_retry(lambda: self._table("simulation_sessions").insert(row).execute())
            remote_call_with_retry(lambda: self._table("paper_trades").update(
                {"simulation_session_id": session_id}).eq("instance_id", instance.id)
                .eq("simulation_session_id", "").execute())
            remote_call_with_retry(lambda: self._table("positions").update(
                {"simulation_session_id": session_id}).eq("instance_id", instance.id)
                .eq("simulation_session_id", "").execute())
        else:
            with self.ledger._lock:
                self.ledger._c.execute("""INSERT INTO simulation_sessions
                    (id,instance_id,session_number,starting_balance,ending_balance,realized_pnl,
                     trades_count,status,started_at,ended_at,end_reason)
                    VALUES (:id,:instance_id,:session_number,:starting_balance,:ending_balance,
                            :realized_pnl,:trades_count,:status,:started_at,:ended_at,:end_reason)""", row)
                self.ledger._c.execute(
                    "UPDATE paper_trades SET simulation_session_id=? WHERE instance_id=? AND COALESCE(simulation_session_id,'')=''",
                    (session_id, instance.id))
                self.ledger._c.execute(
                    "UPDATE positions SET simulation_session_id=? WHERE instance_id=? AND COALESCE(simulation_session_id,'')=''",
                    (session_id, instance.id))
                self.ledger._c.commit()
        instance.simulation_session_id = session_id
        instance.simulation_session_number = 1
        self.save(instance)

    def simulation_session(self, session_id: str) -> dict | None:
        if not session_id:
            return None
        if self.remote:
            rows = remote_call_with_retry(lambda: self._table("simulation_sessions")
                                          .select("*").eq("id", session_id).limit(1).execute()).data
            return dict(rows[0]) if rows else None
        with self.ledger._lock:
            row = self.ledger._c.execute(
                "SELECT * FROM simulation_sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def simulation_sessions(self, instance_id: str) -> list[dict]:
        if self.remote:
            return list(remote_call_with_retry(lambda: self._table("simulation_sessions")
                                               .select("*").eq("instance_id", instance_id)
                                               .order("session_number", desc=True).execute()).data)
        with self.ledger._lock:
            return [dict(row) for row in self.ledger._c.execute(
                "SELECT * FROM simulation_sessions WHERE instance_id=? ORDER BY session_number DESC",
                (instance_id,))]

    def restart_simulation_account(self, instance: TradingInstance, *, previous_balance: float,
                                   initiated_by: str) -> dict:
        """Atomically end one paper session and create its clean successor."""
        timestamp, new_session_id = _now(), _id()
        if self.remote:
            result = remote_call_with_retry(lambda: self.ledger._db.rpc(
                "restart_simulation_account", {
                    "p_instance_id": instance.id,
                    "p_new_session_id": new_session_id,
                    "p_previous_balance": float(previous_balance),
                    "p_initiated_by": initiated_by,
                    "p_timestamp": timestamp,
                }).execute())
            payload = result.data
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            if not isinstance(payload, dict):
                raise RuntimeError("Simulation account restart returned an invalid persistence result")
            return payload

        with self.ledger._lock:
            db = self.ledger._c
            try:
                open_positions = int(db.execute(
                    "SELECT COUNT(*) FROM positions WHERE instance_id=? AND status='open'",
                    (instance.id,)).fetchone()[0])
                market_row = db.execute(
                    "SELECT pending_orders_json FROM instance_market_state WHERE instance_id=?",
                    (instance.id,)).fetchone()
                try:
                    pending_orders = json.loads(market_row[0] or "{}") if market_row else {}
                except (TypeError, ValueError):
                    pending_orders = {}
                pending_count = len(pending_orders) if isinstance(pending_orders, dict) else 0
                closed_trades = int(db.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE instance_id=? AND simulation_session_id=? AND status='closed'",
                    (instance.id, instance.simulation_session_id)).fetchone()[0])
                db.execute(
                    "UPDATE positions SET status='reset', pnl=0, closed_at=? WHERE instance_id=? AND status='open'",
                    (timestamp, instance.id))
                db.execute(
                    "UPDATE paper_trades SET status='cancelled', pnl=0, realized_pnl=0, closed_at=? "
                    "WHERE instance_id=? AND status='open'",
                    (timestamp, instance.id))
                db.execute("""UPDATE simulation_sessions
                    SET status='ended', ending_balance=?, realized_pnl=?, trades_count=?,
                        ended_at=?, end_reason='account restart'
                    WHERE id=? AND instance_id=?""",
                    (previous_balance, previous_balance - instance.starting_equity,
                     closed_trades, timestamp, instance.simulation_session_id, instance.id))
                next_number = max(1, int(instance.simulation_session_number) + 1)
                db.execute("""INSERT INTO simulation_sessions
                    (id,instance_id,session_number,starting_balance,ending_balance,realized_pnl,
                     trades_count,status,started_at,ended_at,end_reason)
                    VALUES (?,?,?,?,NULL,0,0,'active',?,NULL,NULL)""",
                    (new_session_id, instance.id, next_number,
                     instance.starting_equity, timestamp))
                db.execute("""UPDATE trading_instances
                    SET current_realized_equity=?, risk_basis=?, simulation_session_id=?,
                        simulation_session_number=?, updated_at=? WHERE id=?""",
                    (instance.starting_equity, instance.starting_equity, new_session_id,
                     next_number, timestamp, instance.id))
                db.execute(
                    "UPDATE instance_market_state SET pending_orders_json='{}', updated_at=? WHERE instance_id=?",
                    (timestamp, instance.id))
                db.execute("DELETE FROM instance_metrics WHERE instance_id=?", (instance.id,))
                audit = {
                    "id": _id(), "action": "simulation_account_restart",
                    "instance_id": instance.id,
                    "previous_session_id": instance.simulation_session_id,
                    "new_session_id": new_session_id,
                    "previous_balance": float(previous_balance),
                    "new_balance": float(instance.starting_equity),
                    "open_positions_cleared": open_positions,
                    "pending_orders_cleared": pending_count,
                    "timestamp": timestamp, "initiated_by": initiated_by,
                    "result": "success",
                }
                db.execute("""INSERT INTO simulation_account_audit
                    (id,action,instance_id,previous_session_id,new_session_id,previous_balance,
                     new_balance,open_positions_cleared,pending_orders_cleared,timestamp,initiated_by,result)
                    VALUES (:id,:action,:instance_id,:previous_session_id,:new_session_id,:previous_balance,
                            :new_balance,:open_positions_cleared,:pending_orders_cleared,:timestamp,:initiated_by,:result)""",
                    audit)
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {**audit, "session_number": next_number}

    def simulation_restart_audit(self, instance_id: str) -> list[dict]:
        if self.remote:
            return list(remote_call_with_retry(lambda: self._table("simulation_account_audit")
                                               .select("*").eq("instance_id", instance_id)
                                               .order("timestamp", desc=True).execute()).data)
        with self.ledger._lock:
            return [dict(row) for row in self.ledger._c.execute(
                "SELECT * FROM simulation_account_audit WHERE instance_id=? ORDER BY timestamp DESC",
                (instance_id,))]

    def delete(self, instance_id: str, *, purge_sessions: bool = False) -> None:
        """Delete one configuration and its instance-owned auxiliary state."""
        if self.remote:
            if purge_sessions:
                remote_call_with_retry(lambda: self._table("simulation_sessions")
                                       .delete().eq("instance_id", instance_id).execute())
            remote_call_with_retry(lambda: self._table("trading_instances")
                                   .delete().eq("id", instance_id).execute())
            return
        with self.ledger._lock:
            for table in ("instance_market_state", "instance_metrics", "instance_engine_logs"):
                self.ledger._c.execute(f"DELETE FROM {table} WHERE instance_id=?", (instance_id,))
            if purge_sessions:
                self.ledger._c.execute(
                    "DELETE FROM simulation_sessions WHERE instance_id=?", (instance_id,))
            self.ledger._c.execute("DELETE FROM trading_instances WHERE id=?", (instance_id,))
            self.ledger._c.commit()

    def list(self) -> list[TradingInstance]:
        if self.remote:
            try:
                rows = remote_call_with_retry(lambda: self._table("trading_instances")
                                              .select("*").order("created_at", desc=True).execute()).data
            except Exception as exc:
                self.available = False
                self.error = "Trading instance tables are not installed in Supabase; run data/trading_instances_schema.sql"
                return []
        else:
            with self.ledger._lock:
                rows = [dict(r) for r in self.ledger._c.execute("SELECT * FROM trading_instances ORDER BY created_at DESC")]
        result = []
        for raw in rows:
            r = dict(raw)
            r["desired_running"] = bool(r.get("desired_running"))
            r["profit_reinvestment"] = bool(r.get("profit_reinvestment"))
            r["sizing_mode"] = normalize_sizing_mode(r.get("sizing_mode"))
            r["fixed_quantity"] = float(r.get("fixed_quantity") or r.get("fixed_position_size") or 0)
            r["starting_equity"] = float(r.get("capital_allocation") or 0) if r.get("starting_equity") is None else float(r["starting_equity"])
            r["current_realized_equity"] = r["starting_equity"] if r.get("current_realized_equity") is None else float(r["current_realized_equity"])
            r["risk_basis"] = r["starting_equity"] if r.get("risk_basis") is None else float(r["risk_basis"])
            result.append(TradingInstance(**r))
        return result

    def save(self, instance: TradingInstance) -> None:
        instance.updated_at = _now()
        row = instance.to_dict()
        if self.remote:
            remote_call_with_retry(lambda: self._table("trading_instances")
                                   .update(row).eq("id", instance.id).execute())
        else:
            with self.ledger._lock:
                self.ledger._c.execute(
                    """UPDATE trading_instances
                    SET symbol=:symbol, strategy_key=:strategy_key, strategy_label=:strategy_label,
                        strategy_version=:strategy_version, timeframe=:timeframe,
                        risk_per_trade_pct=:risk_per_trade_pct, capital_allocation=:capital_allocation,
                        exchange=:exchange, instrument_type=:instrument_type,
                        max_open_positions=:max_open_positions,
                        sizing_mode=:sizing_mode, fixed_position_size=:fixed_position_size,
                        fixed_quantity=:fixed_quantity, profit_reinvestment=:profit_reinvestment,
                        maximum_risk_amount=:maximum_risk_amount, minimum_equity=:minimum_equity,
                        starting_equity=:starting_equity, current_realized_equity=:current_realized_equity,
                        risk_basis=:risk_basis,
                        sizing_engine_version=:sizing_engine_version,
                        entry_mode=:entry_mode, fill_model=:fill_model, execution_mode=:execution_mode,
                        simulation_session_id=:simulation_session_id,
                        simulation_session_number=:simulation_session_number,
                        owner_id=:owner_id, config_revision=:config_revision,
                        market_data_mode=:market_data_mode, state=:state, desired_running=:desired_running,
                        started_at=:started_at, stopped_at=:stopped_at,
                        updated_at=:updated_at, last_error=:last_error
                    WHERE id=:id""", row)
                self.ledger._c.commit()

    def market_state(self, instance_id: str) -> dict:
        defaults = {"instance_id": instance_id, "last_processed_candle_timestamp": None,
                    "market_data_mode": "paper_forward", "market_data_status": "stopped",
                    "last_market_data_timestamp": None, "data_source": None,
                    "warmup_bars": 0, "duplicate_candles": 0, "missing_candles": 0,
                    "out_of_order_candles": 0, "last_blocker": None,
                    "last_blocker_timestamp": None, "pending_orders_json": {}}
        try:
            if self.remote:
                rows = remote_call_with_retry(lambda: self._table("instance_market_state")
                                              .select("*").eq("instance_id", instance_id).execute()).data
                return {**defaults, **(rows[0] if rows else {})}
            with self.ledger._lock:
                row = self.ledger._c.execute("SELECT * FROM instance_market_state WHERE instance_id=?", (instance_id,)).fetchone()
            values = {**defaults, **(dict(row) if row else {})}
            raw_pending = values.get("pending_orders_json")
            values["pending_orders_json"] = json.loads(raw_pending) if isinstance(raw_pending, str) else (raw_pending or {})
            return values
        except Exception as exc:
            self.available = False
            self.error = "Trading Instance market-state migration is not installed; run data/trading_instances_schema.sql"
            raise RuntimeError(self.error) from exc

    def market_states(self, instance_ids: set[str]) -> dict[str, dict]:
        """Fetch all instance cursors in one storage call for dashboard polling."""
        if not instance_ids:
            return {}
        defaults = lambda key: {  # noqa: E731 - compact per-row factory
            "instance_id": key, "last_processed_candle_timestamp": None,
            "market_data_mode": "paper_forward", "market_data_status": "stopped",
            "last_market_data_timestamp": None, "data_source": None,
            "warmup_bars": 0, "duplicate_candles": 0, "missing_candles": 0,
            "out_of_order_candles": 0, "last_blocker": None,
            "last_blocker_timestamp": None, "pending_orders_json": {},
        }
        try:
            if self.remote:
                rows = remote_call_with_retry(lambda: self._table("instance_market_state")
                                              .select("*").in_("instance_id", list(instance_ids)).execute()).data
            else:
                with self.ledger._lock:
                    rows = [dict(row) for row in self.ledger._c.execute(
                        "SELECT * FROM instance_market_state")
                        if row["instance_id"] in instance_ids]
            indexed = {str(row["instance_id"]): row for row in rows}
            result = {}
            for key in instance_ids:
                values = {**defaults(key), **indexed.get(key, {})}
                raw_pending = values.get("pending_orders_json")
                values["pending_orders_json"] = (json.loads(raw_pending)
                                                  if isinstance(raw_pending, str)
                                                  else (raw_pending or {}))
                result[key] = values
            return result
        except Exception as exc:
            self.available = False
            self.error = "Trading Instance market-state migration is not installed; run data/trading_instances_schema.sql"
            raise RuntimeError(self.error) from exc

    def save_market_state(self, instance_id: str, **values) -> None:
        row = {"instance_id": instance_id, "last_processed_candle_timestamp": None,
               "market_data_mode": "paper_forward", "market_data_status": "warming_up",
               "last_market_data_timestamp": None, "data_source": None,
               "warmup_bars": 0, "duplicate_candles": 0, "missing_candles": 0,
               "out_of_order_candles": 0, "last_blocker": None,
               "last_blocker_timestamp": None, "worker_heartbeat": None,
               "updated_at": _now(), **values}
        if self.remote:
            remote_call_with_retry(lambda: self._table("instance_market_state").upsert(row).execute())
        else:
            with self.ledger._lock:
                self.ledger._c.execute("""INSERT INTO instance_market_state
                (instance_id,last_processed_candle_timestamp,market_data_mode,market_data_status,last_market_data_timestamp,data_source,warmup_bars,duplicate_candles,missing_candles,out_of_order_candles,last_blocker,last_blocker_timestamp,worker_heartbeat,updated_at)
                VALUES (:instance_id,:last_processed_candle_timestamp,:market_data_mode,:market_data_status,:last_market_data_timestamp,:data_source,:warmup_bars,:duplicate_candles,:missing_candles,:out_of_order_candles,:last_blocker,:last_blocker_timestamp,:worker_heartbeat,:updated_at)
                ON CONFLICT(instance_id) DO UPDATE SET
                  last_processed_candle_timestamp=excluded.last_processed_candle_timestamp,
                  market_data_mode=excluded.market_data_mode,
                  market_data_status=excluded.market_data_status,
                  last_market_data_timestamp=excluded.last_market_data_timestamp,
                  data_source=excluded.data_source,
                  warmup_bars=excluded.warmup_bars,
                  duplicate_candles=excluded.duplicate_candles,
                  missing_candles=excluded.missing_candles,
                  out_of_order_candles=excluded.out_of_order_candles,
                  last_blocker=excluded.last_blocker,
                  last_blocker_timestamp=excluded.last_blocker_timestamp,
                  worker_heartbeat=excluded.worker_heartbeat,
                  updated_at=excluded.updated_at""", row)
                self.ledger._c.commit()

    def save_pending_orders(self, instance_id: str, pending_orders: dict) -> None:
        row = {"pending_orders_json": pending_orders, "updated_at": _now()}
        if self.remote:
            remote_call_with_retry(lambda: self._table("instance_market_state")
                                   .update(row).eq("instance_id", instance_id).execute())
            return
        with self.ledger._lock:
            self.ledger._c.execute(
                "UPDATE instance_market_state SET pending_orders_json=?, updated_at=? WHERE instance_id=?",
                (json.dumps(pending_orders), row["updated_at"], instance_id),
            )
            self.ledger._c.commit()

    def save_metrics(self, instance_id: str, metrics: dict) -> None:
        row = {"instance_id": instance_id, "data_json": json.dumps(metrics), "updated_at": _now()}
        if self.remote:
            # PostgREST accepts a Python dict for JSONB.  A JSON *string* is a
            # JSON string value, not the metrics object, and would make the
            # remote data unusable for future analytics queries.
            remote_call_with_retry(lambda: self._table("instance_metrics")
                                   .upsert({**row, "data_json": metrics}).execute())
        else:
            with self.ledger._lock:
                self.ledger._c.execute("INSERT OR REPLACE INTO instance_metrics(instance_id,data_json,updated_at) VALUES (:instance_id,:data_json,:updated_at)", row)
                self.ledger._c.commit()

    def append_engine_log(self, instance_id: str, *, level: str, message: str,
                          timestamp: Optional[str] = None) -> None:
        row = {"id": _id(), "instance_id": instance_id,
               "ts": timestamp or _now(), "level": level, "message": message}
        if self.remote:
            remote_call_with_retry(lambda: self._table("instance_engine_logs").insert(row).execute())
        else:
            with self.ledger._lock:
                self.ledger._c.execute(
                    "INSERT INTO instance_engine_logs(id,instance_id,ts,level,message) "
                    "VALUES (:id,:instance_id,:ts,:level,:message)", row)
                self.ledger._c.commit()

    # ------------------------------------------------------- worker leases
    def worker_lease(self, instance_id: str) -> dict | None:
        if self.remote:
            rows = remote_call_with_retry(
                lambda: self._table("instance_worker_leases").select("*")
                .eq("instance_id", instance_id).limit(1).execute()).data
            return dict(rows[0]) if rows else None
        with self.ledger._lock:
            row = self.ledger._c.execute(
                "SELECT * FROM instance_worker_leases WHERE instance_id=?",
                (instance_id,)).fetchone()
        return dict(row) if row else None

    def worker_leases(self) -> list[dict]:
        if self.remote:
            return list(remote_call_with_retry(
                lambda: self._table("instance_worker_leases").select("*").execute()).data)
        with self.ledger._lock:
            return [dict(row) for row in self.ledger._c.execute(
                "SELECT * FROM instance_worker_leases")]

    def claim_worker_lease(self, instance_id: str, *, worker_id: str, process_id: int,
                           host: str, ttl_seconds: float, now: str | None = None) -> dict:
        """Take exclusive execution ownership of one instance, or raise.

        The write itself carries the predicate. A read-then-upsert would not be
        exclusive at all: two processes could both read "no live lease" and
        both then run an unconditional ON CONFLICT DO UPDATE, and the second
        would simply win -- leaving two workers on one paper account,
        duplicating every order, fill and journal entry, which is precisely
        what this is for. The conditional update means at most one writer can
        take a lease that somebody else holds, and only once it has expired.
        """
        stamp = now or _now()
        expires = _iso_plus(stamp, ttl_seconds)
        row = {"instance_id": instance_id, "worker_id": worker_id,
               "process_id": int(process_id), "host": host,
               "started_at": stamp, "heartbeat_at": stamp, "lease_expires_at": expires}

        def refuse(existing: dict | None) -> None:
            if existing is None:
                raise WorkerLeaseError(
                    f"instance {instance_id} worker lease could not be acquired")
            if _parse_iso(existing.get("lease_expires_at")) is None:
                raise WorkerLeaseError(
                    f"instance {instance_id} has a worker lease with an unreadable "
                    "expiry; refusing to start a second execution owner")
            raise WorkerLeaseError(
                f"instance {instance_id} is already owned by worker "
                f"{existing.get('worker_id')} (pid {existing.get('process_id')} on "
                f"{existing.get('host')}) until {existing.get('lease_expires_at')}")

        if self.remote:
            # PostgREST has no conditional upsert, so this is done as an
            # insert-or-conditional-update pair. The insert is the exclusive
            # step: the primary key means exactly one of two racing processes
            # can create the row, and the loser falls through to the update,
            # which only succeeds against an expired lease or its own.
            try:
                remote_call_with_retry(
                    lambda: self._table("instance_worker_leases").insert(row).execute())
                return row
            except Exception:
                pass                    # the row exists; try to take it over
            taken = remote_call_with_retry(
                lambda: self._table("instance_worker_leases")
                .update({k: v for k, v in row.items() if k != "instance_id"})
                .eq("instance_id", instance_id)
                .or_(f"worker_id.eq.{worker_id},lease_expires_at.lt.{stamp}")
                .execute())
            if not getattr(taken, "data", None):
                refuse(self.worker_lease(instance_id))
            return row

        with self.ledger._lock:
            cursor = self.ledger._c.execute(
                """INSERT INTO instance_worker_leases
                   (instance_id,worker_id,process_id,host,started_at,heartbeat_at,lease_expires_at)
                   VALUES (:instance_id,:worker_id,:process_id,:host,:started_at,:heartbeat_at,:lease_expires_at)
                   ON CONFLICT(instance_id) DO UPDATE SET
                     worker_id=excluded.worker_id, process_id=excluded.process_id,
                     host=excluded.host, started_at=excluded.started_at,
                     heartbeat_at=excluded.heartbeat_at,
                     lease_expires_at=excluded.lease_expires_at
                   WHERE instance_worker_leases.worker_id = excluded.worker_id
                      OR instance_worker_leases.lease_expires_at < excluded.heartbeat_at""",
                row)
            self.ledger._c.commit()
            if cursor.rowcount < 1:
                existing = self.ledger._c.execute(
                    "SELECT * FROM instance_worker_leases WHERE instance_id=?",
                    (instance_id,)).fetchone()
                refuse(dict(existing) if existing else None)
        return row

    def renew_worker_lease(self, instance_id: str, *, worker_id: str,
                           ttl_seconds: float, now: str | None = None) -> bool:
        """Extend a lease this worker still holds. False means it was lost."""
        stamp = now or _now()
        expires = _iso_plus(stamp, ttl_seconds)
        if self.remote:
            result = remote_call_with_retry(
                lambda: self._table("instance_worker_leases")
                .update({"heartbeat_at": stamp, "lease_expires_at": expires})
                .eq("instance_id", instance_id).eq("worker_id", worker_id).execute())
            return bool(getattr(result, "data", None))
        with self.ledger._lock:
            cursor = self.ledger._c.execute(
                "UPDATE instance_worker_leases SET heartbeat_at=?, lease_expires_at=? "
                "WHERE instance_id=? AND worker_id=?",
                (stamp, expires, instance_id, worker_id))
            self.ledger._c.commit()
            return cursor.rowcount > 0

    def release_worker_lease(self, instance_id: str, *, worker_id: str = "") -> None:
        """Give up execution ownership. A blank worker_id releases regardless."""
        if self.remote:
            def drop():
                query = self._table("instance_worker_leases").delete().eq("instance_id", instance_id)
                if worker_id:
                    query = query.eq("worker_id", worker_id)
                return query.execute()
            remote_call_with_retry(drop)
            return
        with self.ledger._lock:
            if worker_id:
                self.ledger._c.execute(
                    "DELETE FROM instance_worker_leases WHERE instance_id=? AND worker_id=?",
                    (instance_id, worker_id))
            else:
                self.ledger._c.execute(
                    "DELETE FROM instance_worker_leases WHERE instance_id=?", (instance_id,))
            self.ledger._c.commit()

    def engine_logs(self, instance_id: str, limit: int = 200) -> list[dict]:
        """Read back one instance's engine log, newest first.

        Every lifecycle event is written here but nothing could read it: the
        API served ``bot_logs`` instead, which is a different table, so the
        structured instance timeline existed and was invisible.
        """
        limit = max(1, min(int(limit), 1000))
        if self.remote:
            return list(remote_call_with_retry(
                lambda: self._table("instance_engine_logs").select("*")
                .eq("instance_id", instance_id).order("ts", desc=True)
                .limit(limit).execute()).data)
        with self.ledger._lock:
            return [dict(row) for row in self.ledger._c.execute(
                "SELECT * FROM instance_engine_logs WHERE instance_id=? "
                "ORDER BY ts DESC LIMIT ?", (instance_id, limit))]

    def platform_settings(self) -> dict:
        defaults = {"max_active_slots": DEFAULT_ACTIVE_SLOTS, "max_global_risk_pct": 0.02,
                    "max_global_daily_loss_pct": 0.05,
                    "max_instance_risk_per_trade_pct": 0.05,
                    "paper_account_capital": None, "default_symbol": "BTCUSDT",
                    "default_timeframe": "5m", "default_strategy": "brain",
                    "default_capital": 1000.0, "default_risk_per_trade_pct": 0.005,
                    "default_max_open_positions": 3, "default_entry_mode": "limit",
                    "default_fill_model": "RealisticFill"}
        if self.remote:
            try:
                rows = remote_call_with_retry(lambda: self._table("trading_instance_platform_settings")
                                              .select("*").eq("id", "default").execute()).data
                return {**defaults, **(rows[0] if rows else {})}
            except Exception as exc:
                self.available = False
                self.error = "Trading instance tables are not installed in Supabase; run data/trading_instances_schema.sql"
                return defaults
        with self.ledger._lock:
            row = self.ledger._c.execute("SELECT * FROM trading_instance_platform_settings WHERE id='default'").fetchone()
        return {**defaults, **(dict(row) if row else {})}

    def save_platform_settings(self, *, max_active_slots: int, max_global_risk_pct: float,
                               max_global_daily_loss_pct: float,
                               max_instance_risk_per_trade_pct: float,
                               paper_account_capital: float, defaults: dict) -> None:
        row = {"id": "default", "max_active_slots": max_active_slots,
               "max_global_risk_pct": max_global_risk_pct,
               "max_global_daily_loss_pct": max_global_daily_loss_pct,
               "max_instance_risk_per_trade_pct": max_instance_risk_per_trade_pct,
               "paper_account_capital": paper_account_capital, **defaults, "updated_at": _now()}
        if self.remote:
            remote_call_with_retry(lambda: self._table("trading_instance_platform_settings")
                                   .upsert(row).execute())
        else:
            with self.ledger._lock:
                self.ledger._c.execute("""INSERT OR REPLACE INTO trading_instance_platform_settings
                    (id,max_active_slots,max_global_risk_pct,max_global_daily_loss_pct,max_instance_risk_per_trade_pct,
                     paper_account_capital,default_symbol,default_timeframe,default_strategy,default_capital,
                     default_risk_per_trade_pct,default_max_open_positions,default_entry_mode,default_fill_model,updated_at)
                    VALUES (:id,:max_active_slots,:max_global_risk_pct,:max_global_daily_loss_pct,:max_instance_risk_per_trade_pct,
                     :paper_account_capital,:default_symbol,:default_timeframe,:default_strategy,:default_capital,
                     :default_risk_per_trade_pct,:default_max_open_positions,:default_entry_mode,:default_fill_model,:updated_at)""", row)
                self.ledger._c.commit()


class TradingInstanceManager:
    def __init__(self, ledger: Ledger, *, strategy_factory: Callable[[str, str], object],
                 live: bool, live_poll_s: float, fetcher=None,
                 max_slots: int = DEFAULT_ACTIVE_SLOTS,
                 max_global_risk_pct: float = 0.02, max_global_daily_loss_pct: float = 0.05,
                 paper_account_capital: float = 10_000.0, decision_store=None,
                 decision_journal=None, trade_memory=None, skipped_store=None,
                 cycle_store=None,
                 max_drawdown_pct: float = 0.20, max_daily_loss_pct: float = 0.0,
                 max_consecutive_losses: int = 0, cooldown_after_loss_min: int = 0,
                 session_start: int = 0, session_end: int = 24,
                 max_weekly_loss_pct: float = 0.0, max_trades_per_day: int = 0,
                 trading_days_mask: int = 127, full_reboot_timeout_s: float | None = None,
                 market_hub=None, symbol_rules_provider=None):
        self.ledger, self.store = ledger, InstanceStore(ledger)
        self.strategy_factory, self.live, self.live_poll_s, self.fetcher = strategy_factory, live, live_poll_s, fetcher
        self.decision_store = decision_store
        self.decision_journal = decision_journal
        self.trade_memory = trade_memory
        self.skipped_store = skipped_store
        self.cycle_store = cycle_store
        self.market_hub = market_hub
        self.symbol_rules_provider = symbol_rules_provider
        # Opt-in news blackout (services/instance_event_guard.py). The server
        # attaches it after construction; None leaves every pipeline without
        # an economic calendar, as before.
        self.event_guard = None
        # Opt-in public paper record (services/public_track_record.py), also
        # attached by the server. Nothing is published unless switched on.
        self.track_record = None
        # Opt-in per instance: the Decision Brain quality score stops blocking
        # this instance's entries (an InstanceSwitches, attached by the
        # server). None, or no entry for the instance, keeps the gate on.
        self.quality_gate = None
        # Instance workers own their positions, but production risk policy is
        # supplied by the server and applied to every isolated pipeline. These
        # values were previously omitted, silently disabling several configured
        # guards for Trading Instances while the legacy engine enforced them.
        self.pipeline_risk = {
            "max_drawdown_pct": max_drawdown_pct,
            "max_daily_loss_pct": max_daily_loss_pct,
            "max_consecutive_losses": max_consecutive_losses,
            "cooldown_after_loss_min": cooldown_after_loss_min,
            "session_start": session_start,
            "session_end": session_end,
            "max_weekly_loss_pct": max_weekly_loss_pct,
            "max_trades_per_day": max_trades_per_day,
            "trading_days_mask": trading_days_mask,
        }
        # Forward trading intentionally does not inherit HUB_USE_LIVE_DATA. It
        # always uses the strict provider-only adapter; a missing provider is a
        # fail-closed market-data error, never a replay fallback.
        from data.forward_market_data import fetch_forward_bars
        self.forward_fetcher = fetch_forward_bars
        configured = self.store.platform_settings() if self.store.available else {}
        self.max_slots = min(MAX_ACTIVE_SLOTS_CEILING,
                             max(1, int(configured.get("max_active_slots", max_slots))))
        self.max_global_risk_pct = min(1.0, max(0.001, float(configured.get("max_global_risk_pct", max_global_risk_pct))))
        self.max_global_daily_loss_pct = min(1.0, max(0.001, float(configured.get("max_global_daily_loss_pct", max_global_daily_loss_pct))))
        self.max_instance_risk_per_trade_pct = min(0.05, max(0.001, float(configured.get("max_instance_risk_per_trade_pct", 0.05))))
        configured_capital = configured.get("paper_account_capital")
        self.paper_account_capital = max(1.0, float(configured_capital if configured_capital is not None else paper_account_capital))
        self.instance_defaults = {
            "default_symbol": str(configured.get("default_symbol") or "BTCUSDT").upper(),
            "default_timeframe": str(configured.get("default_timeframe") or "5m"),
            "default_strategy": str(configured.get("default_strategy") or "brain"),
            "default_capital": float(configured.get("default_capital") or 1000),
            "default_risk_per_trade_pct": float(configured.get("default_risk_per_trade_pct") or 0.005),
            "default_max_open_positions": int(configured.get("default_max_open_positions") or 3),
            "default_entry_mode": str(configured.get("default_entry_mode") or "limit"),
            "default_fill_model": str(configured.get("default_fill_model") or "RealisticFill"),
        }
        self._instances: dict[str, TradingInstance] = {i.id: i for i in self.store.list()}
        for instance in self._instances.values():
            self.store.ensure_simulation_session(instance)
        self._runtime: dict[str, tuple[AutoStrategyEngine, PaperExecutionEngine, SignalPipeline, TradingControl]] = {}
        self._metric_fingerprints: dict[str, tuple] = {}
        self._reboots: dict[str, dict] = {}
        self._reboot_threads: dict[str, threading.Thread] = {}
        configured_reboot_timeout = os.environ.get("HUB_FULL_REBOOT_TIMEOUT_SECONDS", "300")
        self.full_reboot_timeout_s = max(
            1.0, float(full_reboot_timeout_s if full_reboot_timeout_s is not None
                       else configured_reboot_timeout))
        self._lock = threading.RLock()
        # One mutex per instance, separate from the state lock above.
        #
        # Lifecycle actions were not atomic: stop() and pause() read their
        # runtime under the lock and then released it before doing the slow
        # part (joining the worker thread) and before persisting the result.
        # A Start landing in that window found a worker that still reported
        # running, returned it as a success, and was then overwritten by the
        # Stop's write -- so the operator saw "running / desired" come back
        # from the API while the instance ended stopped with no worker.
        #
        # The state lock cannot simply be held across the whole transition:
        # the engine thread takes it from its lifecycle callback, so holding
        # it while joining that same thread would deadlock. A per-instance
        # lifecycle mutex serialises the transitions without touching the
        # engine thread's path, and without blocking sibling instances.
        self._lifecycle_locks: dict[str, threading.RLock] = {}
        self._worker_id = f"{_hostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.worker_lease_ttl_s = max(
            30.0, float(os.environ.get("HUB_WORKER_LEASE_TTL_SECONDS", "120")))

    def _lifecycle_lock(self, instance_id: str) -> threading.RLock:
        with self._lock:
            lock = self._lifecycle_locks.get(instance_id)
            if lock is None:
                lock = self._lifecycle_locks[instance_id] = threading.RLock()
            return lock

    # ------------------------------------------------------------ ownership
    def worker_alive(self, instance_id: str) -> bool:
        """Is there a live execution runtime for this instance, right now?

        One definition, because six hand-inlined copies of it meant a future
        change to what "alive" means -- an added lease or heartbeat condition,
        say -- had to be found in six places, and a miss would silently make
        two parts of the platform disagree about whether a worker exists.
        """
        runtime = self._runtime.get(instance_id)
        if runtime is None:
            return False
        engine = runtime[0]
        thread = getattr(engine, "_thread", None)
        return bool(engine.running and thread is not None and thread.is_alive())

    def worker_health(self) -> list[dict]:
        """Every trading instance the operator wants running: is its worker
        alive, and is its market data current? Read from memory only, so the
        status monitor can ask every minute without touching storage."""
        rows = []
        for inst in list(self._instances.values()):
            if inst.mode != "trading" or not inst.desired_running:
                continue
            runtime = self._runtime.get(inst.id)
            engine = runtime[0] if runtime else None
            rows.append({"id": inst.id, "symbol": inst.symbol,
                         "alive": self.worker_alive(inst.id),
                         "market_data_status": str(getattr(engine, "market_data_status", "") or "")})
        return rows

    def instance_for(self, instance_id: str, owner_id: str | None = None) -> TradingInstance:
        """Look one instance up, verifying ownership when an owner is supplied.

        A missing instance and one owned by somebody else raise the same
        KeyError on purpose: answering differently would turn this into an
        oracle for which instance ids exist on the deployment.
        """
        inst = self._instances[instance_id]
        if owner_id is not None and inst.owner_id != owner_id:
            raise KeyError(instance_id)
        return inst

    def owned_instances(self, owner_id: str | None = None) -> list[TradingInstance]:
        return [item for item in self._instances.values()
                if owner_id is None or item.owner_id == owner_id]

    def _reboot_active(self, instance_id: str) -> bool:
        return self._reboots.get(instance_id, {}).get("status") == _REBOOT_RUNNING

    def _assert_reboot_idle(self, instance_id: str) -> None:
        if self._reboot_active(instance_id):
            raise ValueError("A Full Bot Reboot is already in progress for this Trading Instance")

    def _set_reboot_phase(self, instance_id: str, phase: str, message: str, *,
                          status: str = _REBOOT_RUNNING, error: str = "",
                          details: dict | None = None) -> dict:
        with self._lock:
            current = self._reboots.get(instance_id, {})
            now = _now()
            row = {
                **current,
                "id": current.get("id") or _id(),
                "instance_id": instance_id,
                "status": status,
                "phase": phase,
                "message": message,
                "started_at": current.get("started_at") or now,
                "updated_at": now,
                "completed_at": now if status in _REBOOT_TERMINAL else None,
                "error": error,
            }
            if details:
                row["details"] = {**row.get("details", {}), **details}
            self._reboots[instance_id] = row
            inst = self._instances.get(instance_id)
            if inst is not None:
                level = "error" if status in ("degraded", "failed") else "info"
                try:
                    self.store.append_engine_log(
                        instance_id, level=level,
                        message=f"full_reboot phase={phase} status={status} message={message}")
                except Exception:  # reboot truth must survive telemetry outages
                    pass
            return dict(row)

    def create(self, *, symbol: str, strategy_key: str, strategy_label: str, strategy_version: str,
               timeframe: str, risk_per_trade_pct: float, capital_allocation: float, mode: str = "trading",
               max_open_positions: int = 3,
               sizing_mode: str = FIXED_STARTING_EQUITY_PERCENT,
               fixed_position_size: float = 0.0, fixed_quantity: float | None = None,
               profit_reinvestment: bool = False,
               maximum_risk_amount: float | None = None,
               minimum_equity: float | None = None,
               entry_mode: str = "limit", fill_model: str = "RealisticFill",
               exchange: str = "inherit", instrument_type: str = "spot",
               owner_id: str = OWNER_TENANT) -> TradingInstance:
        with self._lock:
            # This must precede every insert.  A partially installed Supabase
            # migration previously left a stopped row behind when status() later
            # discovered the missing instance_market_state table.
            self.store.assert_runtime_schema()
            if mode not in ("trading", "research"):
                raise ValueError("mode must be trading or research")
            capital_allocation = float(capital_allocation)
            risk_per_trade_pct = float(risk_per_trade_pct)
            if not math.isfinite(capital_allocation) or capital_allocation <= 0:
                raise ValueError("capital_allocation must be a finite value greater than zero")
            if not math.isfinite(risk_per_trade_pct) or not 0 < risk_per_trade_pct <= 0.05:
                raise ValueError("risk_per_trade_pct must be a finite value in (0, 0.05]")
            if risk_per_trade_pct > self.max_instance_risk_per_trade_pct:
                raise ValueError(
                    f"risk_per_trade_pct exceeds the platform ceiling of {self.max_instance_risk_per_trade_pct}")
            sizing_mode = normalize_sizing_mode(sizing_mode)
            quantity = float(fixed_quantity if fixed_quantity is not None else fixed_position_size)
            if sizing_mode not in SIZING_MODES:
                raise ValueError(f"sizing_mode must be one of {', '.join(SIZING_MODES)}")
            if not math.isfinite(quantity) or (sizing_mode == FIXED_QUANTITY and quantity <= 0):
                raise ValueError("fixed_quantity must be finite and greater than zero for fixed quantity sizing")
            if (maximum_risk_amount is not None
                    and (not math.isfinite(float(maximum_risk_amount)) or float(maximum_risk_amount) <= 0)):
                raise ValueError("maximum_risk_amount must be a finite value greater than zero")
            if (minimum_equity is not None
                    and (not math.isfinite(float(minimum_equity))
                         or not 0 < float(minimum_equity) <= capital_allocation)):
                raise ValueError("minimum_equity must be finite, greater than zero and no more than the allocation")
            if entry_mode not in ("limit", "market"):
                raise ValueError("entry_mode must be 'limit' or 'market'")
            from services.fill_model import normalize_fill_model
            fill_model = normalize_fill_model(fill_model)
            exchange = str(exchange or "inherit").strip().lower()
            if self.market_hub is not None and mode == "trading":
                if exchange not in ("inherit", "binance", "binance_usdm"):
                    raise ValueError(
                        "Forward-paper comparison instances require Binance USD-M market data")
                exchange = "binance_usdm"
                instrument_type = "perpetual"
            if exchange not in ("inherit", "binance", "kraken", "coinbase", "bybit"):
                if exchange != "binance_usdm":
                    raise ValueError("exchange must be Binance USD-M for forward paper")
            instrument_type = str(instrument_type or "spot").strip().lower()
            if self.market_hub is not None and mode == "trading":
                instrument_type = "perpetual"
            elif instrument_type != "spot":
                raise ValueError("Only spot instrument parity is currently supported for paper-forward instances")
            if not 1 <= int(max_open_positions) <= 50:
                raise ValueError("max_open_positions must be between 1 and 50")
            if mode == "trading":
                duplicate = next((item for item in self._instances.values()
                                  if item.owner_id == str(owner_id or OWNER_TENANT)
                                  and item.mode == "trading" and item.symbol == symbol.upper()
                                  and item.strategy_key == strategy_key
                                  and item.strategy_version == (strategy_version or "builtin-1")
                                  and item.timeframe == timeframe and item.exchange == exchange
                                  and item.state in (_ACTIVE_INSTANCE_STATES | {"paused"})), None)
                if duplicate is not None:
                    raise ValueError("This Trading Instance is already active")
                allocated = sum(item.capital_allocation for item in self._instances.values() if item.mode == "trading")
                if allocated + capital_allocation > self.paper_account_capital + 1e-9:
                    available = max(0.0, self.paper_account_capital - allocated)
                    raise ValueError(f"Capital allocation exceeds paper account capacity; available {available:.2f}")
            inst = TradingInstance(id=_id(), symbol=symbol.upper(), strategy_key=strategy_key,
                                   strategy_label=strategy_label, strategy_version=strategy_version or "builtin-1",
                                   timeframe=timeframe, risk_per_trade_pct=risk_per_trade_pct,
                                   capital_allocation=capital_allocation,
                                   exchange=exchange, instrument_type=instrument_type,
                                   max_open_positions=int(max_open_positions),
                                   sizing_mode=sizing_mode,
                                   fixed_position_size=quantity, fixed_quantity=quantity,
                                   profit_reinvestment=bool(profit_reinvestment),
                                   maximum_risk_amount=maximum_risk_amount,
                                   minimum_equity=minimum_equity,
                                   starting_equity=capital_allocation,
                                   current_realized_equity=capital_allocation,
                                   risk_basis=capital_allocation,
                                   sizing_engine_version=SIZING_ENGINE_VERSION,
                                   entry_mode=entry_mode,
                                   fill_model=fill_model, mode=mode,
                                   owner_id=str(owner_id or OWNER_TENANT),
                                   market_data_mode="paper_forward" if mode == "trading" else "replay")
            self.store.create(inst)
            try:
                self.store.ensure_simulation_session(inst)
                if mode == "trading":
                    self.store.save_market_state(
                        inst.id, market_data_mode="paper_forward",
                        market_data_status="stopped",
                    )
            except Exception:
                # Remote writes cannot span a PostgREST transaction here. Make
                # the operation atomic from the API's perspective by compensating
                # immediately if the required cursor row cannot be initialized.
                self.store.delete(inst.id, purge_sessions=True)
                raise
            from services.instance_telemetry import log_event
            log_event(self, inst, "INSTANCE_CREATED", status="created",
                      mode=inst.mode, capital_allocation=inst.capital_allocation)
            self._instances[inst.id] = inst
            return inst

    #: How old the worker's last observed price may be and still be used to
    #: realise a position. A mark left in a stopped worker's memory is not a
    #: price, it is a memory of one; writing it into trade history as a fill is
    #: the same fabrication as a guess with a plausible number attached.
    DISPOSAL_MARK_MAX_AGE_S = 120.0

    def open_position_disposition(self, instance_id: str,
                                  owner_id: str | None = None) -> dict:
        """What stands between this instance and deletion, and how to resolve it.

        Refusing a delete is correct -- an open paper position carries risk,
        P&L and a session the ledger still owns -- but a refusal with no route
        forward is a dead end. This names the positions and the one explicit
        action that resolves them, so the choice to realise them is made
        deliberately rather than as a side effect of pressing Delete.
        """
        self.instance_for(instance_id, owner_id)
        # Deliberately not session-scoped, so this lists exactly what the
        # delete guard blocks on -- including a position left open by an
        # earlier simulation session, which is the one most likely to be
        # forgotten and the one whose P&L a silent delete would discard.
        positions = InstanceLedger(self.ledger, instance_id).get_positions("open")
        runtime = self._runtime.get(instance_id)
        engine = runtime[0] if runtime else None
        worker_alive = self.worker_alive(instance_id)
        status = engine.status() if engine else {}
        marks = status.get("last_prices") or {}
        # last_activity is stamped where last_prices is written -- on a closed
        # candle. last_heartbeat is bumped on every loop pass, so a worker whose
        # feed stalled for hours still looked fresh by it, and a 1h instance was
        # always "fresh" while its mark was up to a full timeframe old.
        mark_age = _age_seconds(status.get("last_activity"))
        fresh = (worker_alive and mark_age is not None
                 and mark_age <= self.DISPOSAL_MARK_MAX_AGE_S)
        rows = []
        for position in positions:
            symbol = str(position.get("symbol"))
            entry, size = float(position.get("entry") or 0), float(position.get("size") or 0)
            mark = marks.get(symbol) if fresh else None
            reason = None
            if mark is None:
                reason = ("no worker is running to price this position"
                          if not worker_alive else
                          f"the worker's last price is {mark_age:.0f}s old"
                          if mark_age is not None and not fresh else
                          "the worker has not observed a price for this symbol")
            unrealized = None
            if mark is not None:
                direction = 1 if position.get("side") == "long" else -1
                unrealized = round(direction * (float(mark) - entry) * size, 2)
            rows.append({
                "position_id": position.get("id"), "symbol": symbol,
                "side": position.get("side"), "size": size, "entry": entry,
                "stop": position.get("stop"), "mark": mark,
                "unrealized_pnl": unrealized,
                "mark_available": mark is not None,
                "mark_reason": reason,
                "simulation_session_id": position.get("simulation_session_id"),
                "opened_at": position.get("opened_at"),
            })
        priceable = [row for row in rows if row["mark_available"]]
        return {
            "instance_id": instance_id,
            "deletable": not rows,
            "worker_running": worker_alive,
            "open_positions": rows,
            "resolution": (None if not rows else {
                "action": "close_open_positions",
                "endpoint": f"POST /instances/{instance_id}/close-open-positions",
                "effect": ("Closes every open paper position at a fresh observed mark "
                           "and realises the P&L into the session that owns it, after "
                           "which the instance can be deleted."),
                "requires_confirmation": True,
                "ready": bool(priceable) and len(priceable) == len(rows),
                "blocked_reason": (None if priceable and len(priceable) == len(rows) else
                                   "Start the Trading Instance so its market feed can "
                                   "price the open position(s); a stale or missing mark "
                                   "is never written into trade history as a fill."),
            }),
        }

    def close_open_positions(self, instance_id: str, *, owner_id: str | None = None,
                             initiated_by: str = "operator") -> dict:
        """Realise every open paper position at a fresh, observed mark.

        The explicit handling path behind a refused delete. It never invents a
        price and never uses a stale one: a mark left in a stopped worker's
        memory from hours ago is the same fabrication as a guess, only with a
        plausible number attached. A position that cannot be priced now is left
        open and reported.
        """
        with self._lifecycle_lock(instance_id):
            disposition = self.open_position_disposition(instance_id, owner_id)
            if disposition["deletable"]:
                return {"instance_id": instance_id, "closed": [], "remaining": []}
            inst = self._instances[instance_id]
            runtime = self._runtime.get(instance_id)
            from services.fill_model import from_name as fill_model_from_name
            closed, remaining = [], []
            for row in disposition["open_positions"]:
                if not row["mark_available"]:
                    remaining.append({**row, "reason": row.get("mark_reason")
                                      or "no live mark available"})
                    continue
                # Close against a ledger scoped to THIS position's session, not
                # the worker's current one: a position left by an earlier paper
                # session is exactly what the delete guard blocks on, and the
                # session-scoped engine cannot see it.
                # Close against a ledger scoped to the session that OWNS this
                # position. The worker's engine is scoped to the current
                # session, so a position left by an earlier one was invisible
                # to it: close() found nothing, reported a no-op, and the
                # delete stayed refused with no way forward at all.
                engine = runtime[1] if runtime is not None else None
                owning_session = str(row.get("simulation_session_id") or "")
                if engine is None or owning_session != inst.simulation_session_id:
                    engine = PaperExecutionEngine(
                        InstanceLedger(self.ledger, instance_id, owning_session),
                        inst.starting_equity,
                        fill_model=fill_model_from_name(inst.fill_model))
                try:
                    fill = engine.close(symbol=row["symbol"], exit_price=float(row["mark"]),
                                        execution_id=f"dispose:{instance_id}:{row['position_id']}")
                except Exception as exc:
                    # The paper engine is fail-closed: a position whose trade
                    # row is missing makes it refuse rather than guess. That is
                    # one position's problem, not a reason to abandon the rest
                    # of the disposal, and it must be reported rather than
                    # counted as realised.
                    remaining.append({**row, "reason": f"{type(exc).__name__}: {exc}"})
                    continue
                if fill.action == "noop":
                    # close() resolves one position per symbol. A second
                    # position on the same pair, or one the engine's ledger
                    # scope cannot see, closes nothing -- reporting it as
                    # realised would claim a P&L that was never written.
                    remaining.append({**row, "reason":
                                      "the execution engine found no matching open "
                                      "position for this symbol and session"})
                    continue
                closed.append({"symbol": row["symbol"], "position_id": row["position_id"],
                               "exit": fill.price, "pnl": getattr(fill, "pnl", None),
                               "action": fill.action})
            from services.instance_telemetry import log_event
            log_event(self, inst, "ORDER_CREATED", status="disposed",
                      initiated_by=initiated_by,
                      detail=(f"{len(closed)} open paper position(s) realised at a fresh "
                              f"observed mark before deletion; {len(remaining)} left open"))
            return {"instance_id": instance_id, "closed": closed, "remaining": remaining}

    def delete(self, instance_id: str, *, owner_id: str | None = None) -> str:
        """Delete one stopped instance without touching any sibling worker."""
        with self._lifecycle_lock(instance_id), self._lock:
            self._assert_reboot_idle(instance_id)
            inst = self.instance_for(instance_id, owner_id)
            runtime = self._runtime.get(instance_id)
            if runtime and runtime[0].running:
                raise ValueError("Stop the Trading Instance before deleting it")
            if InstanceLedger(self.ledger, instance_id).get_positions("open"):
                raise ValueError(
                    "This Trading Instance still holds an open paper position. Close it "
                    "explicitly first (GET /instances/{id}/open-positions shows what is "
                    "open and POST /instances/{id}/close-open-positions realises it), so "
                    "the P&L is recorded rather than discarded with the instance.")
            # Delete durable state first. If the database rejects the delete,
            # keep the in-memory worker intact so a transient persistence error
            # cannot silently stop an instance that still exists after restart.
            self.store.delete(instance_id)
            if self.event_guard is not None:
                self.event_guard.forget(instance_id)
            if self.track_record is not None:
                self.track_record.forget(instance_id)
            if self.quality_gate is not None:
                self.quality_gate.forget(instance_id)
            # A terminal-error worker has stopped its engine thread, but its
            # independently owned WebSocket feed may still be alive. Cleanup is
            # best-effort after durable deletion; stale network resources must
            # never make the already-completed delete appear to have failed.
            if runtime is not None:
                # The durable row no longer exists. Detach callbacks before
                # shutdown so a racing final candle/lifecycle event cannot
                # recreate auxiliary state or issue writes against a deleted
                # Supabase parent row.
                if hasattr(runtime[1], "equity_listener"):
                    runtime[1].equity_listener = None
                runtime[0]._lifecycle_callback = None
                runtime[0]._candle_checkpoint = None
                runtime[0]._pending_orders_checkpoint = None
                try:
                    runtime[0].stop("Trading Instance deleted")
                except Exception as exc:
                    self.ledger.log(level="warning", stage="instance",
                                    message=f"Deleted instance worker cleanup failed: {type(exc).__name__}",
                                    symbol=inst.symbol, instance_id=inst.id)
                feed = getattr(runtime[0], "ws_feed", None)
                if feed is not None:
                    try:
                        feed.stop()
                    except Exception as exc:
                        self.ledger.log(level="warning", stage="instance",
                                        message=f"Deleted instance feed cleanup failed: {type(exc).__name__}",
                                        symbol=inst.symbol, instance_id=inst.id)
            # Release ownership explicitly. A lease outliving its instance
            # would make the supervisor's "worker running for a deleted
            # instance" reconciliation check fire on every sweep.
            try:
                self.store.release_worker_lease(instance_id)
            except Exception:  # noqa: BLE001 — the row is already gone
                pass
            self._runtime.pop(instance_id, None)
            self._metric_fingerprints.pop(instance_id, None)
            self._reboots.pop(instance_id, None)
            self._reboot_threads.pop(instance_id, None)
            self._lifecycle_locks.pop(instance_id, None)
            del self._instances[instance_id]
            return inst.id

    def _global_guard(self, instance_id: str, symbol: str, entry: float, stop: float, size: float) -> tuple[bool, str]:
        # Query the scoped records rather than only in-memory workers. An
        # operator may have stopped an instance while it still owns a position;
        # that risk must remain included until the position is closed.
        all_positions = [p for p in self.ledger.get_positions("open")
                         if p.get("instance_id") in self._instances]
        risk = sum(abs(float(p.get("entry", 0)) - float(p.get("stop") or p.get("entry", 0))) * float(p.get("size", 0)) for p in all_positions)
        next_risk = abs(entry - stop) * size
        allocated_capital = sum(i.capital_allocation for i in self._instances.values() if i.mode == "trading")
        capital = allocated_capital or 1.0
        today = datetime.now(timezone.utc).date().isoformat()
        active_sessions = {item.simulation_session_id for item in self._instances.values()}
        daily_pnl = sum(float(t.get("pnl") or 0) for t in self.ledger.get_paper_trades()
                        if t.get("instance_id") in self._instances
                        and t.get("simulation_session_id") in active_sessions
                        and str(t.get("closed_at") or "").startswith(today))
        if daily_pnl <= -(capital * self.max_global_daily_loss_pct):
            return False, "Global daily loss limit reached"
        if risk + next_risk > capital * self.max_global_risk_pct:
            return False, f"Global account risk exceeded ({risk + next_risk:.2f} > {capital * self.max_global_risk_pct:.2f})"
        return True, "global risk within limit"

    def start(self, instance_id: str, *, entry_gate_closed: bool = False,
              allow_during_reboot: bool = False,
              owner_id: str | None = None,
              only_if_desired: bool = False) -> TradingInstance:
        # A start that fails after claiming the lease -- the market-data hub
        # refusing the subscription, a strategy that will not build -- used to
        # keep it until the TTL lapsed. Shutdown releases only running workers'
        # leases, so a restart inside that window found this process's orphan
        # and refused the instance. Released under the lifecycle lock, so a
        # concurrent Start cannot have claimed it in between.
        with self._lifecycle_lock(instance_id):
            try:
                return self._start(instance_id, entry_gate_closed=entry_gate_closed,
                                   allow_during_reboot=allow_during_reboot,
                                   owner_id=owner_id, only_if_desired=only_if_desired)
            except BaseException:
                with self._lock:
                    runtime = self._runtime.get(instance_id)
                    alive = runtime is not None and runtime[0].running
                if not alive:
                    self._release_lease(instance_id)
                raise

    def _start(self, instance_id: str, *, entry_gate_closed: bool,
               allow_during_reboot: bool, owner_id: str | None,
               only_if_desired: bool) -> TradingInstance:
        with self._lifecycle_lock(instance_id), self._lock:
            if only_if_desired:
                # A repair acts on intent, so it must read the intent here,
                # under the lock stop()/pause()/delete() hold. Read earlier, a
                # Stop that finished while this call waited for the lock was
                # silently undone: the worker came back, desired_running was
                # rewritten True, and Delete refused a stopped instance.
                current = self._instances.get(instance_id)
                if current is None or not current.desired_running:
                    raise InstanceNotDesired(instance_id)
                # Likewise a Pause that landed while this call waited: never
                # rebuild a paused instance with its entry gate open.
                entry_gate_closed = entry_gate_closed or current.state == "paused"
            if not allow_during_reboot:
                self._assert_reboot_idle(instance_id)
            inst = self.instance_for(instance_id, owner_id)
            prior_runtime = self._runtime.get(instance_id)
            if prior_runtime is not None and prior_runtime[0].running:
                # Idempotent: a second Start joins the running worker rather
                # than layering a second one over the same paper account.
                self._renew_lease(inst)
                return inst
            # A failed worker may still own a stopped thread and WebSocket feed.
            # Starting it again must replace those resources, never layer a
            # second feed over stale process state. Restart remains idempotent.
            prior_runtime = self._runtime.pop(instance_id, None)
            if prior_runtime is not None:
                prior_runtime[0].stop("Replacing inactive worker before start")
                prior_feed = getattr(prior_runtime[0], "ws_feed", None)
                if prior_feed is not None:
                    prior_feed.stop()
            active_trading = sum(1 for key, runtime in self._runtime.items()
                                 if runtime[0].running and self._instances[key].mode == "trading")
            if inst.mode == "trading" and active_trading >= self.max_slots:
                raise ValueError(f"Maximum active trading slots reached ({self.max_slots})")
            self.store.assert_runtime_schema()
            # Exclusive execution ownership, taken before anything is built.
            # Two containers pointed at one database would otherwise each run a
            # worker for this instance and duplicate every order it produces.
            lease = self.store.claim_worker_lease(
                instance_id, worker_id=self._worker_id, process_id=os.getpid(),
                host=_hostname(), ttl_seconds=self.worker_lease_ttl_s)
            from services.instance_telemetry import log_event
            log_event(self, inst, "INSTANCE_STARTING", status="starting",
                      worker_id=lease["worker_id"], process_id=lease["process_id"],
                      host=lease["host"], lease_expires_at=lease["lease_expires_at"],
                      config_revision=inst.config_revision)
            scoped = InstanceLedger(self.ledger, instance_id, inst.simulation_session_id)
            controls = TradingControl()
            if entry_gate_closed:
                controls.pause_all()
            forward = inst.mode == "trading"
            market = self.store.market_state(instance_id) if forward else {}
            pending_state = self._split_pending_orders(
                market.get("pending_orders_json") or {})
            self._quarantine_pending_ownership(inst, pending_state)
            if pending_state.get("quarantined_intents"):
                self.store.save_pending_orders(instance_id, pending_state)
                controls.pause_all()

            def save_forward_intents(pending: dict) -> None:
                pending_state["forward_paper_intents"] = dict(pending)
                self.store.save_pending_orders(instance_id, dict(pending_state))

            def save_strategy_intents(pending: dict) -> None:
                pending_state["strategy_limit_intents"] = dict(pending)
                self.store.save_pending_orders(instance_id, dict(pending_state))

            engine_type = (ResearchExecutionEngine if inst.mode == "research" else
                           ForwardPaperExecutionEngine if self.market_hub is not None else
                           PaperExecutionEngine)
            from services.fill_model import from_name as fill_model_from_name
            engine_kwargs = {}
            if engine_type is ForwardPaperExecutionEngine:
                engine_kwargs = {
                    "initial_intents": pending_state["forward_paper_intents"],
                    "intents_listener": save_forward_intents,
                }
            paper = engine_type(
                scoped, inst.starting_equity,
                fill_model=fill_model_from_name(inst.fill_model), **engine_kwargs)
            inst.current_realized_equity = paper.current_realized_equity()
            def persist_realized_equity(value: float) -> None:
                inst.current_realized_equity = float(value)
                inst.risk_basis = PositionSizingService.risk_basis(
                    mode=inst.sizing_mode, starting_equity=inst.starting_equity,
                    current_realized_equity=inst.current_realized_equity,
                    profit_reinvestment=inst.profit_reinvestment)
                self.store.save(inst)
            paper.equity_listener = persist_realized_equity
            # Ledger rows retain the immutable instance identity and a stable
            # strategy/version attribution without borrowing legacy state.
            paper.strategy_id = f"{inst.strategy_key}:{inst.strategy_version}"
            pipeline = SignalPipeline(scoped, paper, controls, equity=inst.capital_allocation,
                                      risk_per_trade_pct=inst.risk_per_trade_pct, exposure_limit_pct=0.05,
                                      max_open_positions=inst.max_open_positions,
                                      **self.pipeline_risk,
                                      position_sizing_mode=inst.sizing_mode,
                                      fixed_position_size=inst.fixed_quantity,
                                      equity_provider=paper.current_realized_equity,
                                      profit_reinvestment=inst.profit_reinvestment,
                                      maximum_risk_amount=inst.maximum_risk_amount,
                                      minimum_equity=inst.minimum_equity)
            pipeline.global_entry_guard = lambda **kw: self._global_guard(instance_id, **kw)
            if self.event_guard is not None:
                # Empty unless this instance opted in; read per signal, so the
                # switch applies without rebuilding the worker.
                pipeline.econ_events = self.event_guard.events_for(instance_id)
                pipeline.econ_after_min = self.event_guard.after_min
            # Trading Instances use the same explainable close/review/memory
            # lifecycle as the legacy pipeline, but retain immutable instance
            # provenance so evidence is never silently blended.
            pipeline.journal = self.decision_journal
            pipeline.trade_memory = self.trade_memory
            pipeline.skipped = self.skipped_store
            # Learning evidence is scoped to this worker's ledger/history. A
            # BTC Brain lesson can never suppress an ETH Supertrend instance.
            from services.learning import LearningBook
            learning_path = None
            ledger_path = str(getattr(self.ledger, "path", ""))
            if self.store.remote or (ledger_path and ledger_path != ":memory:"):
                default_root = (os.path.join(os.path.dirname(ledger_path), "instance-learning")
                                if ledger_path and ledger_path != ":memory:"
                                else os.path.join(os.environ.get("HUB_DATA_DIR", "/var/lib/tradexa"),
                                                  "instance-learning"))
                root = os.environ.get("HUB_INSTANCE_LEARNING_DIR", default_root)
                learning_path = os.path.join(root, f"{instance_id}.json")
            pipeline.learning = LearningBook(learning_path)
            pipeline.journal_context = {
                "instance_id": inst.id,
                "simulation_session_id": inst.simulation_session_id,
                "instance_name": f"{inst.symbol} {inst.strategy_label} {inst.timeframe} #{inst.id[:6].upper()}",
                "strategy_id": inst.strategy_key,
                "strategy_name": inst.strategy_label,
                "strategy_version": inst.strategy_version,
                "market_data_mode": "live" if inst.mode == "trading" else "replay",
                "market_data_source": None,
                "fill_model": inst.fill_model,
                "execution_mode": inst.execution_mode,
                "exchange": inst.exchange,
                "instrument_type": inst.instrument_type,
            }
            exchange = ("binance_usdm" if self.market_hub is not None and forward else
                        inst.exchange if inst.exchange != "inherit" else
                        (os.environ.get("HUB_EXCHANGE", "binance").strip() or "binance"))
            pipeline.journal_context.update({
                "market_data_mode": ("forward_paper" if self.market_hub is not None and forward
                                     else "live" if forward else "replay"),
                "market_data_source": ("Binance USD-M public WebSocket"
                                       if self.market_hub is not None and forward else None),
                "exchange": exchange,
                "instrument_type": ("perpetual"
                                    if self.market_hub is not None and forward
                                    else inst.instrument_type),
            })
            if forward:
                if self.symbol_rules_provider is not None:
                    pipeline.symbol_rules_provider = self.symbol_rules_provider
                else:
                    from data.forward_market_data import fetch_forward_symbol_rules
                    pipeline.symbol_rules_provider = lambda symbol: fetch_forward_symbol_rules(symbol, exchange)
            fetch_parameters = inspect.signature(self.forward_fetcher).parameters if forward else {}
            supports_exchange = "exchange" in fetch_parameters
            supports_since = "since_ms" in fetch_parameters
            def instance_forward_fetcher(symbol, timeframe, limit, since_ms=None, **_ignored):
                kwargs = {}
                if supports_since:
                    kwargs["since_ms"] = since_ms
                if supports_exchange:
                    kwargs["exchange"] = exchange
                return self.forward_fetcher(symbol, timeframe, limit, **kwargs)
            ws_feed = None
            runtime_fetcher = instance_forward_fetcher
            # Declared before the feed because the candle sink below wakes the
            # engine, and the feed starts delivering before the engine exists.
            engine_ref: dict[str, AutoStrategyEngine] = {}
            if forward:
                if self.market_hub is not None:
                    holder = {}

                    def on_candle_close(_bar) -> None:
                        """Wake the forward loop the moment a candle closes.

                        Without this the instance read the same live socket the
                        labs do but only every HUB_LIVE_POLL seconds, so a 5m
                        decision landed up to a minute into the next candle and
                        did not line up with PA/SMC on the same close.

                        This is a notice, not a candle delivery: the engine owns
                        a durable cursor and re-reads the feed itself, applying
                        the same continuity checks as a polled pass. Taking it
                        as a bar_sink instead would have put the candle in the
                        hub's pending map, which reports the subscription
                        unreliable until it drains and would have made this
                        instance's stop-loss fills wait on however long the
                        labs' candle processing took. Setting an Event is all
                        that happens here, so it is safe inline on the stream's
                        thread. A candle that closes before the engine exists
                        needs no wake: bootstrap reads everything past the
                        cursor.
                        """
                        engine = engine_ref.get("engine")
                        if engine is not None:
                            engine.notify_new_candle()

                    def on_quote(quote: dict) -> None:
                        subscription = holder.get("subscription")
                        if subscription is None or not subscription.status().get("reliable"):
                            return
                        fills = paper.process_quote(quote)
                        for fill in fills:
                            self.store.append_engine_log(
                                instance_id, level="info", symbol=fill.symbol,
                                message=(f"forward-paper fill {fill.side} {fill.size:.8f} "
                                         f"@ {fill.price:.8f} from Binance USD-M quote"),
                            )

                    ws_feed = self.market_hub.subscription(
                        f"INSTANCE:{instance_id}", quote_sink=on_quote,
                        candle_notice=on_candle_close)
                    holder["subscription"] = ws_feed
                    from services.instance_telemetry import log_event
                    # Whether this instance opened a Binance connection or
                    # joined one another instance already owns is the single
                    # most useful fact when reasoning about feed capacity, so
                    # record which of the two happened.
                    shared = self.market_hub.channel_exists(inst.symbol, inst.timeframe)
                    if not ws_feed.start(inst.symbol, inst.timeframe):
                        log_event(self, inst, "SUBSCRIPTION_CREATED", status="failed",
                                  detail="Binance USD-M hub refused the subscription")
                        raise RuntimeError("Binance USD-M market-data hub failed to start")
                    log_event(self, inst,
                              "SUBSCRIPTION_REUSED" if shared else "SUBSCRIPTION_CREATED",
                              status="subscribed",
                              channel=f"{inst.symbol}:{inst.timeframe}",
                              consumer_id=ws_feed.consumer_id,
                              shared_channel=bool(shared))
                    runtime_fetcher = ws_feed.make_fetcher()
                else:
                    from data.ws_feed import WebSocketFeed
                    ws_feed = WebSocketFeed([inst.symbol], timeframe=inst.timeframe,
                                            exchange=exchange, max_bars=1000)
                    ws_feed.start()  # false is honest: REST remains authoritative
                    runtime_fetcher = ws_feed.make_fetcher(instance_forward_fetcher)
            if forward:
                self.store.save_market_state(instance_id,
                    last_processed_candle_timestamp=market.get("last_processed_candle_timestamp"),
                    market_data_mode="paper_forward", market_data_status="warming_up",
                    last_market_data_timestamp=market.get("last_market_data_timestamp"),
                    data_source=market.get("data_source"), warmup_bars=int(market.get("warmup_bars") or 0),
                    duplicate_candles=int(market.get("duplicate_candles") or 0),
                    missing_candles=int(market.get("missing_candles") or 0),
                    out_of_order_candles=int(market.get("out_of_order_candles") or 0),
                    last_blocker="GATE_REJECTED: WARMUP",
                    last_blocker_timestamp=market.get("last_blocker_timestamp"))
            def checkpoint(timestamp: str) -> None:
                runtime_status = engine_ref["engine"].status() if "engine" in engine_ref else {}
                self.store.save_market_state(instance_id,
                    last_processed_candle_timestamp=timestamp,
                    worker_heartbeat=runtime_status.get("last_heartbeat") or _now(),
                    market_data_mode="paper_forward",
                    market_data_status=runtime_status.get("market_data_status", "healthy"),
                    last_market_data_timestamp=runtime_status.get("last_closed_candle") or timestamp,
                    data_source=runtime_status.get("data_source"),
                    warmup_bars=int(runtime_status.get("warmup_bars") or 0),
                    duplicate_candles=int(runtime_status.get("duplicate_candles_ignored") or 0),
                    missing_candles=int(runtime_status.get("missing_candles") or 0),
                    out_of_order_candles=int(runtime_status.get("out_of_order_candles") or 0),
                    last_blocker=runtime_status.get("last_blocker"),
                    last_blocker_timestamp=runtime_status.get("last_blocker_timestamp"))
            def lifecycle(event: dict) -> None:
                state = str(event["state"])
                with self._lock:
                    target = self._instances.get(instance_id)
                    if target is not None:
                        # Pause is an operator-owned execution gate. The market
                        # worker intentionally stays alive to maintain its
                        # cursor, so background warm-up/recovery transitions
                        # must not silently turn a paused strategy back on.
                        preserve_pause = target.state == "paused" and state not in ("error", "stopped")
                        target.state = "paused" if preserve_pause else state
                        if state == "running":
                            target.last_error = ""
                        elif state in ("data_stale", "recovering", "error"):
                            target.last_error = str(event.get("last_error") or event.get("reason") or "")[:500]
                        if state == "error":
                            # The worker has exhausted its own in-thread
                            # recovery. That is a reason to stop THIS worker,
                            # not a reason to forget that the operator wants
                            # this instance running.
                            #
                            # Clearing desired_running here was the single
                            # largest reliability defect in the platform: five
                            # consecutive feed failures (about 60s of Binance
                            # being unreachable, the delays are 2+4+8+16+30s)
                            # permanently un-desired the instance. Nothing then
                            # restarted it -- not the next candle, not a page
                            # load, and not the next container start, because
                            # startup restores desired instances only. The
                            # operator had to notice and press Start, which is
                            # exactly the "it only works after I touch it"
                            # symptom.
                            #
                            # The intent is durable now and InstanceSupervisor
                            # owns the retry with its own bounded, observable
                            # backoff. An instance is un-desired only by an
                            # explicit operator Stop.
                            target.stopped_at = str(event.get("timestamp") or _now())
                        self.store.save(target)
                if state in ("error", "stopped") and "engine" in engine_ref:
                    runtime_status = engine_ref["engine"].status()
                    self.store.save_market_state(
                        instance_id,
                        last_processed_candle_timestamp=runtime_status.get("last_processed_candle_timestamp"),
                        market_data_mode="paper_forward",
                        market_data_status=runtime_status.get("market_data_status", "error"),
                        last_market_data_timestamp=runtime_status.get("last_closed_candle"),
                        data_source=runtime_status.get("data_source"),
                        warmup_bars=int(runtime_status.get("warmup_bars") or 0),
                        duplicate_candles=int(runtime_status.get("duplicate_candles_ignored") or 0),
                        missing_candles=int(runtime_status.get("missing_candles") or 0),
                        out_of_order_candles=int(runtime_status.get("out_of_order_candles") or 0),
                        last_blocker=runtime_status.get("last_blocker"),
                        last_blocker_timestamp=runtime_status.get("last_blocker_timestamp"),
                    )
                level = "error" if state == "error" else "warning" if state in ("data_stale", "recovering") else "info"
                self.store.append_engine_log(
                    instance_id, level=level, timestamp=event.get("timestamp"),
                    message=(f"state={state} reason={event.get('reason') or ''} "
                             f"symbol={inst.symbol} timeframe={inst.timeframe}"))
                # The same transition in the canonical structured shape, so a
                # lifecycle timeline can be reconstructed by filtering on
                # instance_id without parsing several ad-hoc formats.
                from services.instance_telemetry import log_event
                mapped = _LIFECYCLE_EVENTS.get(state)
                if mapped:
                    log_event(self, inst, mapped, status=state,
                              detail=str(event.get("reason") or ""),
                              lifecycle_state=state)
            engine = AutoStrategyEngine(pipeline, paper, scoped, symbols=[inst.symbol], timeframe=inst.timeframe,
                                        strategy_factory=lambda symbol: self.strategy_factory(inst.strategy_key, symbol),
                                        live=forward, live_poll_s=self.live_poll_s,
                                        fetcher=runtime_fetcher if forward else self.fetcher,
                                        initial_last_processed_candle=market.get("last_processed_candle_timestamp"),
                                        candle_checkpoint=checkpoint if forward else None,
                                        initial_pending_orders=(pending_state["strategy_limit_intents"]
                                                                if forward else None),
                                        pending_orders_checkpoint=(save_strategy_intents if forward else None),
                                        entry_mode=inst.entry_mode, instance_id=instance_id,
                                        lifecycle_callback=lifecycle)
            engine_ref["engine"] = engine
            try:
                probe_strategy = engine.strategy_factory(inst.symbol)
            except Exception as exc:
                if ws_feed is not None:
                    ws_feed.stop()
                self._release_lease(instance_id)
                raise ValueError(
                    f"Cannot initialize {inst.strategy_label} {inst.strategy_version}: "
                    f"{type(exc).__name__}: {exc}") from exc
            required_decision_timeframe = getattr(probe_strategy, "decision_timeframe", None)
            if required_decision_timeframe and inst.timeframe != required_decision_timeframe:
                if ws_feed is not None:
                    ws_feed.stop()
                self._release_lease(instance_id)
                raise ValueError(
                    f"{inst.strategy_label} {inst.strategy_version} requires "
                    f"the {required_decision_timeframe} decision timeframe")
            engine.ws_feed = ws_feed
            # The revision this worker is running, so the dashboard can never
            # show edited settings while the worker is still using the old ones.
            engine.config_revision = inst.config_revision
            engine.worker_id = self._worker_id
            engine.strategy_label = f"{inst.strategy_label} {inst.strategy_version}"
            engine.strategy_key = inst.strategy_key
            engine.strategy_version = inst.strategy_version
            engine.decisions = self.decision_store
            engine.reports = self.cycle_store
            if self.quality_gate is not None:
                # Read per signal, so the switch applies without a rebuild.
                engine.quality_gate_bypass = lambda: self.quality_gate.enabled(instance_id)
            self._runtime[instance_id] = (engine, paper, pipeline, controls)
            # A start with the entry gate closed IS a paused instance. Writing
            # "starting" over it destroyed the only durable record that the
            # operator had disarmed this strategy, so the NEXT restart read
            # state="running", restored with the gate open, and silently
            # re-armed it.
            inst.state = "paused" if entry_gate_closed else "starting"
            inst.desired_running, inst.last_error = True, ""
            inst.started_at, inst.stopped_at = _now(), None
            self.store.save(inst)
            engine.start()
            observed_state = engine.status().get("lifecycle_state")
            if (observed_state and observed_state != inst.state
                    and not entry_gate_closed):
                # A paused instance keeps its paused marker whatever the worker
                # reports; its lifecycle is healthy, its entries are not armed.
                inst.state = observed_state
                self.store.save(inst)
            if pending_state.get("quarantined_intents"):
                inst.state = "degraded"
                inst.last_error = "PENDING_ORDER_OWNERSHIP_INVALID: quarantined intent requires ownership repair; worker remains available"
                engine.last_blocker = "PENDING_ORDER_OWNERSHIP_INVALID"
                self.store.save(inst)
            return inst

    def _renew_lease(self, inst: TradingInstance) -> bool:
        """Extend this process's ownership. False means it was lost."""
        try:
            return self.store.renew_worker_lease(
                inst.id, worker_id=self._worker_id, ttl_seconds=self.worker_lease_ttl_s)
        except Exception:  # noqa: BLE001 — a telemetry write must not stop a worker
            return True

    def _release_lease(self, instance_id: str) -> None:
        try:
            self.store.release_worker_lease(instance_id, worker_id=self._worker_id)
        except Exception:  # noqa: BLE001 — the lease expires on its own regardless
            pass

    def worker_ownership(self, instance_id: str) -> dict:
        """Who owns this instance's execution, and is that this process?"""
        try:
            lease = self.store.worker_lease(instance_id)
        except Exception as exc:  # noqa: BLE001
            return {"known": False, "detail": f"{type(exc).__name__}: {exc}"}
        if not lease:
            return {"known": True, "held": False, "instance_id": instance_id}
        return {
            "known": True, "held": True, "instance_id": instance_id,
            "worker_id": lease.get("worker_id"),
            "process_id": lease.get("process_id"),
            "host": lease.get("host"),
            "started_at": lease.get("started_at"),
            "heartbeat_at": lease.get("heartbeat_at"),
            "lease_expires_at": lease.get("lease_expires_at"),
            "this_process": lease.get("worker_id") == self._worker_id,
            "expired": _iso_before(lease.get("lease_expires_at"), _now()),
        }

    def halt_runtime(self, instance_id: str, *, reason: str, state: str = "error"
                     ) -> TradingInstance | None:
        """Stop this process's worker WITHOUT clearing the operator's intent.

        Distinct from stop(), which is an operator decision that the instance
        should no longer run. This is for the cases where this process must
        give up the runtime but nobody has decided anything -- losing the
        worker lease, or a configuration rebuild that has to tear the worker
        down before building the replacement. Clearing desired_running there
        would write "no longer wanted" into a row other processes read, and
        nothing would ever restart it.
        """
        with self._lifecycle_lock(instance_id):
            with self._lock:
                inst = self._instances.get(instance_id)
                if inst is None:
                    return None
                runtime = self._runtime.get(instance_id)
            if runtime:
                runtime[0].stop(reason)
                feed = getattr(runtime[0], "ws_feed", None)
                if feed is not None:
                    feed.stop()
            with self._lock:
                self._runtime.pop(instance_id, None)
            inst.state = state
            # Only a genuine fault writes last_error. A routine configuration
            # rebuild passing through here would otherwise leave an error the
            # dashboard surfaces and an INSTANCE_ERROR row that makes real
            # worker failures indistinguishable from ordinary edits.
            fault = state not in ("starting", "paused", "stopped")
            inst.last_error = reason[:500] if fault else ""
            self.store.save(inst)
            from services.instance_telemetry import log_event
            log_event(self, inst, "INSTANCE_ERROR" if fault else "INSTANCE_STARTING",
                      status=state,
                      detail=f"runtime halted, restart intent preserved: {reason}")
            return inst

    def stop(self, instance_id: str, *, owner_id: str | None = None) -> TradingInstance:
        # The whole transition runs under this instance's lifecycle mutex. The
        # state lock is taken only for the state read, because engine.stop()
        # joins the worker thread and that thread takes the state lock from its
        # own lifecycle callback.
        with self._lifecycle_lock(instance_id):
            with self._lock:
                self._assert_reboot_idle(instance_id)
                inst = self.instance_for(instance_id, owner_id)
                runtime = self._runtime.get(instance_id)
            if runtime:
                runtime[0].stop("Stopped by instance operator")
                ws_feed = getattr(runtime[0], "ws_feed", None)
                if ws_feed is not None:
                    ws_feed.stop()
            inst.state, inst.desired_running, inst.stopped_at = "stopped", False, _now()
            self.store.save(inst)
            # Ownership goes with the worker. Holding an expired lease for the
            # TTL after a clean stop would refuse a legitimate restart.
            self._release_lease(instance_id)
            from services.instance_telemetry import log_event
            log_event(self, inst, "INSTANCE_STOPPED", status="stopped",
                      detail="stopped by operator; restart intent and worker lease released")
            return inst

    def shutdown(self, timeout_s: float = 15.0) -> dict:
        """Quiesce every worker without erasing durable restart intent.

        Entry gates close first, then each in-flight candle must acknowledge and
        checkpoint. Worker/feed threads are joined only after that boundary.
        """
        deadline = time.monotonic() + max(1.0, float(timeout_s))
        with self._lock:
            runtimes = list(self._runtime.items())
            reboot_threads = list(self._reboot_threads.values())
        report = {"requested": len(runtimes), "acknowledged": 0, "errors": []}
        for instance_id, (engine, _paper, _pipeline, controls) in runtimes:
            controls.pause_all()
            try:
                remaining = max(0.01, deadline - time.monotonic())
                checkpoint = engine.acknowledge_entry_pause(remaining)
                report["acknowledged"] += 1
                self.store.append_engine_log(
                    instance_id, level="info",
                    message=f"shutdown_checkpoint={checkpoint}")
            except Exception as exc:
                report["errors"].append(
                    f"{instance_id}: checkpoint {type(exc).__name__}: {exc}")
            # A process shutdown must not rewrite desired_running=False through
            # the ordinary stopped lifecycle callback. Startup owns restoration.
            engine._lifecycle_callback = None
            try:
                engine.stop("Process shutdown after entry-gate checkpoint")
            except Exception as exc:
                report["errors"].append(
                    f"{instance_id}: engine stop {type(exc).__name__}: {exc}")
            feed = getattr(engine, "ws_feed", None)
            if feed is not None:
                try:
                    feed.stop()
                except Exception as exc:
                    report["errors"].append(
                        f"{instance_id}: feed stop {type(exc).__name__}: {exc}")
            # Hand execution ownership back on a graceful shutdown. Without
            # this the successor process would be refused until the lease TTL
            # elapsed -- correct for a crash, where the old worker genuinely
            # might still be alive, but wrong for a clean restart where this
            # process has just proven it is not.
            self._release_lease(instance_id)
        for thread in reboot_threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                report["errors"].append("full reboot worker did not stop before shutdown timeout")
        return report

    def restart(self, instance_id: str, *, owner_id: str | None = None) -> TradingInstance:
        """Begin a genuine staged Full Bot Reboot and return immediately."""
        inst = self.instance_for(instance_id, owner_id)
        self.request_full_reboot(instance_id)
        return inst

    @staticmethod
    def _split_pending_orders(raw_pending: dict) -> dict[str, dict]:
        """Normalize legacy and namespaced pending-order checkpoints.

        The strategy engine and forward quote-fill engine own different intent
        schemas. Reboot validation must never mistake their namespace keys for
        symbols, and must preserve both sets independently.
        """
        if not isinstance(raw_pending, dict):
            raise RuntimeError("Persisted pending-order state is not an object")
        namespaced = (
            "forward_paper_intents" in raw_pending
            or "strategy_limit_intents" in raw_pending
            or "quarantined_intents" in raw_pending
        )
        if not namespaced:
            return {
                "forward_paper_intents": {},
                "strategy_limit_intents": dict(raw_pending),
            }
        forward = raw_pending.get("forward_paper_intents") or {}
        strategy = raw_pending.get("strategy_limit_intents") or {}
        if not isinstance(forward, dict) or not isinstance(strategy, dict):
            raise RuntimeError("Persisted pending-order namespaces must be objects")
        return {
            "forward_paper_intents": dict(forward),
            "strategy_limit_intents": dict(strategy),
            **({"quarantined_intents": dict(raw_pending["quarantined_intents"])}
               if raw_pending.get("quarantined_intents") else {}),
        }

    @staticmethod
    def _quarantine_pending_ownership(instance, pending_state):
        """Preserve bad records without adopting or executing someone else's intent."""
        for namespace in ("strategy_limit_intents", "forward_paper_intents"):
            for symbol, order in list(pending_state[namespace].items()):
                payload = (order.get("payload") or {}) if isinstance(order, dict) else {}
                context = (order.get("sizing_context") or {}) if isinstance(order, dict) else {}
                valid = str(symbol).upper() == instance.symbol and isinstance(order, dict)
                for owner in (order, payload, context):
                    if not isinstance(owner, dict):
                        valid = False
                        continue
                    for field, expected in (("symbol", instance.symbol), ("instance_id", instance.id),
                                            ("simulation_session_id", instance.simulation_session_id)):
                        if owner.get(field) is not None and str(owner[field]) != expected:
                            valid = False
                if not valid:
                    pending_state.setdefault("quarantined_intents", {})[f"{namespace}:{symbol}"] = {
                        "blocker": "PENDING_ORDER_OWNERSHIP_INVALID", "namespace": namespace,
                        "symbol_key": symbol, "original": order,
                    }
                    del pending_state[namespace][symbol]

    @staticmethod
    def _validate_reboot_recovery(instance: TradingInstance, positions: list[dict],
                                  trades: list[dict], pending_orders: dict) -> None:
        """Fail closed when persisted execution state cannot be restored exactly."""
        if len(positions) > instance.max_open_positions:
            raise RuntimeError(
                f"Reconciliation found {len(positions)} positions, above the configured maximum "
                f"of {instance.max_open_positions}")
        open_trades = [trade for trade in trades if trade.get("status") == "open"]
        if len(open_trades) != len(positions):
            raise RuntimeError(
                "Open position and open paper-trade counts do not reconcile")
        for trade in open_trades:
            if (str(trade.get("instance_id") or "") != instance.id
                    or str(trade.get("simulation_session_id") or "") != instance.simulation_session_id
                    or str(trade.get("symbol") or "").upper() != instance.symbol):
                raise RuntimeError("An open paper-trade has invalid Trading Instance ownership")
        for position in positions:
            if (str(position.get("instance_id") or "") != instance.id
                    or str(position.get("simulation_session_id") or "") != instance.simulation_session_id):
                raise RuntimeError("A persisted position has invalid Trading Instance ownership")
            if str(position.get("symbol") or "").upper() != instance.symbol:
                raise RuntimeError("A persisted position belongs to a different symbol")
            if position.get("side") not in ("long", "short"):
                raise RuntimeError("A persisted position has an invalid side")
            for field_name in ("size", "entry", "stop", "target"):
                value = position.get(field_name)
                if value is None or not math.isfinite(float(value)) or float(value) <= 0:
                    raise RuntimeError(
                        f"Open-position {field_name} is missing or invalid; exact protection cannot be restored")
            raw_management = position.get("management_json") or {}
            if isinstance(raw_management, str):
                try:
                    raw_management = json.loads(raw_management)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Open-position management state is invalid JSON") from exc
            if not isinstance(raw_management, dict):
                raise RuntimeError("Open-position management state is not an object")
            matching_trades = [trade for trade in open_trades
                               if str(trade.get("symbol") or "").upper() == instance.symbol]
            if not matching_trades:
                raise RuntimeError("Open position has no matching open paper-trade record")
        if not isinstance(pending_orders, dict):
            raise RuntimeError("Persisted pending-order state is not an object")
        for symbol, order in pending_orders.items():
            if str(symbol).upper() != instance.symbol or not isinstance(order, dict):
                raise RuntimeError("A pending order has invalid instance/symbol ownership")
            payload = order.get("payload")
            if not isinstance(payload, dict):
                raise RuntimeError("A pending order is missing its persisted execution payload")
            if str(order.get("side") or "").upper() not in ("BUY", "SELL"):
                raise RuntimeError("A pending order has an invalid side")
            try:
                price = float(order.get("price"))
                target = float(order.get("target"))
                ttl = int(order.get("ttl"))
                entry = float(payload.get("entry"))
                stop = float(payload.get("stop"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("A pending order has incomplete execution levels") from exc
            if (not all(math.isfinite(value) and value > 0
                        for value in (price, target, entry, stop)) or ttl <= 0):
                raise RuntimeError("A pending order has invalid execution levels or expiry")
            payload_symbol = str(payload.get("symbol") or symbol).upper()
            if payload_symbol != instance.symbol:
                raise RuntimeError("A pending order payload belongs to a different symbol")

    def request_full_reboot(self, instance_id: str) -> dict:
        """Start one asynchronous, backend-owned runtime reconstruction."""
        with self._lock:
            instance = self._instances[instance_id]
            if instance.mode != "trading":
                raise ValueError("Full Bot Reboot is available only for automated Trading Instances")
            self._assert_reboot_idle(instance_id)
            runtime = self._runtime.get(instance_id)
            if runtime is not None:
                # Close the execution gate before the request returns. The
                # background worker cannot accept a new entry after this point.
                runtime[3].pause_all()
                runtime[0]._lifecycle_callback = None
            reboot_id = _id()
            self._reboots[instance_id] = {
                "id": reboot_id, "instance_id": instance_id,
                "status": _REBOOT_RUNNING, "phase": "blocking_entries",
                "message": "New paper entries are blocked",
                "started_at": _now(), "updated_at": _now(),
                "completed_at": None, "error": "", "details": {},
            }
            thread = threading.Thread(
                target=self._run_full_reboot, args=(instance_id, reboot_id),
                name=f"full-reboot-{instance_id}", daemon=True)
            self._reboot_threads[instance_id] = thread
            try:
                self.store.append_engine_log(
                    instance_id, level="info",
                    message="full_reboot phase=blocking_entries status=running message=New paper entries are blocked")
            except Exception:
                pass
            thread.start()
            return dict(self._reboots[instance_id])

    def _run_full_reboot(self, instance_id: str, reboot_id: str) -> None:
        preserved: dict = {}
        try:
            with self._lock:
                runtime = self._runtime.get(instance_id)
                instance = self._instances[instance_id]
                session_id = instance.simulation_session_id
            if runtime is not None:
                engine, paper, _pipeline, _controls = runtime
                preserved["balance"] = float(paper.current_realized_equity())
                self._set_reboot_phase(instance_id, "stopping_worker",
                                       "Stopping strategy processing gracefully")
                engine.stop("Full Bot Reboot requested by operator")
                worker_thread = getattr(engine, "_thread", None)
                if worker_thread is not None and worker_thread.is_alive():
                    feed = getattr(engine, "ws_feed", None)
                    if feed is not None:
                        feed.stop()
                    raise RuntimeError(
                        "The existing worker did not stop cleanly; runtime replacement was aborted")
                self._set_reboot_phase(instance_id, "disconnecting_market_data",
                                       "Disconnecting the current market-data transport")
                feed = getattr(engine, "ws_feed", None)
                if feed is not None:
                    feed.stop()
                self._set_reboot_phase(instance_id, "flushing_runtime_state",
                                       "Persisting candle, order and position checkpoints")
                preserved["runtime_checkpoint"] = engine.flush_runtime_state()
                paper.equity_listener = None
            self._set_reboot_phase(instance_id, "clearing_transient_state",
                                   "Discarding caches, queues, timers and stale runtime errors")
            with self._lock:
                if runtime is not None and self._runtime.get(instance_id) is runtime:
                    self._runtime.pop(instance_id, None)
                self._metric_fingerprints.pop(instance_id, None)

            self._set_reboot_phase(instance_id, "reloading_configuration",
                                   "Reloading the saved Trading Instance configuration")
            reloaded = next((row for row in self.store.list() if row.id == instance_id), None)
            if reloaded is None:
                raise RuntimeError("Saved Trading Instance configuration could not be reloaded")
            if reloaded.simulation_session_id != session_id:
                raise RuntimeError("Simulation session changed during Full Bot Reboot")
            with self._lock:
                self._instances[instance_id] = reloaded
            scoped = InstanceLedger(self.ledger, instance_id, reloaded.simulation_session_id)
            positions = scoped.get_positions("open")
            trades = scoped.get_paper_trades()
            persisted_balance = reloaded.starting_equity + sum(
                float(trade.get("pnl") or 0) for trade in trades
                if trade.get("status") == "closed")
            if "balance" in preserved and abs(persisted_balance - preserved["balance"]) > 0.000001:
                raise RuntimeError("Persisted paper balance changed while the worker was stopping")
            preserved["balance"] = float(persisted_balance)
            market = self.store.market_state(instance_id)
            pending_state = self._split_pending_orders(
                market.get("pending_orders_json") or {})
            self._quarantine_pending_ownership(reloaded, pending_state)
            if pending_state.get("quarantined_intents"):
                self.store.save_pending_orders(instance_id, pending_state)
            strategy_pending = pending_state["strategy_limit_intents"]
            forward_pending = pending_state["forward_paper_intents"]
            self._set_reboot_phase(instance_id, "reconciling_execution_state",
                                   "Reconciling persisted positions and pending orders")
            self._validate_reboot_recovery(
                reloaded, positions, trades, strategy_pending)
            if forward_pending and self.market_hub is None:
                raise RuntimeError(
                    "Forward-paper intents cannot be restored without the market-data hub")
            preserved.update({
                "simulation_session_id": session_id,
                "position_ids": sorted(str(row.get("id")) for row in positions),
                "pending_order_symbols": sorted(
                    set(strategy_pending) | set(forward_pending)),
            })

            self._set_reboot_phase(instance_id, "connecting_market_data",
                                   "Creating a fresh worker and reconnecting market data")
            self.start(instance_id, entry_gate_closed=True, allow_during_reboot=True)
            with self._lock:
                new_runtime = self._runtime[instance_id]
            new_engine, new_paper, _new_pipeline, new_controls = new_runtime
            restored_positions = new_paper.positions()
            restored_ids = sorted(str(row.get("id")) for row in restored_positions)
            if restored_ids != preserved["position_ids"]:
                raise RuntimeError("Open-position reconciliation changed the persisted position set")
            if dict(new_engine._pending) != strategy_pending:
                raise RuntimeError("Pending-order reconciliation did not restore the persisted order set")
            restored_forward = (
                new_paper.pending_intents()
                if callable(getattr(new_paper, "pending_intents", None)) else {})
            if restored_forward != forward_pending:
                raise RuntimeError(
                    "Forward-paper intent reconciliation did not restore the persisted intent set")
            if abs(float(new_paper.current_realized_equity()) - preserved["balance"]) > 0.000001:
                raise RuntimeError("Paper balance changed during Full Bot Reboot")
            for position in restored_positions:
                if new_engine._adopt(position["symbol"], position) is None:
                    raise RuntimeError("Open-position protection could not be reconstructed")

            deadline = time.monotonic() + self.full_reboot_timeout_s
            last_phase = ""
            while time.monotonic() < deadline:
                status = new_engine.status()
                lifecycle = str(status.get("lifecycle_state") or "starting")
                if lifecycle in ("error", "stopped"):
                    raise RuntimeError(status.get("last_error") or status.get("stop_reason")
                                       or "Replacement worker stopped during health verification")
                phase = ("loading_warmup" if lifecycle in ("starting", "bootstrapping", "warming")
                         else "rebuilding_indicators" if lifecycle == "syncing"
                         else "running_health_checks")
                if phase != last_phase:
                    messages = {
                        "loading_warmup": "Loading closed-candle warm-up history",
                        "rebuilding_indicators": "Rebuilding indicators and strategy state",
                        "running_health_checks": "Verifying worker, feed and execution ownership",
                    }
                    self._set_reboot_phase(instance_id, phase, messages[phase])
                    last_phase = phase
                market_status = str(status.get("market_data_status") or "")
                if lifecycle == "running" and market_status == "healthy":
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("Replacement worker did not become healthy before the reboot timeout")

            if pending_state.get("quarantined_intents"):
                # The worker and existing position protection are restored.
                # Unowned intents stay preserved and cannot authorize entries.
                blocker = "PENDING_ORDER_OWNERSHIP_INVALID"
                new_engine.last_blocker = blocker
                with self._lock:
                    current = self._instances[instance_id]
                    current.state, current.desired_running = "degraded", True
                    current.last_error = f"{blocker}: worker restored; quarantined intents need ownership repair"
                    self.store.save(current)
                self._set_reboot_phase(instance_id, "pending_ownership_blocked", current.last_error,
                                       status="degraded", error=blocker,
                                       details={**preserved, "quarantined_intents": pending_state["quarantined_intents"],
                                                "worker_restored": True})
                return
            new_controls.resume()
            with self._lock:
                current = self._instances[instance_id]
                current.state, current.desired_running = "running", True
                current.last_error = ""
                self.store.save(current)
            self._set_reboot_phase(
                instance_id, "running", "Full Bot Reboot completed; execution gate reopened",
                status="completed", details={**preserved, "transient_state_recreated": True})
        except Exception as exc:  # fail closed; never manufacture a close/fill during recovery
            detail = f"{type(exc).__name__}: {exc}"[:500]
            with self._lock:
                current = self._instances.get(instance_id)
                runtime = self._runtime.get(instance_id)
                if runtime is not None:
                    runtime[3].pause_all()
                if current is not None:
                    current.state = "degraded"
                    current.desired_running = bool(runtime is not None and runtime[0].running)
                    current.last_error = (
                        "Full Bot Reboot reconciliation failed; new entries remain blocked. "
                        f"Manual repair required: {detail}")
                    self.store.save(current)
            self._set_reboot_phase(
                instance_id, "reconciliation_failed",
                "Reboot is degraded; new entries remain blocked pending manual repair",
                status="degraded", error=detail, details=preserved)
        finally:
            with self._lock:
                active = self._reboot_threads.get(instance_id)
                if active is threading.current_thread():
                    self._reboot_threads.pop(instance_id, None)

    def restart_simulation_account(self, instance_id: str, *, initiated_by: str) -> dict:
        """Reset one paper account while preserving configuration and history."""
        with self._lock:
            self._assert_reboot_idle(instance_id)
            inst = self._instances[instance_id]
            if inst.execution_mode.lower() != "paper" or inst.mode != "trading":
                raise ValueError("Simulation account restart is allowed only for Paper Trading instances")
            runtime = self._runtime.get(instance_id)
            prior_state = inst.state
            had_running_worker = bool(runtime is not None and runtime[0].running)
            previous_session_id = inst.simulation_session_id
            if runtime is not None:
                # Close the entry gate before releasing the manager lock. The
                # worker is stopped outside this lock so its final lifecycle
                # callback cannot deadlock while the stop waits for its thread.
                runtime[3].pause_all()

        if runtime is not None:
            self.stop(instance_id)
            feed = getattr(runtime[0], "ws_feed", None)
            if feed is not None:
                feed.stop()

        with self._lock:
            inst = self._instances[instance_id]
            if inst.simulation_session_id != previous_session_id:
                raise RuntimeError("Simulation session changed while account restart was acquiring its execution lock")
            if runtime is not None:
                previous_balance = runtime[1].current_realized_equity()
                runtime[1].equity_listener = None
                self._runtime.pop(instance_id, None)
            else:
                current_trades = self.ledger.get_paper_trades(
                    instance_id=instance_id,
                    simulation_session_id=inst.simulation_session_id)
                previous_balance = inst.starting_equity + sum(
                    float(trade.get("pnl") or 0) for trade in current_trades
                    if trade.get("status") == "closed")
            result = self.store.restart_simulation_account(
                inst, previous_balance=float(previous_balance),
                initiated_by=initiated_by or "authenticated operator")
            inst.current_realized_equity = inst.starting_equity
            inst.risk_basis = inst.starting_equity
            inst.simulation_session_id = str(result["new_session_id"])
            inst.simulation_session_number = int(result["session_number"])
            self._metric_fingerprints.pop(instance_id, None)

            if self.decision_journal is not None:
                try:
                    self.decision_journal.store.cancel_open_for_instance(
                        instance_id,
                        reason="Paper position terminated by simulation account restart; no execution fill was fabricated.")
                except Exception as exc:  # noqa: BLE001 - the ledger reset already committed
                    self.ledger.log(
                        level="warning", stage="simulation_account_restart",
                        message=f"Journal reset annotation failed: {type(exc).__name__}",
                        symbol=inst.symbol, instance_id=inst.id)

            resumed = False
            resume_error = ""
            if had_running_worker:
                try:
                    self.start(instance_id)
                    if prior_state == "paused":
                        self.pause(instance_id)
                    resumed = True
                except Exception as exc:  # account reset is durable even if market data cannot resume
                    resume_error = f"{type(exc).__name__}: {exc}"[:500]
                    inst.state, inst.desired_running = "error", False
                    inst.last_error = f"Simulation account reset succeeded, but worker resume failed: {resume_error}"
                    self.store.save(inst)

            self.ledger.log(
                level="info", stage="simulation_account_restart",
                message=(f"Simulation account restarted: session {result.get('previous_session_id') or 'legacy'} "
                         f"→ {result['new_session_id']}; positions={result['open_positions_cleared']} "
                         f"pending_orders={result['pending_orders_cleared']}"),
                symbol=inst.symbol, instance_id=inst.id)
            return {**result, "resumed": resumed, "resume_error": resume_error,
                    "instance": self.status(instance_id)}

    def pause(self, instance_id: str, *, owner_id: str | None = None) -> TradingInstance:
        """Close the entry gate. The market worker deliberately stays alive."""
        with self._lifecycle_lock(instance_id):
            with self._lock:
                self._assert_reboot_idle(instance_id)
                inst = self.instance_for(instance_id, owner_id)
                runtime = self._runtime.get(instance_id)
            if runtime:
                runtime[3].pause_all()
                try:
                    acknowledgement = runtime[0].acknowledge_entry_pause()
                    self.store.append_engine_log(
                        instance_id, level="info",
                        message=f"pause_acknowledged checkpoint={acknowledgement}")
                except Exception as exc:
                    inst.state, inst.desired_running = "degraded", False
                    inst.last_error = f"Pause acknowledgement failed: {type(exc).__name__}: {exc}"
                    self.store.save(inst)
                    raise RuntimeError(inst.last_error) from exc
            # Pause is an entry gate, not a shutdown: the worker stays alive to
            # keep its durable candle cursor and its market subscription.
            # Clearing desired_running contradicted that the moment the process
            # restarted -- the instance came back paused with no worker at all,
            # so its feed stopped and its cursor froze until someone pressed
            # Resume. The intent stays; restore_desired_instances() brings a
            # paused instance back with its entry gate already closed.
            inst.state, inst.desired_running = "paused", True
            self.store.save(inst)
            from services.instance_telemetry import log_event
            log_event(self, inst, "INSTANCE_PAUSED", status="paused",
                      detail="entry gate closed by operator; market worker retained")
            return inst

    def resume(self, instance_id: str, *, owner_id: str | None = None) -> TradingInstance:
        with self._lifecycle_lock(instance_id):
            with self._lock:
                self._assert_reboot_idle(instance_id)
                inst = self.instance_for(instance_id, owner_id)
                runtime = self._runtime.get(instance_id)
                pending = self.store.market_state(instance_id).get("pending_orders_json") or {}
                if pending.get("quarantined_intents"):
                    raise ValueError("PENDING_ORDER_OWNERSHIP_INVALID: repair quarantined intents before arming entries")
            if runtime and runtime[0].running:
                runtime[3].resume()
                inst.state, inst.desired_running = "running", True
                self.store.save(inst)
                self._renew_lease(inst)
                return inst
            # A terminal error can retain its diagnostics object after the worker
            # thread exits. Never label that dead object running; build a new worker.
            return self.start(instance_id, owner_id=owner_id)

    def update_configuration(self, instance_id: str, *, capital_allocation: float | None = None,
                             risk_per_trade_pct: float | None = None, sizing_mode: str | None = None,
                             fixed_position_size: float | None = None,
                             fixed_quantity: float | None = None,
                             profit_reinvestment: bool | None = None,
                             maximum_risk_amount: float | None = None,
                             minimum_equity: float | None = None,
                             entry_mode: str | None = None,
                             fill_model: str | None = None,
                             exchange: str | None = None,
                             instrument_type: str | None = None,
                             max_open_positions: int | None = None,
                             strategy_key: str | None = None, strategy_label: str | None = None,
                             strategy_version: str | None = None, timeframe: str | None = None,
                             owner_id: str | None = None) -> TradingInstance:
        """Persist execution configuration and safely rebuild an active worker.

        Strategy/timeframe changes cannot mutate a running strategy object in
        place. The manager therefore refuses edits while a position is open,
        stops the worker, persists one authoritative configuration, and
        restores its prior running/paused lifecycle state.
        """
        with self._lifecycle_lock(instance_id), self._lock:
            self._assert_reboot_idle(instance_id)
            inst = self.instance_for(instance_id, owner_id)
            open_positions = InstanceLedger(self.ledger, instance_id).get_positions("open")
            trade_history = self.ledger.get_paper_trades(instance_id=instance_id)
            prior_state, prior_error = inst.state, inst.last_error
            had_runtime = instance_id in self._runtime
            candidate_capital = float(capital_allocation if capital_allocation is not None else inst.capital_allocation)
            if not math.isfinite(candidate_capital) or candidate_capital <= 0:
                raise ValueError("capital_allocation must be a finite value greater than zero")
            if (capital_allocation is not None and trade_history
                    and abs(candidate_capital - inst.capital_allocation) > 0.0000001):
                raise ValueError("Capital allocation is immutable after the first trade; create a new instance")
            if (risk_per_trade_pct is not None
                    and (not math.isfinite(float(risk_per_trade_pct))
                         or not 0 < float(risk_per_trade_pct) <= 0.05)):
                raise ValueError("risk_per_trade_pct must be a finite value in (0, 0.05]")
            if (risk_per_trade_pct is not None
                    and float(risk_per_trade_pct) > self.max_instance_risk_per_trade_pct):
                raise ValueError(
                    f"risk_per_trade_pct exceeds the platform ceiling of {self.max_instance_risk_per_trade_pct}")
            candidate_mode = normalize_sizing_mode(sizing_mode if sizing_mode is not None else inst.sizing_mode)
            if candidate_mode not in SIZING_MODES:
                raise ValueError(f"sizing_mode must be one of {', '.join(SIZING_MODES)}")
            supplied_quantity = fixed_quantity if fixed_quantity is not None else fixed_position_size
            candidate_fixed = float(supplied_quantity if supplied_quantity is not None else inst.fixed_quantity)
            if (not math.isfinite(candidate_fixed) or candidate_fixed < 0
                    or (candidate_mode == FIXED_QUANTITY and candidate_fixed <= 0)):
                raise ValueError("fixed_quantity must be finite and greater than zero for fixed quantity sizing")
            candidate_reinvest = bool(profit_reinvestment if profit_reinvestment is not None else inst.profit_reinvestment)
            candidate_max_risk = maximum_risk_amount if maximum_risk_amount is not None else inst.maximum_risk_amount
            candidate_floor = minimum_equity if minimum_equity is not None else inst.minimum_equity
            if (candidate_max_risk is not None
                    and (not math.isfinite(float(candidate_max_risk)) or float(candidate_max_risk) <= 0)):
                raise ValueError("maximum_risk_amount must be a finite value greater than zero")
            if (candidate_floor is not None
                    and (not math.isfinite(float(candidate_floor))
                         or not 0 < float(candidate_floor) <= candidate_capital)):
                raise ValueError("minimum_equity must be finite, greater than zero and no more than the allocation")
            candidate_entry = entry_mode if entry_mode is not None else inst.entry_mode
            if candidate_entry not in ("limit", "market"):
                raise ValueError("entry_mode must be 'limit' or 'market'")
            from services.fill_model import normalize_fill_model
            candidate_fill = normalize_fill_model(fill_model if fill_model is not None else inst.fill_model)
            if (fill_model is not None and trade_history
                    and candidate_fill != inst.fill_model):
                raise ValueError("Fill model is immutable after the first trade; create a new instance")
            candidate_exchange = str(exchange if exchange is not None else inst.exchange).strip().lower()
            if self.market_hub is not None and inst.mode == "trading":
                if candidate_exchange not in ("inherit", "binance", "binance_usdm"):
                    raise ValueError(
                        "Forward-paper comparison instances require Binance USD-M market data")
                candidate_exchange = "binance_usdm"
            elif candidate_exchange not in ("inherit", "binance", "kraken", "coinbase", "bybit"):
                raise ValueError("exchange must be one of inherit, binance, kraken, coinbase, bybit")
            candidate_instrument = str(instrument_type if instrument_type is not None
                                       else inst.instrument_type).strip().lower()
            if self.market_hub is not None and inst.mode == "trading":
                candidate_instrument = "perpetual"
            elif candidate_instrument != "spot":
                raise ValueError("Only spot instrument parity is currently supported")
            previous_exchange = ("binance_usdm"
                                 if self.market_hub is not None and inst.mode == "trading"
                                 else inst.exchange)
            previous_instrument = ("perpetual"
                                   if self.market_hub is not None and inst.mode == "trading"
                                   else inst.instrument_type)
            if trade_history and (candidate_exchange != previous_exchange
                                  or candidate_instrument != previous_instrument):
                raise ValueError("Venue and instrument type are immutable after the first trade; create a new instance")
            candidate_max = int(max_open_positions if max_open_positions is not None else inst.max_open_positions)
            if not 1 <= candidate_max <= 50:
                raise ValueError("max_open_positions must be between 1 and 50")
            allocated_elsewhere = sum(item.capital_allocation for key, item in self._instances.items()
                                      if key != instance_id and item.mode == "trading")
            if inst.mode == "trading" and allocated_elsewhere + candidate_capital > self.paper_account_capital + 1e-9:
                raise ValueError("Capital allocation exceeds paper account capacity")
            rebuild_required = any(value is not None for value in (
                capital_allocation, sizing_mode, fixed_position_size, fixed_quantity,
                profit_reinvestment, maximum_risk_amount, minimum_equity, entry_mode, fill_model,
                exchange, instrument_type,
                strategy_key, strategy_label, strategy_version, timeframe,
            ))
            if rebuild_required and open_positions:
                raise ValueError("Close the instance's open position before changing execution configuration")
            if len(open_positions) > candidate_max:
                raise ValueError("max_open_positions cannot be below the instance's current open-position count")
            candidate_strategy_key = strategy_key or inst.strategy_key
            candidate_timeframe = timeframe or inst.timeframe
            # Validate the replacement before interrupting a healthy worker.
            # A broken package or incompatible decision timeframe rejects the
            # edit while the current strategy keeps running.
            try:
                probe_strategy = self.strategy_factory(candidate_strategy_key, inst.symbol)
            except Exception as exc:
                raise ValueError(
                    f"Strategy validation failed before restart: {type(exc).__name__}: {exc}") from exc
            required_timeframe = getattr(probe_strategy, "decision_timeframe", None)
            if required_timeframe and candidate_timeframe != required_timeframe:
                raise ValueError(
                    f"{strategy_label or inst.strategy_label} requires the "
                    f"{required_timeframe} decision timeframe")
            if had_runtime and rebuild_required:
                # update_configuration owns the manager lock while it performs
                # an atomic config swap. Detach the old worker callback before
                # joining it: otherwise its final lifecycle event waits for the
                # same manager lock while this thread waits for it to exit. That
                # lock inversion caused slow strategy switches and allowed a
                # late "stopped" event to overwrite the replacement worker.
                self._runtime[instance_id][0]._lifecycle_callback = None
                # halt_runtime, not stop: an edit is not a decision to stop
                # running. stop() clears desired_running and releases the
                # lease, so if the rebuild below failed -- a Binance blip
                # during the restart is enough -- the instance was left
                # un-desired, unsupervised and dark until somebody noticed.
                self.halt_runtime(instance_id,
                                  reason="worker replaced for a configuration change",
                                  state="starting")
            inst.capital_allocation = candidate_capital
            inst.risk_per_trade_pct = float(risk_per_trade_pct if risk_per_trade_pct is not None else inst.risk_per_trade_pct)
            inst.max_open_positions = candidate_max
            inst.sizing_mode = candidate_mode
            inst.fixed_position_size = candidate_fixed
            inst.fixed_quantity = candidate_fixed
            inst.profit_reinvestment = candidate_reinvest
            inst.maximum_risk_amount = candidate_max_risk
            inst.minimum_equity = candidate_floor
            inst.entry_mode = candidate_entry
            inst.fill_model = candidate_fill
            inst.exchange = candidate_exchange
            inst.instrument_type = candidate_instrument
            if capital_allocation is not None and not trade_history:
                inst.starting_equity = candidate_capital
                inst.current_realized_equity = candidate_capital
                inst.risk_basis = candidate_capital
            inst.strategy_key = strategy_key or inst.strategy_key
            inst.strategy_label = strategy_label or inst.strategy_label
            inst.strategy_version = strategy_version or inst.strategy_version
            inst.timeframe = timeframe or inst.timeframe
            inst.last_error = ""
            # Every accepted edit is a new revision. The running worker records
            # the revision it was built from, so status() can say plainly when
            # the dashboard is showing settings the worker has not adopted --
            # rather than letting an edit look applied while the worker keeps
            # trading the old configuration.
            inst.config_revision = int(inst.config_revision or 1) + 1
            self.store.save(inst)
            if rebuild_required and prior_state in _ACTIVE_INSTANCE_STATES:
                self.start(instance_id)
            elif rebuild_required and prior_state == "paused":
                self.start(instance_id)
                self.pause(instance_id)
            elif rebuild_required:
                # Neither restart branch applies: the instance was not active
                # before the edit, so it must come back as what it was, not as
                # the transient "starting" halt_runtime left behind. Persisting
                # "starting" with no worker is the state the four-axis contract
                # exists to make impossible.
                inst.state = prior_state
                if prior_state in ("error", "blocked", "degraded"):
                    # An edit does not resolve a reconciliation failure or a
                    # terminal fault, so the state must not come back stripped
                    # of the reason that explains it.
                    inst.last_error = prior_error
                self.store.save(inst)
            elif had_runtime and instance_id in self._runtime:
                # Risk and position-cap changes affect future entries only and
                # can be applied atomically without interrupting market data.
                #
                # The membership re-check is not defensive padding: a rebuild
                # for a stopped/error instance takes neither restart branch
                # above, and halt_runtime has already removed the runtime, so
                # indexing it here raised KeyError and left the instance
                # persisted as "starting" with no worker.
                pipeline = self._runtime[instance_id][2]
                pipeline.risk_per_trade_pct = inst.risk_per_trade_pct
                pipeline.max_open_positions = inst.max_open_positions
            self.ledger.log(level="info", stage="instance",
                            message=(f"Instance configuration updated: {inst.symbol} "
                                     f"{inst.strategy_label} {inst.strategy_version} {inst.timeframe}"),
                            symbol=inst.symbol, instance_id=inst.id)
            return inst

    def configure(self, *, max_active_slots: int | None = None,
                  max_global_risk_pct: float | None = None,
                  max_global_daily_loss_pct: float | None = None,
                  max_instance_risk_per_trade_pct: float | None = None,
                  paper_account_capital: float | None = None,
                  defaults: dict | None = None) -> dict:
        candidate_slots = self.max_slots if max_active_slots is None else int(max_active_slots)
        candidate_global_risk = self.max_global_risk_pct if max_global_risk_pct is None else float(max_global_risk_pct)
        candidate_daily_loss = self.max_global_daily_loss_pct if max_global_daily_loss_pct is None else float(max_global_daily_loss_pct)
        candidate_ceiling = (self.max_instance_risk_per_trade_pct if max_instance_risk_per_trade_pct is None
                             else float(max_instance_risk_per_trade_pct))
        candidate_capital = self.paper_account_capital if paper_account_capital is None else float(paper_account_capital)
        candidate_defaults = {**self.instance_defaults, **(defaults or {})}

        if not 1 <= candidate_slots <= MAX_ACTIVE_SLOTS_CEILING:
            raise ValueError(
                f"max_active_slots must be between 1 and {MAX_ACTIVE_SLOTS_CEILING}")
        running = sum(1 for key, runtime in self._runtime.items()
                      if runtime[0].running and self._instances[key].mode == "trading")
        if candidate_slots < running:
            raise ValueError("Stop or pause instances before reducing active slots below the running count")
        if not math.isfinite(candidate_global_risk) or not 0.001 <= candidate_global_risk <= 1:
            raise ValueError("max_global_risk_pct must be between 0.001 and 1")
        if not math.isfinite(candidate_daily_loss) or not 0.001 <= candidate_daily_loss <= 1:
            raise ValueError("max_global_daily_loss_pct must be between 0.001 and 1")
        if not math.isfinite(candidate_ceiling) or not 0.001 <= candidate_ceiling <= 0.05:
            raise ValueError("max_instance_risk_per_trade_pct must be between 0.001 and 0.05")
        if any(item.risk_per_trade_pct > candidate_ceiling for item in self._instances.values()):
            raise ValueError("Cannot lower the instance risk ceiling below an existing instance")
        allocated = sum(item.capital_allocation for item in self._instances.values() if item.mode == "trading")
        if not math.isfinite(candidate_capital) or candidate_capital <= 0:
            raise ValueError("paper_account_capital must be greater than zero")
        if candidate_capital < allocated:
            raise ValueError("paper_account_capital cannot be below existing allocated capital")
        if candidate_defaults["default_risk_per_trade_pct"] > candidate_ceiling:
            raise ValueError("default_risk_per_trade_pct cannot exceed max_instance_risk_per_trade_pct")

        self.store.save_platform_settings(max_active_slots=candidate_slots,
                                          max_global_risk_pct=candidate_global_risk,
                                          max_global_daily_loss_pct=candidate_daily_loss,
                                          max_instance_risk_per_trade_pct=candidate_ceiling,
                                          paper_account_capital=candidate_capital,
                                          defaults=candidate_defaults)
        self.max_slots = candidate_slots
        self.max_global_risk_pct = candidate_global_risk
        self.max_global_daily_loss_pct = candidate_daily_loss
        self.max_instance_risk_per_trade_pct = candidate_ceiling
        self.paper_account_capital = candidate_capital
        self.instance_defaults = candidate_defaults
        return self.platform_status()

    def platform_status(self, runtime_states: list[dict] | None = None, *,
                        open_positions: list[dict] | None = None,
                        instance_trades: list[dict] | None = None) -> dict:
        active = sum(1 for key, r in self._runtime.items()
                     if r[0].running and self._instances[key].mode == "trading")
        open_positions = (open_positions if open_positions is not None else
                          [p for p in self.ledger.get_positions("open")
                           if p.get("instance_id") in self._instances])
        used_risk = sum(abs(float(p.get("entry", 0)) - float(p.get("stop") or p.get("entry", 0)))
                        * float(p.get("size", 0)) for p in open_positions)
        allocated_capital = sum(i.capital_allocation for i in self._instances.values() if i.mode == "trading")
        capital = allocated_capital or 1.0
        today = datetime.now(timezone.utc).date().isoformat()
        instance_ids = set(self._instances)
        instance_trades = (instance_trades if instance_trades is not None else
                           [t for t in self.ledger.get_paper_trades()
                            if t.get("instance_id") in instance_ids])
        active_session_by_instance = {
            item.id: item.simulation_session_id for item in self._instances.values()
        }
        instance_trades = [
            trade for trade in instance_trades
            if trade.get("simulation_session_id") == active_session_by_instance.get(str(trade.get("instance_id")))
        ]
        today_closed = [t for t in instance_trades if str(t.get("closed_at") or "").startswith(today)]
        today_pnl = sum(float(t.get("pnl") or 0) for t in today_closed)
        # GET /instances already materializes these expensive, storage-backed
        # snapshots for its response. Reuse them instead of issuing every
        # Supabase query a second time during the same request.
        runtime_states = runtime_states if runtime_states is not None else [self.status(i) for i in self._instances]
        unrealized = sum(float((row.get("current_position") or {}).get("unrealized_pnl") or 0)
                         for row in runtime_states)
        total_equity = sum(float((row.get("execution") or {}).get("current_equity") or 0)
                           for row in runtime_states)
        available_capital = sum(float((row.get("execution") or {}).get("available_capital") or 0)
                                for row in runtime_states)
        account_available = max(0.0, self.paper_account_capital - allocated_capital)
        opened_or_closed_today = {str(t.get("id")) for t in instance_trades
                                  if str(t.get("opened_at") or "").startswith(today)
                                  or str(t.get("closed_at") or "").startswith(today)}
        risk_limit = capital * self.max_global_risk_pct
        daily_limit = capital * self.max_global_daily_loss_pct
        if today_pnl <= -daily_limit:
            risk_status, risk_message = "daily_loss_limit_reached", "Daily loss limit reached — new entries are paused"
        elif risk_limit > 0 and used_risk / risk_limit >= 0.9:
            risk_status, risk_message = "warning", "Risk capacity almost reached"
        else:
            risk_status, risk_message = "healthy", "Global risk within limits"
        active_lifecycle = {"starting", "bootstrapping", "warming", "syncing", "ready",
                            "running", "data_stale", "recovering", "rebooting", "degraded"}
        market_states = [str((row.get("market_data") or {}).get("market_data_status") or "")
                         for row in runtime_states if row.get("mode") == "trading"
                         and (row.get("state") in active_lifecycle or row.get("desired_running"))]
        # Historical/stopped error rows remain visible for diagnosis, but they
        # are not a current platform outage. Only a worker that is still marked
        # as desired-running may make the live platform status critical.
        worker_errors = sum(1 for row in runtime_states
                            if row.get("state") in ("error", "degraded")
                            and row.get("desired_running"))
        if not self.store.available or worker_errors or any(s in ("error", "disconnected") for s in market_states):
            global_status = "critical"
        elif risk_status != "healthy" or any(s in ("stale", "warming_up") for s in market_states):
            global_status = "warning"
        else:
            global_status = "healthy"
        return {"max_active_slots": self.max_slots, "active_slots": active,
                "max_global_risk_pct": self.max_global_risk_pct,
                "max_global_daily_loss_pct": self.max_global_daily_loss_pct,
                "max_instance_risk_per_trade_pct": self.max_instance_risk_per_trade_pct,
                "instance_defaults": dict(self.instance_defaults),
                "settings_metadata": {
                    "instance_defaults": {"scope": "platform", "source": "database",
                                          "editable": True, "restart_required": False,
                                          "applies_to": "new instances only"},
                    "risk_ceilings": {"scope": "platform", "source": "database",
                                      "editable": True, "restart_required": False},
                    "paper_account_capital": {"scope": "platform", "source": "database",
                                              "editable": True, "restart_required": False},
                },
                "max_global_risk_amount": round(allocated_capital * self.max_global_risk_pct, 2),
                "current_global_risk_amount": round(used_risk, 2),
                "total_open_positions": len(open_positions),
                "total_instances": len(self._instances),
                "instance_counts": {state: sum(1 for row in runtime_states if row.get("state") == state)
                                    for state in ("created", "starting", "bootstrapping", "warming", "syncing",
                                                  "ready", "running", "data_stale", "recovering", "rebooting",
                                                  "degraded", "paused", "stopped", "error")},
                "total_allocated_capital": round(allocated_capital, 2),
                "paper_account_capital": round(self.paper_account_capital, 2),
                "total_current_equity": round(total_equity, 2),
                # Allocation capacity is account-level.  Per-worker free cash
                # remains separately visible for execution diagnostics.
                "available_paper_capital": round(account_available, 2),
                "worker_available_capital": round(available_capital, 2),
                "today_pnl": round(today_pnl + unrealized, 2),
                "today_realized_pnl": round(today_pnl, 2),
                "today_unrealized_pnl": round(unrealized, 2),
                "today_trades": len(opened_or_closed_today),
                "global_risk_status": risk_status,
                "global_risk_message": risk_message,
                "market_data_status": ("not_available" if not market_states else
                                       "critical" if any(s in ("error", "disconnected") for s in market_states) else
                                       "warning" if any(s in ("stale", "warming_up") for s in market_states) else "healthy"),
                "global_status": global_status}

    def restore_desired_instances(self) -> list[str]:
        """Restore independently desired workers after an application restart.

        A browser session is deliberately irrelevant here: the persisted
        server-owned desired state decides whether an instance resumes.  Older
        deployments could persist more desired workers than the current
        platform slot limit.  Restore the earliest requested workers only and
        leave the remainder visibly paused for an operator to start after
        freeing a slot; never silently exceed the account-level limit.
        """
        from services.instance_reconciliation import BLOCKING, reconcile
        from services.instance_telemetry import event_payload, format_event
        restored: list[str] = []
        restored_trading = 0
        for inst in sorted(self._instances.values(), key=lambda item: item.created_at):
            if not inst.desired_running:
                continue
            if inst.mode == "trading" and restored_trading >= self.max_slots:
                inst.state = "paused"
                inst.desired_running = False
                inst.last_error = (
                    f"Not restored: maximum active trading slots reached ({self.max_slots}). "
                    "Stop another instance before starting this one."
                )
                inst.stopped_at = _now()
                self.store.save(inst)
                continue
            try:
                # Never trust the persisted state on its own. If the durable
                # records disagree with each other -- an open position with no
                # trade row, a quarantined intent, an instance another process
                # still owns -- restoring it as healthy would let a strategy
                # trade on top of a state nobody has verified.
                report = reconcile(self, inst.id, expect_worker=False)
                if report.blocked:
                    inst.state = "blocked"
                    inst.last_error = (
                        "RECONCILIATION_FAILED: " + "; ".join(
                            item.detail for item in report.findings
                            if item.severity == BLOCKING))[:500]
                    self.store.save(inst)
                    self.store.append_engine_log(
                        inst.id, level="error",
                        message=format_event(event_payload(
                            inst, "INSTANCE_ERROR", status="blocked",
                            detail="startup reconciliation failed",
                            findings=[item.public() for item in report.findings])))
                    continue
                # A paused instance is desired-running with its entry gate
                # closed. Restoring it without the gate would silently re-arm
                # a strategy the operator deliberately disarmed.
                self.start(inst.id, entry_gate_closed=inst.state == "paused")
                restored.append(inst.id)
                if inst.mode == "trading":
                    restored_trading += 1
            except WorkerLeaseError as exc:
                # Another process still owns this instance. Fail closed and
                # keep the intent: the supervisor retries once the lease
                # expires, which is the only safe way to recover from a crash
                # whose worker might still be alive somewhere. Not "blocked":
                # the supervisor never retries that state, so a lease the
                # previous container left behind kept the worker down until
                # someone pressed Start. The lease itself keeps it safe.
                inst.state = "error"
                inst.last_error = (f"WORKER_LEASE_HELD: {exc}; retried once "
                                   "the lease expires")[:500]
                inst.stopped_at = _now()
                self.store.save(inst)
                continue
            except Exception as exc:  # one broken instance cannot block others
                # The intent survives a failed restore. Binance being
                # unreachable at the exact moment the container boots is a
                # transient condition, and treating it as "the operator no
                # longer wants this instance" is what made a restart during an
                # outage permanent. InstanceSupervisor retries with backoff.
                inst.state = "error"
                inst.last_error = str(exc)[:500]
                inst.stopped_at = _now()
                self.store.save(inst)
        return restored

    def metrics(self, instance_id: str, *, trades_snapshot: list[dict] | None = None) -> dict:
        inst = self._instances[instance_id]; runtime = self._runtime.get(instance_id)
        if trades_snapshot is not None:
            trades = [t for t in trades_snapshot
                      if t.get("status") == "closed"
                      and t.get("simulation_session_id") == inst.simulation_session_id]
            if runtime is not None:
                runtime[1]._hist_cache = list(trades)
        else:
            trades = (runtime[1].history() if runtime else
                      [t for t in self.ledger.get_paper_trades(instance_id=instance_id)
                       if t.get("status") == "closed"
                       and t.get("simulation_session_id") == inst.simulation_session_id])
        out = summarize(trades, inst.capital_allocation)
        equity, peak = inst.capital_allocation, inst.capital_allocation
        for trade in sorted(trades, key=lambda item: item.get("closed_at") or ""):
            equity += float(trade.get("pnl") or 0)
            peak = max(peak, equity)
        current_drawdown_pct = round(((peak - equity) / peak * 100) if peak > 0 else 0.0, 2)
        rr = [float(t.get("rr") or 0) for t in trades if t.get("rr") is not None]
        durations = []
        for trade in trades:
            try:
                opened = datetime.fromisoformat(str(trade["opened_at"]).replace("Z", "+00:00"))
                closed = datetime.fromisoformat(str(trade["closed_at"]).replace("Z", "+00:00"))
                durations.append((closed - opened).total_seconds())
            except (KeyError, TypeError, ValueError):
                continue
        out.update({
            "average_rr": round(sum(rr) / len(rr), 3) if rr else 0.0,
            "fees": round(runtime[1].fees_paid(), 8) if runtime else round(sum(float(t.get("fees") or 0) for t in trades), 8),
            "slippage": 0.0,  # execution-quality captures this when a non-perfect fill model is configured
            "average_trade_duration_seconds": round(sum(durations) / len(durations), 1) if durations else 0.0,
            "consecutive_wins": self._current_streak(trades, positive=True),
            "consecutive_losses": self._current_streak(trades, positive=False),
        })
        health = StrategyHealthMonitor().evaluate(trades).to_dict()
        out.update({"instance_id": instance_id, "strategy_health": health,
                    "current_drawdown_pct": current_drawdown_pct})
        # The dashboard polls status frequently. Persist only when the isolated
        # trade record changed, not on every page refresh/poll interval.
        last = trades[-1] if trades else {}
        fingerprint = (len(trades), last.get("id"), last.get("pnl"), last.get("closed_at"))
        if self._metric_fingerprints.get(instance_id) != fingerprint:
            self.store.save_metrics(instance_id, out)
            self._metric_fingerprints[instance_id] = fingerprint
        return out

    @staticmethod
    def _current_streak(trades: list[dict], *, positive: bool) -> int:
        count = 0
        for trade in sorted(trades, key=lambda item: item.get("closed_at") or "", reverse=True):
            pnl = float(trade.get("pnl") or 0)
            if (pnl > 0) == positive and pnl != 0:
                count += 1
            else:
                break
        return count

    def status(self, instance_id: str, *, market_snapshot: dict | None = None,
               positions_snapshot: list[dict] | None = None,
               trades_snapshot: list[dict] | None = None) -> dict:
        inst = self._instances[instance_id]; runtime = self._runtime.get(instance_id)
        engine = runtime[0].status() if runtime else None
        market = (market_snapshot if market_snapshot is not None else self.store.market_state(instance_id)) if inst.mode == "trading" else {
            "market_data_mode": "replay", "market_data_status": "research_replay"}
        current_position = None
        if engine is not None:
            positions = (positions_snapshot if positions_snapshot is not None
                         else runtime[1].positions())
            position = positions[0] if positions else None
            if position:
                mark = engine.get("last_prices", {}).get(position["symbol"])
                unrealized = None
                if mark is not None:
                    direction = position.get("side")
                    unrealized = ((float(mark) - float(position["entry"])) * float(position["size"])
                                  if direction == "long"
                                  else (float(position["entry"]) - float(mark)) * float(position["size"]))
                current_position = {
                    "symbol": position["symbol"], "side": position.get("side"),
                    "size": position.get("size"), "entry": position.get("entry"),
                    "stop": position.get("stop"), "target": runtime[0]._targets.get(position["symbol"]),
                    "mark": mark, "unrealized_pnl": round(unrealized, 2) if unrealized is not None else None,
                }
                entry, stop = float(position.get("entry") or 0), position.get("stop")
                if mark is not None and stop is not None and entry != float(stop):
                    direction = 1 if position.get("side") == "long" else -1
                    current_position["current_r"] = round(direction * (float(mark) - entry) / abs(entry - float(stop)), 3)
                else:
                    current_position["current_r"] = None
                current_position["risk_amount"] = round(abs(entry - float(stop or entry)) * float(position.get("size") or 0), 2)
                current_position["opened_at"] = position.get("opened_at")
                current_position["duration_seconds"] = _age_seconds(position.get("opened_at"))
            # positions() is a storage-backed Supabase read in production.
            # Reuse the snapshot above instead of opening another HTTP request
            # for the same instance during one status calculation.
            engine = {**engine, "open_positions": len(positions),
                      "paper_balance": runtime[1].balance()}
            if inst.mode == "trading":
                market = {**market,
                          "market_data_mode": "paper_forward",
                          "market_data_status": engine.get("market_data_status", market["market_data_status"]),
                          "last_market_data_timestamp": engine.get("last_closed_candle") or market.get("last_market_data_timestamp"),
                          "last_processed_candle_timestamp": engine.get("last_processed_candle_timestamp") or market.get("last_processed_candle_timestamp"),
                          "data_source": engine.get("data_source") or market.get("data_source"),
                          "warmup_bars": engine.get("warmup_bars", market.get("warmup_bars", 0)),
                          "duplicate_candles": engine.get("duplicate_candles_ignored", market.get("duplicate_candles", 0)),
                          "missing_candles": engine.get("missing_candles", market.get("missing_candles", 0)),
                          "out_of_order_candles": engine.get("out_of_order_candles", market.get("out_of_order_candles", 0)),
                          "last_blocker": engine.get("last_blocker"),
                          "last_blocker_timestamp": engine.get("last_blocker_timestamp")}
        reboot = dict(self._reboots.get(instance_id, {})) or None
        if reboot and reboot.get("status") == _REBOOT_RUNNING:
            state = "rebooting"
        elif inst.state == "degraded":
            # A running replacement worker may still be intentionally entry-
            # gated after failed reconciliation. Never let its internal
            # lifecycle badge hide the operator-visible degraded state.
            state = "degraded"
        else:
            state = inst.state if engine is None else ("paused" if inst.state == "paused" else engine.get("lifecycle_state", inst.state))
        if inst.mode == "trading":
            market = _market_health(market, timeframe=inst.timeframe, worker_state=state)
        if engine and state in ("stopped", "error") and inst.state != state:
            # Persist terminal worker state so a stale UI can never claim a
            # dead engine is live.
            inst.state = state
            inst.last_error = engine.get("last_error") or engine.get("stop_reason") or ""
            if state == "stopped" and inst.mode != "trading":
                # A research replay that reaches the end of its data is
                # genuinely finished and must not restart on deploy. A forward
                # trading worker has no end of data, so a stopped one is a
                # fault for the supervisor to repair -- reading status must
                # never be what decides an instance is no longer wanted.
                inst.desired_running = False
            self.store.save(inst)
        metrics = self.metrics(instance_id, trades_snapshot=trades_snapshot)
        execution: dict = {
            "current_equity": metrics.get("balance"),
            "starting_equity": inst.starting_equity,
            "current_realized_equity": metrics.get("balance"),
            "mark_to_market_equity": metrics.get("balance"),
            "available_capital": None,
            "realized_pnl": metrics.get("realized_pnl"),
            "gross_realized_pnl": round(float(metrics.get("realized_pnl") or 0) + float(metrics.get("fees") or 0), 2),
            "fees_paid": metrics.get("fees", 0.0),
            "unrealized_pnl": None,
            "return_pct": round(float(metrics.get("realized_pnl") or 0) / max(inst.capital_allocation, 1.0) * 100, 3),
            "entry_mode": None,
            "fill_model": None,
            "position_sizing_mode": None,
            "fixed_position_size": None,
            "max_open_positions": None,
            "leverage": None,  # leverage is not modelled by the paper engine
            "pending_orders": None,
        }
        risk: dict = {"open_risk_amount": 0.0, "open_risk_pct": None}
        if runtime is not None:
            engine_object, paper, pipeline, _controls = runtime
            marks = (engine or {}).get("last_prices") or {}
            realized_equity = paper.current_realized_equity()
            unrealized_pnl = paper.unrealized_pnl(marks)
            fees_paid = paper.fees_paid()
            execution.update({
                "current_equity": round(paper.equity(marks), 2),
                "starting_equity": round(paper.starting_balance, 2),
                "current_realized_equity": round(realized_equity, 2),
                "mark_to_market_equity": round(realized_equity + unrealized_pnl, 2),
                "available_capital": round(paper.available_balance(), 2),
                "realized_pnl": round(paper.realized_pnl(), 2),
                "gross_realized_pnl": round(paper.gross_realized_pnl(), 2),
                "fees_paid": round(fees_paid, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "entry_mode": (engine or {}).get("entry_mode"),
                "fill_model": type(paper.fill_model).__name__,
                "position_sizing_mode": pipeline.position_sizing_mode,
                "fixed_quantity": pipeline.fixed_position_size if pipeline.position_sizing_mode == FIXED_QUANTITY else None,
                "profit_reinvestment": pipeline.profit_reinvestment,
                "max_open_positions": pipeline.max_open_positions,
                "pending_orders": (engine or {}).get("pending_orders"),
            })
            execution["return_pct"] = round((float(execution["current_equity"]) - inst.capital_allocation) /
                                               max(inst.capital_allocation, 1.0) * 100, 3)
            if current_position:
                open_risk = abs(float(current_position.get("entry") or 0) - float(current_position.get("stop") or current_position.get("entry") or 0)) * float(current_position.get("size") or 0)
                risk = {"open_risk_amount": round(open_risk, 2),
                        "open_risk_pct": round(open_risk / max(float(execution["current_equity"] or 0), 1.0) * 100, 3)}
        else:
            # A stopped worker has no authoritative in-memory mark.  Its
            # closed-trade balance is available. Valuation remains unknown
            # only when a persisted open position still exists; an empty book
            # has exactly zero unrealized P&L and all realized equity available.
            execution["current_equity"] = metrics.get("balance")
            execution["current_realized_equity"] = metrics.get("balance")
            execution["mark_to_market_equity"] = metrics.get("balance")
            persisted_open = (positions_snapshot if positions_snapshot is not None else
                              self.ledger.get_positions(
                                  "open", instance_id=instance_id,
                                  simulation_session_id=inst.simulation_session_id))
            if not persisted_open:
                execution["available_capital"] = metrics.get("balance")
                execution["unrealized_pnl"] = 0.0
            pending = market.get("pending_orders_json") if isinstance(market, dict) else None
            if isinstance(pending, dict) and "forward_paper_intents" in pending:
                execution["pending_orders"] = (
                    len(pending.get("forward_paper_intents") or {}) +
                    len(pending.get("strategy_limit_intents") or {}))
            else:
                execution["pending_orders"] = len(pending) if isinstance(pending, dict) else None
        accounting_changed = False
        observed_realized = execution.get("current_realized_equity")
        if observed_realized is not None and abs(inst.current_realized_equity - float(observed_realized)) > 0.0000001:
            inst.current_realized_equity = float(observed_realized)
            accounting_changed = True
        basis, next_risk = PositionSizingService.risk_budget(
            mode=inst.sizing_mode,
            starting_equity=inst.starting_equity,
            current_realized_equity=float(inst.current_realized_equity if execution.get("current_realized_equity") is None else execution["current_realized_equity"]),
            risk_per_trade_pct=inst.risk_per_trade_pct,
            profit_reinvestment=inst.profit_reinvestment,
            maximum_risk_amount=inst.maximum_risk_amount,
        )
        execution.update({
            "account_id": f"instance:{inst.id}:{inst.simulation_session_id}",
            "account_type": "INSTANCE",
            "risk_basis": round(basis, 2),
            "next_trade_risk_amount": round(next_risk, 2),
            "next_trade_quantity": inst.fixed_quantity if inst.sizing_mode == FIXED_QUANTITY else None,
            "next_trade_quantity_note": ("configured fixed quantity" if inst.sizing_mode == FIXED_QUANTITY
                                         else "calculated after the strategy supplies entry and stop"),
            "sizing_engine_version": inst.sizing_engine_version,
        })
        if abs(inst.risk_basis - basis) > 0.0000001:
            inst.risk_basis = basis
            accounting_changed = True
        if accounting_changed:
            self.store.save(inst)
        risk.update({
            "risk_basis": round(basis, 2),
            "next_trade_max_risk": round(next_risk, 2),
            "maximum_risk_amount": inst.maximum_risk_amount,
            "minimum_equity": inst.minimum_equity,
            "risk_halted": bool(inst.minimum_equity is not None and
                                float(execution.get("current_realized_equity") or 0) < inst.minimum_equity),
        })
        last_decision = None
        if self.decision_store is not None:
            try:
                rows = self.decision_store.list(limit=1, instance_id=instance_id)
                last_decision = rows[0] if rows else None
            except Exception:  # noqa: BLE001 -- observability must not stop a worker
                last_decision = None
        strategy_health = metrics.get("strategy_health") or {}
        health_status = str(strategy_health.get("status") or "").lower()
        market_status = str(market.get("market_data_status") or "").lower()
        last_blocker = ((engine or {}).get("last_blocker") or market.get("last_blocker"))
        worker_strategy = str((engine or {}).get("strategy") or "")
        worker_strategy_key = str((engine or {}).get("strategy_key") or "")
        strategy_matches = not worker_strategy_key or worker_strategy_key == inst.strategy_key
        controls_armed = bool(runtime and runtime[3].trading_allowed())
        if state in ("error", "degraded") or not strategy_matches:
            ui_status = "ERROR"
        elif (state in ("paused", "stopped", "created", "data_stale", "recovering", "rebooting")
              or market_status in ("stale", "disconnected", "error")
              or health_status == "unhealthy"):
            ui_status = "BLOCKED"
        elif state in ("starting", "bootstrapping", "warming", "syncing", "ready"):
            ui_status = "RUNNING_UNARMED"
        elif state == "running" and controls_armed:
            ui_status = "RUNNING_ARMED"
        else:
            ui_status = "RUNNING_UNARMED"
        configuration = {
            "symbol": inst.symbol, "strategy": inst.strategy_label,
            "strategy_key": inst.strategy_key, "strategy_version": inst.strategy_version,
            "timeframe": inst.timeframe, "capital_allocation": inst.capital_allocation,
            "max_open_positions": inst.max_open_positions,
            "risk_per_trade_pct": inst.risk_per_trade_pct,
            "sizing_mode": inst.sizing_mode, "fixed_position_size": inst.fixed_position_size,
            "fixed_quantity": inst.fixed_quantity,
            "profit_reinvestment": inst.profit_reinvestment,
            "maximum_risk_amount": inst.maximum_risk_amount,
            "minimum_equity": inst.minimum_equity,
            "starting_equity": inst.starting_equity,
            "current_realized_equity": inst.current_realized_equity,
            "risk_basis": inst.risk_basis,
            "sizing_engine_version": inst.sizing_engine_version,
            "entry_mode": inst.entry_mode, "fill_model": inst.fill_model,
            "execution_mode": inst.execution_mode, "market_data_mode": inst.market_data_mode,
        }
        effective_exchange = ("binance_usdm" if self.market_hub is not None and inst.mode == "trading" else
                              inst.exchange if inst.exchange != "inherit"
                              else (os.environ.get("HUB_EXCHANGE", "binance").strip() or "binance"))
        from services.mtf_policy import display_contract
        try:
            instance_mtf_policy = ((engine or {}).get("mtf_policy") or
                                   display_contract(inst.timeframe))
        except ValueError as exc:
            instance_mtf_policy = {
                "entry_timeframe": inst.timeframe, "label": str(exc), "evidence": {}}
        # Four independent axes plus the market facts behind them. A single
        # badge had to answer "is the worker alive", "is the feed fresh", "has
        # the strategy warmed up" and "are entries armed" with one word, so it
        # could not say which of the four had failed.
        from services import instance_status as status_contract
        from services.strategy_registry import entry as registry_entry
        registry_row = registry_entry(inst.strategy_key)
        requires_htf = bool(registry_row and any(
            item.startswith("native_") for item in registry_row.required_data))
        worker_alive = self.worker_alive(instance_id)
        contract = status_contract.build(
            instance=inst, engine=engine,
            market={**market, "_worker_state": state,
                    "exchange": effective_exchange,
                    "market_type": ("perpetual" if effective_exchange == "binance_usdm"
                                    else inst.instrument_type)},
            timeframe_seconds=_TIMEFRAME_SECONDS.get(inst.timeframe, 300),
            worker_alive=worker_alive,
            entries_armed=controls_armed,
            health_status=strategy_health.get("status"),
            htf_policy={**instance_mtf_policy, "requires_htf": requires_htf})
        return {**inst.to_dict(), **contract,
                "strategy_lifecycle": (registry_row.lifecycle if registry_row else "UNKNOWN"),
                "effective_exchange": effective_exchange,
                "effective_instrument_type": ("perpetual" if effective_exchange == "binance_usdm"
                                              else inst.instrument_type),
                "state": state, "engine": engine,
                "mtf_policy": instance_mtf_policy,
                "configuration": configuration, "execution": execution,
                "risk": risk, "market_data": market, "current_position": current_position,
                "simulation_session": {
                    "id": inst.simulation_session_id,
                    "number": inst.simulation_session_number,
                    "starting_balance": inst.starting_equity,
                    "status": "active",
                },
                "performance": {**metrics, "net_pnl": execution.get("realized_pnl"),
                                "return_pct": execution.get("return_pct")},
                "strategy_health": strategy_health,
                "last_decision": last_decision,
                "last_blocker": last_blocker,
                "last_blocker_timestamp": ((engine or {}).get("last_blocker_timestamp")
                                           or market.get("last_blocker_timestamp")),
                "ui_status": ui_status,
                "worker_counts": {
                    "signals": int((engine or {}).get("signals") or 0),
                    "accepted": int((engine or {}).get("accepted_signals") or 0),
                    "rejections": int((engine or {}).get("rejections") or 0),
                },
                "strategy_identity": {
                    "configured_id": inst.strategy_key,
                    "configured_label": inst.strategy_label,
                    "worker_id": worker_strategy_key or None,
                    "worker_label": worker_strategy or None,
                    "matches": strategy_matches,
                },
                "metrics": metrics,
                "event_guard": self._event_guard_state(inst.id),
                "quality_gate": self.quality_gate_state(inst.id),
                "reboot": reboot}

    def quality_gate_state(self, instance_id: str) -> dict | None:
        if self.quality_gate is None:
            return None
        row = self.quality_gate.row(instance_id)
        runtime = self._runtime.get(instance_id)
        min_score = runtime[0].min_quality_score if runtime else int(os.environ.get("HUB_MIN_SCORE", "60"))
        return {"enforced": not row.get("enabled"), "min_score": min_score,
                "updated_at": row.get("updated_at"), "by": row.get("by") or None}

    def _event_guard_state(self, instance_id: str) -> dict | None:
        if self.event_guard is None:
            return None
        try:
            return self.event_guard.state(instance_id)
        except Exception as exc:  # noqa: BLE001 -- status must render regardless
            return {"enabled": self.event_guard.enabled(instance_id), "error": str(exc)}

    def snapshot(self, owner_id: str | None = None) -> tuple[list[dict], list[dict], list[dict]]:
        """Materialize one dashboard snapshot without per-instance remote reads.

        Scoped to ``owner_id`` when supplied, so a listing can never include an
        instance the caller does not own.
        """
        instance_ids = {item.id for item in self.owned_instances(owner_id)}
        markets = self.store.market_states(instance_ids)
        positions = [row for row in self.ledger.get_positions("open")
                     if row.get("instance_id") in instance_ids]
        trades = [row for row in self.ledger.get_paper_trades()
                  if row.get("instance_id") in instance_ids]
        positions_by_instance = {key: [] for key in instance_ids}
        trades_by_instance = {key: [] for key in instance_ids}
        for row in positions:
            positions_by_instance.setdefault(str(row.get("instance_id")), []).append(row)
        for row in trades:
            trades_by_instance.setdefault(str(row.get("instance_id")), []).append(row)
        rows = []
        for instance_id in instance_ids:
            rows.append(self.status(
                instance_id,
                market_snapshot=markets.get(instance_id),
                positions_snapshot=positions_by_instance.get(instance_id, []),
                trades_snapshot=trades_by_instance.get(instance_id, []),
            ))
        return rows, positions, trades

    def list(self) -> list[dict]:
        return self.snapshot()[0]

    def leaderboard(self, sort: str = "realized_pnl") -> list[dict]:
        rows = [{**self.status(i), **self.metrics(i)} for i in self._instances]
        allowed = {"realized_pnl", "profit_factor", "win_rate", "max_drawdown_pct", "expectancy", "trades"}
        key = sort if sort in allowed else "realized_pnl"
        return sorted(rows, key=lambda r: float(r.get(key) or 0), reverse=key != "max_drawdown_pct")

    def best_measured_instance(self, *, minimum_sample: int = 5) -> TradingInstance:
        """Promote the best measured *trading* combination into one slot.

        This intentionally ranks only isolated closed-trade records. It never
        treats a raw win rate, a cross-strategy blend, or a backfilled legacy
        trade as evidence. The score rewards profitability and risk-adjusted
        returns while penalising drawdown and undersized samples.
        """
        candidates = []
        for inst in self._instances.values():
            if inst.mode != "trading" or inst.state == "running":
                continue
            metrics = self.metrics(inst.id)
            trades = int(metrics.get("trades") or 0)
            if trades < minimum_sample:
                continue
            pf = min(float(metrics.get("profit_factor") or 0), 5.0)
            expectancy_pct = float(metrics.get("expectancy") or 0) / max(inst.capital_allocation, 1.0) * 100
            score = (pf * 20 + float(metrics.get("sharpe_ratio") or 0) * 8
                     + expectancy_pct * 100 - float(metrics.get("max_drawdown_pct") or 0) * 1.5
                     + min(trades, 100) * 0.1)
            candidates.append((score, inst))
        if not candidates:
            raise ValueError(f"No trading instance has the required {minimum_sample} isolated closed trades")
        return max(candidates, key=lambda item: item[0])[1]

    def auto_select(self, *, minimum_sample: int = 5) -> TradingInstance:
        winner = self.best_measured_instance(minimum_sample=minimum_sample)
        self.start(winner.id)
        return winner
