"""Durable, isolated paper account for the SMC Strategy Lab.

The account uses its own SQLite file and owns no exchange client.  Public
market data may advance the local paper broker, but every response keeps real
execution hard-disabled.
"""
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
from data.market_data_v2 import TF_MS, normalize_symbol
from execution.paper_broker_v2 import OPEN_STATUSES, PaperBrokerV2
from services.lab_lifecycle import (
    blockers as lifecycle_blockers,
    correlation_id as make_correlation_id,
    decision_idempotency_key,
    json_text,
    lifecycle_state,
    paper_performance,
    unavailable_performance,
)
from services.smc_strategy_v1 import ENTRY_MODELS, STRATEGY_ID, STRATEGY_VERSION


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


OPERATING_MODES = {"signals_only", "manual_approval", "automatic"}
SESSION_MODES = {"LIVE_PAPER", "HISTORICAL"}
ACTIVE_MODEL_IDS = {row.id for row in ENTRY_MODELS if row.status == "ACTIVE"}


def _configured_r(config: dict, target_key: str) -> float | None:
    explicit = config.get(f"{target_key}_r")
    if explicit is not None:
        return float(explicit)
    entry = config.get("reference_price")
    stop = config.get("stop_loss")
    target = config.get(target_key)
    if entry is None or stop is None or target is None or float(entry) == float(stop):
        return None
    return abs(float(target) - float(entry)) / abs(float(entry) - float(stop))


@dataclass(frozen=True)
class SMCPaperConfig:
    operating_mode: Literal["signals_only", "manual_approval", "automatic"] = "signals_only"
    model_id: str = "SMC_M1_SWEEP_REVERSAL"
    risk_pct: float = 0.5
    max_risk_pct: float = 1.0
    max_concurrent_risk_pct: float = 2.0

    def validated(self) -> "SMCPaperConfig":
        if self.operating_mode not in OPERATING_MODES:
            raise ValueError("operating mode must be signals_only, manual_approval or automatic")
        if self.model_id not in ACTIVE_MODEL_IDS:
            raise ValueError("the selected SMC entry model is parked or unknown")
        if not 0 < self.risk_pct <= self.max_risk_pct <= 1.0:
            raise ValueError("SMC risk per trade must be above 0% and no greater than 1%")
        if self.max_concurrent_risk_pct < self.risk_pct or self.max_concurrent_risk_pct > 2.0:
            raise ValueError("maximum concurrent SMC risk must be between risk per trade and 2%")
        return self


class SMCPaperAccount:
    """SMC-only account, session, ownership and audit ledger."""

    def __init__(self, path: str | Path, *, starting_balance: float = 10_000.0):
        self.path = str(path)
        self.starting_balance = float(starting_balance)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.broker = PaperBrokerV2(
            self.path, starting_balance=self.starting_balance,
            account_type="SMC_LAB", execution_engine="SMC_LAB",
        )
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript("""
              CREATE TABLE IF NOT EXISTS smc_sessions(
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                starting_balance REAL NOT NULL, status TEXT NOT NULL, end_reason TEXT,
                mode TEXT NOT NULL DEFAULT 'LIVE_PAPER', symbol TEXT NOT NULL DEFAULT 'BTCUSDT',
                timeframe TEXT NOT NULL DEFAULT '5m', replay_cursor INTEGER NOT NULL DEFAULT 0,
                operating_mode TEXT NOT NULL DEFAULT 'signals_only', model_id TEXT NOT NULL DEFAULT 'SMC_M1_SWEEP_REVERSAL',
                risk_pct REAL NOT NULL DEFAULT .5, state_json TEXT NOT NULL DEFAULT '{}',
                metrics_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS smc_settings(
                id INTEGER PRIMARY KEY CHECK(id=1), leverage REAL NOT NULL DEFAULT 1);
              CREATE TABLE IF NOT EXISTS smc_activity(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
                symbol TEXT, model_id TEXT, object_id TEXT, created_at TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}');
              CREATE TABLE IF NOT EXISTS smc_order_meta(
                order_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, ownership TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, proposal_id TEXT, setup_id TEXT, poi_id TEXT,
                model_id TEXT, model_version TEXT, direction TEXT NOT NULL,
                entry REAL, stop REAL, target_1 REAL, target_2 REAL, risk_pct REAL,
                creation_candle TEXT, expiry_candle TEXT, status TEXT NOT NULL,
                reason TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(session_id,idempotency_key));
              CREATE TABLE IF NOT EXISTS smc_candidates(
                proposal_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, setup_id TEXT,
                model_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS smc_funding_events(
                session_id TEXT NOT NULL, symbol TEXT NOT NULL, funding_time TEXT NOT NULL,
                rate REAL NOT NULL, mark_price REAL NOT NULL, amount REAL NOT NULL,
                applied INTEGER NOT NULL, PRIMARY KEY(session_id,symbol,funding_time));
              CREATE TABLE IF NOT EXISTS smc_processed_candles(
                session_id TEXT NOT NULL, symbol TEXT NOT NULL, candle_time TEXT NOT NULL,
                PRIMARY KEY(session_id,symbol,candle_time));
              CREATE TABLE IF NOT EXISTS smc_evaluations(
                correlation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, candle_time TEXT NOT NULL,
                symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
                model_id TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
                missing_conditions_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(session_id,idempotency_key));
              CREATE TABLE IF NOT EXISTS smc_journal_revisions(
                id TEXT PRIMARY KEY, journal_id TEXT NOT NULL, session_id TEXT NOT NULL,
                note TEXT NOT NULL, created_at TEXT NOT NULL);
              -- The same one-session-newest-first reads state() makes, and the
              -- same missing indexes that turned the Price Action lab's status
              -- route into six full table scans per poll until it timed out.
              -- SMC has not hit that wall only because its history is younger.
              -- It matters doubly here: the point of running both labs is to
              -- compare them, and a lab whose status route is slower than the
              -- other's is not being compared on equal terms.
              CREATE INDEX IF NOT EXISTS smc_activity_session_created
                ON smc_activity(session_id, created_at DESC);
              CREATE INDEX IF NOT EXISTS smc_candidates_session_created
                ON smc_candidates(session_id, created_at DESC);
              CREATE INDEX IF NOT EXISTS smc_order_meta_session_created
                ON smc_order_meta(session_id, created_at DESC);
              CREATE INDEX IF NOT EXISTS smc_funding_session_time
                ON smc_funding_events(session_id, funding_time DESC);
              CREATE INDEX IF NOT EXISTS smc_evaluations_session_candle
                ON smc_evaluations(session_id, candle_time DESC);
            """)
            self._db.execute("INSERT OR IGNORE INTO smc_settings(id,leverage) VALUES (1,1)")
            active_session = self._db.execute(
                "SELECT 1 FROM smc_sessions WHERE status='active' LIMIT 1").fetchone()
            broker_exposure = bool(self.broker.positions()) or any(
                row.get("status") in OPEN_STATUSES for row in self.broker.orders())
            if not active_session and not broker_exposure:
                self._insert_session(starting_balance=self.starting_balance)
            self.broker.leverage = float(self._db.execute(
                "SELECT leverage FROM smc_settings WHERE id=1").fetchone()[0])
            self._snapshot()
        if self.session():
            self.reconcile_orders()

    def _insert_session(self, *, starting_balance: float, mode: str = "LIVE_PAPER",
                        symbol: str = "BTCUSDT", timeframe: str = "5m",
                        config: SMCPaperConfig | None = None) -> str:
        config = (config or SMCPaperConfig()).validated()
        sid, now = uuid.uuid4().hex, _iso()
        self._db.execute(
            "INSERT INTO smc_sessions(id,started_at,starting_balance,status,mode,symbol,timeframe,operating_mode,model_id,risk_pct,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, now, float(starting_balance), "active", mode, symbol, timeframe,
             config.operating_mode, config.model_id, config.risk_pct, now),
        )
        return sid

    def session(self) -> dict:
        row = self._db.execute(
            "SELECT * FROM smc_sessions WHERE status='active' ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(row) if row else {}

    @staticmethod
    def _decoded(row: dict) -> dict:
        for key in ("state_json", "metrics_json"):
            row[key.removesuffix("_json")] = json.loads(row.pop(key, "{}") or "{}")
        return row

    def sessions(self) -> list[dict]:
        return [self._decoded(dict(row)) for row in self._db.execute(
            "SELECT * FROM smc_sessions ORDER BY started_at DESC")]

    def _audit(self, kind: str, *, object_id: str = "", payload: dict | None = None,
               session_id: str | None = None) -> None:
        current = self.session()
        sid = session_id or current.get("id")
        if not sid:
            return
        self._db.execute(
            "INSERT INTO smc_activity VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, sid, kind, current.get("symbol", ""), current.get("model_id", ""),
             object_id, _iso(), json.dumps(payload or {}, sort_keys=True, default=str)),
        )

    def _snapshot(self, metrics: dict | None = None) -> None:
        current = self.session()
        if not current:
            return
        self._db.execute(
            "UPDATE smc_sessions SET state_json=?,metrics_json=COALESCE(?,metrics_json),updated_at=? WHERE id=?",
            (json.dumps(self.broker.export_state(), sort_keys=True),
             json.dumps(metrics, sort_keys=True, default=str) if metrics is not None else None,
             _iso(), current["id"]),
        )

    def state(self, marks: dict[str, float] | None = None) -> dict:
        current = self.session()
        sid = current.get("id", "")
        account = self.broker.account(marks, persist_metrics=False)
        positions = self._positions_with_protection()
        open_risk = 0.0
        for position in positions:
            stop = position.get("stop_loss")
            if stop is not None:
                open_risk += abs(float(position["entry_price"]) - float(stop)) * float(position["size"])
        activity = [{**dict(row), "payload": json.loads(row["payload"])} for row in self._db.execute(
            "SELECT * FROM smc_activity WHERE session_id=? ORDER BY created_at DESC LIMIT 1000", (sid,))]
        metadata = [{**dict(row), "config": json.loads(row["config_json"])} for row in self._db.execute(
            "SELECT * FROM smc_order_meta WHERE session_id=? ORDER BY created_at DESC", (sid,))]
        candidates = [{**dict(row), "payload": json.loads(row["payload"])} for row in self._db.execute(
            "SELECT * FROM smc_candidates WHERE session_id=? ORDER BY created_at DESC", (sid,))]
        funding = [dict(row) for row in self._db.execute(
            "SELECT * FROM smc_funding_events WHERE session_id=? ORDER BY funding_time DESC", (sid,))]
        evaluations = [{**dict(row),
                        "missing_conditions": json.loads(row["missing_conditions_json"]),
                        "payload": json.loads(row["payload_json"])}
                       for row in self._db.execute(
            "SELECT * FROM smc_evaluations WHERE session_id=? ORDER BY candle_time DESC LIMIT 500", (sid,))]
        return {
            "research_id": STRATEGY_ID, "strategy_version": STRATEGY_VERSION,
            "account_scope": "SMC_STRATEGY_LAB_ONLY", "currency": "USDT",
            "paper_only": True, "execution_mode": "PAPER", "real_execution_allowed": False,
            "session": self._decoded(dict(current)) if current else {},
            "account": {**account, "available_margin": account["free_margin"], "open_risk": round(open_risk, 8)},
            "positions": positions, "orders": self.broker.orders(), "trades": self.broker.fills(limit=1000),
            "candidates": candidates, "order_metadata": metadata, "activity": activity,
            "funding_events": funding, "evaluations": evaluations,
        }

    def record_evaluation(self, evaluation: dict, *, candle_time: str,
                          feed_status: dict | None = None) -> dict:
        """Persist one source-strategy decision for one confirmed closed candle."""
        current = self.session()
        if not current:
            raise ValueError("no active SMC paper session")
        if not candle_time:
            raise ValueError("SMC evaluation requires a confirmed closed-candle timestamp")
        corr = make_correlation_id(
            lab="SMC", session_id=current["id"], strategy_id=current["model_id"],
            symbol=current["symbol"], timeframe=current["timeframe"], candle_time=candle_time)
        idempotency = decision_idempotency_key(
            strategy_id=current["model_id"], symbol=current["symbol"],
            timeframe=current["timeframe"], candle_time=candle_time)
        existing = self._db.execute(
            "SELECT * FROM smc_evaluations WHERE session_id=? AND idempotency_key=?",
            (current["id"], idempotency)).fetchone()
        if existing:
            return dict(existing)
        signal_found = bool(evaluation.get("state") == "ENTRY_READY" and
                            evaluation.get("proposal") and evaluation.get("trade_plan"))
        state = "SIGNAL_FOUND" if signal_found else "WATCHING"
        reason = ("native closed-candle proposal found" if signal_found else
                  evaluation.get("next_required_event") or "no eligible setup on this closed candle")
        missing = list(evaluation.get("missing_conditions") or [])
        payload = {
            "source_evaluation": evaluation,
            "mtf_evidence": evaluation.get("mtf_evidence") or {},
            "feed_state": (feed_status or {}).get("state"),
            "feed_reason": (feed_status or {}).get("health_reason"),
            "saved_configuration": {
                "operating_mode": current["operating_mode"], "model_id": current["model_id"],
                "risk_pct": current["risk_pct"], "symbol": current["symbol"],
                "timeframe": current["timeframe"],
            },
        }
        now = _iso()
        self._db.execute(
            "INSERT INTO smc_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (corr, current["id"], idempotency, candle_time, current["symbol"],
             current["timeframe"], STRATEGY_ID, STRATEGY_VERSION, current["model_id"],
             state, str(reason), json_text(missing), json_text(payload), now, now))
        self._audit("closed_candle_decision", object_id=corr, payload={
            "correlation_id": corr, "idempotency_key": idempotency,
            "state": state, "reason": reason, "missing_conditions": missing,
            "candle_time": candle_time,
        })
        return dict(self._db.execute(
            "SELECT * FROM smc_evaluations WHERE correlation_id=?", (corr,)).fetchone())

    def _advance_evaluation(self, correlation_id: str | None, state: str,
                            reason: str, **evidence) -> None:
        if not correlation_id:
            return
        row = self._db.execute(
            "SELECT payload_json FROM smc_evaluations WHERE correlation_id=?", (correlation_id,)).fetchone()
        if not row:
            return
        payload = json.loads(row["payload_json"] or "{}")
        payload.setdefault("lifecycle", []).append({"state": state, "reason": reason,
                                                     "at": _iso(), **evidence})
        self._db.execute(
            "UPDATE smc_evaluations SET state=?,reason=?,payload_json=?,updated_at=? WHERE correlation_id=?",
            (state, reason, json_text(payload), _iso(), correlation_id))

    def _positions_with_protection(self) -> list[dict]:
        current = self.session()
        metadata = [dict(row) for row in self._db.execute(
            "SELECT * FROM smc_order_meta WHERE session_id=? AND ownership!='strategy_target_1' AND status IN ('PARTIALLY_FILLED','ENTERED') ORDER BY created_at",
            (current.get("id", ""),),
        )]
        rows = []
        for position in self.broker.positions():
            owners = []
            for meta in metadata:
                try:
                    order = self.broker.order(meta["order_id"])
                except KeyError:
                    continue
                if order.get("symbol") == position.get("symbol") and order.get("status") in {"partially_filled", "filled"}:
                    owners.append((meta, order, json.loads(meta["config_json"] or "{}")))
            planned_values = [value for _meta, _order, config in owners
                              if (value := _configured_r(config, "target_2")) is not None]
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

    def configure(self, *, mode: str | None = None, symbol: str | None = None,
                  timeframe: str | None = None, replay_cursor: int | None = None,
                  config: SMCPaperConfig | None = None) -> dict:
        current = self.session()
        if not current:
            raise ValueError("no active SMC paper session")
        config = (config or SMCPaperConfig(
            operating_mode=current["operating_mode"], model_id=current["model_id"],
            risk_pct=float(current["risk_pct"]))).validated()
        values = {
            "mode": mode or current["mode"], "symbol": normalize_symbol(symbol or current["symbol"]),
            "timeframe": (timeframe or current["timeframe"]).lower(),
            "replay_cursor": int(replay_cursor if replay_cursor is not None else current["replay_cursor"]),
        }
        if values["mode"] not in SESSION_MODES:
            raise ValueError("SMC session mode must be LIVE_PAPER or HISTORICAL")
        if values["timeframe"] not in TF_MS:
            raise ValueError("unsupported SMC session timeframe")
        if (values["mode"], values["symbol"], values["timeframe"]) != (
                current["mode"], current["symbol"], current["timeframe"]):
            if self.broker.positions() or any(row["status"] in OPEN_STATUSES for row in self.broker.orders()):
                raise ValueError("cannot change SMC market, timeframe or mode with an open position or pending order")
        self._db.execute(
            "UPDATE smc_sessions SET mode=?,symbol=?,timeframe=?,replay_cursor=?,operating_mode=?,model_id=?,risk_pct=?,updated_at=? WHERE id=?",
            (values["mode"], values["symbol"], values["timeframe"], values["replay_cursor"],
             config.operating_mode, config.model_id, config.risk_pct, _iso(), current["id"]),
        )
        self._audit("session_configuration_changed", payload={**values, **asdict(config)})
        self._snapshot()
        return self.state()

    def start(self, *, mode: str = "LIVE_PAPER", symbol: str = "BTCUSDT", timeframe: str = "5m",
              starting_balance: float | None = None, config: SMCPaperConfig | None = None) -> dict:
        amount = float(starting_balance or self.starting_balance)
        if amount <= 0:
            raise ValueError("starting balance must be positive")
        config = (config or SMCPaperConfig()).validated()
        if mode not in SESSION_MODES or timeframe not in TF_MS:
            raise ValueError("invalid SMC session mode or timeframe")
        with self._lock, self._db:
            prior = self.session()
            if prior:
                self._snapshot()
                self._db.execute("UPDATE smc_sessions SET status='ended',ended_at=?,end_reason='new_session',updated_at=? WHERE id=?",
                                 (_iso(), _iso(), prior["id"]))
            self.broker.factory_reset(amount)
            sid = self._insert_session(starting_balance=amount, mode=mode,
                                       symbol=normalize_symbol(symbol), timeframe=timeframe, config=config)
            self._audit("session_started", object_id=sid, payload={"mode": mode, "starting_balance": amount})
            self._snapshot()
        return self.state()

    def end(self, reason: str = "user_end") -> dict:
        current = self.session()
        if not current:
            raise ValueError("no active SMC paper session")
        self._snapshot()
        self._audit("session_ended", payload={"reason": reason})
        self._db.execute("UPDATE smc_sessions SET status='ended',ended_at=?,end_reason=?,updated_at=? WHERE id=?",
                         (_iso(), reason, _iso(), current["id"]))
        return self._decoded(dict(self._db.execute("SELECT * FROM smc_sessions WHERE id=?", (current["id"],)).fetchone()))

    def resume(self, session_id: str) -> dict:
        target = self._db.execute("SELECT * FROM smc_sessions WHERE id=?", (session_id,)).fetchone()
        if not target:
            raise KeyError(session_id)
        snapshot = json.loads(target["state_json"] or "{}")
        if not snapshot:
            raise ValueError("SMC session has no resumable account snapshot")
        current = self.session()
        if current and current["id"] != session_id:
            self._snapshot()
            self._db.execute("UPDATE smc_sessions SET status='paused',updated_at=? WHERE id=?", (_iso(), current["id"]))
        self.broker.restore_state(snapshot)
        self._db.execute("UPDATE smc_sessions SET status='active',ended_at=NULL,end_reason=NULL,updated_at=? WHERE id=?",
                         (_iso(), session_id))
        self._audit("session_resumed", object_id=session_id)
        return self.state()

    def duplicate(self, session_id: str) -> dict:
        row = self._db.execute("SELECT * FROM smc_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise KeyError(session_id)
        config = SMCPaperConfig(operating_mode=row["operating_mode"], model_id=row["model_id"], risk_pct=row["risk_pct"])
        result = self.start(mode=row["mode"], symbol=row["symbol"], timeframe=row["timeframe"],
                            starting_balance=row["starting_balance"], config=config)
        self._audit("session_duplicated", payload={"source_session_id": session_id})
        return result

    def reset(self, confirmation: str) -> dict:
        if confirmation != "RESET SMC PAPER":
            raise ValueError("confirmation must exactly match RESET SMC PAPER")
        current = self.session()
        config = SMCPaperConfig(operating_mode=current.get("operating_mode", "signals_only"),
                                model_id=current.get("model_id", "SMC_M1_SWEEP_REVERSAL"),
                                risk_pct=float(current.get("risk_pct", 0.5)))
        previous_id = current.get("id")
        result = self.start(mode=current.get("mode", "LIVE_PAPER"), symbol=current.get("symbol", "BTCUSDT"),
                            timeframe=current.get("timeframe", "5m"),
                            starting_balance=current.get("starting_balance", self.starting_balance), config=config)
        self._db.execute("UPDATE smc_sessions SET end_reason='paper_reset' WHERE id=?", (previous_id,))
        self._audit("paper_account_reset", payload={"previous_session_id": previous_id,
                                                     "confirmation": "verified"})
        return result

    def factory_reset(self) -> dict:
        """Clear all SMC operational data for the global protected reset."""
        with self._lock, self._db:
            self.broker.factory_reset(self.starting_balance)
            for table in ("smc_journal_revisions", "smc_evaluations", "smc_processed_candles", "smc_funding_events", "smc_order_meta",
                          "smc_candidates", "smc_activity", "smc_sessions"):
                self._db.execute(f"DELETE FROM {table}")
            self._db.execute("UPDATE smc_settings SET leverage=1 WHERE id=1")
            self.broker.leverage = 1.0
            self._insert_session(starting_balance=self.starting_balance)
            self._snapshot()
        return self.state()

    def set_leverage(self, leverage: float) -> dict:
        value = float(leverage)
        if not 1 <= value <= 10:
            raise ValueError("SMC paper leverage must be between 1x and 10x")
        prior = self.broker.leverage
        self.broker.leverage = value
        self._db.execute("UPDATE smc_settings SET leverage=? WHERE id=1", (value,))
        self._audit("paper_leverage_changed", payload={"previous": prior, "new": value,
                                                        "existing_positions_resized": False})
        self._snapshot()
        return self.state()

    @staticmethod
    def _multiple(value: float, step: float) -> bool:
        return step <= 0 or abs(value / step - round(value / step)) <= 1e-7

    @staticmethod
    def _rounded_down(value: float, step: float) -> float:
        return value if step <= 0 else math.floor(value / step + 1e-12) * step

    def submit_order(self, *, symbol: str, side: str, order_type: str, rules: dict,
                     reference_price: float, quantity: float | None = None,
                     risk_pct: float | None = None, limit_price: float | None = None,
                     trigger_price: float | None = None, stop_loss: float | None = None,
                     target_1: float | None = None,
                     target_2: float | None = None, idempotency_key: str,
                     ownership: str = "manual", proposal_id: str | None = None,
                     setup_id: str | None = None, poi_id: str | None = None,
                     model_id: str | None = None, creation_candle: str | None = None,
                     decision_timestamp: str | None = None,
                     expiry_candle: str | None = None,
                     correlation_id: str | None = None) -> dict:
        current = self.session()
        if not current:
            raise ValueError("no active SMC paper session")
        symbol = normalize_symbol(symbol)
        if symbol != current["symbol"]:
            raise ValueError("order symbol must match the active SMC session")
        if not idempotency_key.strip():
            raise ValueError("an idempotency key is required")
        existing = self._db.execute(
            "SELECT order_id FROM smc_order_meta WHERE session_id=? AND idempotency_key=?",
            (current["id"], idempotency_key)).fetchone()
        if existing:
            return {"accepted": True, "duplicate": True, "order": self.broker.order(existing["order_id"]),
                    "real_execution_allowed": False}
        open_entries = [row for row in self.broker.orders()
                        if row.get("status") in OPEN_STATUSES and not row.get("reduce_only")]
        if self.broker.positions() or open_entries:
            raise ValueError(
                "another SMC entry or protected position is already active; same-symbol stacking is blocked "
                "so stop, targets and R:R ownership cannot be overwritten"
            )
        side = "buy" if side.lower() in {"buy", "long", "bullish"} else "sell" if side.lower() in {"sell", "short", "bearish"} else ""
        if not side:
            raise ValueError("side must be buy/long or sell/short")
        entry = float(limit_price or (trigger_price if order_type in {"stop", "stop_limit"} else reference_price))
        if entry <= 0:
            raise ValueError("a positive server reference price is required")
        protective_stop = stop_loss
        if risk_pct is not None:
            if protective_stop is None:
                raise ValueError("risk-based sizing requires a protective stop")
            if not 0 < float(risk_pct) <= 1:
                raise ValueError("risk percentage must be above 0% and no greater than 1%")
            if abs(entry - float(protective_stop)) <= 1e-12:
                raise ValueError("risk-based sizing requires a positive entry-to-stop distance")
            risk_amount = self.broker.account()["equity"] * float(risk_pct) / 100
            quantity = self._rounded_down(risk_amount / abs(entry - float(protective_stop)), rules["quantity_step"])
        if quantity is None or quantity <= 0:
            raise ValueError("quantity must be positive")
        quantity = float(quantity)
        if not self._multiple(quantity, float(rules["quantity_step"])):
            raise ValueError(f"quantity must follow Binance step size {rules['quantity_step']}")
        if quantity < float(rules["min_quantity"]) or (rules.get("max_quantity") and quantity > float(rules["max_quantity"])):
            raise ValueError("quantity is outside Binance contract limits")
        if quantity * entry < float(rules["min_notional"]):
            raise ValueError(f"order notional must be at least {rules['min_notional']} USDT")
        for value in (limit_price, trigger_price, stop_loss, target_1, target_2):
            if value is not None and not self._multiple(float(value), float(rules["tick_size"])):
                raise ValueError(f"price must follow Binance tick size {rules['tick_size']}")
        if protective_stop is not None:
            if side == "buy" and not float(protective_stop) < entry:
                raise ValueError("a long protective stop must be below entry")
            if side == "sell" and not float(protective_stop) > entry:
                raise ValueError("a short protective stop must be above entry")
        for label, target in (("T1", target_1), ("T2", target_2)):
            if target is not None and ((side == "buy" and float(target) <= entry) or
                                       (side == "sell" and float(target) >= entry)):
                raise ValueError(f"{label} must be in the profitable direction")
        if target_1 is not None and target_2 is not None and (
                (side == "buy" and target_2 <= target_1) or (side == "sell" and target_2 >= target_1)):
            raise ValueError("T2 must be farther than T1")
        if protective_stop is None or target_1 is None or target_2 is None:
            raise ValueError("SMC paper entry orders require a protective stop, target 1 and target 2")
        risk_distance = abs(entry - float(protective_stop)) if protective_stop is not None else None
        target_1_r = (abs(float(target_1) - entry) / risk_distance
                      if target_1 is not None and risk_distance else None)
        target_2_r = (abs(float(target_2) - entry) / risk_distance
                      if target_2 is not None and risk_distance else None)
        order = self.broker.submit(symbol=symbol, side=side, order_type=order_type,
                                   quantity=quantity, limit_price=limit_price,
                                   stop_price=trigger_price if order_type in {"stop", "stop_limit"} else None,
                                   reduce_only=False, market_open=True,
                                   protection_stop_loss=protective_stop,
                                   protection_take_profit=target_2,
                                   protection_target_r=target_2_r,
                                   protection_tick_size=float(rules.get("tick_size") or 0) or None,
                                   signal_timestamp=creation_candle or _iso(),
                                   decision_timestamp=decision_timestamp or creation_candle or _iso(),
                                   signal_price=reference_price, requested_price=entry,
                                   strategy=model_id or STRATEGY_ID,
                                   strategy_version=STRATEGY_VERSION,
                                   timeframe=current.get("timeframe") or "",
                                   market_data_source="Binance USD-M public WebSocket",
                                   candle_id=idempotency_key)
        now = _iso()
        config = {"reference_price": entry, "stop_loss": protective_stop,
                  "target_1": target_1, "target_2": target_2,
                  "target_1_r": target_1_r, "target_2_r": target_2_r, "rules": rules,
                  "correlation_id": correlation_id, "idempotency_key": idempotency_key,
                  "execution_mode": "PAPER", "live_execution_allowed": False}
        self._db.execute(
            "INSERT INTO smc_order_meta(order_id,session_id,ownership,idempotency_key,proposal_id,setup_id,poi_id,model_id,model_version,direction,entry,stop,target_1,target_2,risk_pct,creation_candle,expiry_candle,status,reason,config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order["id"], current["id"], ownership, idempotency_key, proposal_id, setup_id, poi_id,
             model_id, STRATEGY_VERSION if model_id else None, "bullish" if side == "buy" else "bearish",
             entry, protective_stop, target_1, target_2, risk_pct, creation_candle, expiry_candle,
             "ORDER_PENDING", "accepted by isolated SMC paper broker", json.dumps(config, sort_keys=True), now, now),
        )
        self._audit("paper_order_created", object_id=order["id"], payload={"ownership": ownership,
                    "quantity": quantity, "entry": entry, "stop": protective_stop,
                    "target_1": target_1, "target_2": target_2,
                    "correlation_id": correlation_id, "idempotency_key": idempotency_key})
        self._snapshot()
        return {"accepted": True, "duplicate": False, "order": order, "paper_only": True,
                "real_execution_allowed": False}

    def cancel_order(self, order_id: str) -> dict:
        meta = self._db.execute("SELECT * FROM smc_order_meta WHERE order_id=?", (order_id,)).fetchone()
        if not meta or meta["session_id"] != self.session().get("id"):
            raise KeyError(order_id)
        order = self.broker.cancel(order_id)
        self._db.execute("UPDATE smc_order_meta SET status='CANCELLED',reason='user cancelled',updated_at=? WHERE order_id=?",
                         (_iso(), order_id))
        self._audit("paper_order_cancelled", object_id=order_id)
        self._snapshot()
        return {"order": order, "real_execution_allowed": False}

    def reconcile_orders(self) -> dict:
        """Reconcile statuses and fail closed around existing exposure.

        Unfilled same-symbol entries, including manual entries, are cancelled
        when a position already exists because the broker supports one
        position per symbol and cannot preserve independent protection for a
        second owner. Filled records and their audit history are retained.
        """
        current = self.session()
        sid = current.get("id", "")
        broker_orders = {row["id"]: row for row in self.broker.orders()}
        actions = []
        metadata = [dict(row) for row in self._db.execute(
            "SELECT * FROM smc_order_meta WHERE session_id=?", (sid,)).fetchall()]
        metadata_by_order = {row["order_id"]: row for row in metadata}
        manual_orders_cancelled = 0
        for meta in metadata:
            order = broker_orders.get(meta["order_id"])
            if not order:
                actions.append({"order_id": meta["order_id"], "action": "flagged_missing",
                                "ownership": meta["ownership"]})
                continue
            mapped = {"open": "ORDER_PENDING", "triggered": "ORDER_PENDING",
                      "partially_filled": "PARTIALLY_FILLED", "filled": "ENTERED",
                      "cancelled": "CANCELLED", "rejected": "REJECTED"}.get(order["status"], order["status"].upper())
            # A completed strategy owner remains terminal even though its
            # original entry order is (correctly) still filled in the broker.
            if meta["status"] == "COMPLETED" and order["status"] == "filled":
                mapped = "COMPLETED"
            # Preserve the strategy-level reason for a broker cancellation;
            # reconciliation must not erase whether it expired, was paused by
            # data health, or was cancelled by the adverse-first policy.
            if meta["status"] in {"EXPIRED", "DATA_PAUSED", "CANCELLED_AMBIGUOUS"} and \
                    order["status"] == "cancelled":
                mapped = meta["status"]
            if mapped != meta["status"]:
                self._db.execute("UPDATE smc_order_meta SET status=?,reason=?,updated_at=? WHERE order_id=?",
                                 (mapped, f"reconciled from broker status {order['status']}", _iso(), meta["order_id"]))
                actions.append({"order_id": meta["order_id"], "action": "status_reconciled",
                                "from": meta["status"], "to": mapped, "ownership": meta["ownership"]})
        strategy_owners = [dict(row) for row in self._db.execute(
            "SELECT * FROM smc_order_meta WHERE session_id=? AND ownership!='strategy_target_1' AND status IN ('PARTIALLY_FILLED','ENTERED') ORDER BY created_at",
            (sid,),
        )]
        for position in self.broker.positions():
            for pending in self.broker.orders():
                if pending.get("symbol") != position.get("symbol") or pending.get("reduce_only") or \
                        pending.get("status") not in OPEN_STATUSES or float(pending.get("filled") or 0) > 0:
                    continue
                self.broker.cancel(pending["id"])
                self._db.execute(
                    "UPDATE smc_order_meta SET status='CANCELLED',reason=?,updated_at=? WHERE order_id=?",
                    ("cancelled during legacy exposure reconciliation; same-symbol stacking is unsupported",
                     _iso(), pending["id"]),
                )
                action = {"order_id": pending["id"], "action": "cancelled_pending_entry_during_position_repair",
                          "symbol": position["symbol"]}
                owner = metadata_by_order.get(pending["id"])
                if owner is None or owner.get("ownership") == "manual":
                    manual_orders_cancelled += 1
                actions.append(action)
                self._audit("paper_order_reconciled", object_id=pending["id"], payload=action)
            owners = []
            for meta in strategy_owners:
                order = broker_orders.get(meta["order_id"])
                if order and order.get("symbol") == position.get("symbol") and order.get("status") in {"partially_filled", "filled"}:
                    owners.append((meta, order, json.loads(meta.get("config_json") or "{}")))
            if not owners:
                continue
            try:
                candidates = []
                for _meta, _order, config in owners:
                    stop_value = float(config["stop_loss"])
                    ratio_value = _configured_r(config, "target_2") or _configured_r(config, "target_1")
                    if ratio_value is None or (position["side"] == "long" and stop_value >= position["entry_price"]) or \
                            (position["side"] == "short" and stop_value <= position["entry_price"]):
                        continue
                    candidates.append((stop_value, float(ratio_value),
                                       (config.get("rules") or {}).get("tick_size")))
                if not candidates:
                    continue
                stop_value = (max(row[0] for row in candidates) if position["side"] == "long"
                              else min(row[0] for row in candidates))
                planned_rr = min(row[1] for row in candidates)
                ticks = [float(row[2]) for row in candidates if row[2]]
                stop, target = self.broker._resolved_protection(position, {
                    "stop_loss": stop_value, "take_profit": None,
                    "target_r": planned_rr, "tick_size": max(ticks) if ticks else None,
                })
            except (KeyError, TypeError, ValueError):
                continue
            for meta, _order, config in owners:
                try:
                    self.broker.set_order_protection(
                        meta["order_id"], stop_loss=float(config["stop_loss"]),
                        take_profit=float(config.get("target_2") or config["target_1"]),
                        target_r=float(_configured_r(config, "target_2") or _configured_r(config, "target_1")),
                        tick_size=(config.get("rules") or {}).get("tick_size"),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            stop_changed = position.get("stop_loss") is None or abs(float(position["stop_loss"]) - float(stop)) > 1e-9
            target_changed = position.get("take_profit") is None or abs(float(position["take_profit"]) - float(target)) > 1e-9
            if not stop_changed and not target_changed:
                continue
            repaired = self.broker.set_protection(
                position["symbol"], stop_loss=stop if stop_changed else None,
                take_profit=target if target_changed else None,
            )
            action = {
                "order_id": owners[-1][0]["order_id"],
                "owner_order_ids": [row[0]["order_id"] for row in owners],
                "ownership_resolution": "single" if len(owners) == 1 else "legacy_aggregate_conservative",
                "action": "restored_strategy_protection",
                "stop_loss": repaired.get("stop_loss"), "take_profit": repaired.get("take_profit"),
                "planned_rr": planned_rr,
                "source": "immutable_smc_order_configuration_and_actual_fill",
            }
            actions.append(action)
            self._audit("paper_position_protection_repaired", object_id=owners[-1][0]["order_id"], payload=action)
        if actions:
            self._audit("paper_orders_reconciled", payload={
                "actions": actions, "manual_orders_cancelled": manual_orders_cancelled,
            })
            self._snapshot()
        return {"actions": actions, "manual_orders_cancelled": manual_orders_cancelled,
                "records_deleted": 0,
                "real_execution_allowed": False}

    @staticmethod
    def _candle_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _cancel_stale_staged_entries(self, candle_time: str, *, feed_reliable: bool) -> list[dict]:
        """Cancel unfilled strategy entries before they can fill from stale evidence."""
        current = self.session()
        observed = self._candle_datetime(candle_time)
        if not current:
            return []
        actions: list[dict] = []
        rows = self._db.execute(
            "SELECT * FROM smc_order_meta WHERE session_id=? AND ownership='strategy' "
            "AND status='ORDER_PENDING' ORDER BY created_at",
            (current["id"],),
        ).fetchall()
        for row in rows:
            try:
                order = self.broker.order(row["order_id"])
            except KeyError:
                continue
            if order.get("status") not in OPEN_STATUSES or float(order.get("filled") or 0) > 0:
                continue
            expiry = self._candle_datetime(row["expiry_candle"])
            expired = bool(observed and expiry and observed > expiry)
            if feed_reliable and not expired:
                continue
            status = "EXPIRED" if expired else "DATA_PAUSED"
            reason = (
                "staged SMC entry expired before the next verified closed candle"
                if expired else
                "market data became unreliable before the staged SMC entry could activate"
            )
            try:
                self.broker.cancel(row["order_id"])
            except ValueError:
                continue
            now = _iso()
            self._db.execute(
                "UPDATE smc_order_meta SET status=?,reason=?,updated_at=? WHERE order_id=?",
                (status, reason, now, row["order_id"]),
            )
            self._db.execute(
                "UPDATE smc_candidates SET status=?,reason=?,updated_at=? "
                "WHERE session_id=? AND proposal_id=?",
                (status, reason, now, current["id"], row["proposal_id"]),
            )
            config = json.loads(row["config_json"] or "{}")
            self._advance_evaluation(config.get("correlation_id"), "RISK_REJECTED", reason,
                                     order_id=row["order_id"])
            action = {"order_id": row["order_id"], "to": status, "reason": reason,
                      "observed_candle": candle_time, "expiry_candle": row["expiry_candle"]}
            actions.append(action)
            self._audit("paper_order_reconciled", object_id=row["order_id"], payload=action)
        if actions:
            self._snapshot()
        return actions

    def process_candle(self, symbol: str, candle, *, allow_candle_fills: bool = True) -> dict:
        current = self.session()
        if not current:
            raise ValueError("no active SMC paper session")
        timestamp = getattr(candle, "timestamp", None)
        if timestamp is None and isinstance(candle, dict):
            timestamp = candle.get("timestamp")
        candle_time = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp or "")
        if not candle_time:
            raise ValueError("a timestamped closed candle is required")
        expired_entries = self._cancel_stale_staged_entries(candle_time, feed_reliable=True)
        key = (current["id"], normalize_symbol(symbol), candle_time)
        if self._db.execute("SELECT 1 FROM smc_processed_candles WHERE session_id=? AND symbol=? AND candle_time=?", key).fetchone():
            return {"duplicate": True, "events": [], "real_execution_allowed": False}
        if isinstance(candle, dict):
            candle_values = candle
        else:
            candle_values = {key: getattr(candle, key) for key in ("open", "high", "low", "close", "volume")}
        # If a stop and T1 are both touched in one OHLC candle, chronology is
        # unknowable. Cancel the scale-out instruction so the broker's adverse
        # protective stop handles the whole remaining position first.
        position = next((row for row in self.broker.positions() if row["symbol"] == key[1]), None)
        if position and position.get("stop_loss") is not None:
            stop_hit = (position["side"] == "long" and float(candle_values["low"]) <= position["stop_loss"]) or \
                       (position["side"] == "short" and float(candle_values["high"]) >= position["stop_loss"])
            if stop_hit:
                for meta in self._db.execute(
                        "SELECT * FROM smc_order_meta WHERE session_id=? AND ownership='strategy_target_1' AND status='ORDER_PENDING'",
                        (current["id"],)).fetchall():
                    config = json.loads(meta["config_json"] or "{}")
                    target = config.get("target_1")
                    target_hit = target is not None and ((position["side"] == "long" and float(candle_values["high"]) >= target) or
                                                         (position["side"] == "short" and float(candle_values["low"]) <= target))
                    if target_hit:
                        self.broker.cancel(meta["order_id"])
                        self._db.execute("UPDATE smc_order_meta SET status='CANCELLED_AMBIGUOUS',reason=?,updated_at=? WHERE order_id=?",
                                         ("stop and T1 touched in one candle; conservative stop-first policy", _iso(), meta["order_id"]))
                        self._audit("intrabar_ambiguity_stop_first", object_id=meta["order_id"],
                                    payload={"stop": position["stop_loss"], "target_1": target,
                                             "candle_time": candle_time})
        protections = {}
        for row in self._db.execute(
                "SELECT order_id,config_json FROM smc_order_meta WHERE session_id=? AND status IN ('ORDER_PENDING','PARTIALLY_FILLED')",
                (current["id"],)).fetchall():
            config = json.loads(row["config_json"] or "{}")
            protections[row["order_id"]] = {
                "stop_loss": config.get("stop_loss"),
                "take_profit": config.get("target_2") or config.get("target_1"),
                "target_r": _configured_r(config, "target_2") or _configured_r(config, "target_1"),
                "tick_size": (config.get("rules") or {}).get("tick_size"),
            }
        result = (self.broker.process_candle(symbol, candle, protections=protections)
                  if allow_candle_fills else {
                      "symbol": normalize_symbol(symbol), "events": [],
                      "account": self.broker.account(), "paused": True,
                      "reason": "waiting for the next public quote after decision",
                  })
        self._db.execute("INSERT INTO smc_processed_candles VALUES (?,?,?)", key)
        for event in result["events"]:
            parent = self._db.execute("SELECT * FROM smc_order_meta WHERE order_id=?", (event.get("order_id"),)).fetchone()
            if not parent or parent["ownership"] != "strategy":
                continue
            config = json.loads(parent["config_json"] or "{}")
            self._advance_evaluation(
                config.get("correlation_id"), "FILLED",
                "SMC paper entry fill recorded", order_id=parent["order_id"], fill=event)
            self._advance_evaluation(
                config.get("correlation_id"), "POSITION_OPEN",
                "protected SMC paper entry filled", order_id=parent["order_id"], fill=event)
            target_1 = config.get("target_1")
            if target_1 is None or self._db.execute(
                    "SELECT 1 FROM smc_order_meta WHERE session_id=? AND idempotency_key=?",
                    (current["id"], f"target1:{parent['order_id']}")).fetchone():
                continue
            step = float(config.get("rules", {}).get("quantity_step", 0) or 0)
            quantity = self._rounded_down(float(event["quantity"]) * 0.5, step)
            if quantity <= 0:
                continue
            position = next((row for row in self.broker.positions()
                             if row["symbol"] == key[1]), None)
            target_1_r = _configured_r(config, "target_1")
            if position and config.get("stop_loss") is not None and target_1_r is not None:
                _stop, target_1 = self.broker._resolved_protection(position, {
                    "stop_loss": config["stop_loss"], "take_profit": target_1,
                    "target_r": target_1_r,
                    "tick_size": (config.get("rules") or {}).get("tick_size"),
                })
            side = "sell" if parent["direction"] == "bullish" else "buy"
            child = self.broker.submit(symbol=key[1], side=side, order_type="limit",
                                       quantity=quantity, limit_price=float(target_1),
                                       reduce_only=True, market_open=True)
            now = _iso()
            child_config = {**config, "planned_target_1": config.get("target_1"),
                            "planned_target_2": config.get("target_2"),
                            "target_1": target_1,
                            "target_2": position.get("take_profit") if position else config.get("target_2"),
                            "actual_entry": position.get("entry_price") if position else None,
                            "parent_order_id": parent["order_id"], "scale_out_fraction": 0.5}
            self._db.execute(
                "INSERT INTO smc_order_meta(order_id,session_id,ownership,idempotency_key,proposal_id,setup_id,poi_id,model_id,model_version,direction,entry,stop,target_1,target_2,risk_pct,creation_candle,expiry_candle,status,reason,config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (child["id"], current["id"], "strategy_target_1", f"target1:{parent['order_id']}",
                 parent["proposal_id"], parent["setup_id"], parent["poi_id"], parent["model_id"],
                 parent["model_version"], parent["direction"], parent["entry"], parent["stop"],
                 target_1, child_config.get("target_2"), parent["risk_pct"], parent["creation_candle"],
                 parent["expiry_candle"], "ORDER_PENDING", "50% scale-out at deterministic T1",
                 json.dumps(child_config, sort_keys=True), now, now),
            )
            self._audit("paper_target_1_order_created", object_id=child["id"],
                        payload={"parent_order_id": parent["order_id"], "quantity": quantity,
                                 "target_1": target_1})
        self.reconcile_orders()
        for event in result["events"]:
            event_parent = self._db.execute(
                "SELECT config_json FROM smc_order_meta WHERE order_id=?", (event.get("order_id"),)).fetchone()
            event_config = json.loads(event_parent["config_json"] or "{}") if event_parent else {}
            if not event_parent and str(event.get("order_id") or "").startswith("protective-"):
                owner = self._db.execute(
                    "SELECT order_id,proposal_id,config_json FROM smc_order_meta "
                    "WHERE session_id=? AND ownership='strategy' "
                    "AND status='ENTERED' ORDER BY created_at DESC LIMIT 1",
                    (current["id"],)).fetchone()
                event_config = json.loads(owner["config_json"] or "{}") if owner else {}
                self._advance_evaluation(
                    event_config.get("correlation_id"), "EXITED",
                    "protective SMC paper exit completed", fill=event)
                if owner:
                    exit_ids = list(event_config.get("protective_exit_order_ids") or [])
                    if event["order_id"] not in exit_ids:
                        exit_ids.append(event["order_id"])
                    event_config["protective_exit_order_ids"] = exit_ids
                    self._db.execute(
                        "UPDATE smc_order_meta SET status='COMPLETED',reason=?,config_json=?,updated_at=? "
                        "WHERE order_id=?",
                        ("protective stop or deterministic target completed the SMC paper position",
                         json.dumps(event_config, sort_keys=True), _iso(), owner["order_id"]),
                    )
                    self._db.execute(
                        "UPDATE smc_candidates SET status='COMPLETED',reason=?,updated_at=? "
                        "WHERE session_id=? AND proposal_id=?",
                        ("protected SMC paper position exited", _iso(), current["id"],
                         owner["proposal_id"]),
                    )
            self._audit("paper_fill", object_id=event.get("order_id", ""),
                        payload={**event, "candle_time": candle_time,
                                 "correlation_id": event_config.get("correlation_id")})
        self._snapshot()
        return {**result, "duplicate": False, "expired_entries": expired_entries,
                "paper_only": True, "real_execution_allowed": False}

    def process_quote(self, symbol: str, quote: dict, *, feed_reliable: bool) -> dict:
        """Fill this SMC account only from a reconciled post-decision quote."""
        current = self.session()
        if not current:
            raise ValueError("no active SMC paper session")
        if not feed_reliable:
            return {"events": [], "paused": True,
                    "reason": "market data is not synchronized",
                    "real_execution_allowed": False}
        symbol = normalize_symbol(symbol)
        protections = {}
        for row in self._db.execute(
                "SELECT order_id,config_json FROM smc_order_meta WHERE session_id=? "
                "AND status IN ('ORDER_PENDING','PARTIALLY_FILLED')",
                (current["id"],)).fetchall():
            config = json.loads(row["config_json"] or "{}")
            protections[row["order_id"]] = {
                "stop_loss": config.get("stop_loss"),
                "take_profit": config.get("target_2") or config.get("target_1"),
                "target_r": (_configured_r(config, "target_2")
                             or _configured_r(config, "target_1")),
                "tick_size": (config.get("rules") or {}).get("tick_size"),
            }
        result = self.broker.process_tick(symbol, quote, protections=protections)
        for event in result["events"]:
            parent = self._db.execute(
                "SELECT * FROM smc_order_meta WHERE order_id=?",
                (event.get("order_id"),),
            ).fetchone()
            if parent and parent["ownership"] == "strategy":
                config = json.loads(parent["config_json"] or "{}")
                self._advance_evaluation(
                    config.get("correlation_id"), "FILLED",
                    "SMC paper entry filled from a strictly post-decision public quote",
                    order_id=parent["order_id"], fill=event,
                )
                self._advance_evaluation(
                    config.get("correlation_id"), "POSITION_OPEN",
                    "protected SMC paper entry filled",
                    order_id=parent["order_id"], fill=event,
                )
                target_1 = config.get("target_1")
                target_key = f"target1:{parent['order_id']}"
                exists = self._db.execute(
                    "SELECT 1 FROM smc_order_meta WHERE session_id=? AND idempotency_key=?",
                    (current["id"], target_key),
                ).fetchone()
                if target_1 is not None and not exists:
                    step = float((config.get("rules") or {}).get("quantity_step") or 0)
                    quantity = self._rounded_down(float(event["quantity"]) * 0.5, step)
                    position = next((row for row in self.broker.positions()
                                     if row["symbol"] == symbol), None)
                    target_1_r = _configured_r(config, "target_1")
                    if (quantity > 0 and position and config.get("stop_loss") is not None
                            and target_1_r is not None):
                        _stop, target_1 = self.broker._resolved_protection(position, {
                            "stop_loss": config["stop_loss"],
                            "take_profit": target_1, "target_r": target_1_r,
                            "tick_size": (config.get("rules") or {}).get("tick_size"),
                        })
                        side = "sell" if parent["direction"] == "bullish" else "buy"
                        child = self.broker.submit(
                            symbol=symbol, side=side, order_type="limit",
                            quantity=quantity, limit_price=float(target_1),
                            reduce_only=True, market_open=True,
                            signal_timestamp=event.get("signal_timestamp"),
                            decision_timestamp=event.get("fill_timestamp"),
                            signal_price=event.get("price"), requested_price=float(target_1),
                            strategy=parent["model_id"] or STRATEGY_ID,
                            strategy_version=STRATEGY_VERSION,
                            timeframe=current.get("timeframe") or "",
                            market_data_source="Binance USD-M public WebSocket",
                            candle_id=event.get("candle_id"),
                        )
                        now = _iso()
                        child_config = {
                            **config, "target_1": target_1,
                            "actual_entry": position.get("entry_price"),
                            "parent_order_id": parent["order_id"],
                            "scale_out_fraction": 0.5,
                        }
                        self._db.execute(
                            "INSERT INTO smc_order_meta(order_id,session_id,ownership,idempotency_key,proposal_id,setup_id,poi_id,model_id,model_version,direction,entry,stop,target_1,target_2,risk_pct,creation_candle,expiry_candle,status,reason,config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (child["id"], current["id"], "strategy_target_1", target_key,
                             parent["proposal_id"], parent["setup_id"], parent["poi_id"],
                             parent["model_id"], parent["model_version"], parent["direction"],
                             parent["entry"], parent["stop"], target_1,
                             position.get("take_profit"), parent["risk_pct"],
                             parent["creation_candle"], parent["expiry_candle"],
                             "ORDER_PENDING", "50% scale-out at deterministic T1",
                             json.dumps(child_config, sort_keys=True), now, now),
                        )
            elif not parent and str(event.get("order_id") or "").startswith("protective-"):
                owner = self._db.execute(
                    "SELECT order_id,proposal_id,config_json FROM smc_order_meta "
                    "WHERE session_id=? AND ownership='strategy' AND status='ENTERED' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (current["id"],),
                ).fetchone()
                if owner:
                    config = json.loads(owner["config_json"] or "{}")
                    self._advance_evaluation(
                        config.get("correlation_id"), "EXITED",
                        "protective SMC paper exit completed from public quote",
                        fill=event,
                    )
                    self._db.execute(
                        "UPDATE smc_order_meta SET status='COMPLETED',reason=?,updated_at=? "
                        "WHERE order_id=?",
                        ("protective stop or target completed the SMC paper position",
                         _iso(), owner["order_id"]),
                    )
            event_config = json.loads(parent["config_json"] or "{}") if parent else {}
            self._audit(
                "paper_fill", object_id=event.get("order_id", ""),
                payload={**event, "execution_quote": quote,
                         "correlation_id": event_config.get("correlation_id")},
            )
        self.reconcile_orders()
        if result["events"]:
            self._snapshot()
        return {**result, "paper_only": True, "real_execution_allowed": False}

    def synchronize_candidate(self, evaluation: dict, *, rules: dict,
                              reference_price: float, feed_reliable: bool,
                              closed_candle_time: str | None = None,
                              feed_status: dict | None = None) -> dict:
        """Journal one deterministic M1 decision and enforce the saved mode."""
        current = self.session()
        if not current:
            raise ValueError("no active SMC paper session")
        identity = evaluation.get("data_identity") or {}
        model = evaluation.get("model") or {}
        if evaluation.get("strategy_id") != STRATEGY_ID or \
                evaluation.get("version") != STRATEGY_VERSION:
            raise ValueError("SMC decision is not attested to the active source strategy version")
        if model.get("id") != current.get("model_id") or model.get("status") != "ACTIVE":
            raise ValueError("SMC decision model does not match the active paper session")
        if (normalize_symbol(identity.get("symbol") or ""), identity.get("timeframe")) != (
                normalize_symbol(current.get("symbol") or ""), current.get("timeframe")):
            raise ValueError("SMC decision identity does not match the active paper session")
        proposal = evaluation.get("proposal")
        plan = evaluation.get("trade_plan")
        candle_time = str(
            closed_candle_time or identity.get("selected_candle") or
            (proposal or {}).get("signal_timestamp") or "")
        self._cancel_stale_staged_entries(candle_time, feed_reliable=feed_reliable)
        decision = self.record_evaluation(
            evaluation, candle_time=candle_time,
            feed_status=feed_status or {
                "state": "SYNCHRONIZED" if feed_reliable else "DATA_PAUSED",
                "health_reason": "caller supplied only the reliability boundary",
                "reliable": feed_reliable,
            })
        correlation = decision["correlation_id"]
        decision_key = decision["idempotency_key"]
        if evaluation.get("state") != "ENTRY_READY" or not proposal or not plan:
            return {"created": False, "reason": evaluation.get("next_required_event", "not ready"),
                    "correlation_id": correlation, "decision_recorded": True,
                    "real_execution_allowed": False}
        if (normalize_symbol(proposal.get("symbol") or ""), proposal.get("timeframe")) != (
                normalize_symbol(current.get("symbol") or ""), current.get("timeframe")):
            raise ValueError("SMC proposal identity does not match the active paper session")
        try:
            if abs(float(plan["entry"]) - float(proposal["entry"])) > 1e-9 or \
                    abs(float(plan["stop"]) - float(proposal["stop"])) > 1e-9:
                raise ValueError("SMC trade plan is detached from its native proposal")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("SMC trade plan is detached from its native proposal") from exc
        proposal_id = proposal["id"]
        existing = self._db.execute("SELECT * FROM smc_candidates WHERE proposal_id=?", (proposal_id,)).fetchone()
        if existing:
            return {"created": False, "duplicate": True, "candidate": {**dict(existing),
                    "payload": json.loads(existing["payload"])}, "real_execution_allowed": False}
        mode = current["operating_mode"]
        if not feed_reliable:
            status, reason = "DATA_PAUSED", "market data is not reliable"
        elif mode == "signals_only":
            status, reason = "SIGNAL_ONLY", "signals-only mode cannot create an order"
        elif mode == "manual_approval":
            status, reason = "PENDING_APPROVAL", "waiting for explicit paper approval"
        elif mode == "automatic":
            status, reason = "APPROVED_AUTOMATIC", "automatic paper mode passed the candidate boundary"
        else:
            status, reason = "DATA_PAUSED", "saved SMC operating mode is invalid; execution failed closed"
        payload = {"evaluation": evaluation, "rules": rules, "reference_price": reference_price,
                   "correlation_id": correlation, "idempotency_key": decision_key}
        now = _iso()
        self._db.execute(
            "INSERT INTO smc_candidates VALUES (?,?,?,?,?,?,?,?,?)",
            (proposal_id, current["id"], proposal.get("setup_id"), current["model_id"],
             status, reason, json.dumps(payload, sort_keys=True, default=str), now, now),
        )
        self._audit("strategy_candidate_created", object_id=proposal_id,
                    payload={"status": status, "reason": reason,
                             "native_object_ids": evaluation.get("native_object_ids", []),
                             "correlation_id": correlation, "idempotency_key": decision_key})
        if status == "DATA_PAUSED":
            self._advance_evaluation(correlation, "RISK_REJECTED", reason)
        placed = None
        if status == "APPROVED_AUTOMATIC":
            try:
                placed = self._place_candidate(payload, proposal_id, source="automatic")
            except (ValueError, RuntimeError) as exc:
                status = "REJECTED"
                reason = f"automatic paper placement rejected: {exc}"
                self._db.execute(
                    "UPDATE smc_candidates SET status=?,reason=?,updated_at=? WHERE proposal_id=?",
                    (status, reason, _iso(), proposal_id),
                )
                self._audit("paper_order_rejected", object_id=proposal_id,
                            payload={"reason": reason, "source": "automatic",
                                     "correlation_id": correlation})
                self._advance_evaluation(correlation, "RISK_REJECTED", reason)
        return {"created": True, "candidate_status": status, "order": placed,
                "reason": reason, "correlation_id": correlation,
                "real_execution_allowed": False}

    def _place_candidate(self, payload: dict, proposal_id: str, *, source: str) -> dict:
        evaluation = payload["evaluation"]
        proposal, plan = evaluation["proposal"], evaluation["trade_plan"]
        current = self.session()
        if not current:
            raise ValueError("no active SMC paper session")
        tick = float(payload["rules"]["tick_size"])
        normalized = lambda value: round(round(float(value) / tick) * tick, 12) if tick > 0 else float(value)
        creation_candle = str(proposal.get("signal_timestamp") or "")
        creation_time = self._candle_datetime(creation_candle)
        expiry_candle = (
            creation_time + timedelta(milliseconds=TF_MS[current["timeframe"]])
            if creation_time is not None else None
        )
        result = self.submit_order(
            symbol=proposal["symbol"], side=proposal["direction"], order_type="market",
            rules=payload["rules"], reference_price=float(payload["reference_price"]),
            risk_pct=float(current["risk_pct"]), stop_loss=normalized(plan["stop"]),
            target_1=normalized(plan["target_1"]), target_2=normalized(plan["target_2"]),
            idempotency_key=payload.get("idempotency_key") or f"strategy:{proposal_id}", ownership="strategy",
            proposal_id=proposal_id, setup_id=proposal.get("setup_id"),
            poi_id=next((object_id for object_id in evaluation.get("native_object_ids", [])
                         if object_id.startswith(("ob-", "fvg-"))), None),
            model_id=evaluation["model"]["id"],
            creation_candle=creation_candle,
            decision_timestamp=expiry_candle.isoformat() if expiry_candle else creation_candle,
            expiry_candle=expiry_candle.isoformat() if expiry_candle else None,
            correlation_id=payload.get("correlation_id"),
        )
        self._db.execute("UPDATE smc_candidates SET status='ORDER_CREATED',reason=?,updated_at=? WHERE proposal_id=?",
                         (f"{source} paper order created", _iso(), proposal_id))
        self._advance_evaluation(payload.get("correlation_id"), "ORDER_SUBMITTED",
                                 "protected SMC paper order submitted",
                                 order_id=result["order"]["id"])
        return result

    def approve_candidate(self, proposal_id: str) -> dict:
        row = self._db.execute(
            "SELECT * FROM smc_candidates WHERE session_id=? AND proposal_id=?",
            (self.session().get("id", ""), proposal_id)).fetchone()
        if not row:
            raise KeyError(proposal_id)
        if row["status"] != "PENDING_APPROVAL":
            raise ValueError("candidate is not awaiting explicit paper approval")
        return self._place_candidate(json.loads(row["payload"]), proposal_id, source="manual_approval")

    def apply_funding_once(self, *, symbol: str, funding_time: str | None,
                           rate: float, mark_price: float) -> dict:
        """Book one factual provider funding event at most once per session."""
        session_id = self.session().get("id")
        if not session_id or not funding_time:
            return {"applied": False, "reason": "funding event unavailable"}
        key = (session_id, normalize_symbol(symbol), funding_time)
        existing = self._db.execute(
            "SELECT applied,amount FROM smc_funding_events WHERE session_id=? AND symbol=? AND funding_time=?",
            key).fetchone()
        if existing:
            return {"applied": False, "reason": "funding event already processed",
                    "originally_applied": bool(existing["applied"]), "funding": existing["amount"]}
        result = self.broker.apply_funding(key[1], float(rate), float(mark_price))
        self._db.execute(
            "INSERT INTO smc_funding_events(session_id,symbol,funding_time,rate,mark_price,amount,applied) VALUES (?,?,?,?,?,?,?)",
            (*key, float(rate), float(mark_price), float(result.get("funding") or 0),
             int(bool(result.get("applied")))))
        self._audit("paper_funding_processed", object_id=funding_time,
                    payload={"symbol": key[1], "rate": rate, "mark_price": mark_price,
                             "applied": bool(result.get("applied")),
                             "amount": result.get("funding") or 0})
        self._snapshot()
        return {**result, "funding_time": funding_time, "rate": rate,
                "source": "Binance USDⓈ-M Futures public funding history"}

    def advance_replay_cursor(self, expected_session_id: str, cursor: int) -> None:
        current = self.session()
        if current.get("id") != expected_session_id or current.get("mode") != "HISTORICAL":
            raise ValueError("historical replay cursor does not belong to the active SMC session")
        self._db.execute("UPDATE smc_sessions SET replay_cursor=?,updated_at=? WHERE id=?",
                         (int(cursor), _iso(), expected_session_id))

    def add_journal_note(self, journal_id: str, note: str) -> dict:
        if not note.strip():
            raise ValueError("journal note cannot be empty")
        row = next((item for item in self.journal()["journal"] if item["journal_id"] == journal_id), None)
        if not row:
            raise KeyError(journal_id)
        revision = {"id": uuid.uuid4().hex, "journal_id": journal_id,
                    "session_id": row["session_id"], "note": note.strip(), "created_at": _iso()}
        self._db.execute("INSERT INTO smc_journal_revisions VALUES (?,?,?,?,?)",
                         tuple(revision[key] for key in ("id", "journal_id", "session_id", "note", "created_at")))
        self._audit("journal_note_added", object_id=journal_id,
                    payload={"revision_id": revision["id"]})
        return revision

    def _journal_session_state(self, session_id: str) -> dict:
        session_row = self._db.execute("SELECT * FROM smc_sessions WHERE id=?", (session_id,)).fetchone()
        if not session_row:
            raise KeyError(session_id)
        current = self.session()
        if current.get("id") == session_id:
            return self.state()
        session = self._decoded(dict(session_row))
        snapshot = session.get("state") or {}
        metadata = [{**dict(row), "config": json.loads(row["config_json"])} for row in self._db.execute(
            "SELECT * FROM smc_order_meta WHERE session_id=? ORDER BY created_at DESC", (session_id,))]
        candidates = [{**dict(row), "payload": json.loads(row["payload"])} for row in self._db.execute(
            "SELECT * FROM smc_candidates WHERE session_id=? ORDER BY created_at DESC", (session_id,))]
        return {"session": session, "trades": snapshot.get("fills", []),
                "order_metadata": metadata, "candidates": candidates}

    def _journal_for_session(self, session_id: str) -> list[dict]:
        state = self._journal_session_state(session_id)
        fills_by_order: dict[str, list[dict]] = {}
        for fill in state["trades"]:
            fills_by_order.setdefault(fill["order_id"], []).append(fill)
        rows = []
        for candidate in state["candidates"]:
            evaluation = candidate["payload"].get("evaluation", {})
            proposal = evaluation.get("proposal") or {}
            candidate_meta = [row for row in state["order_metadata"]
                              if row.get("proposal_id") == candidate["proposal_id"]]
            meta = next((row for row in candidate_meta if row.get("ownership") == "strategy"),
                        candidate_meta[0] if candidate_meta else None)
            owned_order_ids = {row["order_id"] for row in candidate_meta}
            for row in candidate_meta:
                owned_order_ids.update(row.get("config", {}).get("protective_exit_order_ids") or [])
            fills = [fill for order_id in owned_order_ids for fill in fills_by_order.get(order_id, [])]
            rows.append({
                "journal_id": f"smc-journal-{candidate['proposal_id']}",
                "session_id": candidate["session_id"], "symbol": proposal.get("symbol", state["session"].get("symbol")),
                "timeframe": proposal.get("timeframe", state["session"].get("timeframe")),
                "strategy_id": STRATEGY_ID, "model_id": candidate["model_id"], "version": STRATEGY_VERSION,
                "direction": proposal.get("direction"), "status": candidate["status"],
                "signal_timestamp": proposal.get("signal_timestamp"),
                "created_at": candidate["created_at"], "updated_at": candidate["updated_at"],
                "native_object_ids": evaluation.get("native_object_ids", []),
                "mtf_evidence": evaluation.get("mtf_evidence") or {},
                "ordered_conditions": evaluation.get("ordered_condition_results", []),
                "missing_conditions": evaluation.get("missing_conditions", []),
                "trade_plan": evaluation.get("trade_plan"), "proposal_id": candidate["proposal_id"],
                "setup_id": candidate.get("setup_id"), "order_id": meta["order_id"] if meta else None,
                "fills": fills, "net_pnl": sum(float(fill["realized_pnl"]) - float(fill["fee"]) for fill in fills),
                "data_quality": "SYNCHRONIZED" if candidate["status"] != "DATA_PAUSED" else "UNRELIABLE",
                "rule_compliance": "PASS" if evaluation.get("state") == "ENTRY_READY" else "INCOMPLETE",
                "notes": [dict(note) for note in self._db.execute(
                    "SELECT id,note,created_at FROM smc_journal_revisions WHERE journal_id=? ORDER BY created_at",
                    (f"smc-journal-{candidate['proposal_id']}",))],
            })
        return rows

    def journal(self, session_id: str | None = None) -> dict:
        session_ids = ([session_id] if session_id else
                       [row["id"] for row in self._db.execute(
                           "SELECT id FROM smc_sessions ORDER BY started_at DESC")])
        rows = [row for sid in session_ids for row in self._journal_for_session(sid)]
        return {"journal": rows, "paper_only": True, "real_execution_allowed": False}

    def metrics(self) -> dict:
        current_id = self.session().get("id")
        journal = self.journal(current_id)["journal"] if current_id else []
        completed = [row for row in journal if row["status"] == "COMPLETED" and
                     any(abs(float(fill.get("realized_pnl") or 0)) > 1e-12
                         for fill in row["fills"])]
        wins = [row for row in completed if row["net_pnl"] > 0]
        losses = [row for row in completed if row["net_pnl"] < 0]
        gross_profit = sum(row["net_pnl"] for row in wins)
        gross_loss = abs(sum(row["net_pnl"] for row in losses))
        net = sum(row["net_pnl"] for row in completed)
        target_1_hits = sum(any(meta.get("ownership") == "strategy_target_1" and
                                meta.get("proposal_id") == row["proposal_id"] and
                                any(fill["order_id"] == meta["order_id"] for fill in row["fills"])
                                for meta in self.state()["order_metadata"]) for row in completed)
        funding = self.state()["funding_events"]
        return {"session_id": self.session().get("id"), "detected_setups": len(journal),
                "orders_placed": len(self.state()["order_metadata"]), "trades_with_fills": len(completed),
                "wins": len(wins), "win_rate": (len(wins) / len(completed) if completed else None),
                "net_pnl": net, "expectancy": (net / len(completed) if completed else None),
                "profit_factor": (gross_profit / gross_loss if gross_loss else None),
                "target_1_hit_rate": (target_1_hits / len(completed) if completed else None),
                "fees_paid": self.broker.account()["fees_paid"],
                "funding_paid": sum(float(row["amount"]) for row in funding),
                "mfe": None, "mae": None,
                "evidence_limitations": ["MFE and MAE are unavailable because tick-path evidence is not stored; values are not fabricated."],
                "sample_size_warning": "INSUFFICIENT_SAMPLE" if len(completed) < 30 else None,
                "paper_only": True, "real_execution_allowed": False}


class SMCStrategyLabRuntime:
    """Small PAPER-only closed-bar worker for the active SMC session."""

    def __init__(self, market, account: SMCPaperAccount, *, poll_seconds: float = 5.0,
                 autostart: bool = True, market_hub=None):
        self.market, self.account = market, account
        self.market_hub = market_hub
        self.poll_seconds = max(1.0, float(poll_seconds))
        self._stop = threading.Event()
        self._tick_lock = threading.Lock()
        self._thread = None
        self._stream_identity: tuple[str, str] | None = None
        self.last_market_health: dict = {
            "state": "DISCONNECTED", "transport_state": "DISCONNECTED",
            "health_reason": "SMC market-data runtime has not synchronized",
            "reliable": False, "new_entries_paused": True,
            "failing_dependency": "BINANCE_USDM_PUBLIC_STREAMS",
            "last_successful_event": None,
        }
        self.stream = None
        self.last_mtf_evidence: dict = {}
        self.last_mtf_context: dict[str, list[Bar]] = {}
        if market_hub is not None:
            self.stream = market_hub.subscription(
                "SMC_LAB",
                bar_sink=self._on_closed_bar,
                quote_sink=self._on_quote,
                event_sink=lambda event: account._audit(
                    "market_data_stream_event", payload=event),
            )
        elif callable(getattr(market, "public_usdm_window", None)):
            from services.price_action_stream import PriceActionPublicStream
            self.stream = PriceActionPublicStream(
                market.public_usdm_window,
                event_sink=lambda event: account._audit(
                    "market_data_stream_event", payload=event),
                bar_sink=self._on_closed_bar,
                quote_sink=self._on_quote,
            )
        from services.native_context_loader import NativeContextLoader
        fetch = (self.stream.make_fetcher() if market_hub is not None else
                 lambda sym, tf, limit: market.public_usdm_window(sym, tf, limit=limit))
        self.native_loader = NativeContextLoader(fetch)
        if autostart:
            self._thread = threading.Thread(target=self._run, name="smc-paper-runtime", daemon=True)
            self._thread.start()

    def _native_context(self, symbol: str, timeframe: str) -> dict[str, list[Bar]]:
        return self.native_loader.context(symbol, timeframe)

    def _native_mtf_evidence(self, symbol: str, timeframe: str,
                             decision_time: datetime) -> dict:
        from services.mtf_policy import evidence_at
        context = self._native_context(symbol, timeframe)
        self.last_mtf_context = context
        evidence = evidence_at(symbol, timeframe, context, decision_time)
        self.last_mtf_evidence = evidence
        if evidence.get("primary") is None:
            raise RuntimeError("HTF_PRIMARY_UNAVAILABLE: SMC primary native HTF candle is unavailable")
        return evidence

    def _on_quote(self, quote: dict) -> None:
        current = self.account.session()
        if not current or current.get("mode") != "LIVE_PAPER" or self.stream is None:
            return
        identity = (normalize_symbol(current.get("symbol") or ""),
                    current.get("timeframe"))
        if identity != self._stream_identity:
            return
        status = self.stream.status()
        self.account.process_quote(
            identity[0], quote,
            feed_reliable=bool(status.get("reliable")),
        )

    def _on_closed_bar(self, _bar) -> None:
        current = self.account.session()
        if not current or current.get("mode") != "LIVE_PAPER":
            return
        # The non-blocking lock prevents the periodic supervisor and the shared
        # hub callback from evaluating the same candle concurrently. Candle
        # idempotency in the account remains the second durable boundary.
        self.tick()

    def reconcile_visual(self, visual: dict, *, symbol: str, timeframe: str,
                         start_stream: bool = True) -> tuple[dict, dict | None]:
        """Use the shared Binance websocket state machine as entry authority."""
        from services.native_smc_live_visual import reconcile_market_state
        rest_quote = None
        if self.stream is None and start_stream:
            try:
                rest_quote = self.market.public_usdm_quote(symbol)
            except Exception:
                pass
        if self.stream is None:
            reconciled = reconcile_market_state(visual, rest_quote, timeframe=timeframe)
            self.last_market_health = dict(reconciled.get("live_display") or self.last_market_health)
            return reconciled, rest_quote
        identity = (normalize_symbol(symbol), timeframe)
        if start_stream and (self._stream_identity != identity or not self.stream.running):
            started = self.stream.start(*identity)
            if not started:
                # Do not cache a failed identity.  The supervisor's next tick
                # must retry the bootstrap instead of leaving a dead stream
                # permanently associated with the active session.
                self._stream_identity = None
                self.last_market_health = {
                    **self.last_market_health,
                    "state": "ERROR", "transport_state": "ERROR",
                    "health_reason": "SMC market-data stream failed to start",
                    "reliable": False, "new_entries_paused": True,
                    "failing_dependency": "BINANCE_USDM_PUBLIC_STREAMS",
                    "retry_state": {
                        "automatic_retry": True,
                        "retry_after_seconds": self.poll_seconds,
                    },
                }
                raise RuntimeError("SMC market-data stream failed to start")
            self._stream_identity = identity
        snapshot = self.stream.snapshot()
        status = snapshot["connection"]
        quote = {**(rest_quote or {}), **{key: value for key, value in snapshot["quote"].items()
                                         if value is not None}}
        stream_bars = snapshot["closed_bars"]
        stream_last = stream_bars[-1].timestamp.isoformat() if stream_bars else None
        visual_last = visual.get("data_provenance", {}).get("last_closed_candle")
        histories_match = bool(stream_last and visual_last and stream_last == visual_last)
        reliable = bool(status.get("reliable")) and histories_match
        live = visual.setdefault("live_display", {})
        live.update({
            "bid": quote.get("bid"), "ask": quote.get("ask"), "mark": quote.get("mark"),
            "funding_rate": quote.get("funding_rate"),
            "last_funding_time": (rest_quote or {}).get("last_funding_time"),
            "next_funding_time": quote.get("next_funding_time"),
            **status, "connection_state": status["state"],
            "reliable": reliable, "new_entries_paused": not reliable,
            "quote_source": "BINANCE_USDM_PUBLIC_WEBSOCKET",
            "health_reason": (status["health_reason"] if histories_match else
                              "websocket and chart completed-candle histories are not reconciled"),
        })
        if not histories_match:
            live["state"] = live["connection_state"] = "SYNCING"
            live["failing_dependency"] = "COMPLETED_CANDLE_RECONCILIATION"
        self.last_market_health = dict(live)
        visual.setdefault("data_provenance", {}).update({
            "connection_state": live["connection_state"],
            "new_entries_paused": not reliable, "market_data_mode": "LIVE",
            "market_data_source": "Binance USDⓈ-M Futures public websocket with REST recovery",
            "exchange": "Binance USDⓈ-M Futures",
        })
        return visual, quote or None

    def live_state(self, symbol: str, timeframe: str, *, visible: int = 240,
                   window: int = 800, model_id: str = "SMC_M1_SWEEP_REVERSAL") -> dict:
        """Hydrate the lab from snapshots, with no provider calls or startup."""
        from services.native_smc_live_visual import live_visual_state, NativeSMCLiveDataUnavailable
        from services.native_smc import SMCConfig, SMCMarketStructureEngine
        from services.mtf_policy import evidence_at, display_contract

        session = self.account.session()
        if (session.get("symbol"), session.get("timeframe")) != (symbol.upper(), timeframe):
            raise ValueError("requested market does not match the saved SMC session")
        snapshot = self.stream.snapshot() if self.stream else {"closed_bars": [], "forming": None}
        context = self._native_context(symbol, timeframe)
        evidence = evidence_at(symbol, timeframe, context, datetime.now(timezone.utc))
        def fetch(_symbol, _timeframe, _venue, limit, **_kwargs):
            rows = list(snapshot["closed_bars"])
            if snapshot.get("forming") is not None:
                rows.append(snapshot["forming"])
            return rows[-limit:]
        try:
            state = live_visual_state(symbol, timeframe, "binance_usdm", limit=window,
                                      visible=visible, fetcher=fetch, model_id=model_id,
                                      mtf_evidence=evidence, mtf_context=context)
        except NativeSMCLiveDataUnavailable:
            state = SMCMarketStructureEngine(SMCConfig(symbol=symbol, timeframe=timeframe)).visual_state()
        if self.stream is not None:
            state, _ = self.reconcile_visual(state, symbol=symbol, timeframe=timeframe, start_stream=False)
        state.update({"session_id": session.get("id"), "operating_mode": session.get("operating_mode"),
                      "mtf_evidence": evidence, "mtf_policy": display_contract(timeframe, evidence),
                      "blockers": [] if evidence.get("primary") else ["HTF_PRIMARY_UNAVAILABLE"]})
        return state

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                current = self.account.session()
                if current and current.get("mode") == "LIVE_PAPER":
                    self.tick()
            except Exception as exc:
                self.last_market_health = {
                    **self.last_market_health, "state": "ERROR", "transport_state": "ERROR",
                    "health_reason": f"SMC runtime tick failed: {type(exc).__name__}: {exc}",
                    "reliable": False, "new_entries_paused": True,
                    "failing_dependency": "SMC_CLOSED_CANDLE_RUNTIME",
                    "retry_state": {"automatic_retry": True, "retry_after_seconds": self.poll_seconds},
                }
                self.account._audit("paper_runtime_paused", payload={"error": f"{type(exc).__name__}: {exc}",
                                                                      "new_orders_created": 0})

    def tick(self) -> dict:
        if not self._tick_lock.acquire(blocking=False):
            return {"skipped": True, "reason": "tick already running", "real_execution_allowed": False}
        try:
            current = self.account.session()
            if not current:
                raise ValueError("no active SMC paper session")
            from services.native_smc_live_visual import live_visual_state
            from services.mtf_policy import candle_close
            if self.stream is not None:
                identity = (normalize_symbol(current["symbol"]), current["timeframe"])
                if self._stream_identity != identity or not self.stream.running:
                    if not self.stream.start(*identity):
                        raise RuntimeError("SMC market-data hub subscription failed to start")
                    self._stream_identity = identity
                hub_snapshot = self.stream.snapshot()
                decision_rows = list(hub_snapshot.get("closed_bars") or [])
                if not decision_rows:
                    raise RuntimeError("SMC decision stream has no closed candle")
                mtf_evidence = self._native_mtf_evidence(
                    current["symbol"], current["timeframe"],
                    candle_close(decision_rows[-1], current["timeframe"]),
                )
                self.last_mtf_evidence = dict(mtf_evidence)

                def shared_fetcher(_symbol, _timeframe, _venue, limit, **_kwargs):
                    rows = list(hub_snapshot.get("closed_bars") or [])
                    forming = hub_snapshot.get("forming")
                    if forming is not None:
                        rows.append(forming)
                    return rows[-max(1, int(limit)):]

                visual = live_visual_state(
                    current["symbol"], current["timeframe"], "binance_usdm",
                    limit=800, visible=400, now=datetime.now(timezone.utc),
                    fetcher=shared_fetcher, model_id=current["model_id"],
                    mtf_evidence=mtf_evidence,
                    mtf_context=self.last_mtf_context,
                )
            else:
                loader = getattr(self.market, "public_usdm_window", None)
                if callable(loader):
                    decision_rows = loader(
                        current["symbol"], current["timeframe"], limit=800)
                    if not decision_rows:
                        raise RuntimeError("SMC decision feed has no candle")
                    mtf_evidence = self._native_mtf_evidence(
                        current["symbol"], current["timeframe"],
                        candle_close(decision_rows[-1], current["timeframe"]),
                    )
                else:
                    # Injectable deterministic tests may replace the complete
                    # visual state without owning a market-data service. The
                    # production app always has public_usdm_window.
                    mtf_evidence = {}
                self.last_mtf_evidence = dict(mtf_evidence)
                visual = live_visual_state(
                    current["symbol"], current["timeframe"], "binance_usdm",
                    limit=800, visible=400, model_id=current["model_id"],
                    mtf_evidence=mtf_evidence,
                    mtf_context=self.last_mtf_context if mtf_evidence else None,
                )
            rules = self.market.usdm_contract_rules(current["symbol"])
            visual, quote = self.reconcile_visual(
                visual, symbol=current["symbol"], timeframe=current["timeframe"])
            reliable = bool(visual["live_display"].get("reliable"))
            closed_candle = visual["candles"][-1]
            closed_time = getattr(closed_candle, "timestamp", None)
            if closed_time is None and isinstance(closed_candle, dict):
                closed_time = closed_candle.get("timestamp")
            closed_time = closed_time.isoformat() if hasattr(closed_time, "isoformat") else str(closed_time or "")
            processed = (self.account.process_candle(
                             current["symbol"], closed_candle,
                             allow_candle_fills=self.stream is None)
                         if reliable else {"duplicate": False, "events": [], "paused": True,
                                           "reason": visual["live_display"].get("health_reason")})
            candidate = self.account.synchronize_candidate(
                visual["source_strategy"], rules=rules,
                reference_price=float((quote or {}).get("mark") or visual["live_display"]["last_price"]),
                feed_reliable=reliable, closed_candle_time=closed_time,
                feed_status=visual["live_display"])
            funding = self.account.apply_funding_once(
                symbol=current["symbol"], funding_time=(quote or {}).get("last_funding_time"),
                rate=float((quote or {}).get("funding_rate") or 0),
                mark_price=float((quote or {}).get("mark") or visual["live_display"]["last_price"]),
            ) if reliable else {"applied": False, "reason": "market data is not synchronized"}
            return {"processed": processed, "candidate": candidate,
                    "funding": funding, "market_data_health": visual["live_display"],
                    "paper_only": True, "real_execution_allowed": False}
        finally:
            self._tick_lock.release()

    def bot_status(self) -> dict:
        """One factual, scope-labelled control-plane view for the dashboard."""
        from services.mtf_policy import display_contract, evidence_at
        paper = self.account.state()
        session = paper.get("session") or {}
        connection = dict(self.last_market_health)
        if self.stream is not None:
            raw = self.stream.status()
            # Keep the reconciler's authoritative state/reason while taking
            # current transport heartbeat and retry evidence from the stream.
            connection = {
                **raw, **connection,
                "last_successful_event": raw.get("last_successful_event")
                    or connection.get("last_successful_event"),
                "retry_state": raw.get("retry_state") or connection.get("retry_state"),
                "connecting_age_seconds": raw.get("connecting_age_seconds"),
                "last_update": raw.get("last_update") or connection.get("last_update"),
            }
        evaluations = paper.get("evaluations") or []
        latest = evaluations[0] if evaluations else None
        pending = [row for row in paper.get("orders", []) if row.get("status") in OPEN_STATUSES]
        pending_entries = [row for row in pending if not row.get("reduce_only")]
        positions = paper.get("positions") or []
        orphaned_exposure = not session and bool(positions or pending_entries)
        blockers = lifecycle_blockers(
            connection=connection, operating_mode=session.get("operating_mode", "signals_only"),
            account=paper.get("account") or {},
            strategy_valid=(session.get("model_id") in ACTIVE_MODEL_IDS),
            positions=positions, pending_orders=pending_entries,
            risk_pct=session.get("risk_pct"),
            max_risk_pct=1.0)
        if orphaned_exposure:
            blockers.insert(0, "paper exposure has no active owning session; explicitly resume, repair, or close it")
        fills = paper.get("trades") or []
        live_performance = paper_performance(
            fills=fills, account=paper.get("account") or {}, realized_r_values=[],
            orders=paper.get("orders") or [])
        activity = paper.get("activity") or []
        evidence = (evidence_at(session["symbol"], session["timeframe"],
                                self._native_context(session["symbol"], session["timeframe"]),
                                datetime.now(timezone.utc)) if session else {})
        if session.get("mode") == "LIVE_PAPER" and not evidence.get("primary"):
            blockers.append("HTF_PRIMARY_UNAVAILABLE")
        execution_armed = session.get("operating_mode") == "automatic"
        operator_state = (
            "ERROR" if orphaned_exposure else
            "BLOCKED" if not session or blockers else
            "RUNNING_ARMED" if execution_armed else "RUNNING_UNARMED"
        )
        mtf_policy = (display_contract(session["timeframe"], evidence)
                      if session.get("timeframe") else None)
        return {
            "lab": "SMC", "account_scope": paper["account_scope"],
            "scope_label": "SMC Strategy Lab session · isolated paper ledger",
            "paper_only": True, "real_execution_allowed": False,
            "strategy": {"id": STRATEGY_ID, "model_id": session.get("model_id"),
                         "version": STRATEGY_VERSION},
            "symbol": session.get("symbol"), "timeframe": session.get("timeframe"),
            "mtf_policy": mtf_policy,
            "mode": session.get("operating_mode"),
            "session_id": session.get("id"), "operating_mode": session.get("operating_mode"),
            "session_state": operator_state,
            "execution_armed": execution_armed,
            "saved_configuration": {
                "operating_mode": session.get("operating_mode"),
                "model_id": session.get("model_id"), "risk_pct": session.get("risk_pct")},
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
            "latest_signal": next((row for row in evaluations
                                   if (row.get("payload") or {}).get("source_evaluation", {}).get("state") == "ENTRY_READY"), None),
            "latest_order": (paper.get("order_metadata") or [None])[0],
            "latest_fill": (fills or [None])[0],
            "last_heartbeat": connection.get("last_update") or session.get("updated_at"),
            "latest_activity": activity[0] if activity else None,
            "performance": {
                "backtest": unavailable_performance(
                    "BACKTEST", "No normalized SMC backtest ledger is attached to this live-paper session"),
                "forward_validation": unavailable_performance(
                    "FORWARD_VALIDATION", "No normalized SMC forward-validation ledger is attached to this live-paper session"),
                "live_paper": live_performance,
            },
        }

    def stop(self) -> None:
        self.native_loader.stop()
        self._stop.set()
        if self.stream is not None:
            self.stream.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def replay_step(self, *, steps: int = 1) -> dict:
        current = self.account.session()
        if not current or current.get("mode") != "HISTORICAL":
            raise ValueError("an active HISTORICAL SMC session is required")
        rows = self.market.bars(current["symbol"], current["timeframe"], limit=3000)
        if not rows:
            raise RuntimeError("verified cached Binance history is required for SMC replay")
        start = max(0, min(int(current.get("replay_cursor") or 0), len(rows)))
        end = min(len(rows), start + max(1, min(int(steps), 500)))
        from services.native_smc import SMCConfig, SMCMarketStructureEngine
        from services.smc_strategy_v1 import evaluate
        engine = SMCMarketStructureEngine(SMCConfig(symbol=current["symbol"],
                                                     timeframe=current["timeframe"]))
        if start:
            engine.ingest_authoritative_closed_bars(rows[:start],
                timeframe_seconds=TF_MS[current["timeframe"]] / 1000)
        rules = self.market.usdm_contract_rules(current["symbol"])
        results = []
        for bar in rows[start:end]:
            engine.process_closed_bar(bar)
            processed = self.account.process_candle(current["symbol"], bar)
            decision = evaluate(engine, current["model_id"], candle_at=bar.timestamp)
            candidate = self.account.synchronize_candidate(
                decision, rules=rules, reference_price=float(bar.close), feed_reliable=True,
                closed_candle_time=bar.timestamp.isoformat(),
                feed_status={"state": "SYNCHRONIZED", "reliable": True,
                             "health_reason": "deterministic historical replay"})
            results.append({"candle": bar.timestamp.isoformat(), "processed": processed,
                            "candidate": candidate, "decision_state": decision["state"]})
        self.account.advance_replay_cursor(current["id"], end)
        self.account._snapshot()
        return {"session_id": current["id"], "cursor": end, "total": len(rows),
                "has_next": end < len(rows), "future_candles_visible": False,
                "steps": results, "paper_only": True, "real_execution_allowed": False}

    def set_protection(self, symbol: str, *, stop_loss: float | None = None,
                       take_profit: float | None = None) -> dict:
        position = self.broker.set_protection(symbol, stop_loss=stop_loss, take_profit=take_profit)
        self._audit("paper_position_protection_changed", object_id=symbol,
                    payload={"stop_loss": stop_loss, "take_profit": take_profit})
        self._snapshot()
        return {"position": position, "real_execution_allowed": False}

    def export_session(self, session_id: str | None = None) -> dict:
        sid = session_id or self.session().get("id")
        row = self._db.execute("SELECT * FROM smc_sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise KeyError(sid)
        return {"format_version": "SMC_PAPER_1", "exported_at": _iso(),
                "paper_only": True, "real_execution_allowed": False,
                "session": self._decoded(dict(row)),
                "orders": [dict(item) for item in self._db.execute("SELECT * FROM smc_order_meta WHERE session_id=?", (sid,))],
                "activity": [dict(item) for item in self._db.execute("SELECT * FROM smc_activity WHERE session_id=?", (sid,))]}
