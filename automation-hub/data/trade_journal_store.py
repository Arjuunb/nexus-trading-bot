"""Canonical trade journal storage — the single structured record of every trade.

Lives in the same SQLite file as the decision journal (``settings.journal_db``)
so there is one journal database, not two. The legacy
``trade_decision_journal`` table is left untouched; it keeps feeding the
permanent trade memory and is linked to the canonical row by trade id.

Tables
    journal_trades          one row per executed trade (or operational order
                            outcome). Every field used for filtering or
                            statistics is a first-class column.
    journal_trade_links     source references (ledger trade ids, remainder rows
                            after a partial exit, lab order ids, legacy journal
                            ids) -> canonical trade. The primary key on
                            (link_type, ref) is what makes "one executed trade
                            creates exactly one canonical journal trade" a
                            database constraint rather than a hope.
    journal_executions      every fill (entry, partial exit, exit), keyed by
                            execution id, so a replayed fill is a no-op.
    journal_fees            commission / funding per execution.
    journal_modifications   stop/target/size changes after entry. The original
                            values stay on the trade row in initial_* columns.
    journal_snapshots       the frozen decision state at entry (immutable).
    journal_events          the trade timeline (append-only).
    journal_reviews         agent reviews, stored apart from the trade facts.
    journal_corrections     audit trail of every change to an immutable fact.
    journal_notes           manual commentary (append-only).
    journal_weekly_reviews  persisted weekly strategy reviews.

Integrity is enforced by SQLite triggers, not by convention: once an entry is
locked, the identity, entry and risk facts cannot change unless the same
transaction inserts a matching ``journal_corrections`` row and bumps
``correction_seq``. Once a trade is finalised the exit facts are guarded the
same way. Snapshots and every append-only table refuse UPDATE and DELETE.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from data.tenant_scope import ensure_tenant_column

SCHEMA_VERSION = 1

#: Trading modes. Never mixed in analytics unless the caller asks for them
#: explicitly (see ``list_trades(modes=...)``).
TRADING_MODES = ("BACKTEST", "SIMULATION", "FORWARD_PAPER",
                 "ISOLATED_FORWARD_PAPER", "LIVE", "UNKNOWN")

#: Lifecycle statuses.
STATUSES = ("PENDING", "OPEN", "PARTIALLY_CLOSED", "CLOSED", "CANCELLED",
            "REJECTED", "FAILED", "UNCERTAIN")

#: Trade results. The first five are trading outcomes and count in strategy
#: statistics; the rest are operational events and never do.
TRADING_RESULTS = ("WIN", "LOSS", "BREAK_EVEN", "PARTIAL_WIN", "PARTIAL_LOSS")
OPERATIONAL_RESULTS = ("CANCELLED", "REJECTED", "EXECUTION_FAILED", "EXECUTION_UNCERTAIN")
RESULTS = TRADING_RESULTS + OPERATIONAL_RESULTS

# Columns in the order they are declared. Kept in one list so the CREATE, the
# migration and the row mapping cannot drift apart.
_TRADE_COLUMNS: tuple[tuple[str, str], ...] = (
    # identity
    ("trade_id", "TEXT PRIMARY KEY"),
    ("trade_ref", "TEXT UNIQUE"),
    ("source_system", "TEXT NOT NULL"),
    ("source_trade_key", "TEXT NOT NULL"),
    ("order_id", "TEXT"),
    ("execution_id", "TEXT"),
    ("position_id", "TEXT"),
    ("instance_id", "TEXT"),
    ("instance_name", "TEXT"),
    ("bot_id", "TEXT"),
    ("lab_id", "TEXT"),
    ("lab_session_id", "TEXT"),
    ("simulation_session_id", "TEXT"),
    ("strategy_id", "TEXT"),
    ("strategy_name", "TEXT"),
    ("strategy_family", "TEXT"),
    ("strategy_version", "TEXT"),
    ("trade_source", "TEXT"),
    ("trading_mode", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
    ("exchange", "TEXT"),
    ("market_type", "TEXT"),
    ("symbol", "TEXT NOT NULL"),
    ("base_asset", "TEXT"),
    ("quote_asset", "TEXT"),
    ("direction", "TEXT NOT NULL"),
    ("timeframe", "TEXT"),
    ("htf_timeframe", "TEXT"),
    ("status", "TEXT NOT NULL"),
    # entry
    ("signal_at", "TEXT"),
    ("order_created_at", "TEXT"),
    ("entry_filled_at", "TEXT"),
    ("requested_entry_price", "REAL"),
    ("entry_price", "REAL"),
    ("entry_slippage", "REAL"),
    ("entry_slippage_cost", "REAL"),
    ("quantity", "REAL"),
    ("contract_size", "REAL"),
    ("notional_value", "REAL"),
    ("margin_used", "REAL"),
    ("leverage", "REAL"),
    ("leverage_source", "TEXT"),
    ("account_balance_before", "REAL"),
    ("account_equity_before", "REAL"),
    ("available_margin_before", "REAL"),
    ("entry_locked", "INTEGER NOT NULL DEFAULT 0"),
    # risk (initial_* are the values at entry and never change)
    ("initial_stop", "REAL"),
    ("initial_target", "REAL"),
    ("current_stop", "REAL"),
    ("current_target", "REAL"),
    ("stop_distance", "REAL"),
    ("target_distance", "REAL"),
    ("risk_amount", "REAL"),
    ("risk_pct", "REAL"),
    ("planned_reward", "REAL"),
    ("planned_rr", "REAL"),
    ("max_allowed_risk_pct", "REAL"),
    ("max_allowed_risk_amount", "REAL"),
    ("risk_rule_status", "TEXT"),
    # timing
    ("entry_weekday", "TEXT"),
    ("entry_session", "TEXT"),
    ("entry_hour_london", "INTEGER"),
    ("entry_at_london", "TEXT"),
    ("exit_at_london", "TEXT"),
    ("in_preferred_session", "INTEGER"),
    ("session_model", "TEXT"),
    # exit / result
    ("exit_reason", "TEXT"),
    ("exit_reason_source", "TEXT"),
    ("exit_at", "TEXT"),
    ("exit_price", "REAL"),
    ("closed_quantity", "REAL"),
    ("partial_exit_count", "INTEGER NOT NULL DEFAULT 0"),
    ("gross_pnl", "REAL"),
    ("fees_total", "REAL"),
    ("funding_total", "REAL"),
    ("slippage_cost_total", "REAL"),
    ("net_pnl", "REAL"),
    ("pnl_pct", "REAL"),
    ("return_on_margin_pct", "REAL"),
    ("gross_r", "REAL"),
    ("realised_r", "REAL"),
    ("duration_s", "REAL"),
    ("result", "TEXT"),
    ("result_reason", "TEXT"),
    ("is_operational", "INTEGER NOT NULL DEFAULT 0"),
    ("counts_in_stats", "INTEGER NOT NULL DEFAULT 0"),
    ("finalised_at", "TEXT"),
    # excursions
    ("mfe_price", "REAL"),
    ("mae_price", "REAL"),
    ("mfe_amount", "REAL"),
    ("mae_amount", "REAL"),
    ("mfe_r", "REAL"),
    ("mae_r", "REAL"),
    ("excursion_source", "TEXT"),
    # decision summary (filterable copies of the frozen snapshot)
    ("decision", "TEXT"),
    ("setup_score", "REAL"),
    ("confidence", "REAL"),
    ("htf_bias", "TEXT"),
    ("market_regime", "TEXT"),
    ("rule_violation", "INTEGER"),
    ("rule_violation_count", "INTEGER"),
    # integrity / provenance
    ("data_completeness", "TEXT"),
    ("provenance", "TEXT"),
    ("correction_seq", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "TEXT NOT NULL"),
    ("updated_at", "TEXT NOT NULL"),
)
TRADE_COLUMNS = tuple(name for name, _ in _TRADE_COLUMNS)

#: Identity facts: may be filled once, never changed without a correction.
IDENTITY_FIELDS = (
    "trade_ref", "source_system", "source_trade_key", "order_id", "instance_id",
    "lab_id", "strategy_id", "strategy_name", "strategy_version", "trade_source",
    "trading_mode", "symbol", "direction", "timeframe", "htf_timeframe",
)
#: Entry and risk facts: frozen once ``entry_locked`` = 1.
ENTRY_FIELDS = (
    "signal_at", "order_created_at", "entry_filled_at", "requested_entry_price",
    "entry_price", "quantity", "notional_value", "margin_used", "leverage",
    "account_balance_before", "account_equity_before", "available_margin_before",
    "initial_stop", "initial_target", "risk_amount", "risk_pct", "planned_reward",
    "planned_rr", "stop_distance", "target_distance", "max_allowed_risk_pct",
    "risk_rule_status", "entry_session", "entry_weekday", "entry_hour_london",
)
#: Exit facts: frozen once ``finalised_at`` is set.
EXIT_FIELDS = (
    "exit_reason", "exit_at", "exit_price", "closed_quantity", "gross_pnl",
    "fees_total", "funding_total", "net_pnl", "realised_r", "gross_r", "result",
    "duration_s",
)
#: Fields an operator may correct through the audit trail.
CORRECTABLE_FIELDS = tuple(dict.fromkeys(
    IDENTITY_FIELDS[3:] + ENTRY_FIELDS + EXIT_FIELDS
    + ("exit_reason_source", "mfe_r", "mae_r", "mfe_amount", "mae_amount",
       "htf_bias", "market_regime", "position_id", "instance_name")))

_APPEND_ONLY = ("journal_executions", "journal_fees", "journal_modifications",
                "journal_events", "journal_reviews", "journal_corrections",
                "journal_notes", "journal_snapshots", "journal_weekly_reviews")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str)


def _guard_sql(fields: Iterable[str]) -> str:
    return " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in fields)


_CORRECTION_PRESENT = (
    "(NEW.correction_seq > OLD.correction_seq AND EXISTS ("
    "SELECT 1 FROM journal_corrections c WHERE c.trade_id = OLD.trade_id "
    "AND c.seq = NEW.correction_seq))")


class TradeJournalStore:
    """Thread-safe SQLite store for the canonical journal."""

    def __init__(self, path: str = ":memory:", connection: Optional[sqlite3.Connection] = None):
        self.path = str(path)
        if connection is None:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, check_same_thread=False)
        self._c = connection
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._schema()

    # ------------------------------------------------------------------ schema
    def _schema(self) -> None:
        cols = ",\n  ".join(f"{name} {decl}" for name, decl in _TRADE_COLUMNS)
        self._c.executescript(f"""
        CREATE TABLE IF NOT EXISTS journal_trades (
          {cols},
          UNIQUE(source_system, source_trade_key));
        CREATE TABLE IF NOT EXISTS journal_trade_links (
          link_type TEXT NOT NULL, ref TEXT NOT NULL, trade_id TEXT NOT NULL,
          created_at TEXT NOT NULL, PRIMARY KEY(link_type, ref));
        CREATE TABLE IF NOT EXISTS journal_executions (
          execution_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, kind TEXT NOT NULL,
          side TEXT, quantity REAL, requested_price REAL, price REAL, fee REAL,
          slippage REAL, slippage_cost REAL, realized_gross_pnl REAL,
          liquidity TEXT, executed_at TEXT, source_ref TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS journal_fees (
          id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, fee_type TEXT NOT NULL,
          amount REAL NOT NULL, currency TEXT, rate REAL, basis REAL,
          source_ref TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(trade_id, fee_type, source_ref));
        CREATE TABLE IF NOT EXISTS journal_modifications (
          id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, field TEXT NOT NULL,
          old_value REAL, new_value REAL, reason TEXT NOT NULL, actor TEXT NOT NULL,
          detail TEXT, modified_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS journal_snapshots (
          trade_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, decision TEXT,
          decision_reason TEXT, conditions_passed TEXT, conditions_failed TEXT,
          conditions_missing TEXT, confidence REAL, setup_score REAL,
          market_bias TEXT, htf_bias TEXT, risk_decision TEXT, feed_health TEXT,
          candle_freshness TEXT, htf_freshness TEXT, strategy_state TEXT,
          execution_state TEXT, strategy_family TEXT, setup_json TEXT,
          market_context_json TEXT, risk_json TEXT, raw_json TEXT,
          source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS journal_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT NOT NULL,
          ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT, actor TEXT,
          payload_json TEXT, event_key TEXT UNIQUE, recorded_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS journal_reviews (
          id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, reviewer TEXT NOT NULL,
          review_version TEXT NOT NULL, created_at TEXT NOT NULL,
          setup_quality TEXT, execution_quality TEXT, risk_management TEXT,
          outcome TEXT, grade TEXT, summary TEXT, mistakes TEXT, went_well TEXT,
          went_wrong TEXT, improvement TEXT, rule_violations TEXT, raw_json TEXT);
        CREATE TABLE IF NOT EXISTS journal_corrections (
          id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, seq INTEGER NOT NULL,
          field TEXT NOT NULL, previous_value TEXT, new_value TEXT,
          reason TEXT NOT NULL, actor TEXT NOT NULL, corrected_at TEXT NOT NULL,
          UNIQUE(trade_id, seq, field));
        CREATE TABLE IF NOT EXISTS journal_notes (
          id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, note TEXT NOT NULL,
          author TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS journal_weekly_reviews (
          id TEXT PRIMARY KEY, week_key TEXT NOT NULL, scope_json TEXT NOT NULL,
          generated_by TEXT NOT NULL, created_at TEXT NOT NULL, report_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS journal_sequences (
          year INTEGER PRIMARY KEY, last_value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS journal_meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE INDEX IF NOT EXISTS idx_jt_mode_entry ON journal_trades(trading_mode, entry_filled_at);
        CREATE INDEX IF NOT EXISTS idx_jt_strategy ON journal_trades(strategy_id, strategy_name);
        CREATE INDEX IF NOT EXISTS idx_jt_instance ON journal_trades(instance_id);
        CREATE INDEX IF NOT EXISTS idx_jt_symbol ON journal_trades(symbol);
        CREATE INDEX IF NOT EXISTS idx_jt_status ON journal_trades(status, result);
        CREATE INDEX IF NOT EXISTS idx_jt_session ON journal_trades(entry_session);
        CREATE INDEX IF NOT EXISTS idx_jt_source ON journal_trades(source_system, trade_source);
        CREATE INDEX IF NOT EXISTS idx_jlink_trade ON journal_trade_links(trade_id);
        CREATE INDEX IF NOT EXISTS idx_jexec_trade ON journal_executions(trade_id, executed_at);
        CREATE INDEX IF NOT EXISTS idx_jfee_trade ON journal_fees(trade_id);
        CREATE INDEX IF NOT EXISTS idx_jmod_trade ON journal_modifications(trade_id, modified_at);
        CREATE INDEX IF NOT EXISTS idx_jevt_trade ON journal_events(trade_id, ts);
        CREATE INDEX IF NOT EXISTS idx_jrev_trade ON journal_reviews(trade_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_jcor_trade ON journal_corrections(trade_id, seq);
        CREATE INDEX IF NOT EXISTS idx_jweek ON journal_weekly_reviews(week_key, created_at);
        """)
        # Additive column migration for databases created by an older build.
        existing = {row[1] for row in self._c.execute("PRAGMA table_info(journal_trades)")}
        for name, decl in _TRADE_COLUMNS:
            if name not in existing:
                plain = decl.replace(" PRIMARY KEY", "").replace(" UNIQUE", "")
                self._c.execute(f"ALTER TABLE journal_trades ADD COLUMN {name} {plain}")
        self._create_triggers()
        for table in ("journal_trades",) + _APPEND_ONLY:
            ensure_tenant_column(self._c, table)
        self._c.execute("INSERT OR IGNORE INTO journal_meta(key, value) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),))
        self._c.commit()

    def _create_triggers(self) -> None:
        identity_guard = " OR ".join(
            f"(OLD.{name} IS NOT NULL AND NEW.{name} IS NOT OLD.{name})"
            for name in IDENTITY_FIELDS)
        statements = [
            f"""CREATE TRIGGER IF NOT EXISTS jt_identity_immutable
                BEFORE UPDATE ON journal_trades
                WHEN ({identity_guard}) AND NOT {_CORRECTION_PRESENT}
                BEGIN SELECT RAISE(ABORT, 'journal identity facts are immutable; use a correction'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS jt_entry_immutable
                BEFORE UPDATE ON journal_trades
                WHEN OLD.entry_locked = 1 AND ({_guard_sql(ENTRY_FIELDS)}) AND NOT {_CORRECTION_PRESENT}
                BEGIN SELECT RAISE(ABORT, 'journal entry facts are immutable; use a correction'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS jt_exit_immutable
                BEFORE UPDATE ON journal_trades
                WHEN OLD.finalised_at IS NOT NULL AND ({_guard_sql(EXIT_FIELDS)}) AND NOT {_CORRECTION_PRESENT}
                BEGIN SELECT RAISE(ABORT, 'journal exit facts are immutable once finalised; use a correction'); END""",
            """CREATE TRIGGER IF NOT EXISTS jt_lock_is_one_way
               BEFORE UPDATE ON journal_trades
               WHEN NEW.entry_locked < OLD.entry_locked
                 OR (OLD.finalised_at IS NOT NULL AND NEW.finalised_at IS NOT OLD.finalised_at)
                 OR NEW.correction_seq < OLD.correction_seq
               BEGIN SELECT RAISE(ABORT, 'journal locks cannot be released'); END""",
            """CREATE TRIGGER IF NOT EXISTS jt_no_delete
               BEFORE DELETE ON journal_trades
               BEGIN SELECT RAISE(ABORT, 'journal trades are never deleted'); END""",
            """CREATE TRIGGER IF NOT EXISTS jlink_no_update
               BEFORE UPDATE ON journal_trade_links
               BEGIN SELECT RAISE(ABORT, 'journal links are immutable'); END""",
            """CREATE TRIGGER IF NOT EXISTS jlink_no_delete
               BEFORE DELETE ON journal_trade_links
               BEGIN SELECT RAISE(ABORT, 'journal links are immutable'); END""",
        ]
        for table in _APPEND_ONLY:
            statements.append(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END""")
            statements.append(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END""")
        for sql in statements:
            self._c.execute(sql)

    def _drop_triggers(self) -> None:
        for row in self._c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND "
                "(tbl_name LIKE 'journal_%')").fetchall():
            self._c.execute(f"DROP TRIGGER IF EXISTS {row[0]}")

    def factory_reset(self) -> None:
        """Clear every journal table. Only the audited factory reset calls this;
        the triggers are restored before the lock is released."""
        with self._lock:
            self._drop_triggers()
            try:
                for table in ("journal_trades", "journal_trade_links", "journal_sequences",
                              "journal_meta") + _APPEND_ONLY:
                    self._c.execute(f"DELETE FROM {table}")
                self._c.execute("DELETE FROM sqlite_sequence WHERE name='journal_events'") \
                    if self._c.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() else None
                self._c.execute("INSERT OR IGNORE INTO journal_meta(key, value) VALUES ('schema_version', ?)",
                                (str(SCHEMA_VERSION),))
            finally:
                self._create_triggers()
                self._c.commit()

    # ------------------------------------------------------------------ helpers
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._c.execute("SELECT value FROM journal_meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._c.execute("INSERT OR REPLACE INTO journal_meta(key, value) VALUES (?, ?)", (key, value))
            self._c.commit()

    def _next_ref(self, when: Optional[str]) -> str:
        try:
            year = datetime.fromisoformat(str(when).replace("Z", "+00:00")).year if when else None
        except ValueError:
            year = None
        year = year or datetime.now(timezone.utc).year
        row = self._c.execute("SELECT last_value FROM journal_sequences WHERE year=?", (year,)).fetchone()
        value = (row[0] if row else 0) + 1
        self._c.execute("INSERT OR REPLACE INTO journal_sequences(year, last_value) VALUES (?, ?)",
                        (year, value))
        return f"TRD-{year}-{value:06d}"

    # ------------------------------------------------------------------ trades
    def create_trade(self, fields: dict, *, links: Iterable[tuple[str, str]] = ()) -> tuple[str, bool]:
        """Insert a canonical trade. Returns ``(trade_id, created)``.

        Idempotent on ``(source_system, source_trade_key)`` and on every link:
        if either already exists the existing trade id is returned and nothing
        is written."""
        links = [(t, r) for t, r in links if r]
        with self._lock:
            existing = self._c.execute(
                "SELECT trade_id FROM journal_trades WHERE source_system=? AND source_trade_key=?",
                (fields["source_system"], fields["source_trade_key"])).fetchone()
            if existing:
                return existing[0], False
            for link_type, ref in links:
                hit = self.resolve_link(link_type, ref)
                if hit:
                    return hit, False
            now = _now()
            row = {name: fields.get(name) for name in TRADE_COLUMNS}
            row["trade_id"] = fields.get("trade_id") or uuid.uuid4().hex
            row["trade_ref"] = fields.get("trade_ref") or self._next_ref(
                fields.get("entry_filled_at") or fields.get("order_created_at")
                or fields.get("signal_at"))
            row["created_at"] = fields.get("created_at") or now
            row["updated_at"] = now
            for flag in ("entry_locked", "partial_exit_count", "is_operational",
                         "counts_in_stats", "correction_seq"):
                row[flag] = int(row.get(flag) or 0)
            row["trading_mode"] = row.get("trading_mode") or "UNKNOWN"
            names = [name for name in TRADE_COLUMNS]
            try:
                self._c.execute("SAVEPOINT create_trade")
                self._c.execute(
                    f"INSERT INTO journal_trades({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                    [row[name] for name in names])
                for link_type, ref in links:
                    self._c.execute(
                        "INSERT INTO journal_trade_links(link_type, ref, trade_id, created_at) VALUES (?,?,?,?)",
                        (link_type, str(ref), row["trade_id"], now))
                self._c.execute("RELEASE SAVEPOINT create_trade")
                self._c.commit()
            except Exception:
                self._c.execute("ROLLBACK TO SAVEPOINT create_trade")
                self._c.execute("RELEASE SAVEPOINT create_trade")
                self._c.commit()
                raise
            return row["trade_id"], True

    def update_trade(self, trade_id: str, fields: dict) -> int:
        """Update mutable lifecycle columns. Guarded facts are protected by
        triggers; callers that need to change one use :meth:`correct`."""
        fields = {k: v for k, v in fields.items() if k in TRADE_COLUMNS and k not in (
            "trade_id", "created_at", "correction_seq")}
        if not fields:
            return 0
        fields["updated_at"] = _now()
        sets = ", ".join(f"{name}=?" for name in fields)
        with self._lock:
            cur = self._c.execute(f"UPDATE journal_trades SET {sets} WHERE trade_id=?",
                                  [*fields.values(), trade_id])
            self._c.commit()
            return cur.rowcount or 0

    def fill_missing(self, trade_id: str, fields: dict) -> list[str]:
        """Set only fields that are currently NULL (first-write-wins). Returns
        the names actually written. Used by reconciliation so it can restore
        missing information without ever overwriting a recorded fact."""
        current = self.get_trade(trade_id)
        if current is None:
            return []
        missing = {k: v for k, v in fields.items()
                   if k in TRADE_COLUMNS and v is not None and current.get(k) is None}
        if missing:
            self.update_trade(trade_id, missing)
        return sorted(missing)

    def correct(self, trade_id: str, changes: dict, *, reason: str, actor: str) -> dict:
        """Change guarded facts through the audit trail.

        Writes one ``journal_corrections`` row per field with the previous and
        new value, then updates the trade with an incremented
        ``correction_seq`` in the same transaction. The triggers verify the
        correction rows exist; without them the update is refused."""
        if not reason or not str(reason).strip():
            raise ValueError("a correction requires a reason")
        if not actor or not str(actor).strip():
            raise ValueError("a correction requires an actor")
        unknown = [k for k in changes if k not in CORRECTABLE_FIELDS]
        if unknown:
            raise ValueError(f"fields are not correctable: {', '.join(sorted(unknown))}")
        with self._lock:
            row = self._c.execute("SELECT * FROM journal_trades WHERE trade_id=?", (trade_id,)).fetchone()
            if row is None:
                raise KeyError(trade_id)
            current = dict(row)
            real = {k: v for k, v in changes.items() if current.get(k) != v}
            if not real:
                return {"trade_id": trade_id, "changed": [], "seq": current["correction_seq"]}
            seq = int(current["correction_seq"] or 0) + 1
            now = _now()
            try:
                self._c.execute("SAVEPOINT correction")
                for name, value in real.items():
                    self._c.execute(
                        "INSERT INTO journal_corrections(id, trade_id, seq, field, previous_value, "
                        "new_value, reason, actor, corrected_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, trade_id, seq, name, _json(current.get(name)),
                         _json(value), str(reason).strip(), str(actor).strip(), now))
                sets = ", ".join(f"{name}=?" for name in real)
                self._c.execute(
                    f"UPDATE journal_trades SET {sets}, correction_seq=?, updated_at=? WHERE trade_id=?",
                    [*real.values(), seq, now, trade_id])
                self._c.execute(
                    "INSERT INTO journal_events(trade_id, ts, kind, detail, actor, payload_json, recorded_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (trade_id, now, "journal-corrected",
                     f"{', '.join(sorted(real))} corrected: {str(reason).strip()}",
                     str(actor).strip(), _json({"seq": seq, "fields": sorted(real)}), now))
                self._c.execute("RELEASE SAVEPOINT correction")
                self._c.commit()
            except Exception:
                self._c.execute("ROLLBACK TO SAVEPOINT correction")
                self._c.execute("RELEASE SAVEPOINT correction")
                self._c.commit()
                raise
            return {"trade_id": trade_id, "changed": sorted(real), "seq": seq}

    def get_trade(self, trade_id: str) -> Optional[dict]:
        with self._lock:
            row = self._c.execute("SELECT * FROM journal_trades WHERE trade_id=?", (trade_id,)).fetchone()
            return self._decode(row) if row else None

    def find(self, ref: str) -> Optional[dict]:
        """Resolve a canonical id, a TRD reference or any linked source id."""
        if not ref:
            return None
        with self._lock:
            row = self._c.execute("SELECT * FROM journal_trades WHERE trade_id=? OR trade_ref=?",
                                  (ref, ref)).fetchone()
            if row is None:
                link = self._c.execute("SELECT trade_id FROM journal_trade_links WHERE ref=? LIMIT 1",
                                       (ref,)).fetchone()
                if link:
                    row = self._c.execute("SELECT * FROM journal_trades WHERE trade_id=?",
                                          (link[0],)).fetchone()
            return self._decode(row) if row else None

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        data = dict(row)
        if data.get("provenance"):
            try:
                data["provenance"] = json.loads(data["provenance"])
            except (TypeError, ValueError):
                pass
        for flag in ("entry_locked", "is_operational", "counts_in_stats"):
            data[flag] = bool(data.get(flag))
        for flag in ("rule_violation", "in_preferred_session"):
            if data.get(flag) is not None:
                data[flag] = bool(data[flag])
        return data

    # ------------------------------------------------------------------ links
    def resolve_link(self, link_type: str, ref: str) -> Optional[str]:
        with self._lock:
            row = self._c.execute(
                "SELECT trade_id FROM journal_trade_links WHERE link_type=? AND ref=?",
                (link_type, str(ref))).fetchone()
            return row[0] if row else None

    def add_link(self, trade_id: str, link_type: str, ref: str) -> bool:
        if not ref:
            return False
        with self._lock:
            cur = self._c.execute(
                "INSERT OR IGNORE INTO journal_trade_links(link_type, ref, trade_id, created_at) "
                "VALUES (?,?,?,?)", (link_type, str(ref), trade_id, _now()))
            self._c.commit()
            return bool(cur.rowcount)

    def links(self, trade_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT link_type, ref, created_at FROM journal_trade_links WHERE trade_id=? "
                "ORDER BY created_at", (trade_id,))]

    def linked_refs(self, link_type: str) -> set[str]:
        with self._lock:
            return {r[0] for r in self._c.execute(
                "SELECT ref FROM journal_trade_links WHERE link_type=?", (link_type,))}

    # ------------------------------------------------------------- executions
    def add_execution(self, trade_id: str, execution: dict) -> bool:
        """Append one fill. Returns False when the execution id is already
        recorded (duplicate execution prevention)."""
        execution_id = str(execution.get("execution_id") or "")
        if not execution_id:
            raise ValueError("an execution requires an execution_id")
        with self._lock:
            cur = self._c.execute(
                "INSERT OR IGNORE INTO journal_executions(execution_id, trade_id, kind, side, quantity, "
                "requested_price, price, fee, slippage, slippage_cost, realized_gross_pnl, liquidity, "
                "executed_at, source_ref, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (execution_id, trade_id, execution["kind"], execution.get("side"),
                 execution.get("quantity"), execution.get("requested_price"), execution.get("price"),
                 execution.get("fee"), execution.get("slippage"), execution.get("slippage_cost"),
                 execution.get("realized_gross_pnl"), execution.get("liquidity"),
                 execution.get("executed_at"), execution.get("source_ref"), _now()))
            self._c.commit()
            return bool(cur.rowcount)

    def executions(self, trade_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT * FROM journal_executions WHERE trade_id=? ORDER BY executed_at, created_at",
                (trade_id,))]

    def has_execution(self, execution_id: str) -> bool:
        with self._lock:
            return self._c.execute("SELECT 1 FROM journal_executions WHERE execution_id=?",
                                   (execution_id,)).fetchone() is not None

    # ------------------------------------------------------------------ fees
    def add_fee(self, trade_id: str, *, fee_type: str, amount: float, source_ref: str,
                rate: Optional[float] = None, basis: Optional[float] = None,
                currency: str = "USDT") -> bool:
        with self._lock:
            cur = self._c.execute(
                "INSERT OR IGNORE INTO journal_fees(id, trade_id, fee_type, amount, currency, rate, basis, "
                "source_ref, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, trade_id, fee_type, float(amount), currency, rate, basis,
                 str(source_ref), _now()))
            self._c.commit()
            return bool(cur.rowcount)

    def fees(self, trade_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT fee_type, amount, currency, rate, basis, source_ref, created_at "
                "FROM journal_fees WHERE trade_id=? ORDER BY created_at", (trade_id,))]

    # --------------------------------------------------------- modifications
    def add_modification(self, trade_id: str, *, field: str, old_value, new_value,
                         reason: str, actor: str, detail: str = "",
                         modified_at: Optional[str] = None) -> None:
        with self._lock:
            self._c.execute(
                "INSERT INTO journal_modifications(id, trade_id, field, old_value, new_value, reason, "
                "actor, detail, modified_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, trade_id, field, old_value, new_value, reason, actor, detail,
                 modified_at or _now()))
            self._c.commit()

    def modifications(self, trade_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT field, old_value, new_value, reason, actor, detail, modified_at "
                "FROM journal_modifications WHERE trade_id=? ORDER BY modified_at", (trade_id,))]

    # ------------------------------------------------------------- snapshots
    def save_snapshot(self, trade_id: str, snapshot: dict) -> bool:
        """Freeze the decision snapshot. First write wins; later writes are
        ignored, so a restart or a retry can never alter what was decided."""
        with self._lock:
            cur = self._c.execute(
                "INSERT OR IGNORE INTO journal_snapshots(trade_id, captured_at, decision, decision_reason, "
                "conditions_passed, conditions_failed, conditions_missing, confidence, setup_score, "
                "market_bias, htf_bias, risk_decision, feed_health, candle_freshness, htf_freshness, "
                "strategy_state, execution_state, strategy_family, setup_json, market_context_json, "
                "risk_json, raw_json, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (trade_id, snapshot.get("captured_at") or _now(), snapshot.get("decision"),
                 snapshot.get("decision_reason"), _json(snapshot.get("conditions_passed") or []),
                 _json(snapshot.get("conditions_failed") or []),
                 _json(snapshot.get("conditions_missing") or []), snapshot.get("confidence"),
                 snapshot.get("setup_score"), snapshot.get("market_bias"), snapshot.get("htf_bias"),
                 snapshot.get("risk_decision"), snapshot.get("feed_health"),
                 snapshot.get("candle_freshness"), snapshot.get("htf_freshness"),
                 snapshot.get("strategy_state"), snapshot.get("execution_state"),
                 snapshot.get("strategy_family"), _json(snapshot.get("setup")),
                 _json(snapshot.get("market_context")), _json(snapshot.get("risk")),
                 _json(snapshot.get("raw")), snapshot.get("source") or "unknown"))
            self._c.commit()
            return bool(cur.rowcount)

    def snapshot(self, trade_id: str) -> Optional[dict]:
        with self._lock:
            row = self._c.execute("SELECT * FROM journal_snapshots WHERE trade_id=?", (trade_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        for key, out in (("conditions_passed", "conditions_passed"),
                         ("conditions_failed", "conditions_failed"),
                         ("conditions_missing", "conditions_missing"),
                         ("setup_json", "setup"), ("market_context_json", "market_context"),
                         ("risk_json", "risk"), ("raw_json", "raw")):
            raw = data.pop(key, None)
            try:
                data[out] = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                data[out] = raw
        return data

    # ---------------------------------------------------------------- events
    def add_event(self, trade_id: str, kind: str, detail: str = "", *,
                  ts: Optional[str] = None, actor: str = "journal",
                  payload: Optional[dict] = None, event_key: Optional[str] = None) -> bool:
        with self._lock:
            cur = self._c.execute(
                "INSERT OR IGNORE INTO journal_events(trade_id, ts, kind, detail, actor, payload_json, "
                "event_key, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                (trade_id, ts or _now(), kind, detail, actor, _json(payload), event_key, _now()))
            self._c.commit()
            return bool(cur.rowcount)

    def events(self, trade_id: str) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._c.execute(
                "SELECT ts, kind, detail, actor, payload_json, recorded_at FROM journal_events "
                "WHERE trade_id=? ORDER BY ts, id", (trade_id,))]
        for row in rows:
            raw = row.pop("payload_json", None)
            row["payload"] = json.loads(raw) if raw else None
        return rows

    # --------------------------------------------------------------- reviews
    def add_review(self, trade_id: str, review: dict) -> dict:
        row = {
            "id": uuid.uuid4().hex, "trade_id": trade_id,
            "reviewer": str(review.get("reviewer") or "unknown"),
            "review_version": str(review.get("review_version") or "1"),
            "created_at": _now(),
            "setup_quality": review.get("setup_quality"),
            "execution_quality": review.get("execution_quality"),
            "risk_management": review.get("risk_management"),
            "outcome": review.get("outcome"), "grade": review.get("grade"),
            "summary": review.get("summary"),
            "mistakes": _json(list(review.get("mistakes") or [])),
            "went_well": _json(list(review.get("went_well") or [])),
            "went_wrong": _json(list(review.get("went_wrong") or [])),
            "improvement": review.get("improvement"),
            "rule_violations": _json(list(review.get("rule_violations") or [])),
            "raw_json": _json(review),
        }
        with self._lock:
            names = list(row)
            self._c.execute(f"INSERT INTO journal_reviews({','.join(names)}) VALUES "
                            f"({','.join('?' for _ in names)})", [row[n] for n in names])
            self._c.commit()
        return self._decode_review(row)

    @staticmethod
    def _decode_review(row: dict) -> dict:
        data = dict(row)
        for key in ("mistakes", "went_well", "went_wrong", "rule_violations"):
            try:
                data[key] = json.loads(data[key]) if data.get(key) else []
            except (TypeError, ValueError):
                data[key] = []
        data.pop("raw_json", None)
        return data

    def reviews(self, trade_id: str) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._c.execute(
                "SELECT * FROM journal_reviews WHERE trade_id=? ORDER BY created_at", (trade_id,))]
        return [self._decode_review(r) for r in rows]

    def reviews_for(self, trade_ids: Iterable[str]) -> dict[str, dict]:
        """Latest review per trade, for weekly roll-ups."""
        ids = list(trade_ids)
        out: dict[str, dict] = {}
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for r in self._c.execute(
                        f"SELECT * FROM journal_reviews WHERE trade_id IN ({','.join('?' for _ in chunk)}) "
                        "ORDER BY created_at", chunk):
                    out[r["trade_id"]] = self._decode_review(dict(r))
        return out

    # ----------------------------------------------------- corrections/notes
    def corrections(self, trade_id: str) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._c.execute(
                "SELECT seq, field, previous_value, new_value, reason, actor, corrected_at "
                "FROM journal_corrections WHERE trade_id=? ORDER BY seq, field", (trade_id,))]
        for row in rows:
            for key in ("previous_value", "new_value"):
                try:
                    row[key] = json.loads(row[key]) if row[key] is not None else None
                except (TypeError, ValueError):
                    pass
        return rows

    def add_note(self, trade_id: str, note: str, author: str) -> dict:
        if not note or not note.strip():
            raise ValueError("a note cannot be empty")
        row = {"id": uuid.uuid4().hex, "trade_id": trade_id, "note": note.strip()[:4000],
               "author": author or "operator", "created_at": _now()}
        with self._lock:
            self._c.execute("INSERT INTO journal_notes(id, trade_id, note, author, created_at) "
                            "VALUES (?,?,?,?,?)", tuple(row.values()))
            self._c.commit()
        return row

    def notes(self, trade_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT id, note, author, created_at FROM journal_notes WHERE trade_id=? "
                "ORDER BY created_at", (trade_id,))]

    # --------------------------------------------------------- weekly reviews
    def save_weekly_review(self, week_key: str, scope: dict, report: dict,
                           generated_by: str) -> dict:
        row = {"id": uuid.uuid4().hex, "week_key": week_key, "scope_json": _json(scope),
               "generated_by": generated_by, "created_at": _now(), "report_json": _json(report)}
        with self._lock:
            self._c.execute("INSERT INTO journal_weekly_reviews(id, week_key, scope_json, generated_by, "
                            "created_at, report_json) VALUES (?,?,?,?,?,?)", tuple(row.values()))
            self._c.commit()
        return {**row, "scope": scope, "report": report}

    def weekly_reviews(self, week_key: Optional[str] = None, limit: int = 20) -> list[dict]:
        query = "SELECT * FROM journal_weekly_reviews"
        args: list = []
        if week_key:
            query += " WHERE week_key=?"
            args.append(week_key)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = [dict(r) for r in self._c.execute(query, args)]
        for row in rows:
            row["scope"] = json.loads(row.pop("scope_json") or "{}")
            row["report"] = json.loads(row.pop("report_json") or "{}")
        return rows

    # ----------------------------------------------------------------- query
    def list_trades(self, *, modes: Optional[Iterable[str]] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, strategy: Optional[str] = None,
                    instance_id: Optional[str] = None, lab: Optional[str] = None,
                    trade_source: Optional[str] = None, symbol: Optional[str] = None,
                    direction: Optional[str] = None, result: Optional[str] = None,
                    session: Optional[str] = None, timeframe: Optional[str] = None,
                    leverage_min: Optional[float] = None, leverage_max: Optional[float] = None,
                    rr_min: Optional[float] = None, rr_max: Optional[float] = None,
                    realised_r_min: Optional[float] = None, realised_r_max: Optional[float] = None,
                    pnl_min: Optional[float] = None, pnl_max: Optional[float] = None,
                    rule_violation: Optional[bool] = None, exit_reason: Optional[str] = None,
                    status: Optional[str] = None, include_operational: bool = True,
                    only_stats: bool = False, limit: Optional[int] = None, offset: int = 0,
                    order: str = "desc") -> list[dict]:
        where, args = self._where(
            modes=modes, date_from=date_from, date_to=date_to, strategy=strategy,
            instance_id=instance_id, lab=lab, trade_source=trade_source, symbol=symbol,
            direction=direction, result=result, session=session, timeframe=timeframe,
            leverage_min=leverage_min, leverage_max=leverage_max, rr_min=rr_min, rr_max=rr_max,
            realised_r_min=realised_r_min, realised_r_max=realised_r_max,
            pnl_min=pnl_min, pnl_max=pnl_max, rule_violation=rule_violation,
            exit_reason=exit_reason, status=status, include_operational=include_operational,
            only_stats=only_stats)
        query = "SELECT * FROM journal_trades"
        if where:
            query += " WHERE " + " AND ".join(where)
        direction_sql = "ASC" if str(order).lower() == "asc" else "DESC"
        query += (" ORDER BY COALESCE(entry_filled_at, order_created_at, signal_at, created_at) "
                  f"{direction_sql}, trade_ref {direction_sql}")
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            args.extend([int(limit), int(offset)])
        with self._lock:
            return [self._decode(r) for r in self._c.execute(query, args)]

    def count_trades(self, **filters) -> int:
        where, args = self._where(**filters)
        query = "SELECT COUNT(*) FROM journal_trades"
        if where:
            query += " WHERE " + " AND ".join(where)
        with self._lock:
            return int(self._c.execute(query, args).fetchone()[0])

    def mode_counts(self) -> dict[str, int]:
        with self._lock:
            return {r[0]: r[1] for r in self._c.execute(
                "SELECT trading_mode, COUNT(*) FROM journal_trades GROUP BY trading_mode")}

    def facets(self) -> dict:
        """Distinct values for filter dropdowns."""
        out = {}
        with self._lock:
            for name in ("strategy_name", "instance_id", "lab_id", "trade_source", "symbol",
                         "timeframe", "entry_session", "exit_reason", "trading_mode",
                         "source_system"):
                out[name] = [r[0] for r in self._c.execute(
                    f"SELECT DISTINCT {name} FROM journal_trades WHERE {name} IS NOT NULL "
                    f"AND {name} != '' ORDER BY {name}")]
            out["instances"] = [dict(r) for r in self._c.execute(
                "SELECT instance_id, MAX(instance_name) AS instance_name FROM journal_trades "
                "WHERE instance_id IS NOT NULL AND instance_id != '' GROUP BY instance_id")]
            out["leverage"] = [r[0] for r in self._c.execute(
                "SELECT DISTINCT leverage FROM journal_trades WHERE leverage IS NOT NULL ORDER BY leverage")]
        return out

    @staticmethod
    def _where(*, modes=None, date_from=None, date_to=None, strategy=None, instance_id=None,
               lab=None, trade_source=None, symbol=None, direction=None, result=None,
               session=None, timeframe=None, leverage_min=None, leverage_max=None,
               rr_min=None, rr_max=None, realised_r_min=None, realised_r_max=None,
               pnl_min=None, pnl_max=None, rule_violation=None, exit_reason=None,
               status=None, include_operational=True, only_stats=False, **_ignored):
        where: list[str] = []
        args: list = []
        modes = [m.upper() for m in (modes or []) if m]
        if modes and "ALL" not in modes:
            where.append(f"trading_mode IN ({','.join('?' for _ in modes)})")
            args.extend(modes)
        when = "COALESCE(entry_filled_at, order_created_at, signal_at, created_at)"
        if date_from:
            where.append(f"{when} >= ?")
            args.append(date_from)
        if date_to:
            # A bare date means "through the end of that day".
            where.append(f"{when} <= ?")
            args.append(date_to + "T23:59:59.999999+00:00" if len(date_to) == 10 else date_to)
        if strategy:
            where.append("(strategy_id = ? OR strategy_name = ? OR strategy_family = ?)")
            args.extend([strategy, strategy, strategy])
        if instance_id:
            where.append("instance_id = ?")
            args.append(instance_id)
        if lab:
            where.append("lab_id = ?")
            args.append(lab)
        if trade_source:
            where.append("trade_source = ?")
            args.append(trade_source)
        if symbol:
            where.append("symbol = ?")
            args.append(symbol.upper())
        if direction:
            where.append("direction = ?")
            args.append(direction.upper())
        if result:
            values = [r.strip().upper() for r in str(result).split(",") if r.strip()]
            groups = {"WINS": ("WIN", "PARTIAL_WIN"), "LOSSES": ("LOSS", "PARTIAL_LOSS"),
                      "OPERATIONAL": OPERATIONAL_RESULTS, "OPEN": ()}
            expanded: list[str] = []
            want_open = False
            for value in values:
                if value == "OPEN":
                    want_open = True
                expanded.extend(groups.get(value, (value,)) if value != "OPEN" else ())
            clauses = []
            if expanded:
                clauses.append(f"result IN ({','.join('?' for _ in expanded)})")
                args.extend(expanded)
            if want_open:
                clauses.append("status IN ('OPEN','PARTIALLY_CLOSED','PENDING')")
            if clauses:
                where.append("(" + " OR ".join(clauses) + ")")
        if session:
            where.append("entry_session = ?")
            args.append(session.upper())
        if timeframe:
            where.append("timeframe = ?")
            args.append(timeframe)
        if leverage_min is not None:
            where.append("leverage >= ?")
            args.append(float(leverage_min))
        if leverage_max is not None:
            where.append("leverage <= ?")
            args.append(float(leverage_max))
        if rr_min is not None:
            where.append("planned_rr >= ?")
            args.append(float(rr_min))
        if rr_max is not None:
            where.append("planned_rr <= ?")
            args.append(float(rr_max))
        if realised_r_min is not None:
            where.append("realised_r >= ?")
            args.append(float(realised_r_min))
        if realised_r_max is not None:
            where.append("realised_r <= ?")
            args.append(float(realised_r_max))
        if pnl_min is not None:
            where.append("net_pnl >= ?")
            args.append(float(pnl_min))
        if pnl_max is not None:
            where.append("net_pnl <= ?")
            args.append(float(pnl_max))
        if rule_violation is not None:
            where.append("COALESCE(rule_violation, 0) = ?" if not rule_violation
                         else "rule_violation = 1")
            if not rule_violation:
                args.append(0)
        if exit_reason:
            where.append("exit_reason = ?")
            args.append(exit_reason.upper())
        if status:
            where.append("status = ?")
            args.append(status.upper())
        if not include_operational:
            where.append("is_operational = 0")
        if only_stats:
            where.append("counts_in_stats = 1")
        return where, args

    def open_trades(self, *, instance_id: Optional[str] = None, symbol: Optional[str] = None,
                    source_system: Optional[str] = None,
                    source_systems: Optional[Iterable[str]] = None) -> list[dict]:
        query = "SELECT * FROM journal_trades WHERE status IN ('OPEN','PARTIALLY_CLOSED')"
        args: list = []
        if source_systems:
            values = list(source_systems)
            query += f" AND source_system IN ({','.join('?' for _ in values)})"
            args.extend(values)
        if instance_id is not None:
            query += " AND COALESCE(instance_id,'') = ?"
            args.append(instance_id)
        if symbol:
            query += " AND symbol = ?"
            args.append(symbol)
        if source_system:
            query += " AND source_system = ?"
            args.append(source_system)
        query += " ORDER BY entry_filled_at DESC"
        with self._lock:
            return [self._decode(r) for r in self._c.execute(query, args)]

    def trades_with_status(self, statuses: Iterable[str]) -> list[dict]:
        values = list(statuses)
        with self._lock:
            return [self._decode(r) for r in self._c.execute(
                f"SELECT * FROM journal_trades WHERE status IN ({','.join('?' for _ in values)})",
                values)]
