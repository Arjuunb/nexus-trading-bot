"""Additive SQLite decision-journal evidence migration, version 1.

These tables contain correlation metadata and captured receipts, never account
balances or a second paper ledger. Existing rows deliberately retain NULL
identity/lineage. Apply before instrumented producers start. Reapplying is safe.
Rollback is to stop the producers and run the previous application against this
superset schema; retain evidence tables rather than deleting financial history.
This migration only changes the journal. Sprint 1.5 separately adds the
immutable paper_evidence_outbox metadata table at the SQLite accounting
transaction boundary; it preserves existing financial rows.
"""
from __future__ import annotations

import sqlite3


HEADER_COLUMNS = {
    "strategy_config_hash": "TEXT", "source_hash": "TEXT", "identity_status": "TEXT",
    "signal_id": "TEXT", "decision_id": "TEXT", "order_id": "TEXT", "execution_id": "TEXT",
    "episode_id": "TEXT", "parent_trade_id": "TEXT", "initial_risk_amount_text": "TEXT",
    "evidence_schema_version": "INTEGER", "signal_at": "TEXT", "decision_at": "TEXT",
    "executed_at": "TEXT", "owner_id": "TEXT", "account_id": "TEXT",
    "lab_id": "TEXT", "source_kind": "TEXT",
}


def apply_evidence_migrations(connection: sqlite3.Connection) -> None:
    """Upgrade only the journal database; caller owns locking/commit."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(trade_decision_journal)")}
    for name, sql_type in HEADER_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE trade_decision_journal ADD COLUMN {name} {sql_type}")
    event_columns = {row[1] for row in connection.execute("PRAGMA table_info(trade_decision_events)")}
    if "event_id" not in event_columns:
        connection.execute("ALTER TABLE trade_decision_events ADD COLUMN event_id TEXT")
    connection.executescript("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_event_id
      ON trade_decision_events(event_id) WHERE event_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_journal_evidence_cohort ON trade_decision_journal
      (strategy_id,strategy_version,strategy_config_hash,instance_id,execution_mode);
    CREATE TABLE IF NOT EXISTS strategy_evidence_versions (
      strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
      strategy_config_hash TEXT NOT NULL, configuration_json TEXT NOT NULL,
      source_hash TEXT, identity_json TEXT NOT NULL, captured_at TEXT NOT NULL,
      PRIMARY KEY(strategy_id,strategy_version,strategy_config_hash));
    CREATE TABLE IF NOT EXISTS strategy_evidence_events (
      event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
      envelope_json TEXT NOT NULL, captured_at TEXT NOT NULL, observed_at TEXT,
      strategy_id TEXT, strategy_version TEXT, strategy_config_hash TEXT,
      instance_id TEXT, simulation_session_id TEXT, execution_mode TEXT,
      owner_id TEXT, account_id TEXT, lab_id TEXT, source_kind TEXT, symbol TEXT,
      signal_id TEXT, decision_id TEXT, order_id TEXT, trade_id TEXT,
      position_id TEXT, episode_id TEXT,
      FOREIGN KEY(strategy_id,strategy_version,strategy_config_hash)
        REFERENCES strategy_evidence_versions(strategy_id,strategy_version,strategy_config_hash));
    CREATE INDEX IF NOT EXISTS idx_strategy_evidence_scope ON strategy_evidence_events
      (strategy_id,strategy_version,strategy_config_hash,instance_id,execution_mode);
    CREATE INDEX IF NOT EXISTS idx_strategy_evidence_decision ON strategy_evidence_events(decision_id);
    CREATE INDEX IF NOT EXISTS idx_strategy_evidence_episode ON strategy_evidence_events(episode_id);
    CREATE TABLE IF NOT EXISTS strategy_position_episodes (
      episode_id TEXT PRIMARY KEY, root_trade_id TEXT NOT NULL,
      root_position_id TEXT NOT NULL, metadata_json TEXT NOT NULL,
      opened_at TEXT, initial_risk_amount_text TEXT,
      strategy_id TEXT, strategy_version TEXT, strategy_config_hash TEXT,
      instance_id TEXT, simulation_session_id TEXT, execution_mode TEXT,
      owner_id TEXT, account_id TEXT, lab_id TEXT, source_kind TEXT, symbol TEXT,
      FOREIGN KEY(strategy_id,strategy_version,strategy_config_hash)
        REFERENCES strategy_evidence_versions(strategy_id,strategy_version,strategy_config_hash));
    CREATE TABLE IF NOT EXISTS strategy_episode_legs (
      leg_key TEXT PRIMARY KEY, episode_id TEXT NOT NULL,
      trade_id TEXT NOT NULL, position_id TEXT NOT NULL,
      parent_trade_id TEXT, entry_event_id TEXT NOT NULL,
      metadata_json TEXT NOT NULL,
      FOREIGN KEY(episode_id) REFERENCES strategy_position_episodes(episode_id),
      FOREIGN KEY(entry_event_id) REFERENCES strategy_evidence_events(event_id));
    CREATE INDEX IF NOT EXISTS idx_strategy_episode_legs ON strategy_episode_legs(episode_id);
    CREATE TABLE IF NOT EXISTS strategy_evidence_reconciliation_runs (
      run_id TEXT PRIMARY KEY, calculated_at TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN
        ('COMPLETE','PARTIAL','UNKNOWN','CONFLICTED','RECOVERING')),
      scope_json TEXT NOT NULL, cohort_json TEXT NOT NULL,
      watermarks_json TEXT NOT NULL, report_json TEXT NOT NULL,
      captured_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_evidence_reconciliation_status_time
      ON strategy_evidence_reconciliation_runs(status,calculated_at);
    """)
    for table in ("strategy_evidence_versions", "strategy_evidence_events",
                  "strategy_position_episodes", "strategy_episode_legs",
                  "strategy_evidence_reconciliation_runs"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()}
              BEFORE {action} ON {table} BEGIN
              SELECT RAISE(ABORT, 'immutable strategy evidence'); END""")
    immutable = ("strategy_id", "strategy_version", "strategy_config_hash", "source_hash",
                 "episode_id", "parent_trade_id", "initial_risk_amount_text", "signal_id",
                 "decision_id", "order_id", "execution_id", "instance_id", "simulation_session_id",
                 "execution_mode", "owner_id", "account_id", "lab_id", "source_kind")
    changes = " OR ".join(f"NEW.{name} IS NOT OLD.{name}" for name in immutable)
    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_journal_evidence_header
      BEFORE UPDATE ON trade_decision_journal
      WHEN OLD.strategy_config_hash IS NOT NULL AND ({changes})
      BEGIN SELECT RAISE(ABORT, 'immutable journal evidence header'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS valid_journal_strategy_snapshot
      BEFORE INSERT ON trade_decision_journal
      WHEN NEW.strategy_config_hash IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM strategy_evidence_versions WHERE strategy_id=NEW.strategy_id
          AND strategy_version=NEW.strategy_version
          AND strategy_config_hash=NEW.strategy_config_hash)
      BEGIN SELECT RAISE(ABORT, 'journal strategy snapshot missing'); END""")
