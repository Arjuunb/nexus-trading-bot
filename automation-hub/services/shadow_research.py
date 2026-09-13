"""Physically partitioned PA/SMC shadow research persistence.

The store deliberately defines no account, margin, capacity, or real-position
table.  Every mutating API requires ``execution_class == SHADOW``.  This makes
shadow isolation structural rather than a boolean branch in a paper broker.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from data.sqlite_runtime import runtime_connection


EXECUTION_CLASS = "SHADOW"
BLOCKERS = {
    "SETUP_FOUND", "NO_SETUP", "SIGNALS_ONLY", "GATE_REJECTED",
    "MARKET_DATA_STALE", "INSTANCE_CAPACITY", "INSTANCE_PAUSED",
    "PA_UNARMED", "SMC_CONDITION_MISSING", "HTF_MISALIGNED",
    "ZONE_NOT_FRESH", "RECLAIM_FAILED", "RR_TOO_LOW", "RISK_TOO_HIGH",
    "LIMIT_EXPIRED", "NONE", "OUT_OF_ORDER_QUOTE",
}


def iso(value: datetime | str | None = None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def key(*parts: object) -> str:
    material = "|".join(str(part or "") for part in parts)
    return f"{material}|{hashlib.sha256(material.encode()).hexdigest()[:20]}"


def canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


class ShadowResearchStore:
    """Additive immutable decision/order/fill/outcome ledger for experiments."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._db = runtime_connection(self.path)
        self._lock = threading.RLock()
        self._schema()

    def _schema(self) -> None:
        with self._lock, self._db:
            self._db.executescript("""
              CREATE TABLE IF NOT EXISTS shadow_decisions(
                decision_id TEXT PRIMARY KEY,
                decision_key TEXT NOT NULL UNIQUE,
                engine TEXT NOT NULL,
                account_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                candle_id TEXT NOT NULL,
                action_class TEXT NOT NULL,
                direction TEXT,
                execution_class TEXT NOT NULL CHECK(execution_class='SHADOW'),
                blocker TEXT NOT NULL,
                decision_timestamp TEXT NOT NULL,
                snapshot_lineage TEXT NOT NULL,
                context_json TEXT NOT NULL,
                created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS shadow_orders(
                order_id TEXT PRIMARY KEY,
                order_key TEXT NOT NULL UNIQUE,
                decision_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                order_type TEXT NOT NULL,
                side TEXT NOT NULL,
                requested_price REAL,
                stop_loss REAL,
                take_profit REAL,
                quantity REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES shadow_decisions(decision_id));
              CREATE TABLE IF NOT EXISTS shadow_fills(
                fill_id TEXT PRIMARY KEY,
                fill_key TEXT NOT NULL UNIQUE,
                order_id TEXT NOT NULL,
                quote_event_id TEXT NOT NULL,
                event_timestamp TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                executable_side_price REAL NOT NULL,
                fill_price REAL NOT NULL,
                quantity REAL NOT NULL,
                spread_attribution REAL NOT NULL,
                spread_charged_again REAL NOT NULL DEFAULT 0,
                slippage REAL NOT NULL,
                commission REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES shadow_orders(order_id));
              CREATE TABLE IF NOT EXISTS shadow_outcomes(
                outcome_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE,
                exit_reason TEXT NOT NULL,
                exit_price REAL NOT NULL,
                gross_pnl REAL NOT NULL,
                commission REAL NOT NULL,
                slippage REAL NOT NULL,
                funding REAL NOT NULL,
                net_pnl REAL NOT NULL,
                gross_r REAL NOT NULL,
                net_r REAL NOT NULL,
                closed_at TEXT NOT NULL,
                validation_state TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES shadow_orders(order_id));
              CREATE TABLE IF NOT EXISTS shadow_mae_mfe(
                order_id TEXT PRIMARY KEY,
                mae_price REAL NOT NULL DEFAULT 0,
                mfe_price REAL NOT NULL DEFAULT 0,
                mae_pct REAL NOT NULL DEFAULT 0,
                mfe_pct REAL NOT NULL DEFAULT 0,
                mae_r REAL NOT NULL DEFAULT 0,
                mfe_r REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES shadow_orders(order_id));
              CREATE TABLE IF NOT EXISTS shadow_funding(
                funding_key TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                position_id TEXT NOT NULL,
                funding_timestamp TEXT NOT NULL,
                amount REAL NOT NULL,
                created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS shadow_quote_cursors(
                symbol TEXT PRIMARY KEY,
                event_timestamp TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                quote_event_id TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_shadow_decisions_strategy
                ON shadow_decisions(strategy_id,strategy_version,config_hash);
              CREATE INDEX IF NOT EXISTS ix_shadow_decisions_candle
                ON shadow_decisions(candle_id,engine);
              -- A FOREIGN KEY declaration creates no index in SQLite, so
              -- measurements() — a four-way LEFT JOIN over every decision ever
              -- recorded — re-scanned all of shadow_orders for each decision
              -- and all of shadow_fills for each order. That is quadratic, and
              -- the observatory writes one decision per variant per candle:
              -- nine variants on a 5m chart is about 2,600 rows a day, so the
              -- PA-vs-SMC comparison report grew until it timed out, which is
              -- the one answer the two labs exist to produce.
              CREATE INDEX IF NOT EXISTS ix_shadow_orders_decision
                ON shadow_orders(decision_id);
              CREATE INDEX IF NOT EXISTS ix_shadow_fills_order
                ON shadow_fills(order_id);
              -- shadow_outcomes.order_id and shadow_mae_mfe.order_id are
              -- already indexed by their UNIQUE and PRIMARY KEY constraints.
              -- This one supplies the report's ordering, which was otherwise a
              -- temp B-tree sort over the whole joined result.
              CREATE INDEX IF NOT EXISTS ix_shadow_decisions_created
                ON shadow_decisions(created_at DESC);
            """)
            order_columns = {row[1] for row in self._db.execute(
                "PRAGMA table_info(shadow_orders)"
            )}
            if "symbol" not in order_columns:
                self._db.execute(
                    "ALTER TABLE shadow_orders ADD COLUMN symbol TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _shadow(execution_class: str) -> None:
        if execution_class != EXECUTION_CLASS:
            raise ValueError("shadow store accepts execution_class=SHADOW only")

    def record_decision(self, *, engine: str, account_id: str,
                        strategy_id: str, strategy_version: str,
                        config_hash: str, candle_id: str, action_class: str,
                        direction: str | None, blocker: str,
                        decision_timestamp: datetime | str,
                        snapshot_lineage: str, context: dict,
                        execution_class: str = EXECUTION_CLASS) -> dict:
        self._shadow(execution_class)
        if blocker not in BLOCKERS:
            raise ValueError(f"unknown research blocker '{blocker}'")
        required = (engine, account_id, strategy_id, strategy_version,
                    config_hash, candle_id, action_class, snapshot_lineage)
        if not all(str(value).strip() for value in required):
            raise ValueError("complete shadow decision provenance is required")
        decision_key = key(engine, account_id, strategy_id, strategy_version,
                           candle_id, action_class)
        decision_id = "shadow-decision-" + hashlib.sha256(decision_key.encode()).hexdigest()[:28]
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO shadow_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, decision_key, engine, account_id, strategy_id,
                 strategy_version, config_hash, candle_id, action_class,
                 direction, EXECUTION_CLASS, blocker, iso(decision_timestamp),
                 snapshot_lineage, canonical(context), iso()),
            )
            row = self._db.execute(
                "SELECT * FROM shadow_decisions WHERE decision_key=?", (decision_key,)
            ).fetchone()
        return self._decode(row)

    def record_order(self, decision_id: str, *, symbol: str, order_type: str, side: str,
                     requested_price: float | None, stop_loss: float | None,
                     take_profit: float | None, quantity: float = 1,
                     status: str = "INTENT",
                     execution_class: str = EXECUTION_CLASS) -> dict:
        self._shadow(execution_class)
        if quantity <= 0:
            raise ValueError("shadow quantity must be positive")
        with self._lock, self._db:
            decision = self._db.execute(
                "SELECT * FROM shadow_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if not decision:
                raise KeyError(decision_id)
            order_key = key(decision["decision_key"], order_type, side)
            order_id = "shadow-order-" + hashlib.sha256(order_key.encode()).hexdigest()[:28]
            self._db.execute(
                "INSERT OR IGNORE INTO shadow_orders(order_id,order_key,decision_id,symbol,"
                "order_type,side,requested_price,stop_loss,take_profit,quantity,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (order_id, order_key, decision_id, symbol.upper().replace("/", ""), order_type, side,
                 requested_price, stop_loss, take_profit, float(quantity), status, iso()),
            )
            row = self._db.execute(
                "SELECT * FROM shadow_orders WHERE order_key=?", (order_key,)
            ).fetchone()
        return self._decode(row)

    def accept_quote(self, symbol: str, quote: dict) -> tuple[bool, str]:
        event_at = iso(quote.get("event_timestamp") or quote.get("received_at"))
        sequence = int(quote.get("sequence", 0))
        quote_id = str(quote.get("quote_event_id") or key(
            symbol, event_at, sequence, quote.get("bid"), quote.get("ask"), quote.get("mark")
        ))
        normalized = symbol.upper().replace("/", "")
        with self._lock, self._db:
            prior = self._db.execute(
                "SELECT * FROM shadow_quote_cursors WHERE symbol=?", (normalized,)
            ).fetchone()
            if prior:
                prior_at = iso(prior["event_timestamp"])
                if str(prior["quote_event_id"]) == quote_id:
                    return True, quote_id
                if event_at < prior_at or (event_at == prior_at and sequence <= int(prior["sequence"])):
                    return False, "OUT_OF_ORDER_QUOTE"
            self._db.execute(
                "INSERT INTO shadow_quote_cursors VALUES (?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET event_timestamp=excluded.event_timestamp,"
                "sequence=excluded.sequence,quote_event_id=excluded.quote_event_id",
                (normalized, event_at, sequence, quote_id),
            )
        return True, quote_id

    def record_fill(self, order_id: str, quote: dict, *, slippage_bps: float,
                    commission_bps: float,
                    execution_class: str = EXECUTION_CLASS) -> dict | None:
        self._shadow(execution_class)
        try:
            bid, ask = float(quote["bid"]), float(quote["ask"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("shadow fill requires public bid and ask") from exc
        if bid <= 0 or ask < bid:
            raise ValueError("shadow bid/ask is invalid")
        with self._lock:
            row = self._db.execute(
                "SELECT o.*,d.decision_timestamp FROM shadow_orders o "
                "JOIN shadow_decisions d ON d.decision_id=o.decision_id WHERE o.order_id=?",
                (order_id,),
            ).fetchone()
        if not row:
            raise KeyError(order_id)
        event_at = iso(quote.get("event_timestamp") or quote.get("received_at"))
        received_at = iso(quote.get("received_at") or quote.get("event_timestamp"))
        if event_at <= iso(row["decision_timestamp"]) or received_at <= iso(row["decision_timestamp"]):
            return None
        quote_symbol = str(quote.get("symbol") or row["symbol"]).upper().replace("/", "")
        if quote_symbol != str(row["symbol"]):
            raise ValueError("shadow quote symbol does not match the order")
        accepted, quote_id = self.accept_quote(quote_symbol, quote)
        if not accepted:
            return None
        fill_key = key(order_id, quote_id)
        side = str(row["side"]).lower()
        executable = ask if side in {"buy", "long"} else bid
        if str(row["order_type"]).lower() == "limit" and row["requested_price"] is not None:
            limit = float(row["requested_price"])
            if (side in {"buy", "long"} and ask > limit) or (
                    side not in {"buy", "long"} and bid < limit):
                return None
        slip = executable * max(0.0, float(slippage_bps)) / 10_000
        fill_price = executable + slip if side in {"buy", "long"} else executable - slip
        quantity = float(row["quantity"])
        commission = fill_price * quantity * max(0.0, float(commission_bps)) / 10_000
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO shadow_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("shadow-fill-" + uuid.uuid4().hex, fill_key, order_id, quote_id,
                 event_at, int(quote.get("sequence", 0)), executable, fill_price,
                 quantity, ask - bid, 0.0, slip * quantity, commission, iso()),
            )
            self._db.execute(
                "UPDATE shadow_orders SET status='FILLED' WHERE order_id=?", (order_id,)
            )
            fill = self._db.execute(
                "SELECT * FROM shadow_fills WHERE fill_key=?", (fill_key,)
            ).fetchone()
        return self._decode(fill)

    def record_funding(self, *, account_id: str, position_id: str,
                       funding_timestamp: datetime | str, amount: float,
                       execution_class: str = EXECUTION_CLASS) -> dict:
        self._shadow(execution_class)
        stamp = iso(funding_timestamp)
        funding_key = key(account_id, position_id, stamp)
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO shadow_funding VALUES (?,?,?,?,?,?)",
                (funding_key, account_id, position_id, stamp, float(amount), iso()),
            )
            row = self._db.execute(
                "SELECT * FROM shadow_funding WHERE funding_key=?", (funding_key,)
            ).fetchone()
        return self._decode(row)

    def funding_total(self, *, account_id: str, position_id: str) -> float:
        """Return immutable funding attribution for one shadow position."""
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM shadow_funding "
                "WHERE account_id=? AND position_id=?",
                (account_id, position_id),
            ).fetchone()
        return float(row[0])

    def observe_mae_mfe(self, order_id: str, quote: dict,
                        *, execution_class: str = EXECUTION_CLASS) -> dict:
        """Update excursion against the executable exit side of a public quote."""
        self._shadow(execution_class)
        try:
            bid, ask = float(quote["bid"]), float(quote["ask"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("MAE/MFE requires public bid and ask") from exc
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT o.side,o.stop_loss,f.executable_side_price,f.quantity "
                "FROM shadow_orders o JOIN shadow_fills f ON f.order_id=o.order_id "
                "WHERE o.order_id=?", (order_id,),
            ).fetchone()
            if not row:
                raise KeyError(order_id)
            side = str(row["side"]).lower()
            entry = float(row["executable_side_price"])
            exit_side = bid if side in {"buy", "long"} else ask
            signed = exit_side - entry if side in {"buy", "long"} else entry - exit_side
            adverse, favorable = max(0.0, -signed), max(0.0, signed)
            risk = abs(entry - float(row["stop_loss"])) if row["stop_loss"] is not None else 0
            prior = self._db.execute(
                "SELECT * FROM shadow_mae_mfe WHERE order_id=?", (order_id,)
            ).fetchone()
            mae = max(float(prior["mae_price"]) if prior else 0, adverse)
            mfe = max(float(prior["mfe_price"]) if prior else 0, favorable)
            values = (
                order_id, mae, mfe, mae / entry * 100, mfe / entry * 100,
                mae / risk if risk else 0, mfe / risk if risk else 0, iso(),
            )
            self._db.execute(
                "INSERT INTO shadow_mae_mfe VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(order_id) DO UPDATE SET mae_price=excluded.mae_price,"
                "mfe_price=excluded.mfe_price,mae_pct=excluded.mae_pct,"
                "mfe_pct=excluded.mfe_pct,mae_r=excluded.mae_r,"
                "mfe_r=excluded.mfe_r,updated_at=excluded.updated_at", values,
            )
            result = self._db.execute(
                "SELECT * FROM shadow_mae_mfe WHERE order_id=?", (order_id,)
            ).fetchone()
        return self._decode(result)

    def observe_open_orders(self, quote: dict,
                            *, execution_class: str = EXECUTION_CLASS) -> list[dict]:
        self._shadow(execution_class)
        with self._lock:
            order_ids = [row[0] for row in self._db.execute(
                "SELECT o.order_id FROM shadow_orders o "
                "JOIN shadow_fills f ON f.order_id=o.order_id "
                "LEFT JOIN shadow_outcomes x ON x.order_id=o.order_id "
                "WHERE o.status='FILLED' AND x.order_id IS NULL"
            ).fetchall()]
        return [self.observe_mae_mfe(order_id, quote) for order_id in order_ids]

    def record_outcome(self, order_id: str, quote: dict, *, exit_reason: str,
                       slippage_bps: float, commission_bps: float,
                       funding: float = 0.0,
                       execution_class: str = EXECUTION_CLASS) -> dict:
        """Close one shadow observation with cost-adjusted, no-double-spread P&L."""
        self._shadow(execution_class)
        try:
            bid, ask = float(quote["bid"]), float(quote["ask"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("shadow outcome requires public bid and ask") from exc
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT o.*,f.executable_side_price,f.slippage AS entry_slippage,"
                "f.commission AS entry_commission FROM shadow_orders o "
                "JOIN shadow_fills f ON f.order_id=o.order_id WHERE o.order_id=?",
                (order_id,),
            ).fetchone()
            if not row:
                raise KeyError(order_id)
            existing = self._db.execute(
                "SELECT * FROM shadow_outcomes WHERE order_id=?", (order_id,)
            ).fetchone()
            if existing:
                return self._decode(existing)
            side = str(row["side"]).lower()
            long = side in {"buy", "long"}
            entry_reference = float(row["executable_side_price"])
            exit_reference = bid if long else ask
            quantity = float(row["quantity"])
            exit_slip_unit = exit_reference * max(0.0, float(slippage_bps)) / 10_000
            exit_price = exit_reference - exit_slip_unit if long else exit_reference + exit_slip_unit
            # Gross uses executable-side references. Their bid/ask difference
            # already contains spread, so spread is never subtracted again.
            gross_pnl = ((exit_reference - entry_reference) if long else
                         (entry_reference - exit_reference)) * quantity
            exit_commission = exit_price * quantity * max(0.0, float(commission_bps)) / 10_000
            commission = float(row["entry_commission"]) + exit_commission
            slippage = float(row["entry_slippage"]) + exit_slip_unit * quantity
            net_pnl = gross_pnl - commission - slippage - float(funding)
            risk = (abs(entry_reference - float(row["stop_loss"])) * quantity
                    if row["stop_loss"] is not None else 0)
            gross_r = gross_pnl / risk if risk else 0
            net_r = net_pnl / risk if risk else 0
            outcome_id = "shadow-outcome-" + hashlib.sha256(order_id.encode()).hexdigest()[:28]
            self._db.execute(
                "INSERT INTO shadow_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (outcome_id, order_id, exit_reason, exit_price, gross_pnl,
                 commission, slippage, float(funding), net_pnl, gross_r, net_r,
                 iso(quote.get("event_timestamp") or quote.get("received_at")),
                 "INSUFFICIENT_SAMPLE"),
            )
            self._db.execute(
                "UPDATE shadow_orders SET status='CLOSED' WHERE order_id=?", (order_id,)
            )
            result = self._db.execute(
                "SELECT * FROM shadow_outcomes WHERE order_id=?", (order_id,)
            ).fetchone()
        return self._decode(result)

    def measurements(self, *, limit: int = 1000) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT d.engine,d.strategy_id,d.strategy_version,d.config_hash,d.execution_class,"
                "d.candle_id,d.snapshot_lineage,d.blocker,d.context_json,o.*,f.*,x.*,m.* "
                "FROM shadow_decisions d LEFT JOIN shadow_orders o ON o.decision_id=d.decision_id "
                "LEFT JOIN shadow_fills f ON f.order_id=o.order_id "
                "LEFT JOIN shadow_outcomes x ON x.order_id=o.order_id "
                "LEFT JOIN shadow_mae_mfe m ON m.order_id=o.order_id "
                "ORDER BY d.created_at DESC LIMIT ?", (max(1, int(limit)),),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        sql = ("SELECT o.*,d.account_id,d.decision_timestamp,d.blocker,d.context_json "
               "FROM shadow_orders o JOIN shadow_decisions d ON d.decision_id=o.decision_id "
               "WHERE o.status IN ('INTENT','SHADOW_REJECTED_INTENT','FILLED')")
        args: tuple = ()
        if symbol:
            sql += " AND o.symbol=?"
            args = (symbol.upper().replace("/", ""),)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY o.created_at", args).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row) -> dict:
        value = dict(row)
        if "context_json" in value:
            value["context"] = json.loads(value.pop("context_json"))
        return value

    def table_counts(self) -> dict[str, int]:
        names = ("shadow_decisions", "shadow_orders", "shadow_fills",
                 "shadow_outcomes", "shadow_mae_mfe", "shadow_funding")
        with self._lock:
            return {name: int(self._db.execute(
                f"SELECT COUNT(*) FROM {name}"  # trusted constant table names
            ).fetchone()[0]) for name in names}

    def decisions(self, *, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM shadow_decisions ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._decode(row) for row in rows]
