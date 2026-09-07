"""Binance visual feed and isolated virtual account for the Price Action Lab."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from bot.types import Bar
from data.market_data_v2 import TF_MS, MarketDataService, normalize_symbol
from execution.paper_broker_v2 import PaperBrokerV2
from services.native_price_action import (
    RESEARCH_ID as PRICE_ACTION_RESEARCH_ID,
    STRATEGIES,
    STRATEGY_VERSION as PRICE_ACTION_STRATEGY_VERSION,
    NativePriceActionEngine,
    PriceActionConfig,
)
from data.sqlite_runtime import is_sqlite_busy, runtime_connection
from services.price_action_governance import PersistenceBlocked, PriceActionJournalStore
from services.price_action_stream import PriceActionPublicStream
from services.lab_lifecycle import (
    blockers as lifecycle_blockers,
    correlation_id as make_correlation_id,
    decision_idempotency_key,
    journal_performance,
    json_text,
    lifecycle_state,
    paper_performance,
)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candle(row: Bar) -> dict:
    return {"timestamp": row.timestamp.isoformat(), "open": row.open, "high": row.high,
            "low": row.low, "close": row.close, "volume": row.volume}


OPERATING_MODES = {"signals_only", "manual_approval", "automatic"}
OPEN_BROKER_ORDER_STATUSES = {"open", "partially_filled", "triggered"}
OPEN_STRATEGY_ORDER_STATUSES = {"ORDER_PENDING", "PARTIALLY_FILLED", "ENTERED"}


@dataclass(frozen=True)
class PaperExecutionConfig:
    operating_mode: Literal["signals_only", "manual_approval", "automatic"] = "signals_only"
    strategy_id: str = "PA1_SR_REJECTION"
    risk_pct: float = 0.5
    max_risk_pct: float = 1.0
    max_concurrent_risk_pct: float = 2.0
    target_r: float = 2.5

    def validated(self) -> "PaperExecutionConfig":
        if self.operating_mode not in OPERATING_MODES:
            raise ValueError("operating mode must be signals_only, manual_approval or automatic")
        if not 0 < self.risk_pct <= self.max_risk_pct <= 5:
            raise ValueError("risk must be positive and no greater than the configured maximum")
        if self.max_concurrent_risk_pct < self.risk_pct:
            raise ValueError("maximum concurrent risk cannot be below risk per trade")
        if abs(self.target_r - 2.5) > 1e-9:
            raise ValueError("Price Action V1 automatic paper target is fixed at 2.5R")
        if self.strategy_id not in STRATEGIES:
            raise ValueError("unknown Price Action strategy")
        return self


class PriceActionPaperAccount:
    """Persistent, Price-Action-only paper ledger with durable sessions."""

    def __init__(self, path: str | Path, *, starting_balance: float = 10_000.0):
        self.path = str(path)
        self.starting_balance = float(starting_balance)
        self.broker = PaperBrokerV2(
            self.path, starting_balance=self.starting_balance,
            account_type="PA_LAB", execution_engine="PA_LAB",
        )
        self._lock = threading.RLock()
        # Metadata and the broker share one SQLite file but use separate
        # connections. Autocommit prevents a metadata write lock from being
        # held while the broker performs its own atomic ledger transaction.
        self._db = runtime_connection(self.path, autocommit=True)
        self._persistence_blocker: dict | None = None
        with self._db:
            self._db.executescript("""
              CREATE TABLE IF NOT EXISTS pa_sessions(
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                starting_balance REAL NOT NULL, status TEXT NOT NULL,
                end_reason TEXT);
              CREATE TABLE IF NOT EXISTS pa_activity(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
                symbol TEXT, strategy_id TEXT, object_id TEXT, created_at TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}');
              CREATE TABLE IF NOT EXISTS pa_funding_events(
                session_id TEXT NOT NULL, symbol TEXT NOT NULL,
                funding_time TEXT NOT NULL, funding_rate REAL NOT NULL,
                mark_price REAL NOT NULL, applied INTEGER NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id,symbol,funding_time));
              CREATE TABLE IF NOT EXISTS pa_settings(
                id INTEGER PRIMARY KEY CHECK(id=1), leverage REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS pa_order_meta(
                order_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL, setup_id TEXT NOT NULL,
                zone_id TEXT NOT NULL, direction TEXT NOT NULL,
                strategy_id TEXT NOT NULL, config_json TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                valid_until_index INTEGER NOT NULL,
                UNIQUE(session_id,zone_id,direction));
              CREATE TABLE IF NOT EXISTS pa_candidates(
                proposal_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                source_proposal_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS pa_evaluations(
                correlation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, candle_time TEXT NOT NULL,
                symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL,
                missing_conditions_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(session_id,idempotency_key));
              CREATE TABLE IF NOT EXISTS pa_position_remediations(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, symbol TEXT NOT NULL,
                reason_code TEXT NOT NULL, initiated_by TEXT NOT NULL,
                original_position_json TEXT NOT NULL,
                market_evidence_json TEXT NOT NULL,
                close_result_json TEXT NOT NULL,
                journal_id TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS pa_runtime_controls(
                session_id TEXT PRIMARY KEY,
                readiness_recheck_required INTEGER NOT NULL DEFAULT 0,
                entry_pause_reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL);
            """)
            session_columns = {row[1] for row in self._db.execute("PRAGMA table_info(pa_sessions)")}
            for name, ddl in (
                ("mode", "TEXT NOT NULL DEFAULT 'LIVE_PAPER'"),
                ("symbol", "TEXT NOT NULL DEFAULT 'BTCUSDT'"),
                ("timeframe", "TEXT NOT NULL DEFAULT '5m'"),
                ("replay_cursor", "INTEGER NOT NULL DEFAULT 0"),
                ("operating_mode", "TEXT NOT NULL DEFAULT 'signals_only'"),
                ("strategy_config_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("execution_config_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("state_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("metrics_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("updated_at", "TEXT"),
            ):
                if name not in session_columns:
                    self._db.execute(f"ALTER TABLE pa_sessions ADD COLUMN {name} {ddl}")
            candidate_columns = {row[1] for row in self._db.execute("PRAGMA table_info(pa_candidates)")}
            if "source_proposal_id" not in candidate_columns:
                self._db.execute("ALTER TABLE pa_candidates ADD COLUMN source_proposal_id TEXT NOT NULL DEFAULT ''")
                self._db.execute("UPDATE pa_candidates SET source_proposal_id=proposal_id WHERE source_proposal_id='' ")
            funding_columns = {row[1] for row in self._db.execute("PRAGMA table_info(pa_funding_events)")}
            if "order_id" not in funding_columns:
                self._db.execute("ALTER TABLE pa_funding_events ADD COLUMN order_id TEXT")
            self._db.execute("INSERT OR IGNORE INTO pa_settings VALUES (1,1)")
            active_session = self._db.execute(
                "SELECT 1 FROM pa_sessions WHERE status='active' LIMIT 1").fetchone()
            broker_exposure = bool(self.broker.positions()) or any(
                row.get("status") in OPEN_BROKER_ORDER_STATUSES for row in self.broker.orders())
            if not active_session and not broker_exposure:
                self._db.execute(
                    "INSERT INTO pa_sessions(id,started_at,ended_at,starting_balance,status,end_reason,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, _iso(), None, self.starting_balance, "active", None, _iso()),
                )
            active_session = self._db.execute(
                "SELECT id FROM pa_sessions WHERE status='active' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if active_session:
                self._db.execute(
                    "INSERT OR IGNORE INTO pa_runtime_controls VALUES (?,?,?,?)",
                    (active_session["id"], 0, "", _iso()),
                )
            leverage = self._db.execute("SELECT leverage FROM pa_settings WHERE id=1").fetchone()[0]
            self.broker.leverage = float(leverage)
            self._snapshot_current()
        self.journal = PriceActionJournalStore(self.path)
        if self.session():
            self.reconcile_pending_orders()

    def session(self) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM pa_sessions WHERE status='active' ORDER BY started_at DESC LIMIT 1").fetchone()
            return dict(row) if row else {}

    def sessions(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM pa_sessions ORDER BY started_at DESC").fetchall()
        return [self._decode_session(dict(row)) for row in rows]

    def persistence_status(self) -> dict:
        with self._lock:
            return dict(self._persistence_blocker or {
                "state": "AVAILABLE", "retryable": False, "last_error": None,
            })

    def _mark_persistence_blocked(self, exc: PersistenceBlocked) -> dict:
        value = {
            "state": "PERSISTENCE_BLOCKED", "code": exc.code,
            "operation": exc.operation, "retryable": True,
            "last_error": str(exc), "observed_at": _iso(),
        }
        with self._lock:
            self._persistence_blocker = value
        return dict(value)

    def _clear_persistence_blocker(self) -> dict:
        with self._lock:
            self._persistence_blocker = None
        return self.persistence_status()

    @staticmethod
    def _decode_session(row: dict) -> dict:
        for key in ("strategy_config_json", "execution_config_json", "state_json", "metrics_json"):
            raw = row.pop(key, "{}") or "{}"
            row[key.removesuffix("_json")] = json.loads(raw)
        return row

    def _audit(self, kind: str, *, symbol: str = "", strategy_id: str = "",
               object_id: str = "", payload: dict | None = None,
               session_id: str | None = None) -> None:
        sid = session_id or self.session().get("id")
        if not sid:
            return
        self._db.execute(
            "INSERT INTO pa_activity(id,session_id,kind,symbol,strategy_id,object_id,created_at,payload) VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, kind, symbol, strategy_id, object_id, _iso(),
             json.dumps(payload or {}, sort_keys=True, default=str)),
        )

    def _runtime_control(self) -> dict:
        session_id = self.session().get("id")
        if not session_id:
            return {"readiness_recheck_required": True,
                    "entry_pause_reason": "no active Price Action session"}
        self._db.execute(
            "INSERT OR IGNORE INTO pa_runtime_controls VALUES (?,?,?,?)",
            (session_id, 0, "", _iso()),
        )
        return dict(self._db.execute(
            "SELECT * FROM pa_runtime_controls WHERE session_id=?", (session_id,)
        ).fetchone())

    def _set_readiness_recheck(self, required: bool, reason: str) -> dict:
        session_id = self.session().get("id")
        if not session_id:
            raise ValueError("no active Price Action session")
        self._db.execute(
            "INSERT INTO pa_runtime_controls(session_id,readiness_recheck_required,entry_pause_reason,updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
            "readiness_recheck_required=excluded.readiness_recheck_required,"
            "entry_pause_reason=excluded.entry_pause_reason,updated_at=excluded.updated_at",
            (session_id, int(required), reason, _iso()),
        )
        return self._runtime_control()

    def recheck_readiness(self, connection: dict) -> dict:
        """Re-run every execution gate after destructive PAPER cleanup."""
        session = self.session()
        reasons: list[str] = []
        if connection.get("state") != "SYNCHRONIZED" or not connection.get("reliable"):
            reasons.append("candles, bid/ask and mark are not synchronized")
        if session.get("operating_mode") != "automatic":
            reasons.append("saved Price Action mode is not Automatic Paper")
        account = self.broker.account()
        if float(account.get("balance") or 0) <= 0 or float(account.get("equity") or 0) <= 0:
            reasons.append("paper balance and equity must be positive")
        positions = self._positions_with_protection()
        if positions:
            reasons.append("an unresolved Price Action paper position remains open")
        pending = [row for row in self.broker.orders()
                   if row.get("status") in OPEN_BROKER_ORDER_STATUSES]
        if pending:
            reasons.append("pending Price Action paper orders must be reconciled")
        try:
            execution = self._execution_config()
            if not 0 < execution.risk_pct <= execution.max_risk_pct:
                reasons.append("saved Price Action risk configuration is invalid")
            strategy = json.loads(session.get("strategy_config_json") or "{}")
            if strategy.get("stop_model", "rejection_extreme") not in {
                    "rejection_extreme", "pattern", "structural_zone"}:
                reasons.append("saved Price Action stop-loss model is invalid")
            if execution.target_r <= 0:
                reasons.append("saved Price Action exit/target configuration is invalid")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            reasons.append(f"saved strategy or risk configuration is invalid: {exc}")
        ready = not reasons
        reason = ("all Price Action readiness gates passed after legacy remediation"
                  if ready else "; ".join(reasons))
        control = self._set_readiness_recheck(not ready, reason)
        self._audit(
            "legacy_remediation_readiness_recheck",
            symbol=session.get("symbol", ""),
            strategy_id=(json.loads(session.get("execution_config_json") or "{}")
                         .get("strategy_id", "")),
            payload={"ready": ready, "blockers": reasons,
                     "connection_state": connection.get("state"),
                     "operating_mode": session.get("operating_mode"),
                     "balance": account.get("balance"),
                     "positions": len(positions), "pending_orders": len(pending),
                     "execution_resumed_automatically": False,
                     "real_execution_allowed": False},
        )
        return {"ready": ready, "state": "READY" if ready else "BLOCKED",
                "blockers": reasons, "control": control,
                "real_execution_allowed": False}

    def remediate_legacy_position(
            self, symbol: str, *, mark_price: float, market_evidence: dict,
            initiated_by: str) -> dict:
        """Archive and close one explicitly confirmed legacy PAPER position."""
        symbol = normalize_symbol(symbol)
        session = self.session()
        if not session or normalize_symbol(session.get("symbol") or "") != symbol:
            raise ValueError("position symbol does not match the active Price Action session")
        position = next((row for row in self._positions_with_protection()
                         if row["symbol"] == symbol), None)
        if not position:
            raise ValueError("no open Price Action paper position")
        if position.get("protection_status") != "LEGACY_UNPROTECTED_REQUIRES_CLOSE_OR_PROTECTION":
            raise ValueError("only a LEGACY / UNPROTECTED position can use remediation close")
        if market_evidence.get("state") != "SYNCHRONIZED" or not market_evidence.get("reliable"):
            raise ValueError("legacy remediation requires synchronized candles, bid/ask and mark")
        remediation_id = f"pa-remediation-{uuid.uuid4().hex}"
        self._set_readiness_recheck(
            True, "legacy remediation completed; readiness gates must be re-run"
        )
        self._audit(
            "legacy_position_remediation_requested", symbol=symbol,
            object_id=remediation_id,
            payload={"reason": "LEGACY_POSITION_REMEDIATION",
                     "original_position": position,
                     "initiated_by": initiated_by,
                     "confirmation_required": True,
                     "execution_paused": True,
                     "real_execution_allowed": False},
        )
        close_result = self.broker.close_position_at_mark(
            symbol, mark_price, reason="LEGACY_POSITION_REMEDIATION"
        )
        journal_id = self.journal.record_legacy_remediation(
            session=self._decode_session(dict(session)),
            original_position=position, close_result=close_result,
            market_evidence=market_evidence,
            remediation_id=remediation_id, initiated_by=initiated_by,
        )
        now = _iso()
        # This connection is autocommit. Never retain a metadata transaction
        # while the broker advances its separate durable ledger connection.
        with self._lock:
            self._db.execute(
                "INSERT INTO pa_position_remediations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (remediation_id, session["id"], symbol,
                 "LEGACY_POSITION_REMEDIATION", initiated_by,
                 json_text(position), json_text(market_evidence),
                 json_text(close_result), journal_id, now),
            )
            self._db.execute(
                "UPDATE pa_order_meta SET status='COMPLETED',reason=?,updated_at=? "
                "WHERE session_id=? AND status='ENTERED'",
                ("closed by LEGACY_POSITION_REMEDIATION; historical protection was not inferred",
                 now, session["id"]),
            )
            self._audit(
                "legacy_position_remediated", symbol=symbol,
                object_id=remediation_id,
                payload={"reason": "LEGACY_POSITION_REMEDIATION",
                         "reference_mark_price": mark_price,
                         "fill": close_result.get("fill"),
                         "original_position": position,
                         "journal_id": journal_id,
                         "readiness_recheck_required": True,
                         "execution_resumed_automatically": False,
                         "real_execution_allowed": False},
            )
            self._snapshot_current()
        return {"remediation_id": remediation_id, "journal_id": journal_id,
                "close": close_result, "original_position_preserved": True,
                "readiness_recheck_required": True,
                "execution_resumed_automatically": False,
                "real_execution_allowed": False}

    def _snapshot_current(self, *, metrics: dict | None = None) -> None:
        current = self.session()
        if not current:
            return
        self._db.execute(
            "UPDATE pa_sessions SET state_json=?,metrics_json=COALESCE(?,metrics_json),updated_at=? WHERE id=?",
            (json.dumps(self.broker.export_state(), sort_keys=True),
             json.dumps(metrics, sort_keys=True, default=str) if metrics is not None else None,
             _iso(), current["id"]),
        )

    def configure(self, *, mode: str | None = None, symbol: str | None = None,
                  timeframe: str | None = None, replay_cursor: int | None = None,
                  strategy_config: dict | None = None,
                  execution_config: PaperExecutionConfig | None = None) -> dict:
        current = self.session()
        if not current:
            raise ValueError("no active Price Action session")
        execution = (execution_config or PaperExecutionConfig(**json.loads(
            current.get("execution_config_json") or "{}"))).validated()
        values = {
            "mode": mode or current.get("mode") or "LIVE_PAPER",
            "symbol": (symbol or current.get("symbol") or "BTCUSDT").upper(),
            "timeframe": timeframe or current.get("timeframe") or "5m",
            "replay_cursor": int(replay_cursor if replay_cursor is not None else current.get("replay_cursor") or 0),
            "operating_mode": execution.operating_mode,
            "strategy_config_json": json.dumps(strategy_config or json.loads(current.get("strategy_config_json") or "{}"), sort_keys=True),
            "execution_config_json": json.dumps(asdict(execution), sort_keys=True),
        }
        if values["timeframe"] not in TF_MS:
            raise ValueError("unsupported session timeframe")
        current_identity = (current.get("mode") or "LIVE_PAPER", current.get("symbol") or "BTCUSDT",
                            current.get("timeframe") or "5m")
        next_identity = (values["mode"], values["symbol"], values["timeframe"])
        if next_identity != current_identity:
            open_orders = [row for row in self.broker.orders()
                           if row.get("status") in OPEN_BROKER_ORDER_STATUSES]
            if self.broker.positions() or open_orders:
                raise ValueError(
                    "cannot change the Price Action session market, timeframe or mode while "
                    "paper positions or pending orders exist"
                )
        with self._lock, self._db:
            self._db.execute(
                "UPDATE pa_sessions SET mode=:mode,symbol=:symbol,timeframe=:timeframe,replay_cursor=:replay_cursor,operating_mode=:operating_mode,strategy_config_json=:strategy_config_json,execution_config_json=:execution_config_json,updated_at=:updated_at WHERE id=:id",
                {**values, "updated_at": _iso(), "id": current["id"]},
            )
            self._audit("session_configuration_changed", symbol=values["symbol"],
                        strategy_id=execution.strategy_id, payload=values)
        return self.state()

    def audit_pending_orders(self) -> dict:
        """Read-only ownership/status audit for strategy-generated paper orders."""
        current = self.session()
        session_id = current.get("id", "")
        with self._lock:
            metadata = [dict(row) for row in self._db.execute(
                "SELECT * FROM pa_order_meta WHERE session_id=? ORDER BY created_at", (session_id,))]
        broker_orders = {row["id"]: row for row in self.broker.orders()}
        meta_by_order = {row["order_id"]: row for row in metadata}
        duplicate_keys: dict[tuple[str, str], list[str]] = {}
        discrepancies: list[dict] = []
        for row in metadata:
            order = broker_orders.get(row["order_id"])
            meta_open = row["status"] in OPEN_STRATEGY_ORDER_STATUSES
            broker_open = bool(order and order.get("status") in OPEN_BROKER_ORDER_STATUSES)
            if broker_open or row["status"] in {"ORDER_PENDING", "PARTIALLY_FILLED"}:
                duplicate_keys.setdefault((row["zone_id"], row["direction"]), []).append(row["order_id"])
            if meta_open and order is None:
                discrepancies.append({"order_id": row["order_id"], "kind": "metadata_order_missing"})
            elif meta_open != broker_open and not (row["status"] == "ENTERED" and order and order.get("status") == "filled"):
                discrepancies.append({
                    "order_id": row["order_id"], "kind": "status_mismatch",
                    "metadata_status": row["status"],
                    "broker_status": order.get("status") if order else None,
                })
        duplicate_strategy_orders = [
            {"zone_id": key[0], "direction": key[1], "order_ids": ids}
            for key, ids in duplicate_keys.items() if len(ids) > 1
        ]
        pending_paper = [row for row in broker_orders.values()
                         if row.get("status") in OPEN_BROKER_ORDER_STATUSES]
        pending_strategy = [row for row in pending_paper if row["id"] in meta_by_order]
        pending_manual = [row for row in pending_paper if row["id"] not in meta_by_order]
        return {
            "session_id": session_id,
            "symbol": current.get("symbol"), "timeframe": current.get("timeframe"),
            "pending_paper_orders": len(pending_paper),
            "pending_strategy_orders": len(pending_strategy),
            "pending_manual_orders": len(pending_manual),
            "duplicate_strategy_orders": duplicate_strategy_orders,
            "discrepancies": discrepancies,
            "safe_to_reconcile": True,
            "manual_orders_are_never_auto_cancelled": False,
            "manual_order_policy": "unfilled same-symbol entries are cancelled when a position already exists",
        }

    def _position_owners(self, position: dict, metadata: list[dict]) -> list[tuple[dict, dict, dict]]:
        owners = []
        for meta in metadata:
            try:
                order = self.broker.order(meta["order_id"])
            except KeyError:
                continue
            expected_side = "long" if meta.get("direction") == "bullish" else "short"
            if order.get("symbol") != position.get("symbol") or expected_side != position.get("side"):
                continue
            if order.get("status") not in {"partially_filled", "filled"} or float(order.get("filled") or 0) <= 0:
                continue
            try:
                config = json.loads(meta["config_json"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            owners.append((meta, order, config))
        return owners

    def _composite_protection(self, position: dict,
                              owners: list[tuple[dict, dict, dict]]) -> tuple[float, float, float, float | None]:
        entry = float(position["entry_price"])
        candidates = []
        for _meta, _order, config in owners:
            try:
                stop = float(config["stop"])
                ratio = float(config.get("target_r", 2.5))
                tick = (config.get("contract_rules") or {}).get("tick_size")
            except (KeyError, TypeError, ValueError):
                continue
            if ratio <= 0 or (position["side"] == "long" and stop >= entry) or (
                    position["side"] == "short" and stop <= entry):
                continue
            candidates.append((stop, ratio, float(tick) if tick else None))
        if not candidates:
            raise ValueError("no immutable owner configuration has valid protection geometry")
        stop = (max(row[0] for row in candidates) if position["side"] == "long"
                else min(row[0] for row in candidates))
        ratio = min(row[1] for row in candidates)
        ticks = [row[2] for row in candidates if row[2]]
        tick = max(ticks) if ticks else None
        stop, target = self.broker._resolved_protection(position, {
            "stop_loss": stop, "take_profit": None, "target_r": ratio, "tick_size": tick,
        })
        return float(stop), float(target), ratio, tick

    def reconcile_pending_orders(self, visual_state: dict | None = None,
                                 candle: Bar | None = None,
                                 *, feed_reliable: bool = True) -> dict:
        """Safely reconcile order metadata and fail closed around existing exposure.

        A strategy-owned position may survive an application restart from an
        older snapshot that predates durable stop/target attachment. Missing
        protection is restored only from filled orders in the current session
        whose immutable configuration has valid geometry for the restored
        position. Multiple legacy owners are consolidated conservatively; no
        stop or target is fabricated when ownership cannot be proved.
        """
        before = self.audit_pending_orders()
        current = self.session()
        session_id = current.get("id", "")
        zones = {row["id"]: row for row in (visual_state or {}).get("zones", [])}
        state = visual_state or {}
        absolute_index = state.get("absolute_last_bar_index")
        # Native engine proposals store absolute signal/expiry indices.  The
        # candle payload is only a bounded visual window, so its length cannot
        # be used after the engine has processed more candles than that window.
        # Retain the fallback for imported legacy/synthetic states.
        index = (int(absolute_index) if absolute_index is not None
                 else len(state.get("candles", [])) - 1)
        positions = {row["symbol"]: row for row in self.broker.positions()}
        actions: list[dict] = []
        manual_orders_changed = 0
        with self._lock, self._db:
            metadata = [dict(row) for row in self._db.execute(
                "SELECT * FROM pa_order_meta WHERE session_id=? ORDER BY created_at", (session_id,))]
            for meta in metadata:
                try:
                    broker_order = self.broker.order(meta["order_id"])
                except KeyError:
                    broker_order = None
                meta_open = meta["status"] in OPEN_STRATEGY_ORDER_STATUSES
                broker_open = bool(broker_order and broker_order.get("status") in OPEN_BROKER_ORDER_STATUSES)
                terminal_status = None
                reason = None
                if meta_open and broker_order is None:
                    terminal_status, reason = "ORPHANED", "strategy metadata has no corresponding paper broker order"
                elif meta_open and broker_order and not broker_open:
                    broker_status = str(broker_order.get("status") or "unknown")
                    if broker_status == "filled":
                        terminal_status = "ENTERED" if broker_order.get("symbol") in positions else "COMPLETED"
                    else:
                        terminal_status = {
                            "cancelled": "CANCELLED", "rejected": "REJECTED", "expired": "EXPIRED",
                        }.get(broker_status, "CLOSED")
                    reason = f"metadata reconciled to broker status {broker_status}"
                elif not meta_open and broker_open:
                    try:
                        self.broker.cancel(meta["order_id"])
                        action = {"order_id": meta["order_id"], "action": "cancelled_stale_broker_order",
                                  "reason": f"metadata is already terminal ({meta['status']})"}
                        actions.append(action)
                        self._audit("paper_order_reconciled", object_id=meta["order_id"], payload=action)
                    except (KeyError, ValueError):
                        pass
                elif meta_open and broker_open and not feed_reliable:
                    terminal_status = "DATA_PAUSED"
                    reason = ("market data became unreliable before a provable activation; "
                              "pending paper order cancelled without inferring a fill")
                    try:
                        self.broker.cancel(meta["order_id"])
                    except (KeyError, ValueError):
                        pass
                elif meta_open and broker_open and visual_state is not None:
                    config = json.loads(meta["config_json"])
                    zone = zones.get(meta["zone_id"])
                    if index > int(meta["valid_until_index"]):
                        terminal_status, reason = "EXPIRED", "setup confirmation order expired"
                    elif zone is not None and not zone.get("active", True):
                        terminal_status, reason = "INVALIDATED", "structural zone invalidated before entry"
                    elif candle is not None:
                        is_long = meta["direction"] == "bullish"
                        entry_touched = candle.high >= config["entry"] if is_long else candle.low <= config["entry"]
                        stop_breached = candle.low <= config["stop"] if is_long else candle.high >= config["stop"]
                        if stop_breached and not entry_touched:
                            terminal_status, reason = "CANCELLED", "protective premise breached before entry"
                    if terminal_status:
                        try:
                            self.broker.cancel(meta["order_id"])
                        except (KeyError, ValueError):
                            pass
                if terminal_status and (terminal_status != meta["status"] or reason != meta["reason"]):
                    self._db.execute(
                        "UPDATE pa_order_meta SET status=?,reason=?,updated_at=? WHERE order_id=?",
                        (terminal_status, reason, _iso(), meta["order_id"]),
                    )
                    action = {"order_id": meta["order_id"], "action": "metadata_reconciled",
                              "from": meta["status"], "to": terminal_status, "reason": reason}
                    actions.append(action)
                    self._audit("paper_order_reconciled", object_id=meta["order_id"], payload=action)
            entered = [dict(row) for row in self._db.execute(
                "SELECT * FROM pa_order_meta WHERE session_id=? AND status IN ('ENTERED','PARTIALLY_FILLED') ORDER BY created_at",
                (session_id,))]
            for position in self.broker.positions():
                for pending in self.broker.orders():
                    if pending.get("symbol") != position.get("symbol") or pending.get("reduce_only") or \
                            pending.get("status") not in OPEN_BROKER_ORDER_STATUSES or float(pending.get("filled") or 0) > 0:
                        continue
                    self.broker.cancel(pending["id"])
                    self._db.execute(
                        "UPDATE pa_order_meta SET status='CANCELLED',reason=?,updated_at=? WHERE order_id=?",
                        ("cancelled during legacy exposure reconciliation; same-symbol stacking is unsupported",
                         _iso(), pending["id"]),
                    )
                    action = {"order_id": pending["id"], "action": "cancelled_pending_entry_during_position_repair",
                              "symbol": position["symbol"]}
                    if not any(row["order_id"] == pending["id"] for row in metadata):
                        manual_orders_changed += 1
                    actions.append(action)
                    self._audit("paper_order_reconciled", object_id=pending["id"], payload=action)
                owners = self._position_owners(position, entered)
                if not owners:
                    continue
                try:
                    stop, target, planned_rr, tick = self._composite_protection(position, owners)
                    entry = float(position["entry_price"])
                except (KeyError, TypeError, ValueError):
                    continue
                valid_geometry = (
                    stop < entry < target if position["side"] == "long"
                    else target < entry < stop
                )
                if not valid_geometry:
                    continue
                for meta, _order, cfg in owners:
                    try:
                        self.broker.set_order_protection(
                            meta["order_id"], stop_loss=float(cfg["stop"]),
                            take_profit=float(cfg["target"]),
                            target_r=float(cfg.get("target_r", 2.5)),
                            tick_size=(cfg.get("contract_rules") or {}).get("tick_size"),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                missing_stop = position.get("stop_loss") is None
                missing_target = position.get("take_profit") is None
                stop_changed = missing_stop or abs(float(position["stop_loss"]) - float(stop)) > 1e-9
                target_changed = missing_target or abs(float(position["take_profit"]) - float(target)) > 1e-9
                if not stop_changed and not target_changed:
                    continue
                repaired = self.broker.set_protection(
                    position["symbol"],
                    stop_loss=stop if stop_changed else None,
                    take_profit=target if target_changed else None,
                )
                action = {
                    "order_id": owners[-1][0]["order_id"],
                    "owner_order_ids": [row[0]["order_id"] for row in owners],
                    "ownership_resolution": "single" if len(owners) == 1 else "legacy_aggregate_conservative",
                    "symbol": position["symbol"],
                    "action": "reconciled_strategy_protection",
                    "stop_loss_restored": missing_stop,
                    "take_profit_restored": missing_target,
                    "stop_loss_adjusted": stop_changed and not missing_stop,
                    "take_profit_adjusted": target_changed and not missing_target,
                    "stop_loss": repaired.get("stop_loss"),
                    "take_profit": repaired.get("take_profit"),
                    "planned_rr": planned_rr,
                    "source": "immutable_pa_order_configuration_and_actual_fill",
                }
                actions.append(action)
                self._audit("paper_position_protection_repaired",
                            strategy_id=owners[-1][0]["strategy_id"], object_id=owners[-1][0]["order_id"],
                            payload=action)
            if actions:
                self._snapshot_current()
        return {"before": before, "after": self.audit_pending_orders(), "actions": actions,
                "records_deleted": 0, "manual_orders_changed": manual_orders_changed,
                "real_execution_allowed": False}

    def state(self, marks: dict[str, float] | None = None) -> dict:
        current = self.session()
        with self._lock:
            candidates = [dict(row) for row in self._db.execute(
                "SELECT * FROM pa_candidates WHERE session_id=? ORDER BY created_at DESC", (current.get("id", ""),))]
            activity = [dict(row) for row in self._db.execute(
                "SELECT * FROM pa_activity WHERE session_id=? ORDER BY created_at DESC LIMIT 500", (current.get("id", ""),))]
            metadata = [dict(row) for row in self._db.execute(
                "SELECT * FROM pa_order_meta WHERE session_id=? ORDER BY created_at DESC", (current.get("id", ""),))]
            funding = [dict(row) for row in self._db.execute(
                "SELECT * FROM pa_funding_events WHERE session_id=? ORDER BY funding_time DESC",
                (current.get("id", ""),))]
            evaluations = [{**dict(row),
                            "missing_conditions": json.loads(row["missing_conditions_json"]),
                            "payload": json.loads(row["payload_json"])}
                           for row in self._db.execute(
                "SELECT * FROM pa_evaluations WHERE session_id=? ORDER BY candle_time DESC LIMIT 500",
                (current.get("id", ""),))]
            remediations = [{**dict(row),
                             "original_position": json.loads(row["original_position_json"]),
                             "market_evidence": json.loads(row["market_evidence_json"]),
                             "close_result": json.loads(row["close_result_json"])}
                            for row in self._db.execute(
                "SELECT * FROM pa_position_remediations WHERE session_id=? ORDER BY created_at DESC",
                (current.get("id", ""),))]
        return {
            "account_scope": "PRICE_ACTION_VISUAL_LAB_ONLY",
            "currency": "USDT",
            "execution_mode": "PAPER",
            "real_funds": False,
            "live_execution_allowed": False,
            "session": self._decode_session(dict(current)) if current else {},
            "account": self.broker.account(marks),
            "positions": self._positions_with_protection(),
            "orders": self.broker.orders(),
            "trades": self.broker.fills(limit=500),
            "candidates": [{**row, "payload": json.loads(row["payload"])} for row in candidates],
            "order_metadata": [{**row, "config": json.loads(row["config_json"])} for row in metadata],
            "activity": [{**row, "payload": json.loads(row["payload"])} for row in activity],
            "funding_events": funding,
            "evaluations": evaluations,
            "position_remediations": remediations,
            "entry_control": self._runtime_control(),
            "persistence": self.persistence_status(),
            "order_audit": self.audit_pending_orders(),
        }

    def record_evaluation(self, visual_state: dict, candle: Bar,
                          feed_status: dict) -> dict:
        """Persist exactly one saved-strategy decision for a confirmed candle."""
        from services.mtf_policy import material_evidence

        current = self.session()
        config = self._execution_config()
        candle_time = candle.timestamp.isoformat()
        corr = make_correlation_id(
            lab="PA", session_id=current["id"], strategy_id=config.strategy_id,
            symbol=current["symbol"], timeframe=current["timeframe"],
            candle_time=candle_time,
        )
        idempotency = decision_idempotency_key(
            strategy_id=config.strategy_id, symbol=current["symbol"],
            timeframe=current["timeframe"], candle_time=candle_time,
        )
        existing = self._db.execute(
            "SELECT * FROM pa_evaluations WHERE session_id=? AND idempotency_key=?",
            (current["id"], idempotency),
        ).fetchone()
        if existing:
            return dict(existing)
        snapshot = visual_state.get("snapshot") or {}
        traces = [row for row in snapshot.get("strategy_traces", [])
                  if row.get("strategy_id") == config.strategy_id]
        proposals = [row for row in visual_state.get("proposals", [])
                     if row.get("strategy_id") == config.strategy_id and
                     (not row.get("signal_at") or str(row.get("signal_at")) == candle_time)]
        selected = min(traces, key=lambda row: len(row.get("missing_conditions", []))) if traces else {}
        missing = list(selected.get("missing_conditions") or [])
        state = "SIGNAL_FOUND" if proposals else "WATCHING"
        reason = ("native closed-candle proposal found" if proposals else
                  selected.get("next_required_event") or "no eligible setup on this closed candle")
        payload = {
            "closed_candle": _candle(candle), "trace": selected,
            "proposal_ids": [row.get("id") for row in proposals],
            "mtf_evidence": material_evidence(visual_state.get("mtf_evidence")),
            "feed_state": feed_status.get("state"),
            "feed_reason": feed_status.get("health_reason"),
            "saved_execution_config": asdict(config),
        }
        now = _iso()
        self._db.execute(
            "INSERT INTO pa_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (corr, current["id"], idempotency, candle_time, current["symbol"],
             current["timeframe"], config.strategy_id, PRICE_ACTION_STRATEGY_VERSION,
             state, str(reason), json_text(missing), json_text(payload), now, now),
        )
        self._audit("closed_candle_decision", symbol=current["symbol"],
                    strategy_id=config.strategy_id, object_id=corr,
                    payload={"correlation_id": corr, "idempotency_key": idempotency,
                             "state": state, "reason": reason,
                             "missing_conditions": missing, "candle_time": candle_time})
        return dict(self._db.execute(
            "SELECT * FROM pa_evaluations WHERE correlation_id=?", (corr,)).fetchone())

    def _advance_evaluation(self, correlation_id: str | None, state: str,
                            reason: str, **evidence) -> None:
        if not correlation_id:
            return
        row = self._db.execute(
            "SELECT payload_json FROM pa_evaluations WHERE correlation_id=?", (correlation_id,)).fetchone()
        if not row:
            return
        payload = json.loads(row["payload_json"] or "{}")
        payload.setdefault("lifecycle", []).append({"state": state, "reason": reason,
                                                     "at": _iso(), **evidence})
        self._db.execute(
            "UPDATE pa_evaluations SET state=?,reason=?,payload_json=?,updated_at=? WHERE correlation_id=?",
            (state, reason, json_text(payload), _iso(), correlation_id),
        )

    def _positions_with_protection(self) -> list[dict]:
        current = self.session()
        metadata = [dict(row) for row in self._db.execute(
            "SELECT * FROM pa_order_meta WHERE session_id=? AND status IN ('PARTIALLY_FILLED','ENTERED') ORDER BY created_at",
            (current.get("id", ""),),
        )]
        rows = []
        for position in self.broker.positions():
            owners = self._position_owners(position, metadata)
            planned_values = []
            for _meta, _order, config in owners:
                try:
                    planned_values.append(float(config.get("target_r", 2.5)))
                except (TypeError, ValueError):
                    continue
            broker_owners = [order for order in self.broker.orders()
                             if order.get("symbol") == position.get("symbol")
                             and not order.get("reduce_only")
                             and order.get("status") in {"partially_filled", "filled"}
                             and float(order.get("filled") or 0) > 0
                             and order.get("protection_target_r") is not None]
            if not planned_values:
                planned_values = [float(order["protection_target_r"]) for order in broker_owners]
            stop, target, entry = position.get("stop_loss"), position.get("take_profit"), position.get("entry_price")
            effective_rr = None
            if stop is not None and target is not None and entry is not None and abs(float(entry) - float(stop)) > 0:
                effective_rr = abs(float(target) - float(entry)) / abs(float(entry) - float(stop))
            rows.append({
                "symbol": position["symbol"], "side": position["side"], "size": position["size"],
                "entry_price": entry, "stop_loss": stop, "take_profit": target,
                "planned_rr": min(planned_values) if planned_values else None,
                "effective_rr": round(effective_rr, 6) if effective_rr is not None else None,
                "protection_status": ("PROTECTED" if stop is not None and target is not None
                                      else "LEGACY_UNPROTECTED_REQUIRES_CLOSE_OR_PROTECTION"),
                "protection_order_id": (owners[-1][0]["order_id"] if owners
                                        else broker_owners[-1]["id"] if broker_owners else None),
                "peak_price": position.get("peak_price"), "opened_at": position.get("opened_at"),
                "estimated_liquidation_price": position.get("estimated_liquidation_price"),
            })
        return rows

    def reset(self) -> dict:
        with self._lock:
            old = self.session()
            self._snapshot_current()
            self.broker.factory_reset(self.starting_balance)
            now, new_id = _iso(), uuid.uuid4().hex
            with self._db:
                if old:
                    self._db.execute("UPDATE pa_sessions SET ended_at=?,status='ended',end_reason='account_restart' WHERE id=?",
                                     (now, old["id"]))
                self._db.execute(
                    "INSERT INTO pa_sessions(id,started_at,ended_at,starting_balance,status,end_reason,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (new_id, now, None, self.starting_balance, "active", None, now),
                )
                if old:
                    self._db.execute(
                        "UPDATE pa_sessions SET mode=?,symbol=?,timeframe=?,operating_mode=?,strategy_config_json=?,execution_config_json=?,updated_at=? WHERE id=?",
                        (old.get("mode", "LIVE_PAPER"), old.get("symbol", "BTCUSDT"), old.get("timeframe", "5m"),
                         old.get("operating_mode", "signals_only"), old.get("strategy_config_json", "{}"),
                         old.get("execution_config_json", "{}"), now, new_id),
                    )
                self._audit("session_reset", object_id=new_id,
                            payload={"previous_session_id": old.get("id") if old else None,
                                     "new_session_id": new_id, "confirmation_required": True})
                self._snapshot_current()
            return self.state()

    def factory_reset(self) -> dict:
        """Erase all PA operational history; used only by the global Factory Reset."""
        with self._lock:
            self.broker.factory_reset(self.starting_balance)
            self.journal.factory_reset()
            now, new_id = _iso(), uuid.uuid4().hex
            for table in ("pa_evaluations", "pa_order_meta", "pa_candidates", "pa_funding_events", "pa_activity", "pa_sessions"):
                self._db.execute(f"DELETE FROM {table}")
            self._db.execute(
                "INSERT INTO pa_sessions(id,started_at,starting_balance,status,updated_at) VALUES (?,?,?,?,?)",
                (new_id, now, self.starting_balance, "active", now),
            )
            self._snapshot_current()
        return self.state()

    def start(self, *, mode: str = "LIVE_PAPER", symbol: str = "BTCUSDT",
              timeframe: str = "5m", starting_balance: float | None = None,
              execution_config: PaperExecutionConfig | None = None,
              strategy_config: dict | None = None) -> dict:
        amount = float(starting_balance or self.starting_balance)
        if amount <= 0:
            raise ValueError("starting balance must be positive")
        config = (execution_config or PaperExecutionConfig()).validated()
        with self._lock, self._db:
            prior = self.session()
            if prior:
                self._snapshot_current()
                self._db.execute("UPDATE pa_sessions SET status='ended',ended_at=?,end_reason='new_session' WHERE id=?", (_iso(), prior["id"]))
            self.broker.factory_reset(amount)
            sid, now = uuid.uuid4().hex, _iso()
            self._db.execute("INSERT INTO pa_sessions(id,started_at,starting_balance,status,mode,symbol,timeframe,operating_mode,strategy_config_json,execution_config_json,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (sid, now, amount, "active", mode, symbol.upper(), timeframe,
                              config.operating_mode, json.dumps(strategy_config or {}, sort_keys=True),
                              json.dumps(asdict(config), sort_keys=True), now))
            self._audit("session_started", symbol=symbol, strategy_id=config.strategy_id,
                        payload={"mode": mode, "timeframe": timeframe, "starting_balance": amount})
            self._snapshot_current()
        return self.state()

    def end(self, reason: str = "user_end") -> dict:
        with self._lock, self._db:
            current = self.session()
            if not current:
                raise ValueError("no active session")
            self._snapshot_current()
            self._audit("session_ended", payload={"reason": reason})
            self._db.execute("UPDATE pa_sessions SET status='ended',ended_at=?,end_reason=?,updated_at=? WHERE id=?",
                             (_iso(), reason, _iso(), current["id"]))
        return self._decode_session(dict(self._db.execute("SELECT * FROM pa_sessions WHERE id=?", (current["id"],)).fetchone()))

    def resume(self, session_id: str) -> dict:
        with self._lock, self._db:
            target = self._db.execute("SELECT * FROM pa_sessions WHERE id=?", (session_id,)).fetchone()
            if not target:
                raise KeyError(session_id)
            current = self.session()
            if current and current["id"] != session_id:
                self._snapshot_current()
                self._db.execute("UPDATE pa_sessions SET status='paused',updated_at=? WHERE id=?", (_iso(), current["id"]))
            snapshot = json.loads(target["state_json"] or "{}")
            if not snapshot:
                raise ValueError("session has no resumable broker snapshot")
            self.broker.restore_state(snapshot)
            self.broker.leverage = float(self._db.execute("SELECT leverage FROM pa_settings WHERE id=1").fetchone()[0])
            self._db.execute("UPDATE pa_sessions SET status='active',ended_at=NULL,end_reason=NULL,updated_at=? WHERE id=?", (_iso(), session_id))
            self._audit("session_resumed", session_id=session_id, payload={"restored": True})
        self.reconcile_pending_orders()
        return self.state()

    def duplicate(self, session_id: str) -> dict:
        row = self._db.execute("SELECT * FROM pa_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise KeyError(session_id)
        config = PaperExecutionConfig(**json.loads(row["execution_config_json"] or "{}"))
        return self.start(mode=row["mode"], symbol=row["symbol"], timeframe=row["timeframe"],
                          starting_balance=row["starting_balance"], execution_config=config,
                          strategy_config=json.loads(row["strategy_config_json"] or "{}"))

    def export_session(self, session_id: str | None = None) -> dict:
        current = self.session()
        sid = session_id or current.get("id")
        row = self._db.execute("SELECT * FROM pa_sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise KeyError(sid)
        if current and current.get("id") == sid:
            with self._lock, self._db:
                self._snapshot_current()
            row = self._db.execute("SELECT * FROM pa_sessions WHERE id=?", (sid,)).fetchone()
        activity = [dict(item) for item in self._db.execute("SELECT * FROM pa_activity WHERE session_id=? ORDER BY created_at", (sid,))]
        funding = [dict(item) for item in self._db.execute("SELECT * FROM pa_funding_events WHERE session_id=? ORDER BY funding_time", (sid,))]
        return {"format_version": "2.0", "session": self._decode_session(dict(row)),
                "activity": [{**item, "payload": json.loads(item["payload"])} for item in activity],
                "funding": funding, "real_execution_allowed": False}

    def record_external_event(self, event: dict) -> None:
        with self._lock, self._db:
            self._audit(event.get("kind", "runtime_event"), symbol=event.get("symbol", ""),
                        object_id=event.get("object_id", ""), payload=event)

    def set_replay_cursor(self, cursor: int, metrics: dict | None = None) -> None:
        current = self.session()
        if not current:
            raise ValueError("no active session")
        with self._lock, self._db:
            self._db.execute("UPDATE pa_sessions SET replay_cursor=?,metrics_json=?,updated_at=? WHERE id=?",
                             (int(cursor), json.dumps(metrics or {}, sort_keys=True, default=str),
                              _iso(), current["id"]))
            self._snapshot_current(metrics=metrics)

    def set_leverage(self, leverage: float) -> dict:
        value = float(leverage)
        if value < 1 or value > 20:
            raise ValueError("Price Action paper leverage must be between 1x and 20x")
        with self._lock, self._db:
            self._db.execute("UPDATE pa_settings SET leverage=? WHERE id=1", (value,))
            self.broker.leverage = value
        return self.state()

    @staticmethod
    def _round_step(value: float, step: float, *, upward: bool = False) -> float:
        if step <= 0:
            return float(value)
        units = math.ceil(value / step - 1e-12) if upward else math.floor(value / step + 1e-12)
        return round(units * step, 12)

    def _execution_config(self) -> PaperExecutionConfig:
        current = self.session()
        return PaperExecutionConfig(**json.loads(current.get("execution_config_json") or "{}")) .validated()

    @staticmethod
    def _assert_native_state_identity(visual_state: dict, current: dict) -> None:
        """Accept decisions only from the active native Price Action core."""
        if visual_state.get("research_id") != PRICE_ACTION_RESEARCH_ID or \
                visual_state.get("strategy_version") != PRICE_ACTION_STRATEGY_VERSION:
            raise ValueError("Price Action decision state is not attested to the active native core version")
        state_identity = (
            normalize_symbol(visual_state.get("symbol") or ""),
            str(visual_state.get("timeframe") or ""),
        )
        session_identity = (
            normalize_symbol(current.get("symbol") or ""),
            str(current.get("timeframe") or ""),
        )
        if state_identity != session_identity:
            raise ValueError(
                "Price Action decision identity does not match the active paper session"
            )

    @staticmethod
    def _proposal_attestation_error(proposal: dict, setup: dict,
                                    config: PaperExecutionConfig) -> str | None:
        if not setup or setup.get("id") != proposal.get("setup_id"):
            return "native proposal has no matching setup"
        if setup.get("phase") != "ORDER_PENDING":
            return "native setup is not at the closed-bar ORDER_PENDING boundary"
        if setup.get("strategy_id") != proposal.get("strategy_id") or \
                setup.get("direction") != proposal.get("direction"):
            return "native setup and proposal ownership do not match"
        try:
            entry = float(proposal["entry"])
            stop = float(proposal["stop"])
            target = float(proposal["target"])
            direction = proposal["direction"]
            risk = abs(entry - stop)
            ratio = abs(target - entry) / risk
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return "native proposal has incomplete risk geometry"
        valid = (
            stop < entry < target if direction == "bullish"
            else target < entry < stop if direction == "bearish"
            else False
        )
        if not valid or abs(ratio - config.target_r) > 1e-6:
            return "native proposal does not preserve the configured directional R:R"
        return None

    def _candidate(self, proposal: dict, setup: dict, rules: dict, status: str,
                   reason: str, config: PaperExecutionConfig, *,
                   correlation_id: str | None = None,
                   idempotency_key: str | None = None) -> dict:
        current, now = self.session(), _iso()
        payload = {"proposal": proposal, "setup": setup, "rules": rules,
                   "execution_config": asdict(config), "reason": reason,
                   "correlation_id": correlation_id, "idempotency_key": idempotency_key}
        candidate_id = f"{current['id']}:{proposal['id']}"
        self._db.execute(
            "INSERT OR IGNORE INTO pa_candidates(proposal_id,session_id,source_proposal_id,status,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (candidate_id, current["id"], proposal["id"], status,
             json.dumps(payload, sort_keys=True, default=str), now, now),
        )
        self._audit("strategy_candidate", symbol=current.get("symbol", ""),
                    strategy_id=proposal["strategy_id"], object_id=proposal["id"],
                    payload={"status": status, "reason": reason})
        return payload

    def _place_proposal(self, proposal: dict, setup: dict, rules: dict,
                        config: PaperExecutionConfig, *, source: str,
                        correlation_id: str | None = None,
                        idempotency_key: str | None = None) -> dict:
        current = self.session()
        control = self._runtime_control()
        if control.get("readiness_recheck_required"):
            reason = control.get("entry_pause_reason") or (
                "Price Action readiness recheck is required before a new paper entry"
            )
            self._candidate(proposal, setup, rules, "REJECTED", reason, config,
                            correlation_id=correlation_id,
                            idempotency_key=idempotency_key)
            self._advance_evaluation(correlation_id, "RISK_REJECTED", reason)
            return {"accepted": False, "reason": reason}
        zone_id = str(setup.get("zone_id") or "NO_ZONE")
        duplicate = self._db.execute(
            "SELECT order_id,status FROM pa_order_meta WHERE session_id=? AND zone_id=? AND direction=?",
            (current["id"], zone_id, proposal["direction"]),
        ).fetchone()
        if duplicate:
            reason = "duplicate trade blocked for the same session, zone and direction"
            self._candidate(proposal, setup, rules, "REJECTED", reason, config,
                            correlation_id=correlation_id, idempotency_key=idempotency_key)
            self._advance_evaluation(correlation_id, "RISK_REJECTED", reason)
            return {"accepted": False, "reason": reason, "duplicate_order_id": duplicate["order_id"]}
        open_entries = [row for row in self.broker.orders()
                        if row.get("status") in OPEN_BROKER_ORDER_STATUSES and not row.get("reduce_only")]
        if self.broker.positions() or open_entries:
            reason = ("another Price Action entry or protected position is already active; "
                      "same-symbol stacking is blocked so stop, target and R:R ownership cannot be overwritten")
            self._candidate(proposal, setup, rules, "REJECTED", reason, config,
                            correlation_id=correlation_id, idempotency_key=idempotency_key)
            self._advance_evaluation(correlation_id, "RISK_REJECTED", reason)
            return {"accepted": False, "reason": reason}
        account = self.broker.account()
        risk_pct = min(config.risk_pct, config.max_risk_pct)
        risk_amount = account["equity"] * risk_pct / 100
        open_risk = 0.0
        for row in self._db.execute(
                "SELECT config_json FROM pa_order_meta WHERE session_id=? AND status IN ('ORDER_PENDING','PARTIALLY_FILLED','ENTERED')",
                (current["id"],)):
            open_risk += float(json.loads(row["config_json"]).get("risk_amount") or 0)
        if open_risk + risk_amount > account["equity"] * config.max_concurrent_risk_pct / 100 + 1e-9:
            reason = "maximum concurrent paper risk would be exceeded"
            self._candidate(proposal, setup, rules, "REJECTED", reason, config,
                            correlation_id=correlation_id, idempotency_key=idempotency_key)
            self._advance_evaluation(correlation_id, "RISK_REJECTED", reason)
            return {"accepted": False, "reason": reason}
        risk_distance = abs(float(proposal["entry"]) - float(proposal["stop"]))
        if risk_distance <= 0:
            reason = "entry-to-stop distance is not positive"
            self._candidate(proposal, setup, rules, "REJECTED", reason, config,
                            correlation_id=correlation_id, idempotency_key=idempotency_key)
            self._advance_evaluation(correlation_id, "RISK_REJECTED", reason)
            return {"accepted": False, "reason": reason}
        tick, step = float(rules.get("tick_size") or 0), float(rules.get("quantity_step") or 0)
        is_long = proposal["direction"] == "bullish"
        entry = self._round_step(float(proposal["entry"]), tick, upward=is_long)
        stop = self._round_step(float(proposal["stop"]), tick, upward=not is_long)
        actual_risk = abs(entry - stop)
        if actual_risk <= 0 or (is_long and stop >= entry) or (not is_long and stop <= entry):
            reason = "rounded entry and protective stop do not form valid directional risk"
            self._candidate(proposal, setup, rules, "REJECTED", reason, config,
                            correlation_id=correlation_id, idempotency_key=idempotency_key)
            self._advance_evaluation(correlation_id, "RISK_REJECTED", reason)
            return {"accepted": False, "reason": reason}
        target = self._round_step(entry + actual_risk * config.target_r * (1 if is_long else -1), tick,
                                  upward=is_long)
        quantity = self._round_step(risk_amount / actual_risk, step)
        margin_cap = account["free_margin"] * self.broker.leverage / max(entry, 1e-12)
        quantity = min(quantity, self._round_step(margin_cap, step))
        minimum = float(rules.get("min_quantity") or 0)
        notional = quantity * entry
        rejection = None
        if quantity <= 0 or quantity < minimum:
            rejection = f"risk-sized quantity {quantity} is below minimum {minimum}"
        elif rules.get("max_quantity") and quantity > float(rules["max_quantity"]):
            rejection = "risk-sized quantity exceeds contract maximum"
        elif notional < float(rules.get("min_notional") or 0):
            rejection = f"risk-sized notional {notional:.8f} is below contract minimum"
        if rejection:
            self._candidate(proposal, setup, rules, "REJECTED", rejection, config,
                            correlation_id=correlation_id, idempotency_key=idempotency_key)
            self._advance_evaluation(correlation_id, "RISK_REJECTED", rejection)
            return {"accepted": False, "reason": rejection}
        order_config = {
            "research_id": "PRICE_ACTION_NATIVE_V1_RESEARCH", "source": source,
            "session_id": current["id"], "proposal": proposal, "setup": setup,
            "strategy_configuration": json.loads(current.get("strategy_config_json") or "{}"),
            "execution": asdict(config), "contract_rules": rules,
            "risk_amount": risk_amount, "risk_pct": risk_pct, "quantity": quantity,
            "entry": entry, "stop": stop, "target": target,
            "target_r": config.target_r, "planned_rr": config.target_r,
            "leverage": self.broker.leverage, "execution_mode": "PAPER",
            "correlation_id": correlation_id, "idempotency_key": idempotency_key,
            "live_execution_allowed": False,
        }
        order = self.broker.submit(symbol=current.get("symbol") or proposal.get("symbol") or "BTCUSDT",
                                   side="buy" if is_long else "sell", order_type="stop",
                                   quantity=quantity, stop_price=entry, market_open=True,
                                   protection_stop_loss=stop, protection_take_profit=target,
                                   protection_target_r=config.target_r,
                                   protection_tick_size=tick or None,
                                   signal_timestamp=str(proposal.get("_decision_timestamp") or proposal.get("created_at") or _iso()),
                                   decision_timestamp=str(proposal.get("_decision_timestamp") or proposal.get("created_at") or _iso()),
                                   signal_price=float(proposal.get("entry") or entry),
                                   requested_price=entry,
                                   strategy=proposal["strategy_id"],
                                   strategy_version=PRICE_ACTION_STRATEGY_VERSION,
                                   timeframe=current.get("timeframe") or "",
                                   market_data_source="Binance USD-M public WebSocket",
                                   candle_id=str(idempotency_key or ""))
        now = _iso()
        self._db.execute(
            "INSERT INTO pa_order_meta(order_id,session_id,proposal_id,setup_id,zone_id,direction,strategy_id,config_json,status,reason,created_at,updated_at,valid_until_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order["id"], current["id"], proposal["id"], proposal["setup_id"], zone_id,
             proposal["direction"], proposal["strategy_id"], json.dumps(order_config, sort_keys=True, default=str),
             "ORDER_PENDING", "eligible setup accepted for paper execution", now, now,
             int(proposal["valid_until_index"])),
        )
        self._db.execute("UPDATE pa_candidates SET status='ORDER_PENDING',updated_at=? WHERE session_id=? AND source_proposal_id=?",
                         (now, current["id"], proposal["id"]))
        self._audit("paper_order_accepted", symbol=current.get("symbol", ""),
                    strategy_id=proposal["strategy_id"], object_id=order["id"], payload=order_config)
        self._advance_evaluation(correlation_id, "ORDER_SUBMITTED",
                                 "protected paper order submitted", order_id=order["id"])
        self._snapshot_current()
        return {"accepted": True, "order": order, "configuration": order_config}

    def synchronize_strategy(self, visual_state: dict, *, contract_rules: dict,
                             candle: Bar, feed_reliable: bool = True,
                             feed_status: dict | None = None,
                             execution_quote: dict | None = None,
                             allow_candle_fills: bool = True) -> dict:
        """Advance broker state and consume newly eligible proposals exactly once."""
        config = self._execution_config()
        current = self.session()
        if not current:
            raise ValueError("no active Price Action session")
        self._assert_native_state_identity(visual_state, current)
        observed_health = feed_status or {
            "state": "SYNCHRONIZED" if feed_reliable else "DATA_PAUSED",
            "health_reason": "caller supplied only the reliability boundary",
            "reliable": feed_reliable,
        }
        evaluation = self.record_evaluation(visual_state, candle, observed_health)
        correlation = evaluation["correlation_id"]
        idempotency = evaluation["idempotency_key"]
        evaluation_payload = json.loads(evaluation.get("payload_json") or "{}")
        eligible_proposal_ids = set(evaluation_payload.get("proposal_ids") or [])
        # The visual state intentionally contains historical proposals for
        # research.  Execution authority belongs only to proposals attested by
        # this closed-candle evaluation; replayed bootstrap history must never
        # be converted into a new paper order.
        proposals = {
            row["id"]: row for row in visual_state.get("proposals", [])
            if row.get("id") in eligible_proposal_ids
        }
        setups = {row["id"]: row for row in visual_state.get("setups", [])}
        reconciliation = self.reconcile_pending_orders(
            visual_state, candle, feed_reliable=feed_reliable)
        # The account connection is autocommit. Broker processing and order
        # submission use a separate connection, so no metadata transaction may
        # span either operation.
        with self._lock:
            open_meta = [dict(row) for row in self._db.execute(
                "SELECT * FROM pa_order_meta WHERE session_id=? AND status IN ('ORDER_PENDING','PARTIALLY_FILLED','ENTERED')",
                (current["id"],))]
            protections = {}
            for meta in open_meta:
                cfg = json.loads(meta["config_json"])
                protections[meta["order_id"]] = {
                    "stop_loss": cfg["stop"], "take_profit": cfg["target"],
                    "target_r": cfg.get("target_r", 2.5),
                    "tick_size": (cfg.get("contract_rules") or {}).get("tick_size"),
                }
            result = (self.broker.process_candle(
                current.get("symbol", "BTCUSDT"), candle, protections=protections)
                if feed_reliable and allow_candle_fills else {
                    "symbol": current.get("symbol", "BTCUSDT"), "events": [],
                    "account": self.broker.account(), "paused": True,
                    "reason": ("waiting for the next public quote after decision"
                               if feed_reliable else
                               "unreliable market data; no fills or exits inferred"),
                })
            quote_evidence = None
            if feed_reliable and (feed_status or {}).get("state") == "SYNCHRONIZED":
                candidate_quote = execution_quote or {}
                try:
                    bid, ask, mark = (float(candidate_quote[key]) for key in ("bid", "ask", "mark"))
                    if bid > 0 and ask >= bid and mark > 0:
                        quote_evidence = {
                            "bid": bid, "ask": ask, "mark": mark,
                            "quote_observed_at": (feed_status or {}).get("last_quote_update"),
                            "mark_observed_at": (feed_status or {}).get("last_mark_update"),
                            "source": "Binance USDⓈ-M reconciled public streams",
                            "market_data_health": "SYNCHRONIZED",
                        }
                except (KeyError, TypeError, ValueError):
                    quote_evidence = None
            for event in result["events"]:
                meta = self._db.execute("SELECT * FROM pa_order_meta WHERE order_id=?", (event.get("order_id"),)).fetchone()
                if meta:
                    meta_config = json.loads(meta["config_json"] or "{}")
                    meta_correlation = meta_config.get("correlation_id")
                    order = self.broker.order(meta["order_id"])
                    status = "ENTERED" if order["status"] == "filled" else "PARTIALLY_FILLED"
                    self._db.execute("UPDATE pa_order_meta SET status=?,reason=?,updated_at=? WHERE order_id=?",
                                     (status, "paper fill simulated from verified market data", _iso(), meta["order_id"]))
                    self._audit("paper_order_filled", strategy_id=meta["strategy_id"], object_id=meta["order_id"],
                                payload={**event, "execution_quote": quote_evidence,
                                         "correlation_id": meta_correlation})
                    self._advance_evaluation(meta_correlation, "FILLED",
                                             "paper order filled from a verified closed candle",
                                             order_id=meta["order_id"], fill=event)
                    if status == "ENTERED":
                        self._advance_evaluation(meta_correlation, "POSITION_OPEN",
                                                 "protected Price Action paper position opened",
                                                 order_id=meta["order_id"])
                elif str(event.get("order_id", "")).startswith("protective-"):
                    owning = self._db.execute(
                        "SELECT order_id,strategy_id,config_json FROM pa_order_meta WHERE session_id=? AND status='ENTERED' ORDER BY created_at DESC LIMIT 1",
                        (current["id"],)).fetchone()
                    self._audit("paper_position_completed",
                                strategy_id=owning["strategy_id"] if owning else "",
                                object_id=owning["order_id"] if owning else event["order_id"],
                                payload={**event, "execution_quote": quote_evidence,
                                         "broker_event_order_id": event["order_id"]})
                    owning_config = json.loads(owning["config_json"] or "{}") if owning else {}
                    self._advance_evaluation(
                        owning_config.get("correlation_id"),
                        "EXITED", "protective paper exit completed",
                        broker_event_order_id=event["order_id"], fill=event)
                    self._db.execute(
                        "UPDATE pa_order_meta SET status='COMPLETED',reason=?,updated_at=? WHERE session_id=? AND status='ENTERED'",
                        ("protective stop or fixed 2.5R target completed the paper position",
                         _iso(), current["id"]),
                    )
            created, rejected = [], []
            for proposal in proposals.values():
                if proposal["strategy_id"] != config.strategy_id:
                    continue
                if self._db.execute("SELECT 1 FROM pa_candidates WHERE session_id=? AND source_proposal_id=?",
                                    (current["id"], proposal["id"])).fetchone() or \
                   self._db.execute("SELECT 1 FROM pa_order_meta WHERE session_id=? AND proposal_id=?",
                                    (current["id"], proposal["id"])).fetchone():
                    continue
                setup = setups.get(proposal["setup_id"], {})
                attestation_error = self._proposal_attestation_error(proposal, setup, config)
                if attestation_error:
                    self._candidate(proposal, setup, contract_rules, "REJECTED",
                                    attestation_error, config, correlation_id=correlation,
                                    idempotency_key=idempotency)
                    self._advance_evaluation(correlation, "RISK_REJECTED", attestation_error)
                    rejected.append({"proposal_id": proposal["id"],
                                     "reason": attestation_error})
                elif self.persistence_status().get("state") == "PERSISTENCE_BLOCKED":
                    reason = "PERSISTENCE_BLOCKED: immutable Price Action evidence is not durable"
                    self._candidate(proposal, setup, contract_rules, "DATA_PAUSED",
                                    reason, config, correlation_id=correlation,
                                    idempotency_key=idempotency)
                    self._advance_evaluation(correlation, "RISK_REJECTED", reason)
                    rejected.append({"proposal_id": proposal["id"], "reason": reason})
                elif not feed_reliable:
                    self._candidate(proposal, setup, contract_rules, "DATA_PAUSED",
                                    "new entry paused because market feed is unreliable", config,
                                    correlation_id=correlation, idempotency_key=idempotency)
                    self._advance_evaluation(correlation, "RISK_REJECTED",
                                             "new entry paused because market feed is unreliable")
                    rejected.append({"proposal_id": proposal["id"], "reason": "feed unreliable"})
                elif config.operating_mode == "signals_only":
                    self._candidate(proposal, setup, contract_rules, "SIGNAL_ONLY", "signals-only mode does not create an order", config,
                                    correlation_id=correlation, idempotency_key=idempotency)
                elif config.operating_mode == "manual_approval":
                    self._candidate(proposal, setup, contract_rules, "PENDING_APPROVAL", "waiting for explicit paper-order approval", config,
                                    correlation_id=correlation, idempotency_key=idempotency)
                elif config.operating_mode == "automatic":
                    try:
                        decision_time = (
                            candle.timestamp + timedelta(
                                milliseconds=TF_MS[current.get("timeframe") or "5m"])
                        ).isoformat()
                        proposal = {**proposal, "_decision_timestamp": decision_time}
                        placed = self._place_proposal(
                            proposal, setup, contract_rules, config, source="automatic",
                            correlation_id=correlation, idempotency_key=idempotency)
                    except (ValueError, RuntimeError) as exc:
                        reason = f"automatic paper placement rejected: {exc}"
                        self._candidate(proposal, setup, contract_rules, "REJECTED", reason, config,
                                        correlation_id=correlation, idempotency_key=idempotency)
                        self._advance_evaluation(correlation, "RISK_REJECTED", reason)
                        placed = {"accepted": False, "proposal_id": proposal["id"],
                                  "reason": reason}
                    (created if placed["accepted"] else rejected).append(placed)
                else:
                    reason = "saved Price Action operating mode is invalid; execution failed closed"
                    self._candidate(proposal, setup, contract_rules, "DATA_PAUSED", reason, config,
                                    correlation_id=correlation, idempotency_key=idempotency)
                    self._advance_evaluation(correlation, "RISK_REJECTED", reason)
                    rejected.append({"proposal_id": proposal["id"], "reason": reason})
            self._snapshot_current(metrics=visual_state.get("metrics", {}))
        try:
            captured = self.journal.capture(
                visual_state=visual_state, session=self._decode_session(dict(current)),
                paper_state=self.state(), feed_status=observed_health,
                partition_label="paper_forward" if current.get("mode") == "LIVE_PAPER" else "development",
            )
            persistence = self._clear_persistence_blocker()
        except PersistenceBlocked as exc:
            # Journal evidence persistence is operationally blocked, but this
            # is not a websocket failure. Preserve the stream and fail closed
            # for entries until a later closed candle retries successfully.
            captured = []
            persistence = self._mark_persistence_blocked(exc)
        except sqlite3.OperationalError as exc:
            if not is_sqlite_busy(exc):
                raise
            captured = []
            persistence = self._mark_persistence_blocked(
                PersistenceBlocked("journal evidence snapshot", exc))
        return {"broker": result, "created": created, "rejected": rejected,
                "reconciliation": reconciliation,
                "journal_entries_captured": captured,
                "journal_persistence": persistence,
                "operating_mode": config.operating_mode, "feed_reliable": feed_reliable,
                "real_execution_allowed": False}

    def process_quote(self, symbol: str, quote: dict, *, feed_reliable: bool) -> dict:
        """Advance this isolated account from a strictly post-decision quote.

        Closed candles create paper intents; they never fill them in the live
        runtime.  The shared public-feed hub invokes this method with the next
        reconciled bid/ask/mark snapshot.  Account and metadata writes remain
        scoped to the Price Action database.
        """
        current = self.session()
        if not current:
            raise ValueError("no active Price Action session")
        if not feed_reliable:
            return {"events": [], "paused": True,
                    "reason": "market data is not synchronized",
                    "real_execution_allowed": False}
        with self._lock:
            rows = [dict(row) for row in self._db.execute(
                "SELECT * FROM pa_order_meta WHERE session_id=? "
                "AND status IN ('ORDER_PENDING','PARTIALLY_FILLED','ENTERED')",
                (current["id"],),
            )]
            protections = {}
            for row in rows:
                config = json.loads(row["config_json"] or "{}")
                protections[row["order_id"]] = {
                    "stop_loss": config.get("stop"),
                    "take_profit": config.get("target"),
                    "target_r": config.get("target_r", 2.5),
                    "tick_size": (config.get("contract_rules") or {}).get("tick_size"),
                }
        result = self.broker.process_tick(symbol, quote, protections=protections)
        for event in result["events"]:
            with self._lock:
                meta = self._db.execute(
                    "SELECT * FROM pa_order_meta WHERE order_id=?",
                    (event.get("order_id"),),
                ).fetchone()
                if meta:
                    config = json.loads(meta["config_json"] or "{}")
                    order = self.broker.order(meta["order_id"])
                    status = "ENTERED" if order["status"] == "filled" else "PARTIALLY_FILLED"
                    self._db.execute(
                        "UPDATE pa_order_meta SET status=?,reason=?,updated_at=? WHERE order_id=?",
                        (status, "paper fill simulated from post-decision public quote",
                         _iso(), meta["order_id"]),
                    )
                    self._audit(
                        "paper_order_filled", strategy_id=meta["strategy_id"],
                        object_id=meta["order_id"],
                        payload={**event, "execution_quote": quote,
                                 "correlation_id": config.get("correlation_id")},
                    )
                    self._advance_evaluation(
                        config.get("correlation_id"), "FILLED",
                        "paper order filled from a strictly post-decision public quote",
                        order_id=meta["order_id"], fill=event,
                    )
                    if status == "ENTERED":
                        self._advance_evaluation(
                            config.get("correlation_id"), "POSITION_OPEN",
                            "protected Price Action paper position opened",
                            order_id=meta["order_id"], fill=event,
                        )
                elif str(event.get("order_id", "")).startswith("protective-"):
                    owner = self._db.execute(
                        "SELECT order_id,strategy_id,config_json FROM pa_order_meta "
                        "WHERE session_id=? AND status='ENTERED' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (current["id"],),
                    ).fetchone()
                    owner_config = json.loads(owner["config_json"] or "{}") if owner else {}
                    self._audit(
                        "paper_position_completed",
                        strategy_id=owner["strategy_id"] if owner else "",
                        object_id=owner["order_id"] if owner else event["order_id"],
                        payload={**event, "execution_quote": quote,
                                 "broker_event_order_id": event["order_id"]},
                    )
                    self._advance_evaluation(
                        owner_config.get("correlation_id"), "EXITED",
                        "protective paper exit completed from public quote",
                        broker_event_order_id=event["order_id"], fill=event,
                    )
                    self._db.execute(
                        "UPDATE pa_order_meta SET status='COMPLETED',reason=?,updated_at=? "
                        "WHERE session_id=? AND status='ENTERED'",
                        ("protective stop or target completed the paper position",
                         _iso(), current["id"]),
                    )
        if result["events"]:
            self._snapshot_current()
        return {**result, "paper_only": True, "real_execution_allowed": False}

    def approve_candidate(self, proposal_id: str) -> dict:
        with self._lock, self._db:
            current = self.session()
            row = self._db.execute("SELECT * FROM pa_candidates WHERE session_id=? AND source_proposal_id=?",
                                   (current.get("id", ""), proposal_id)).fetchone()
            if not row:
                raise KeyError(proposal_id)
            if row["status"] != "PENDING_APPROVAL":
                raise ValueError("candidate is not awaiting manual approval")
            payload = json.loads(row["payload"])
            config = PaperExecutionConfig(**payload["execution_config"]).validated()
            return self._place_proposal(payload["proposal"], payload["setup"], payload["rules"], config,
                                        source="manual_approval",
                                        correlation_id=payload.get("correlation_id"),
                                        idempotency_key=payload.get("idempotency_key"))

    def apply_funding_once(self, *, symbol: str, funding_time: str,
                           rate: float, mark_price: float) -> dict:
        """Apply one provider funding event at most once per lab session."""
        session_id = self.session().get("id")
        if not session_id or not funding_time:
            return {"applied": False, "reason": "funding event unavailable"}
        key = (session_id, symbol.upper(), funding_time)
        with self._lock:
            existing = self._db.execute(
                "SELECT applied,amount FROM pa_funding_events WHERE session_id=? AND symbol=? AND funding_time=?", key).fetchone()
            if existing:
                return {"applied": False, "reason": "funding event already processed",
                        "originally_applied": bool(existing["applied"]), "funding": existing["amount"]}
            result = self.broker.apply_funding(symbol, rate, mark_price)
            owning = self._db.execute(
                "SELECT order_id FROM pa_order_meta WHERE session_id=? AND status='ENTERED' ORDER BY created_at DESC LIMIT 1",
                (session_id,)).fetchone()
            with self._db:
                self._db.execute(
                    "INSERT INTO pa_funding_events(session_id,symbol,funding_time,funding_rate,mark_price,applied,amount,order_id) VALUES (?,?,?,?,?,?,?,?)",
                    (*key, float(rate), float(mark_price), int(bool(result.get("applied"))),
                     float(result.get("funding") or 0), owning["order_id"] if owning else None),
                )
            return {**result, "funding_time": funding_time, "rate": rate,
                    "source": "Binance USDⓈ-M Futures public funding history"}


def binance_visual_state(market: MarketDataService, symbol: str, timeframe: str, *,
                         limit: int = 800, visible: int = 400,
                         observed_at: datetime | None = None) -> dict:
    symbol = normalize_symbol(symbol)
    if timeframe not in TF_MS:
        raise ValueError(f"unsupported timeframe '{timeframe}'")
    now = observed_at or datetime.now(timezone.utc)
    raw = market.public_usdm_window(symbol, timeframe, limit=limit)
    step = timedelta(milliseconds=TF_MS[timeframe])
    closed = [row for row in raw if row.timestamp + step <= now]
    forming = next((row for row in reversed(raw) if row.timestamp + step > now), None)
    if not closed:
        raise RuntimeError("Binance USD-M returned no completed candles")
    engine = NativePriceActionEngine(PriceActionConfig(symbol=symbol, timeframe=timeframe))
    engine.ingest_closed_bars(closed)
    state = engine.visual_state(candle_window=max(50, min(visible, 1500)))
    previous = closed[-1].close
    current = forming.close if forming else previous
    try:
        quote = market.public_usdm_quote(symbol)
        bid, ask, mark = (float(quote[key]) for key in ("bid", "ask", "mark"))
        provider_time = datetime.fromisoformat(str(quote["provider_time"]).replace("Z", "+00:00"))
        quote_age = max(0.0, (now - provider_time).total_seconds())
        reference = current or previous
        deviation_bps = max(abs(bid - reference), abs(ask - reference),
                            abs(mark - reference)) / reference * 10_000
        reconciled = bid > 0 and bid <= ask and mark > 0 and quote_age <= 15 and deviation_bps <= 100
        connection_state = "SYNCHRONIZED" if reconciled else "QUOTE_MISMATCH"
        health_reason = ("REST candle, bid/ask and mark snapshot are reconciled and fresh" if reconciled
                         else "REST quote is stale, invalid or inconsistent with the displayed candle")
        entries_paused = not reconciled
    except Exception:
        # OHLC remains factual and usable for inspection, but the absence of
        # the independent quote/mark channel is made explicit and automated
        # paper entries stay paused.
        quote = {"bid": current, "ask": current, "mark": current,
                 "funding_rate": None, "last_funding_time": None, "next_funding_time": None}
        connection_state = "DELAYED"
        health_reason = "independent REST bid/ask and mark snapshot is unavailable"
        quote_age = None
        deviation_bps = None
        entries_paused = True
    state["forming_candle"] = _candle(forming) if forming else None
    state["live_display"] = {
        "is_forming": forming is not None,
        "observed_at": now.isoformat(),
        "candle_closes_at": (forming.timestamp + step).isoformat() if forming else None,
        "last_price": current,
        "price_direction": "up" if current > previous else "down" if current < previous else "unchanged",
        "bid": quote["bid"], "ask": quote["ask"], "mark": quote["mark"],
        "funding_rate": quote["funding_rate"],
        "last_funding_time": quote.get("last_funding_time"),
        "next_funding_time": quote["next_funding_time"],
        "connection_state": connection_state,
        "transport_state": "REST_POLL",
        "health_reason": health_reason,
        "reliable": not entries_paused,
        "quote_source": "PUBLIC_REST_SNAPSHOT",
        "quote_age_seconds": quote_age,
        "mark_age_seconds": quote_age,
        "candle_quote_deviation_bps": deviation_bps,
        "new_entries_paused": entries_paused,
        "refresh_interval_seconds": 3,
        "execution_uses_closed_bars_only": True,
    }
    state["data_provenance"] = {
        "mode": "LIVE_BINANCE_DISPLAY_WITH_CLOSED_BAR_PRICE_ACTION",
        "market_data_mode": "LIVE",
        "market_data_source": "Binance USDⓈ-M Futures public OHLCV",
        "exchange": "Binance USDⓈ-M Futures",
        "market_type": "USDT perpetual",
        "symbol": symbol,
        "timeframe": timeframe,
        "observed_at": now.isoformat(),
        "raw_candles_received": len(raw),
        "closed_candles_used": len(closed),
        "forming_candle_excluded": forming is not None,
        "connection_state": connection_state,
        "new_entries_paused": entries_paused,
        "real_execution_allowed": False,
    }
    return state


def replay_state(market: MarketDataService, symbol: str, timeframe: str, *,
                 cursor: int, limit: int = 1000) -> dict:
    symbol = normalize_symbol(symbol)
    try:
        rows = market.bars(symbol, timeframe, limit=max(100, min(limit, 3000)))
    except ValueError:
        rows = []
    if not rows:
        raise RuntimeError("verified cached history is required for replay; download Binance USD-M history first")
    reveal = max(1, min(int(cursor), len(rows)))
    visible = rows[:reveal]
    engine = NativePriceActionEngine(PriceActionConfig(symbol=symbol, timeframe=timeframe))
    engine.ingest_closed_bars(visible)
    state = engine.visual_state(candle_window=min(limit, reveal))
    state["replay"] = {"cursor": reveal, "total": len(rows), "future_candles_visible": False,
                       "has_next": reveal < len(rows)}
    state["data_provenance"] = {
        "mode": "HISTORICAL_REPLAY",
        "market_data_mode": "REPLAY",
        "market_data_source": "verified Binance USDⓈ-M Futures cache",
        "exchange": "Binance USDⓈ-M Futures",
        "symbol": symbol,
        "timeframe": timeframe,
        "closed_candles_used": reveal,
        "future_candles_visible": False,
        "real_execution_allowed": False,
    }
    return state


class PriceActionLabRuntime:
    """One durable public-data/engine/executor owner for the Visual Lab."""

    def __init__(self, market: MarketDataService, account: PriceActionPaperAccount, *,
                 poll_seconds: float = 5.0, autostart: bool = False,
                 market_hub=None):
        self.market, self.account = market, account
        self.poll_seconds = max(1.0, float(poll_seconds))
        self._lock = threading.RLock()
        self._supervisor_stop = threading.Event()
        self._supervisor_thread: threading.Thread | None = None
        self.engine: NativePriceActionEngine | None = None
        self.identity: tuple[str, str] | None = None
        self.config_signature = ""
        self.market_hub = market_hub
        self._shadow_runs: dict[str, dict] = {}
        self.stream = (
            market_hub.subscription(
                "PA_LAB", event_sink=account.record_external_event,
                bar_sink=self._on_closed_bar, quote_sink=self._on_quote,
            )
            if market_hub is not None else
            PriceActionPublicStream(
                market.public_usdm_window, event_sink=account.record_external_event,
                bar_sink=self._on_closed_bar, quote_sink=self._on_quote,
            )
        )
        if autostart:
            self._supervisor_thread = threading.Thread(
                target=self._run_supervisor, name="price-action-paper-runtime", daemon=True)
            self._supervisor_thread.start()

    def ensure(self, symbol: str, timeframe: str) -> None:
        identity = (normalize_symbol(symbol), timeframe)
        config = self._engine_config(*identity)
        signature = json.dumps(asdict(config), sort_keys=True)
        with self._lock:
            engine_matches = (
                self.identity == identity and self.engine is not None and
                self.config_signature == signature
            )
            if engine_matches and self.stream.running and (
                    self.stream.symbol, self.stream.timeframe) == identity:
                return
            if not engine_matches:
                self.identity = identity
                self.config_signature = signature
                self.engine = NativePriceActionEngine(config)
            started = self.stream.start(*identity)
            if started:
                # A restarted stream may have closed bars that arrived while
                # the worker was down. Rebuild the core from its reconciled
                # REST bootstrap, but do not execute historical proposals.
                self.engine = NativePriceActionEngine(config)
                self.engine.set_native_mtf_context(self._native_context(*identity))
                self.engine.ingest_closed_bars(
                    self.stream.snapshot()["closed_bars"],
                    market_data_health="LIVE_BOOTSTRAP_RECONCILED",
                )

    def _native_context(self, symbol: str, timeframe: str) -> dict[str, list[Bar]]:
        """Load the shared policy's independent native Binance candles."""
        from services.mtf_policy import native_timeframes
        context: dict[str, list[Bar]] = {}
        if self.market_hub is not None:
            fallback = lambda sym, tf, limit, **_kwargs: self.market.public_usdm_window(
                sym, tf, limit=limit)
            fetcher = self.stream.make_fetcher(fallback)
            for htf in native_timeframes(timeframe):
                result = fetcher(symbol, htf, 500)
                context[htf] = list(result[0] if isinstance(result, tuple) else result)
        else:
            for htf in native_timeframes(timeframe):
                context[htf] = list(self.market.public_usdm_window(symbol, htf, limit=500))
        return context

    def _maintain_live_session(self) -> dict:
        """Keep the saved LIVE_PAPER session running without any browser client."""
        session = self.account.session()
        if not session:
            return {"active": False, "reason": "no active Price Action session"}
        if session.get("mode") != "LIVE_PAPER":
            return {"active": False, "reason": "active session is not LIVE_PAPER"}
        identity = (normalize_symbol(session.get("symbol") or "BTCUSDT"),
                    session.get("timeframe") or "5m")
        self.ensure(*identity)
        return {"active": self.stream.running, "symbol": identity[0],
                "timeframe": identity[1], "connection": self.stream.status()["state"]}

    def _run_supervisor(self) -> None:
        while not self._supervisor_stop.is_set():
            try:
                self._maintain_live_session()
            except Exception as exc:
                self.account.record_external_event({
                    "kind": "paper_runtime_paused",
                    "error": f"{type(exc).__name__}: {exc}",
                    "new_orders_created": 0,
                })
            self._supervisor_stop.wait(self.poll_seconds)

    def _session_identity(self, expected_mode: str) -> tuple[dict, tuple[str, str]]:
        session = self.account.session()
        if not session:
            raise RuntimeError("no active Price Action session")
        if session.get("mode") != expected_mode:
            raise ValueError(
                f"active Price Action session mode is {session.get('mode')}; expected {expected_mode}"
            )
        return session, (normalize_symbol(session.get("symbol") or "BTCUSDT"),
                         session.get("timeframe") or "5m")

    def _assert_session_identity(self, symbol: str, timeframe: str, expected_mode: str) -> dict:
        session, identity = self._session_identity(expected_mode)
        requested = (normalize_symbol(symbol), timeframe)
        if requested != identity:
            raise ValueError(
                f"requested {requested[0]} {requested[1]} does not match active Price Action "
                f"session {identity[0]} {identity[1]}"
            )
        return session

    def _engine_config(self, symbol: str, timeframe: str) -> PriceActionConfig:
        session = self.account.session()
        raw = json.loads(session.get("strategy_config_json") or "{}")
        allowed = set(PriceActionConfig.__dataclass_fields__) - {"symbol", "timeframe", "execution_allowed"}
        return PriceActionConfig(symbol=symbol, timeframe=timeframe,
                                 **{key: value for key, value in raw.items() if key in allowed})

    def _on_quote(self, quote: dict) -> None:
        with self._lock:
            if self.identity is None:
                return
            session = self.account.session()
            if not session or session.get("mode") != "LIVE_PAPER":
                return
            if (normalize_symbol(session.get("symbol") or ""),
                    session.get("timeframe")) != self.identity:
                return
            status = self.stream.status()
            self.account.process_quote(
                self.identity[0], quote,
                feed_reliable=bool(status.get("reliable")),
            )

    def _on_closed_bar(self, bar: Bar) -> None:
        with self._lock:
            if self.engine is None:
                return
            status = self.stream.status()
            try:
                from services.mtf_policy import candle_close, evidence_at
                native_context = self._native_context(*self.identity)
                evidence = evidence_at(
                    self.identity[0], self.identity[1], native_context,
                    candle_close(bar, self.identity[1]),
                )
                if evidence.get("primary") is None:
                    raise RuntimeError(
                        "Price Action primary native HTF candle is unavailable")
                self.engine.set_native_mtf_context(native_context)
                self.engine.process_closed_bar(bar, market_data_health=status["state"])
            except ValueError:
                # A REST reconciliation can deliver an older missing bar. Rebuild
                # from the stream's now-contiguous history to preserve chronology.
                rows = self.stream.snapshot()["closed_bars"]
                self.engine = NativePriceActionEngine(self._engine_config(*self.identity))
                self.engine.set_native_mtf_context(self._native_context(*self.identity))
                self.engine.ingest_closed_bars(rows, market_data_health=status["state"])
            state = self.engine.visual_state(candle_window=1500)
            self._advance_shadows(bar)
            rules = self.market.usdm_contract_rules(self.identity[0])
            session = self.account.session()
            stream_snapshot = self.stream.snapshot()
            quote = stream_snapshot.get("quote") or {}
            state["live_display"] = {
                "bid": quote.get("bid"), "ask": quote.get("ask"),
                "mark": quote.get("mark"), "connection_state": status["state"],
            }
            state["data_provenance"] = {
                "mode": "LIVE_BINANCE_WEBSOCKET_WITH_REST_RECOVERY",
                "market_data_mode": "LIVE",
                "market_data_source": "Binance USDⓈ-M public streams",
                "exchange": "Binance USDⓈ-M Futures",
                "symbol": self.identity[0], "timeframe": self.identity[1],
                "connection_state": status["state"],
                "real_execution_allowed": False,
            }
            state["metrics_scope"].update({
                "session_id": session.get("id"),
                "mode": "LIVE_PUBLIC_MARKET_OBSERVATION",
            })
            session_matches = (
                session.get("mode") == "LIVE_PAPER" and
                (session.get("symbol"), session.get("timeframe")) == self.identity
            )
            if session_matches:
                try:
                    self.account.synchronize_strategy(
                        state, contract_rules=rules, candle=bar,
                        feed_reliable=bool(status["reliable"]),
                        feed_status=status, execution_quote=quote,
                        allow_candle_fills=False)
                except PersistenceBlocked as exc:
                    self.account._mark_persistence_blocked(exc)
                    return
                except sqlite3.OperationalError as exc:
                    if not is_sqlite_busy(exc):
                        raise
                    self.account._mark_persistence_blocked(
                        PersistenceBlocked("closed-candle persistence", exc))
                    return
            else:
                self.account.record_external_event({
                    "kind": "view_only_market_update", "symbol": self.identity[0],
                    "timeframe": self.identity[1],
                    "reason": "active paper session owns a different symbol/timeframe",
                })
            if status["reliable"]:
                # Use the mark that participated in the stream-health
                # reconciliation for account-changing liquidation checks.  A
                # separate REST snapshot may supply the last funding timestamp,
                # but it may never substitute an unreconciled mark.
                reconciled_mark: float | None = None
                try:
                    stream_quote = self.stream.snapshot()["quote"]
                    reconciled_mark = float(stream_quote["mark"])
                    if reconciled_mark <= 0:
                        raise ValueError("reconciled stream mark is invalid")
                    liquidation = self.account.broker.process_mark(
                        self.identity[0], reconciled_mark)
                    if liquidation.get("liquidated"):
                        self.account.record_external_event({"kind": "paper_liquidation", **liquidation})
                except Exception as exc:
                    self.account.record_external_event({
                        "kind": "paper_mark_processing_delayed", "error": str(exc),
                        "symbol": self.identity[0],
                    })
                try:
                    if reconciled_mark is None:
                        raise ValueError("funding requires a reconciled stream mark")
                    funding_quote = self.market.public_usdm_quote(self.identity[0])
                    funding_mark = float(funding_quote["mark"])
                    deviation_bps = abs(funding_mark - reconciled_mark) / reconciled_mark * 10_000
                    if deviation_bps > 100:
                        raise ValueError(
                            f"funding snapshot mark differs by {deviation_bps:.2f} bps")
                    self.account.apply_funding_once(
                        symbol=self.identity[0],
                        funding_time=funding_quote.get("last_funding_time"),
                        rate=funding_quote.get("funding_rate") or 0,
                        mark_price=reconciled_mark,
                    )
                except Exception as exc:
                    self.account.record_external_event({
                        "kind": "paper_funding_processing_delayed", "error": str(exc),
                        "symbol": self.identity[0],
                    })
            else:
                self.account.record_external_event({
                    "kind": "paper_mark_processing_paused", "symbol": self.identity[0],
                    "market_data_health": status["state"],
                    "reason": "unreconciled market data cannot change paper account state",
                })

    def live_state(self, symbol: str, timeframe: str, *, visible: int = 500,
                   request_id: str | None = None) -> dict:
        session = self._assert_session_identity(symbol, timeframe, "LIVE_PAPER")
        self.ensure(symbol, timeframe)
        with self._lock:
            if self.engine is None:
                raise RuntimeError("Price Action runtime failed to initialize")
            snapshot = self.stream.snapshot()
            state = self.engine.visual_state(candle_window=max(50, min(visible, 1500)))
        quote, connection = snapshot["quote"], snapshot["connection"]
        quote_source = "PUBLIC_WEBSOCKET"
        if quote.get("bid") is None or quote.get("mark") is None:
            try:
                quote = {**quote, **self.market.public_usdm_quote(symbol)}
                quote_source = "PUBLIC_REST_FALLBACK_UNRECONCILED"
            except Exception:
                pass
        forming = snapshot["forming"]
        last = quote.get("last") or (forming.close if forming else (self.engine.bars[-1].close if self.engine.bars else None))
        state["forming_candle"] = _candle(forming) if forming else None
        state["live_display"] = {
            "is_forming": forming is not None, "observed_at": _iso(),
            "refresh_interval_seconds": 0, "candle_closes_at": (
                (forming.timestamp + timedelta(milliseconds=TF_MS[timeframe])).isoformat() if forming else None),
            "last_price": last, "bid": quote.get("bid"), "ask": quote.get("ask"),
            "mark": quote.get("mark"), "funding_rate": quote.get("funding_rate"),
            "next_funding_time": quote.get("next_funding_time"),
            "connection_state": connection["state"],
            "transport_state": connection["transport_state"],
            "health_reason": connection["health_reason"],
            "reliable": connection["reliable"],
            "quote_source": quote_source,
            "last_update": connection["last_update"],
            "last_candle_update": connection["last_candle_update"],
            "last_quote_update": connection["last_quote_update"],
            "last_mark_update": connection["last_mark_update"],
            "last_closed_update": connection["last_closed_update"],
            "candle_age_seconds": connection["candle_age_seconds"],
            "quote_age_seconds": connection["quote_age_seconds"],
            "mark_age_seconds": connection["mark_age_seconds"],
            "closed_candle_age_seconds": connection["closed_candle_age_seconds"],
            "candle_quote_deviation_bps": connection["candle_quote_deviation_bps"],
            "failing_dependency": connection.get("failing_dependency"),
            "last_successful_event": connection.get("last_successful_event"),
            "retry_state": connection.get("retry_state"),
            "connecting_age_seconds": connection.get("connecting_age_seconds"),
            "new_entries_paused": connection["new_entries_paused"],
            "execution_uses_closed_bars_only": True,
        }
        state["data_identity"] = {
            "request_id": request_id, "session_id": session["id"],
            "mode": session["mode"], "symbol": normalize_symbol(symbol),
            "timeframe": timeframe,
        }
        state["metrics_scope"].update({
            "session_id": session["id"], "mode": "LIVE_PUBLIC_MARKET_OBSERVATION",
        })
        state["connection"] = connection
        state["data_provenance"] = {
            "mode": "LIVE_BINANCE_WEBSOCKET_WITH_REST_RECOVERY",
            "market_data_mode": "LIVE", "market_data_source": "Binance USDⓈ-M public streams",
            "exchange": "Binance USDⓈ-M Futures", "symbol": normalize_symbol(symbol),
            "timeframe": timeframe, "closed_candles_used": len(self.engine.bars),
            "session_id": session["id"], "request_id": request_id,
            "forming_candle_excluded": forming is not None,
            "connection_state": connection["state"],
            "new_entries_paused": connection["new_entries_paused"],
            "real_execution_allowed": False,
        }
        return state

    def bot_status(self) -> dict:
        """One factual, scope-labelled control-plane view for the dashboard."""
        from services.mtf_policy import display_contract
        paper = self.account.state()
        session = paper.get("session") or {}
        connection = self.stream.status()
        evaluations = paper.get("evaluations") or []
        latest = evaluations[0] if evaluations else None
        pending = [row for row in paper.get("orders", [])
                   if row.get("status") in OPEN_BROKER_ORDER_STATUSES]
        pending_entries = [row for row in pending if not row.get("reduce_only")]
        positions = paper.get("positions") or []
        orphaned_exposure = not session and bool(positions or pending_entries)
        config = session.get("execution_config") or {}
        blockers = lifecycle_blockers(
            connection=connection, operating_mode=session.get("operating_mode", "signals_only"),
            account=paper.get("account") or {}, strategy_valid=bool(config.get("strategy_id")),
            positions=positions, pending_orders=pending_entries,
            risk_pct=config.get("risk_pct"),
            max_risk_pct=float(config.get("max_risk_pct") or 1.0))
        if orphaned_exposure:
            blockers.insert(0, "paper exposure has no active owning session; explicitly resume, repair, or close it")
        control = paper.get("entry_control") or {}
        if control.get("readiness_recheck_required"):
            blockers.append(control.get("entry_pause_reason") or
                            "Price Action readiness recheck is required")
        persistence = self.account.persistence_status()
        if persistence.get("state") == "PERSISTENCE_BLOCKED":
            blockers.insert(0, "PERSISTENCE_BLOCKED: immutable journal write is waiting for SQLite")
        blockers = list(dict.fromkeys(blockers))
        paper_rows = self.account.journal.list(
            session_id=session.get("id"), partition="paper_forward").get("entries", [])
        development = self.account.journal.list(partition="development").get("entries", [])
        validation = self.account.journal.list(partition="validation").get("entries", [])
        live_performance = paper_performance(
            fills=paper.get("trades") or [], account=paper.get("account") or {},
            orders=paper.get("orders") or [],
            realized_r_values=[row["outcome"].get("net_r") for row in paper_rows
                               if row.get("outcome", {}).get("net_r") is not None])
        activity = paper.get("activity") or []
        execution_armed = session.get("operating_mode") == "automatic"
        operator_state = (
            "ERROR" if orphaned_exposure else
            "BLOCKED" if not session or blockers else
            "RUNNING_ARMED" if execution_armed else "RUNNING_UNARMED"
        )
        mtf_evidence = dict(self.engine._mtf_evidence) if self.engine is not None else {}
        mtf_policy = (display_contract(session["timeframe"], mtf_evidence)
                      if session.get("timeframe") else None)
        return {
            "lab": "PRICE_ACTION", "account_scope": paper["account_scope"],
            "scope_label": "Price Action session · isolated paper ledger",
            "paper_only": True, "real_execution_allowed": False,
            "strategy": {"id": config.get("strategy_id"),
                         "version": PRICE_ACTION_STRATEGY_VERSION},
            "symbol": session.get("symbol"), "timeframe": session.get("timeframe"),
            "mtf_policy": mtf_policy,
            "mode": session.get("operating_mode"), "saved_configuration": config,
            "session_state": operator_state,
            "execution_armed": execution_armed,
            "feed": connection,
            "decision_state": lifecycle_state(
                connection_state=connection.get("state", "DISCONNECTED"),
                reliable=bool(connection.get("reliable")),
                has_position=bool(paper.get("positions")), has_order=bool(pending),
                last_decision_state=(latest or {}).get("state")),
            "execution_state": operator_state,
            "blockers": blockers,
            "account": paper.get("account"), "open_positions": len(positions),
            "pending_orders": len(pending), "positions": paper.get("positions") or [],
            "latest_closed_candle_decision": latest,
            # A signal remains evidence after its lifecycle advances to an
            # order, position or exit.  Do not make it disappear merely
            # because the canonical state is no longer SIGNAL_FOUND.
            "latest_signal": next((row for row in evaluations
                                   if (row.get("payload") or {}).get("proposal_ids")), None),
            "latest_order": (paper.get("order_metadata") or [None])[0],
            "latest_fill": (paper.get("trades") or [None])[0],
            "last_heartbeat": connection.get("last_update") or session.get("updated_at"),
            "latest_activity": activity[0] if activity else None,
            "entry_control": control,
            "persistence": persistence,
            "performance": {
                "backtest": journal_performance("BACKTEST", development),
                "forward_validation": journal_performance("FORWARD_VALIDATION", validation),
                "live_paper": live_performance,
            },
        }

    def remediate_legacy_position(self, symbol: str, *, initiated_by: str) -> dict:
        """Close a confirmed legacy PAPER position using only reconciled data."""
        session = self._assert_session_identity(
            symbol, self.account.session().get("timeframe") or "5m", "LIVE_PAPER"
        )
        self.ensure(session["symbol"], session["timeframe"])
        connection = self.stream.status()
        snapshot = self.stream.snapshot()
        quote = snapshot.get("quote") or {}
        if connection.get("state") != "SYNCHRONIZED" or not connection.get("reliable"):
            raise RuntimeError(
                connection.get("health_reason") or
                "Price Action market data is not synchronized"
            )
        try:
            bid, ask, mark = (float(quote[key]) for key in ("bid", "ask", "mark"))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("reconciled bid, ask and mark are required") from exc
        if bid <= 0 or ask < bid or mark <= 0:
            raise RuntimeError("reconciled bid, ask or mark is invalid")
        market_evidence = {
            "state": connection["state"], "reliable": True,
            "health_reason": connection.get("health_reason"),
            "bid": bid, "ask": ask, "mark": mark,
            "last_closed_update": connection.get("last_closed_update"),
            "last_quote_update": connection.get("last_quote_update"),
            "last_mark_update": connection.get("last_mark_update"),
            "source": "Binance USDⓈ-M reconciled public streams",
            "symbol": session["symbol"], "timeframe": session["timeframe"],
        }
        remediation = self.account.remediate_legacy_position(
            session["symbol"], mark_price=mark,
            market_evidence=market_evidence, initiated_by=initiated_by,
        )
        # Closing exposure never grants execution authority by itself.  This
        # explicit second pass recalculates the complete readiness boundary.
        readiness = self.account.recheck_readiness(self.stream.status())
        return {**remediation, "readiness": readiness,
                "paper": self.account.state({session["symbol"]: mark}),
                "real_execution_allowed": False}

    def replay_step(self, symbol: str, timeframe: str, cursor: int, *, limit: int = 3000) -> dict:
        symbol = normalize_symbol(symbol)
        session = self._assert_session_identity(symbol, timeframe, "HISTORICAL")
        rows = self.market.bars(symbol, timeframe, limit=max(100, min(limit, 10_000)))
        if not rows:
            raise RuntimeError("verified cached history is required for replay")
        reveal = max(1, min(int(cursor), len(rows)))
        owns_replay = True
        prior_cursor = int(session.get("replay_cursor") or 0) if owns_replay else reveal
        advances_session = owns_replay and reveal >= prior_cursor
        engine = NativePriceActionEngine(self._engine_config(symbol, timeframe) if owns_replay else
                                         PriceActionConfig(symbol=symbol, timeframe=timeframe))
        warm_to = min(prior_cursor, reveal)
        engine.ingest_closed_bars(rows[:warm_to])
        rules = self.market.usdm_contract_rules(symbol) if owns_replay else None
        for row in rows[warm_to:reveal]:
            engine.process_closed_bar(row, market_data_health="HISTORICAL_REPLAY")
            if advances_session:
                step_state = engine.visual_state(candle_window=3000)
                step_state["data_provenance"] = {
                    "mode": "HISTORICAL_REPLAY", "market_data_mode": "REPLAY",
                    "market_data_source": "verified Binance USDⓈ-M Futures cache",
                    "exchange": "Binance USDⓈ-M Futures", "symbol": symbol,
                    "timeframe": timeframe, "real_execution_allowed": False,
                }
                step_state["metrics_scope"].update({
                    "session_id": session.get("id"), "mode": "HISTORICAL_REPLAY",
                })
                self.account.synchronize_strategy(step_state,
                                                  contract_rules=rules, candle=row,
                                                  feed_reliable=True,
                                                  feed_status={
                                                      "state": "HISTORICAL_REPLAY",
                                                      "health_reason": "verified cached completed candle",
                                                  })
        state = engine.visual_state(candle_window=min(limit, reveal))
        if advances_session:
            self.account.set_replay_cursor(reveal, state.get("metrics"))
        state["replay"] = {"cursor": reveal, "total": len(rows), "future_candles_visible": False,
                           "has_next": reveal < len(rows), "session_execution_advanced": advances_session,
                           "rewind_is_view_only": bool(owns_replay and reveal < prior_cursor)}
        state["data_identity"] = {
            "request_id": None, "session_id": session["id"], "mode": session["mode"],
            "symbol": symbol, "timeframe": timeframe,
        }
        state["metrics_scope"].update({
            "session_id": session["id"], "mode": "HISTORICAL_REPLAY",
        })
        state["data_provenance"] = {
            "mode": "HISTORICAL_REPLAY", "market_data_mode": "REPLAY",
            "market_data_source": "verified Binance USDⓈ-M Futures cache",
            "exchange": "Binance USDⓈ-M Futures", "symbol": symbol, "timeframe": timeframe,
            "session_id": session["id"],
            "closed_candles_used": reveal, "future_candles_visible": False,
            "real_execution_allowed": False,
        }
        return state

    def reconcile_paper_orders(self) -> dict:
        session = self.account.session()
        visual_state = None
        candle = None
        with self._lock:
            if self.engine is not None and self.identity == (
                    session.get("symbol"), session.get("timeframe")):
                visual_state = self.engine.visual_state(candle_window=1500)
                candle = self.engine.bars[-1] if self.engine.bars else None
        feed_reliable = True
        if session.get("mode") == "LIVE_PAPER" and self.identity == (
                session.get("symbol"), session.get("timeframe")):
            feed_reliable = bool(self.stream.status()["reliable"])
        return self.account.reconcile_pending_orders(
            visual_state, candle, feed_reliable=feed_reliable)

    def start_shadow(self, candidate_id: str) -> dict:
        """Start an isolated PAPER-only candidate observer on the baseline bars."""
        with self._lock:
            if self.engine is None or self.identity is None:
                raise RuntimeError("Price Action baseline engine is not initialized")
            candidate = self.account.journal.candidate(candidate_id)
            if candidate["status"] != "APPROVED_FOR_SHADOW":
                raise ValueError("candidate requires explicit approval before shadow observation")
            config = asdict(self.engine.config)
            key = candidate["rule_key"]
            if key not in config:
                raise ValueError("candidate rule is not part of the native Price Action configuration")
            config[key] = candidate["rule_value"]
            config["execution_allowed"] = False
            shadow = NativePriceActionEngine(PriceActionConfig(**config))
            shadow.ingest_closed_bars(self.engine.bars)
            run_id = f"pa-shadow-{uuid.uuid4().hex}"
            self._shadow_runs[candidate_id] = {
                "run_id": run_id, "engine": shadow, "session_id": self.account.session()["id"],
                "started_after": self.engine.bars[-1].timestamp if self.engine.bars else None,
            }
            self.account.journal.candidate_transition(
                candidate_id, action="start_shadow", reason="explicit PAPER shadow start",
                initiated_by="authenticated_user")
            return {"run_id": run_id, "candidate_id": candidate_id,
                    "execution_mode": "PAPER", "official_account_affected": False,
                    "real_execution_allowed": False}

    def _advance_shadows(self, bar: Bar) -> None:
        if self.engine is None:
            return
        baseline = self.engine.snapshots.get(bar.timestamp)
        for candidate_id, run in list(self._shadow_runs.items()):
            shadow: NativePriceActionEngine = run["engine"]
            if bar.timestamp not in shadow.processed:
                shadow.process_closed_bar(bar)
            candidate = shadow.snapshots.get(bar.timestamp)
            started_after = run.get("started_after")
            baseline_proposals = [
                asdict(self.engine.proposals[row]) for row in (baseline.proposal_ids if baseline else ())
                if row in self.engine.proposals]
            candidate_proposals = [
                asdict(shadow.proposals[row]) for row in (candidate.proposal_ids if candidate else ())
                if row in shadow.proposals]
            self.account.journal.record_shadow(
                run_id=run["run_id"], candidate_id=candidate_id,
                session_id=run["session_id"], candle_identity=bar.timestamp.isoformat(),
                baseline={
                    "proposal_ids": list(baseline.proposal_ids) if baseline else [],
                    "proposals": baseline_proposals,
                    "setup_ids": list(baseline.setup_ids) if baseline else [],
                    "structure_bias": baseline.structure_bias if baseline else None,
                    "trades": [
                        asdict(row) for row in self.engine.research_trades.values()
                        if started_after is None or row.created_at > started_after],
                },
                candidate={
                    "proposal_ids": list(candidate.proposal_ids) if candidate else [],
                    "proposals": candidate_proposals,
                    "setup_ids": list(candidate.setup_ids) if candidate else [],
                    "structure_bias": candidate.structure_bias if candidate else None,
                    "trades": [
                        asdict(row) for row in shadow.research_trades.values()
                        if started_after is None or row.created_at > started_after],
                },
            )

    def stop_shadow(self, candidate_id: str) -> dict:
        with self._lock:
            run = self._shadow_runs.pop(candidate_id, None)
            if not run:
                raise KeyError(candidate_id)
            self.account.journal.candidate_transition(
                candidate_id, action="stop_shadow", reason="explicit PAPER shadow stop",
                initiated_by="authenticated_user")
        return self.account.journal.shadow_report(candidate_id)

    def shadow_report(self, candidate_id: str) -> dict:
        return self.account.journal.shadow_report(candidate_id)

    def stop(self) -> None:
        self._supervisor_stop.set()
        thread = self._supervisor_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        self.stream.stop()
