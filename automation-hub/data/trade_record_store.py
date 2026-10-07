"""The canonical trade record: one row per execution lifecycle, from any source.

Before this, seven stores each held part of a trade (see docs/JOURNAL_AUDIT.md)
and the one the Journal read stopped receiving Trading Instance trades when
forward-paper fills became deferred. This store is written by one recorder
(services/journal_recorder.py) that projects durable execution facts -- the
paper ledger, the lab journals, the SMC agent's intents -- so every row can be
rebuilt from the evidence it came from.

Guarantees, enforced by the database rather than by callers:

* one execution -> one record: ``execution_key`` is UNIQUE, and so is the
  entry ``trade_id``;
* a finalized record's facts cannot change: a trigger aborts any UPDATE that
  alters a non-NULL fact column unless ``correction_seq`` advances, which only
  :meth:`TradeRecordStore.correct` does, together with a correction row;
* facts and interpretation are separate tables: an agent review, a note or a
  weekly review can never touch a trade record;
* a weekly review is unique per agent, strategy, period and version.

Timestamps are ISO-8601 UTC strings. Unknown values are NULL, never guessed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1

RECORD_SOURCES = ("INSTANCE", "LEGACY_ENGINE", "ADAPTIVE_LAB", "PA_LAB", "SMC_LAB",
                  "AGENT", "MANUAL")
RECORD_ORIGINS = ("FORWARD_PAPER", "SIMULATION", "BACKTEST", "RESEARCH",
                  "LEGACY_MIGRATION")
STATUSES = ("PENDING", "OPEN", "CLOSED", "CANCELLED", "REJECTED",
            "EXECUTION_FAILED", "EXECUTION_UNCERTAIN")
OUTCOMES = ("WIN", "LOSS", "BREAKEVEN", "CANCELLED", "REJECTED", "BLOCKED",
            "EXECUTION_FAILED", "EXECUTION_UNCERTAIN")
COMPLETENESS = ("FULL", "PARTIAL", "MINIMAL")
#: A record in one of these statuses is finished; its facts are frozen.
TERMINAL = ("CLOSED", "CANCELLED", "REJECTED", "EXECUTION_FAILED")

#: What a record is and where it came from. Frozen with the facts once the
#: record is finalized: re-labelling a finished trade from forward paper to
#: anything else (or back) changes which statistics it counts in, so it is a
#: correction with a reason and an actor, never a side effect of a later pass.
LABEL_COLUMNS = ("record_origin", "record_source")

#: Columns a finalized record may not change except through correct().
FACT_COLUMNS = (
    "trade_id", "decision_id", "signal_id", "intent_id", "order_id", "position_id",
    "session_id", "instance_id", "strategy_id", "strategy_name", "strategy_version",
    "symbol", "timeframe", "side",
    "signal_detected_at", "decision_created_at", "intent_created_at",
    "order_submitted_at", "order_acknowledged_at", "entry_filled_at",
    "position_opened_at", "exit_signal_at", "exit_submitted_at", "exit_filled_at",
    "position_closed_at",
    "setup_json", "evidence_json",
    "signal_price", "planned_entry", "planned_stop_loss", "planned_take_profit",
    "planned_rr", "risk_percent", "risk_amount", "quantity",
    "requested_entry", "actual_entry", "requested_quantity", "filled_quantity",
    "bid", "ask", "spread", "slippage",
    "actual_exit", "exit_reason", "gross_pnl", "fees", "funding", "net_pnl",
    "realized_r", "achieved_rr", "mae_r", "mfe_r", "outcome",
)

_TRADE_COLUMNS = (
    # identity
    ("journal_record_id", "TEXT PRIMARY KEY"),
    ("execution_key", "TEXT NOT NULL UNIQUE"),
    ("record_source", "TEXT NOT NULL"),
    ("record_origin", "TEXT NOT NULL"),
    ("verification", "TEXT NOT NULL DEFAULT 'VERIFIED'"),
    ("operating_mode", "TEXT NOT NULL DEFAULT 'paper'"),
    ("data_completeness", "TEXT NOT NULL"),
    ("status", "TEXT NOT NULL"),
    ("outcome", "TEXT"),
    ("trade_id", "TEXT"), ("decision_id", "TEXT"), ("signal_id", "TEXT"),
    ("intent_id", "TEXT"), ("order_id", "TEXT"), ("position_id", "TEXT"),
    ("session_id", "TEXT"), ("instance_id", "TEXT"), ("lab_id", "TEXT"),
    ("agent_id", "TEXT"), ("strategy_id", "TEXT"), ("strategy_name", "TEXT"),
    ("strategy_version", "TEXT"), ("symbol", "TEXT"), ("exchange", "TEXT"),
    ("market_type", "TEXT"), ("timeframe", "TEXT"), ("htf_timeframe", "TEXT"),
    ("side", "TEXT"),
    # timeline
    ("signal_detected_at", "TEXT"), ("decision_created_at", "TEXT"),
    ("intent_created_at", "TEXT"), ("order_submitted_at", "TEXT"),
    ("order_acknowledged_at", "TEXT"), ("entry_filled_at", "TEXT"),
    ("position_opened_at", "TEXT"), ("exit_signal_at", "TEXT"),
    ("exit_submitted_at", "TEXT"), ("exit_filled_at", "TEXT"),
    ("position_closed_at", "TEXT"), ("journal_finalized_at", "TEXT"),
    ("decision_latency_ms", "REAL"), ("execution_latency_ms", "REAL"),
    ("trade_duration_s", "REAL"),
    # setup snapshot (frozen at decision time)
    ("setup_type", "TEXT"), ("market_regime", "TEXT"), ("trading_session", "TEXT"),
    ("htf_bias", "TEXT"), ("setup_json", "TEXT"), ("evidence_json", "TEXT"),
    # plan
    ("signal_price", "REAL"), ("planned_entry", "REAL"), ("planned_stop_loss", "REAL"),
    ("planned_take_profit", "REAL"), ("planned_rr", "REAL"), ("risk_percent", "REAL"),
    ("risk_amount", "REAL"), ("quantity", "REAL"), ("leverage", "REAL"),
    ("balance_before", "REAL"), ("equity_before", "REAL"),
    ("available_balance_before", "REAL"), ("risk_check_json", "TEXT"),
    # execution
    ("requested_entry", "REAL"), ("actual_entry", "REAL"),
    ("requested_quantity", "REAL"), ("filled_quantity", "REAL"),
    ("bid", "REAL"), ("ask", "REAL"), ("spread", "REAL"), ("slippage", "REAL"),
    ("order_type", "TEXT"), ("execution_status", "TEXT"), ("fill_model", "TEXT"),
    # result
    ("actual_exit", "REAL"), ("exit_reason", "TEXT"), ("gross_pnl", "REAL"),
    ("fees", "REAL"), ("funding", "REAL"), ("net_pnl", "REAL"),
    ("realized_r", "REAL"), ("achieved_rr", "REAL"), ("mae_r", "REAL"),
    ("mfe_r", "REAL"), ("max_trade_drawdown", "REAL"),
    # bookkeeping
    ("legs_json", "TEXT"), ("missing_json", "TEXT"), ("source_ref_json", "TEXT"),
    ("facts_hash", "TEXT"), ("finalized", "INTEGER NOT NULL DEFAULT 0"),
    ("correction_seq", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "TEXT NOT NULL"), ("updated_at", "TEXT NOT NULL"),
)
TRADE_COLUMNS = tuple(name for name, _ in _TRADE_COLUMNS)
_JSON_COLUMNS = {"setup_json", "evidence_json", "risk_check_json", "legs_json",
                 "missing_json", "source_ref_json"}

DECISION_TYPES = (
    "SIGNAL_GENERATED", "TRADE_OPENED", "WAITING_CONFIRMATION", "HTF_BLOCKED",
    "QUALITY_BLOCKED", "RISK_BLOCKED", "CONTEXT_BLOCKED", "NEWS_BLACKOUT",
    "STALE_DATA", "FEED_UNAVAILABLE", "SIGNALS_ONLY", "APPROVAL_REQUIRED",
    "SESSION_BLOCKED", "ORDER_REJECTED", "EXECUTION_FAILED",
    "EXECUTION_UNCERTAIN", "DUPLICATE_PREVENTED", "SETUP_REJECTED",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_id_for(execution_key: str) -> str:
    """A deterministic id, so a rebuilt record keeps its identity and links."""
    return "tr_" + hashlib.sha256(execution_key.encode()).hexdigest()[:24]


def decision_id_for(decision_key: str) -> str:
    return "dr_" + hashlib.sha256(decision_key.encode()).hexdigest()[:24]


def _dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def facts_hash(row: dict) -> str:
    material = {k: row.get(k) for k in FACT_COLUMNS}
    return hashlib.sha256(_dumps(material).encode()).hexdigest()


class FinalizedRecordError(RuntimeError):
    """A finalized fact was about to change outside a controlled correction."""


class TradeRecordStore:
    def __init__(self, path: str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if self.path != ":memory:":
                self._c.execute("PRAGMA journal_mode=WAL")
            self._c.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    # ------------------------------------------------------------------ schema
    def _migrate(self) -> None:
        c = self._c
        columns = ",\n  ".join(f"{name} {decl}" for name, decl in _TRADE_COLUMNS)
        c.executescript(f"""
        CREATE TABLE IF NOT EXISTS trade_records(
          {columns});
        CREATE UNIQUE INDEX IF NOT EXISTS ux_trade_records_trade_id
          ON trade_records(trade_id) WHERE trade_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_trade_records_open
          ON trade_records(position_opened_at);
        CREATE INDEX IF NOT EXISTS ix_trade_records_scope
          ON trade_records(record_source, record_origin, strategy_id, symbol);
        CREATE INDEX IF NOT EXISTS ix_trade_records_instance
          ON trade_records(instance_id, position_closed_at);

        CREATE TABLE IF NOT EXISTS trade_record_events(
          journal_record_id TEXT NOT NULL REFERENCES trade_records(journal_record_id),
          stage TEXT NOT NULL, at TEXT, status TEXT NOT NULL, detail TEXT,
          seq INTEGER NOT NULL,
          PRIMARY KEY(journal_record_id, stage));

        CREATE TABLE IF NOT EXISTS trade_record_corrections(
          id TEXT PRIMARY KEY, journal_record_id TEXT NOT NULL,
          at TEXT NOT NULL, kind TEXT NOT NULL, field TEXT NOT NULL,
          before_json TEXT, after_json TEXT, reason TEXT NOT NULL, actor TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_corrections_record
          ON trade_record_corrections(journal_record_id, at);

        CREATE TABLE IF NOT EXISTS decision_records(
          decision_record_id TEXT PRIMARY KEY,
          decision_key TEXT NOT NULL UNIQUE,
          record_source TEXT NOT NULL, record_origin TEXT NOT NULL,
          instance_id TEXT, lab_id TEXT, agent_id TEXT,
          strategy_id TEXT, strategy_name TEXT, strategy_version TEXT,
          symbol TEXT, timeframe TEXT, side TEXT, candle_time TEXT,
          decided_at TEXT NOT NULL, signal TEXT, decision_type TEXT NOT NULL,
          status TEXT, blocker TEXT, reason TEXT,
          conditions_passed_json TEXT, conditions_missing_json TEXT,
          market_data_state TEXT, evidence_json TEXT, source_ref_json TEXT,
          journal_record_id TEXT, trade_id TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_decision_records_at
          ON decision_records(decided_at);
        CREATE INDEX IF NOT EXISTS ix_decision_records_scope
          ON decision_records(record_source, decision_type, symbol);

        CREATE TABLE IF NOT EXISTS trade_reviews(
          trade_review_id TEXT PRIMARY KEY,
          journal_record_id TEXT NOT NULL REFERENCES trade_records(journal_record_id),
          agent_id TEXT NOT NULL, review_version INTEGER NOT NULL,
          reviewed_at TEXT NOT NULL,
          setup_quality TEXT, execution_quality TEXT,
          risk_compliance TEXT, strategy_compliance TEXT,
          rule_violations_json TEXT, mistakes_json TEXT,
          positive_behaviours_json TEXT, review_tags_json TEXT,
          observations_json TEXT, recommendations_json TEXT,
          basis_json TEXT,
          UNIQUE(journal_record_id, agent_id, review_version));

        CREATE TABLE IF NOT EXISTS trade_notes(
          note_id TEXT PRIMARY KEY, journal_record_id TEXT,
          created_at TEXT NOT NULL, author TEXT NOT NULL, text TEXT NOT NULL,
          tags_json TEXT);
        CREATE INDEX IF NOT EXISTS ix_notes_record ON trade_notes(journal_record_id, created_at);

        CREATE TABLE IF NOT EXISTS weekly_reviews(
          review_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
          strategy_id TEXT NOT NULL, scope_json TEXT NOT NULL,
          period_start TEXT NOT NULL, period_end TEXT NOT NULL,
          review_version INTEGER NOT NULL, generated_at TEXT NOT NULL,
          journal_record_ids_json TEXT NOT NULL, stats_json TEXT NOT NULL,
          comparison_json TEXT, findings_json TEXT NOT NULL,
          validation_json TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1, superseded_by TEXT, revision_reason TEXT,
          UNIQUE(agent_id, strategy_id, period_start, period_end, review_version, revision));

        CREATE TABLE IF NOT EXISTS improvement_proposals(
          proposal_id TEXT PRIMARY KEY, review_id TEXT NOT NULL,
          agent_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
          affected_strategy TEXT NOT NULL, title TEXT NOT NULL,
          evidence_json TEXT NOT NULL, expected_benefit TEXT NOT NULL,
          risk TEXT NOT NULL, sample_size INTEGER NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL,
          decided_at TEXT, decided_by TEXT, decision_note TEXT,
          UNIQUE(review_id, title));

        CREATE TABLE IF NOT EXISTS review_runs(
          agent_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
          period_start TEXT NOT NULL, period_end TEXT NOT NULL,
          status TEXT NOT NULL, started_at TEXT, finished_at TEXT,
          review_id TEXT, error TEXT,
          PRIMARY KEY(agent_id, strategy_id, period_start, period_end));

        CREATE TABLE IF NOT EXISTS recorder_state(
          key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
        """)
        if "revision" not in {row[1] for row in c.execute("PRAGMA table_info(weekly_reviews)")}:
            # Revisions arrived after the first weekly reviews were written. The
            # uniqueness key gains the revision, which SQLite can only change by
            # rebuilding the table; every existing review becomes revision 1.
            c.executescript("""
            ALTER TABLE weekly_reviews RENAME TO weekly_reviews_before_revisions;
            CREATE TABLE weekly_reviews(
              review_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
              strategy_id TEXT NOT NULL, scope_json TEXT NOT NULL,
              period_start TEXT NOT NULL, period_end TEXT NOT NULL,
              review_version INTEGER NOT NULL, generated_at TEXT NOT NULL,
              journal_record_ids_json TEXT NOT NULL, stats_json TEXT NOT NULL,
              comparison_json TEXT, findings_json TEXT NOT NULL,
              validation_json TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 1, superseded_by TEXT, revision_reason TEXT,
              UNIQUE(agent_id, strategy_id, period_start, period_end, review_version, revision));
            INSERT INTO weekly_reviews(review_id, agent_id, strategy_id, scope_json, period_start,
              period_end, review_version, generated_at, journal_record_ids_json, stats_json,
              comparison_json, findings_json, validation_json)
            SELECT review_id, agent_id, strategy_id, scope_json, period_start, period_end,
              review_version, generated_at, journal_record_ids_json, stats_json,
              comparison_json, findings_json, validation_json
            FROM weekly_reviews_before_revisions;
            DROP TABLE weekly_reviews_before_revisions;
            """)
        existing = {row[1] for row in c.execute("PRAGMA table_info(trade_records)")}
        for name, decl in _TRADE_COLUMNS:
            if name not in existing:
                c.execute(f"ALTER TABLE trade_records ADD COLUMN {name} "
                          f"{decl.replace('PRIMARY KEY', '').replace('UNIQUE', '')}")
        guarded = " OR ".join(
            f"(OLD.{col} IS NOT NULL AND NEW.{col} IS NOT OLD.{col})"
            for col in (*FACT_COLUMNS, *LABEL_COLUMNS, "execution_key", "journal_record_id"))
        c.execute("DROP TRIGGER IF EXISTS trg_trade_records_immutable")
        c.execute(f"""
        CREATE TRIGGER trg_trade_records_immutable
        BEFORE UPDATE ON trade_records
        WHEN OLD.finalized = 1 AND NEW.correction_seq <= OLD.correction_seq AND ({guarded})
        BEGIN
          SELECT RAISE(ABORT, 'finalized trade record facts are immutable; use a correction');
        END""")
        # Un-finalizing a record would switch the guard above off, after which
        # its facts could be edited or the record deleted. Nothing reopens a
        # finished record, not even a correction.
        c.execute("DROP TRIGGER IF EXISTS trg_trade_records_stay_final")
        c.execute("""
        CREATE TRIGGER trg_trade_records_stay_final
        BEFORE UPDATE OF finalized ON trade_records
        WHEN OLD.finalized = 1 AND NEW.finalized IS NOT 1
        BEGIN
          SELECT RAISE(ABORT, 'a finalized trade record cannot be reopened');
        END""")
        c.execute("DROP TRIGGER IF EXISTS trg_trade_records_no_delete")
        c.execute("""
        CREATE TRIGGER trg_trade_records_no_delete
        BEFORE DELETE ON trade_records WHEN OLD.finalized = 1
        BEGIN
          SELECT RAISE(ABORT, 'finalized trade records are never deleted');
        END""")
        c.execute("INSERT OR IGNORE INTO recorder_state VALUES ('schema_version', ?, ?)",
                  (str(SCHEMA_VERSION), utcnow()))
        c.commit()

    # --------------------------------------------------------------- helpers
    def _row(self, row: sqlite3.Row | None) -> Optional[dict]:
        if row is None:
            return None
        out = dict(row)
        for key in list(out):
            if key in _JSON_COLUMNS or key.endswith("_json"):
                raw = out.pop(key)
                # A NaN or infinity in source evidence is no number: it reads as
                # unknown, and the API can still answer in JSON.
                out[key[:-5]] = json.loads(raw, parse_constant=lambda _c: None) if raw else None
        return out

    def state(self, key: str, default=None):
        with self._lock:
            row = self._c.execute("SELECT value_json FROM recorder_state WHERE key=?",
                                  (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_state(self, key: str, value) -> None:
        text = _dumps(value)
        with self._lock:
            row = self._c.execute("SELECT value_json FROM recorder_state WHERE key=?",
                                  (key,)).fetchone()
            if row is not None and row["value_json"] == text:
                return                       # unchanged: no write, no disk sync
            self._c.execute(
                "INSERT INTO recorder_state VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                "SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, text, utcnow()))
            self._c.commit()

    # ------------------------------------------------------------ trade records
    def upsert_trade(self, record: dict, *, events: Iterable[dict] = ()) -> dict:
        """Insert or advance one canonical record; never duplicates, never rewrites.

        ``record`` carries column values (JSON columns as Python objects under
        their ``*_json`` name). A non-finalized record takes the new values;
        a finalized one only fills fields that were unknown (NULL), each fill
        logged as an ENRICH correction. A differing non-NULL fact on a
        finalized record is logged as a DISCREPANCY and left unchanged.
        """
        key = str(record["execution_key"])
        rid = record.get("journal_record_id") or record_id_for(key)
        now = utcnow()
        values = {name: record.get(name) for name in TRADE_COLUMNS if name in record}
        values["journal_record_id"] = rid
        for name in _JSON_COLUMNS:
            if name in values and not isinstance(values[name], (str, type(None))):
                values[name] = _dumps(values[name])
        result = {"journal_record_id": rid, "action": "unchanged"}
        with self._lock:
            existing = self._c.execute(
                "SELECT * FROM trade_records WHERE execution_key=?", (key,)).fetchone()
            if existing is None and values.get("trade_id"):
                # Same execution reached under a different key (for example a
                # legacy journal row whose ledger trade is now projected).
                existing = self._c.execute(
                    "SELECT * FROM trade_records WHERE trade_id=?",
                    (values["trade_id"],)).fetchone()
                if existing is not None:
                    values["execution_key"] = existing["execution_key"]
                    values["journal_record_id"] = rid = existing["journal_record_id"]
                    result["journal_record_id"] = rid
            try:
                if existing is None:
                    values.setdefault("created_at", now)
                    values["updated_at"] = now
                    finalize = values.get("status") in TERMINAL
                    values["finalized"] = 1 if finalize else 0
                    if finalize:
                        values.setdefault("journal_finalized_at", now)
                    values["facts_hash"] = facts_hash(values)
                    cols = list(values)
                    self._c.execute(
                        f"INSERT INTO trade_records({','.join(cols)}) VALUES "
                        f"({','.join('?' * len(cols))})", [values[c] for c in cols])
                    result["action"] = "inserted"
                else:
                    current = dict(existing)
                    changes: dict = {}
                    discrepancies = []
                    for name, value in values.items():
                        if name in ("journal_record_id", "execution_key", "created_at",
                                    "finalized", "correction_seq", "facts_hash"):
                            continue
                        old = current.get(name)
                        if value is None or value == old:
                            continue
                        if current["finalized"] and (name in FACT_COLUMNS or name in LABEL_COLUMNS) \
                                and old is not None:
                            if not _same(old, value):
                                discrepancies.append((name, old, value))
                            continue
                        if current["finalized"] and name in ("status",):
                            continue          # a finished record does not reopen
                        changes[name] = value
                    if current["finalized"]:
                        for name in list(changes):
                            if name in FACT_COLUMNS:
                                self._log_correction(rid, "ENRICH", name, None, changes[name],
                                                     "previously unknown value found in source evidence",
                                                     "journal_recorder")
                    for name, old, value in discrepancies:
                        self._log_discrepancy(rid, name, old, value)
                    if not current["finalized"] and changes.get("status") in TERMINAL:
                        changes["finalized"] = 1
                        changes["journal_finalized_at"] = now
                    if changes:
                        changes["updated_at"] = now
                        merged = {**current, **changes}
                        changes["facts_hash"] = facts_hash(merged)
                        sets = ",".join(f"{c}=?" for c in changes)
                        self._c.execute(
                            f"UPDATE trade_records SET {sets} WHERE journal_record_id=?",
                            [*changes.values(), rid])
                        result["action"] = "updated"
                    if discrepancies:
                        result["discrepancies"] = [d[0] for d in discrepancies]
                self._write_events(rid, events)
                self._c.commit()
            except Exception:
                self._c.rollback()
                raise
        return result

    def _write_events(self, rid: str, events: Iterable[dict]) -> None:
        events = list(events)
        if not events:
            return
        # The recorder re-projects every record on each pass. Write only the
        # stages whose stored row would change, so an unchanged record costs
        # a read, not a write and a disk sync.
        current = {row["stage"]: row for row in self._c.execute(
            "SELECT stage, at, status, detail, seq FROM trade_record_events "
            "WHERE journal_record_id=?", (rid,))}
        for seq, event in enumerate(events):
            old = current.get(event["stage"])
            if old is not None:
                after = (old["at"] if old["at"] is not None else event.get("at"),
                         event.get("status") or "DONE",
                         event.get("detail") if event.get("detail") is not None else old["detail"],
                         seq)
                if after == (old["at"], old["status"], old["detail"], old["seq"]):
                    continue
            self._c.execute(
                "INSERT INTO trade_record_events(journal_record_id,stage,at,status,detail,seq) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(journal_record_id,stage) DO UPDATE SET "
                "at=COALESCE(trade_record_events.at, excluded.at), status=excluded.status, "
                "detail=COALESCE(excluded.detail, trade_record_events.detail), seq=excluded.seq",
                (rid, event["stage"], event.get("at"), event.get("status") or "DONE",
                 event.get("detail"), seq))

    def _log_correction(self, rid, kind, field, before, after, reason, actor) -> None:
        self._c.execute(
            "INSERT INTO trade_record_corrections VALUES (?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, rid, utcnow(), kind, field, _dumps(before),
             _dumps(after), reason, actor))

    def _log_discrepancy(self, rid, field, old, new) -> None:
        seen = self._c.execute(
            "SELECT 1 FROM trade_record_corrections WHERE journal_record_id=? AND kind='DISCREPANCY' "
            "AND field=? AND after_json=?", (rid, field, _dumps(new))).fetchone()
        if not seen:
            self._log_correction(rid, "DISCREPANCY", field, old, new,
                                 "source evidence disagrees with the finalized record; "
                                 "record left unchanged pending a controlled correction",
                                 "journal_recorder")

    def correct(self, journal_record_id: str, field: str, value, *, reason: str,
                actor: str) -> dict:
        """The one controlled way to change a finalized fact or label. Logged, never silent."""
        if field not in FACT_COLUMNS and field not in LABEL_COLUMNS:
            raise ValueError(f"{field} is not a correctable fact column")
        if field == "record_origin" and value not in RECORD_ORIGINS:
            raise ValueError(f"unknown record origin {value!r}")
        if field == "record_source" and value not in RECORD_SOURCES:
            raise ValueError(f"unknown record source {value!r}")
        if not reason.strip() or not actor.strip():
            raise ValueError("a correction needs a reason and an actor")
        with self._lock:
            row = self._c.execute("SELECT * FROM trade_records WHERE journal_record_id=?",
                                  (journal_record_id,)).fetchone()
            if row is None:
                raise KeyError(journal_record_id)
            stored = _dumps(value) if field in _JSON_COLUMNS else value
            try:
                self._log_correction(journal_record_id, "CORRECTION", field, row[field],
                                     value, reason, actor)
                merged = {**dict(row), field: stored}
                self._c.execute(
                    f"UPDATE trade_records SET {field}=?, correction_seq=correction_seq+1, "
                    "facts_hash=?, updated_at=? WHERE journal_record_id=?",
                    (stored, facts_hash(merged), utcnow(), journal_record_id))
                self._c.commit()
            except Exception:
                self._c.rollback()
                raise
        return self.get(journal_record_id)

    def get(self, journal_record_id: str) -> Optional[dict]:
        with self._lock:
            row = self._c.execute(
                "SELECT * FROM trade_records WHERE journal_record_id=? OR trade_id=? "
                "OR execution_key=?", (journal_record_id,) * 3).fetchone()
            if row is None:
                return None
            record = self._row(row)
            rid = record["journal_record_id"]
            record["timeline"] = [dict(r) for r in self._c.execute(
                "SELECT stage, at, status, detail FROM trade_record_events "
                "WHERE journal_record_id=? ORDER BY seq", (rid,))]
            record["corrections"] = [self._row(r) for r in self._c.execute(
                "SELECT * FROM trade_record_corrections WHERE journal_record_id=? ORDER BY at",
                (rid,))]
            record["reviews"] = [self._row(r) for r in self._c.execute(
                "SELECT * FROM trade_reviews WHERE journal_record_id=? "
                "ORDER BY review_version DESC", (rid,))]
            record["notes"] = [self._row(r) for r in self._c.execute(
                "SELECT * FROM trade_notes WHERE journal_record_id=? ORDER BY created_at",
                (rid,))]
            record["decisions"] = [self._row(r) for r in self._c.execute(
                "SELECT * FROM decision_records WHERE journal_record_id=? ORDER BY decided_at",
                (rid,))]
            return record

    def by_key(self, execution_key: str) -> Optional[dict]:
        with self._lock:
            return self._row(self._c.execute(
                "SELECT * FROM trade_records WHERE execution_key=?", (execution_key,)).fetchone())

    def query_trades(self, *, where: str = "", params: Iterable = (), order: str =
                     "COALESCE(position_opened_at, decision_created_at, created_at) DESC",
                     limit: int = 500, offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM trade_records" + (f" WHERE {where}" if where else "")
        sql += f" ORDER BY {order} LIMIT ? OFFSET ?"
        with self._lock:
            return [self._row(r) for r in self._c.execute(sql, [*params, int(limit), int(offset)])]

    def count_trades(self, *, where: str = "", params: Iterable = ()) -> int:
        with self._lock:
            return int(self._c.execute(
                "SELECT COUNT(*) FROM trade_records" + (f" WHERE {where}" if where else ""),
                list(params)).fetchone()[0])

    def unfinished(self, sources: Iterable[str]) -> list[dict]:
        """Records of these sources not finalized yet (OPEN, PENDING, ...)."""
        marks = ",".join("?" * len(tuple(sources)))
        with self._lock:
            return [self._row(r) for r in self._c.execute(
                f"SELECT * FROM trade_records WHERE finalized=0 AND record_source IN ({marks})",
                tuple(sources))]

    def keys_with_status(self, sources: Iterable[str]) -> dict[str, tuple[str, int]]:
        marks = ",".join("?" * len(tuple(sources)))
        with self._lock:
            return {r["execution_key"]: (r["status"], r["finalized"]) for r in self._c.execute(
                f"SELECT execution_key, status, finalized FROM trade_records "
                f"WHERE record_source IN ({marks})", tuple(sources))}

    # --------------------------------------------------------- decision records
    #: Columns whose current value in the source is the truth, including
    #: "none": a decision that stopped being blocked must lose its blocker.
    #: Every other column keeps what it had when a later pass does not know it.
    _DECISION_OVERWRITE = ("blocker",)

    def upsert_decision(self, decision: dict, *, overwrite: Iterable[str] = ()) -> str:
        """``overwrite`` names further columns whose given value, None included,
        is the truth (a caller that always knows them, such as a trade link)."""
        replace = (*self._DECISION_OVERWRITE, *overwrite)
        key = str(decision["decision_key"])
        did = decision_id_for(key)
        now = utcnow()
        row = {
            "decision_record_id": did, "decision_key": key,
            **{k: decision.get(k) for k in (
                "record_source", "record_origin", "instance_id", "lab_id", "agent_id",
                "strategy_id", "strategy_name", "strategy_version", "symbol", "timeframe",
                "side", "candle_time", "decided_at", "signal", "decision_type", "status",
                "blocker", "reason", "market_data_state", "journal_record_id", "trade_id")},
            "conditions_passed_json": _dumps(decision.get("conditions_passed")),
            "conditions_missing_json": _dumps(decision.get("conditions_missing")),
            "evidence_json": _dumps(decision.get("evidence")),
            "source_ref_json": _dumps(decision.get("source_ref")),
        }
        cols = list(row)
        updatable = [c for c in cols if c not in ("decision_record_id", "decision_key")]
        with self._lock:
            existing = self._c.execute(
                "SELECT * FROM decision_records WHERE decision_key=?", (key,)).fetchone()
            if existing is None:
                row["decided_at"] = row["decided_at"] or now
            else:
                # Without a decision time of its own, a decision keeps the one it
                # was first recorded with; it used to take the time of every pass.
                row["decided_at"] = row["decided_at"] or existing["decided_at"]
                # What the upsert below would store; when that is what is already
                # stored, skip it: the recorder re-projects every decision on each
                # pass, and a write per unchanged decision is a disk sync each.
                if all(existing[c] == (row[c] if c in replace
                                       or row[c] is not None else existing[c])
                       for c in updatable):
                    return did
            self._c.execute(
                f"INSERT INTO decision_records({','.join(cols)},created_at,updated_at) "
                f"VALUES ({','.join('?' * len(cols))},?,?) ON CONFLICT(decision_key) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" if c in replace else
                           f"{c}=COALESCE(excluded.{c}, decision_records.{c})" for c in updatable)
                + ", updated_at=excluded.updated_at",
                [row[c] for c in cols] + [now, now])
            self._c.commit()
        return did

    def query_decisions(self, *, where: str = "", params: Iterable = (),
                        limit: int = 500, offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM decision_records" + (f" WHERE {where}" if where else "")
        sql += " ORDER BY decided_at DESC LIMIT ? OFFSET ?"
        with self._lock:
            return [self._row(r) for r in self._c.execute(sql, [*params, int(limit), int(offset)])]

    def get_decision(self, decision_record_id: str) -> Optional[dict]:
        with self._lock:
            return self._row(self._c.execute(
                "SELECT * FROM decision_records WHERE decision_record_id=? OR decision_key=?",
                (decision_record_id, decision_record_id)).fetchone())

    def count_decisions(self, *, where: str = "", params: Iterable = ()) -> int:
        with self._lock:
            return int(self._c.execute(
                "SELECT COUNT(*) FROM decision_records" + (f" WHERE {where}" if where else ""),
                list(params)).fetchone()[0])

    # ---------------------------------------------------------- review layer
    def add_review(self, review: dict) -> dict:
        """Store an agent review of a record. Reviews never touch the record."""
        rid = review["journal_record_id"]
        with self._lock:
            if self._c.execute("SELECT 1 FROM trade_records WHERE journal_record_id=?",
                               (rid,)).fetchone() is None:
                raise KeyError(rid)
            version = review.get("review_version")
            if version is None:
                version = int(self._c.execute(
                    "SELECT COALESCE(MAX(review_version),0)+1 FROM trade_reviews "
                    "WHERE journal_record_id=? AND agent_id=?",
                    (rid, review["agent_id"])).fetchone()[0])
            review_id = "rv_" + hashlib.sha256(
                f"{rid}|{review['agent_id']}|{version}".encode()).hexdigest()[:24]
            self._c.execute(
                "INSERT OR IGNORE INTO trade_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, rid, review["agent_id"], version,
                 review.get("reviewed_at") or utcnow(),
                 review.get("setup_quality"), review.get("execution_quality"),
                 review.get("risk_compliance"), review.get("strategy_compliance"),
                 _dumps(review.get("rule_violations") or []), _dumps(review.get("mistakes") or []),
                 _dumps(review.get("positive_behaviours") or []),
                 _dumps(review.get("review_tags") or []),
                 _dumps(review.get("observations") or []),
                 _dumps(review.get("recommendations") or []),
                 _dumps(review.get("basis") or {})))
            self._c.commit()
            return self._row(self._c.execute("SELECT * FROM trade_reviews WHERE trade_review_id=?",
                                             (review_id,)).fetchone())

    def reviews_for(self, journal_record_ids: Iterable[str]) -> dict[str, dict]:
        ids = list(journal_record_ids)
        if not ids:
            return {}
        out: dict[str, dict] = {}
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for r in self._c.execute(
                        f"SELECT * FROM trade_reviews WHERE journal_record_id IN "
                        f"({','.join('?' * len(chunk))}) ORDER BY review_version", chunk):
                    out[r["journal_record_id"]] = self._row(r)   # latest version wins
        return out

    def add_note(self, journal_record_id: Optional[str], text: str, *, author: str,
                 tags: Optional[list[str]] = None) -> dict:
        text = str(text or "").strip()
        if not text:
            raise ValueError("a note needs text")
        note_id = uuid.uuid4().hex
        with self._lock:
            if journal_record_id and self._c.execute(
                    "SELECT 1 FROM trade_records WHERE journal_record_id=?",
                    (journal_record_id,)).fetchone() is None:
                raise KeyError(journal_record_id)
            self._c.execute("INSERT INTO trade_notes VALUES (?,?,?,?,?,?)",
                            (note_id, journal_record_id, utcnow(), author, text[:4000],
                             _dumps(tags or [])))
            self._c.commit()
            return self._row(self._c.execute("SELECT * FROM trade_notes WHERE note_id=?",
                                             (note_id,)).fetchone())

    def notes(self, *, limit: int = 200) -> list[dict]:
        with self._lock:
            return [self._row(r) for r in self._c.execute(
                "SELECT n.*, t.symbol, t.strategy_name, t.side, t.position_opened_at "
                "FROM trade_notes n LEFT JOIN trade_records t USING(journal_record_id) "
                "ORDER BY n.created_at DESC LIMIT ?", (int(limit),))]

    # ---------------------------------------------------------- weekly reviews
    def save_weekly_review(self, review: dict) -> dict:
        """Persist a weekly review once; a second save of the same key is a no-op.

        A review that ``supersedes`` an earlier revision of the same week marks
        that one superseded, and retires its proposals still awaiting a person:
        they were drawn from evidence that has since changed. Proposals a
        person already decided keep their decision.
        """
        with self._lock:
            self._c.execute(
                "INSERT OR IGNORE INTO weekly_reviews(review_id, agent_id, strategy_id, scope_json, "
                "period_start, period_end, review_version, generated_at, journal_record_ids_json, "
                "stats_json, comparison_json, findings_json, validation_json, revision, "
                "revision_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (review["review_id"], review["agent_id"], review["strategy_id"],
                 _dumps(review["scope"]), review["period_start"], review["period_end"],
                 int(review["review_version"]), review["generated_at"],
                 _dumps(review["journal_record_ids"]), _dumps(review["stats"]),
                 _dumps(review.get("comparison")), _dumps(review["findings"]),
                 _dumps(review["validation"]), int(review.get("revision") or 1),
                 review.get("revision_reason")))
            if review.get("supersedes"):
                self._c.execute(
                    "UPDATE weekly_reviews SET superseded_by=? WHERE review_id=? "
                    "AND superseded_by IS NULL", (review["review_id"], review["supersedes"]))
                self._c.execute(
                    "UPDATE improvement_proposals SET status='SUPERSEDED', decision_note=? "
                    "WHERE review_id=? AND status='PENDING_APPROVAL'",
                    (f"superseded by review {review['review_id']}", review["supersedes"]))
            for proposal in review.get("proposals") or []:
                self._c.execute(
                    "INSERT OR IGNORE INTO improvement_proposals VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (proposal["proposal_id"], review["review_id"], review["agent_id"],
                     review["strategy_id"], proposal["affected_strategy"], proposal["title"],
                     _dumps(proposal["evidence"]), proposal["expected_benefit"],
                     proposal["risk"], int(proposal["sample_size"]), "PENDING_APPROVAL",
                     review["generated_at"], None, None, None))
            self._c.commit()
            row = self._c.execute(
                "SELECT * FROM weekly_reviews WHERE agent_id=? AND strategy_id=? AND "
                "period_start=? AND period_end=? AND review_version=? AND revision=?",
                (review["agent_id"], review["strategy_id"], review["period_start"],
                 review["period_end"], int(review["review_version"]),
                 int(review.get("revision") or 1))).fetchone()
            return self._row(row)

    def weekly_reviews(self, *, agent_id: Optional[str] = None,
                       strategy_id: Optional[str] = None, limit: int = 52,
                       include_superseded: bool = False) -> list[dict]:
        """The current revision of each weekly review, newest week first. A
        superseded revision stays readable by id (weekly_review)."""
        cond, args = ([] if include_superseded else ["superseded_by IS NULL"]), []
        if agent_id:
            cond.append("agent_id=?"); args.append(agent_id)
        if strategy_id:
            cond.append("strategy_id=?"); args.append(strategy_id)
        sql = "SELECT * FROM weekly_reviews" + (" WHERE " + " AND ".join(cond) if cond else "")
        sql += " ORDER BY period_start DESC, review_version DESC, revision DESC LIMIT ?"
        with self._lock:
            return [self._row(r) for r in self._c.execute(sql, [*args, int(limit)])]

    def current_weekly_review(self, agent_id: str, strategy_id: str, period_start: str,
                              period_end: str) -> Optional[dict]:
        """The revision of one agent's review of one week that is in force."""
        with self._lock:
            return self._row(self._c.execute(
                "SELECT * FROM weekly_reviews WHERE agent_id=? AND strategy_id=? AND "
                "period_start=? AND period_end=? AND superseded_by IS NULL "
                "ORDER BY review_version DESC, revision DESC LIMIT 1",
                (agent_id, strategy_id, period_start, period_end)).fetchone())

    def weekly_review(self, review_id: str) -> Optional[dict]:
        with self._lock:
            row = self._row(self._c.execute("SELECT * FROM weekly_reviews WHERE review_id=?",
                                            (review_id,)).fetchone())
            if row is not None:
                row["proposals"] = [self._row(r) for r in self._c.execute(
                    "SELECT * FROM improvement_proposals WHERE review_id=? ORDER BY created_at",
                    (review_id,))]
            return row

    def proposals(self, *, status: Optional[str] = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM improvement_proposals"
        args: list = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            return [self._row(r) for r in self._c.execute(sql, [*args, int(limit)])]

    def decide_proposal(self, proposal_id: str, *, approve: bool, actor: str,
                        note: str = "") -> dict:
        """Only an explicit human decision moves a proposal. Nothing is applied."""
        if not actor.strip():
            raise ValueError("a proposal decision needs the person who made it")
        with self._lock:
            row = self._c.execute("SELECT status FROM improvement_proposals WHERE proposal_id=?",
                                  (proposal_id,)).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] != "PENDING_APPROVAL":
                raise ValueError(f"proposal is already {row['status']}")
            self._c.execute(
                "UPDATE improvement_proposals SET status=?, decided_at=?, decided_by=?, "
                "decision_note=? WHERE proposal_id=?",
                ("APPROVED" if approve else "REJECTED", utcnow(), actor, note[:2000],
                 proposal_id))
            self._c.commit()
            return self._row(self._c.execute(
                "SELECT * FROM improvement_proposals WHERE proposal_id=?",
                (proposal_id,)).fetchone())

    # ------------------------------------------------------------- scheduler
    def claim_review_run(self, agent_id: str, strategy_id: str, period_start: str,
                         period_end: str) -> bool:
        """Durable claim of one weekly run; False if it is done or in progress."""
        with self._lock:
            row = self._c.execute(
                "SELECT status, started_at FROM review_runs WHERE agent_id=? AND strategy_id=? "
                "AND period_start=? AND period_end=?",
                (agent_id, strategy_id, period_start, period_end)).fetchone()
            if row is not None and row["status"] in ("DONE", "RUNNING"):
                # A RUNNING claim from a process that died is retried: the
                # review itself is idempotent on its unique key.
                if row["status"] == "DONE":
                    return False
                started = _parse(row["started_at"])
                if started and (datetime.now(timezone.utc) - started).total_seconds() < 600:
                    return False
            self._c.execute(
                "INSERT INTO review_runs VALUES (?,?,?,?,'RUNNING',?,NULL,NULL,NULL) "
                "ON CONFLICT(agent_id,strategy_id,period_start,period_end) DO UPDATE SET "
                "status='RUNNING', started_at=excluded.started_at, error=NULL",
                (agent_id, strategy_id, period_start, period_end, utcnow()))
            self._c.commit()
            return True

    def finish_review_run(self, agent_id: str, strategy_id: str, period_start: str,
                          period_end: str, *, review_id: Optional[str] = None,
                          error: Optional[str] = None) -> None:
        with self._lock:
            self._c.execute(
                "UPDATE review_runs SET status=?, finished_at=?, review_id=?, error=? "
                "WHERE agent_id=? AND strategy_id=? AND period_start=? AND period_end=?",
                ("FAILED" if error else "DONE", utcnow(), review_id, error,
                 agent_id, strategy_id, period_start, period_end))
            self._c.commit()

    def review_runs(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(
                "SELECT * FROM review_runs ORDER BY period_start DESC, agent_id LIMIT ?",
                (int(limit),))]


def _same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return str(a) == str(b)


def _parse(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
