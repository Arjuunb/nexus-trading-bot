"""Sprint 2 additive journal-only intelligence metadata migration.

No financial tables are created or modified. Definitions and context evidence
are append-only. Derived calculation runs are reproducible caches, never an
accounting authority. Reapply interrupted migration for forward recovery. For
application rollback, stop intelligence producers and retain these superset
tables; the previous journal SQL remains compatible.
"""
from __future__ import annotations

import sqlite3


def apply_context_migrations(connection: sqlite3.Connection) -> None:
    """Apply after journal evidence migrations; caller owns lock and commit."""
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS regime_classifier_versions (
      classifier_id TEXT NOT NULL, classifier_version TEXT NOT NULL,
      parameter_hash TEXT NOT NULL, parameters_json TEXT NOT NULL,
      definition_json TEXT NOT NULL, captured_at TEXT NOT NULL,
      PRIMARY KEY(classifier_id,classifier_version),
      UNIQUE(classifier_id,classifier_version,parameter_hash));
    CREATE TABLE IF NOT EXISTS market_context_snapshots (
      snapshot_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL,
      trade_id TEXT NOT NULL, classification_kind TEXT NOT NULL
        CHECK(classification_kind IN ('ENTRY','RESEARCH')),
      classifier_id TEXT NOT NULL, classifier_version TEXT NOT NULL,
      parameter_hash TEXT NOT NULL, strategy_id TEXT, strategy_version TEXT,
      strategy_config_hash TEXT, owner_id TEXT, account_id TEXT,
      instance_id TEXT, simulation_session_id TEXT, lab_id TEXT,
      execution_mode TEXT, source_kind TEXT, symbol TEXT,
      signal_timestamp TEXT, entry_timestamp TEXT, classification_timestamp TEXT NOT NULL,
      entry_timeframe TEXT, higher_timeframe TEXT, direction TEXT,
      session TEXT, trend_regime TEXT, volatility_regime TEXT,
      structure_regime TEXT, context_quality TEXT, evidence_quality TEXT,
      payload_json TEXT NOT NULL, captured_at TEXT NOT NULL,
      UNIQUE(episode_id,trade_id,classifier_id,classifier_version,parameter_hash,classification_kind),
      FOREIGN KEY(episode_id) REFERENCES strategy_position_episodes(episode_id),
      FOREIGN KEY(classifier_id,classifier_version,parameter_hash)
        REFERENCES regime_classifier_versions(classifier_id,classifier_version,parameter_hash));
    CREATE INDEX IF NOT EXISTS idx_market_context_identity_scope
      ON market_context_snapshots(owner_id,account_id,strategy_id,strategy_version,
        strategy_config_hash,instance_id,simulation_session_id,lab_id,execution_mode,source_kind);
    CREATE INDEX IF NOT EXISTS idx_market_context_episode_classifier
      ON market_context_snapshots(episode_id,trade_id,classifier_id,classifier_version,
        parameter_hash,classification_kind);
    CREATE INDEX IF NOT EXISTS idx_market_context_time
      ON market_context_snapshots(owner_id,signal_timestamp,snapshot_id);
    CREATE INDEX IF NOT EXISTS idx_market_context_dimensions
      ON market_context_snapshots(owner_id,symbol,entry_timeframe,session,trend_regime,volatility_regime,direction);
    CREATE TABLE IF NOT EXISTS intelligence_calculation_runs (
      run_id TEXT PRIMARY KEY, cache_key TEXT NOT NULL, input_watermark TEXT NOT NULL,
      contract_version TEXT NOT NULL, calculated_at TEXT NOT NULL,
      owner_id TEXT, account_id TEXT, instance_id TEXT,
      scope_json TEXT NOT NULL, cohort_json TEXT NOT NULL,
      report_json TEXT NOT NULL, captured_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_intelligence_cache_input
      ON intelligence_calculation_runs(cache_key,input_watermark,calculated_at);
    CREATE INDEX IF NOT EXISTS idx_intelligence_cache_latest
      ON intelligence_calculation_runs(owner_id,cache_key,calculated_at);
    """)
    for table in ("regime_classifier_versions", "market_context_snapshots", "intelligence_calculation_runs"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()}
              BEFORE {action} ON {table}
              BEGIN SELECT RAISE(ABORT, 'immutable intelligence metadata'); END""")
