"""Sprint 2 additive journal-only intelligence metadata migration.

No financial tables are created or modified. Definitions and context evidence
are append-only. Derived calculation runs are reproducible caches, never an
accounting authority. Reapply interrupted migration for forward recovery. For
application rollback, stop intelligence producers and retain these superset
tables; the previous journal SQL remains compatible.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


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
    CREATE TABLE IF NOT EXISTS strategy_validation_runs (
      run_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, strategy_version TEXT,
      config_fingerprint TEXT, dataset_identity TEXT NOT NULL, data_revision TEXT,
      execution_mode TEXT NOT NULL, method TEXT NOT NULL, period_start TEXT,
      period_end TEXT, policy_version TEXT, cost_model_version TEXT,
      classifier_version TEXT, software_revision TEXT, random_seed INTEGER,
      status TEXT NOT NULL, failure_reason TEXT, holdout_access_count INTEGER NOT NULL DEFAULT 0,
      report_json TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
      owner_id TEXT, account_id TEXT, instance_id TEXT, simulation_session_id TEXT,
      UNIQUE(run_id));
    CREATE INDEX IF NOT EXISTS idx_strategy_validation_scope
      ON strategy_validation_runs(owner_id,account_id,instance_id,simulation_session_id,created_at);
    CREATE INDEX IF NOT EXISTS idx_strategy_validation_identity
      ON strategy_validation_runs(strategy_id,strategy_version,config_fingerprint,dataset_identity,method);
    CREATE TABLE IF NOT EXISTS research_jobs (
      job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, owner_id TEXT NOT NULL,
      account_id TEXT, instance_id TEXT, simulation_session_id TEXT,
      job_type TEXT NOT NULL, strategy_id TEXT NOT NULL, strategy_version TEXT,
      config_fingerprint TEXT, frozen_config_json TEXT NOT NULL, request_json TEXT NOT NULL,
      status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, error_class TEXT,
      error_message TEXT, result_json TEXT, requested_at TEXT NOT NULL,
      started_at TEXT, completed_at TEXT, lease_until TEXT,
      UNIQUE(owner_id,idempotency_key));
    CREATE INDEX IF NOT EXISTS idx_research_jobs_scope
      ON research_jobs(owner_id,account_id,instance_id,simulation_session_id,requested_at);
    CREATE INDEX IF NOT EXISTS idx_research_jobs_status
      ON research_jobs(status,lease_until,requested_at);
    """)
    # Sprint 4.6: durable worker ownership and append-only lifecycle history.
    # These are research metadata only; they never participate in accounting.
    existing_job_columns = {row[1] for row in connection.execute("PRAGMA table_info(research_jobs)")}
    for column, definition in (
        ("attempt_number", "INTEGER NOT NULL DEFAULT 0"),
        ("worker_id", "TEXT"),
        ("lease_token", "TEXT"),
        ("heartbeat_at", "TEXT"),
        ("lease_expires_at", "TEXT"),
        ("cancel_requested_at", "TEXT"),
        ("retry_at", "TEXT"),
        ("max_attempts", "INTEGER NOT NULL DEFAULT 3"),
        ("logical_result_key", "TEXT"),
        ("result_reference", "TEXT"),
    ):
        if column not in existing_job_columns:
            connection.execute(f"ALTER TABLE research_jobs ADD COLUMN {column} {definition}")
    connection.executescript("""
    CREATE INDEX IF NOT EXISTS idx_research_jobs_claimable
      ON research_jobs(status,retry_at,lease_expires_at,requested_at);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_research_jobs_result_key
      ON research_jobs(logical_result_key) WHERE logical_result_key IS NOT NULL;
    CREATE TABLE IF NOT EXISTS research_job_attempts (
      attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_number INTEGER NOT NULL,
      worker_id TEXT NOT NULL, lease_token TEXT NOT NULL, status TEXT NOT NULL,
      claimed_at TEXT NOT NULL, started_at TEXT, heartbeat_at TEXT, lease_expires_at TEXT,
      completed_at TEXT, error_class TEXT, error_message TEXT,
      UNIQUE(job_id,attempt_number), UNIQUE(job_id,lease_token),
      FOREIGN KEY(job_id) REFERENCES research_jobs(job_id));
    CREATE INDEX IF NOT EXISTS idx_research_job_attempts_job
      ON research_job_attempts(job_id,attempt_number);
    CREATE TABLE IF NOT EXISTS research_job_events (
      event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, from_status TEXT,
      to_status TEXT NOT NULL, attempt_number INTEGER NOT NULL DEFAULT 0,
      worker_id TEXT, reason TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(job_id) REFERENCES research_jobs(job_id));
    CREATE INDEX IF NOT EXISTS idx_research_job_events_job
      ON research_job_events(job_id,created_at,event_id);
    CREATE TABLE IF NOT EXISTS strategy_research_candidates (
      candidate_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
      strategy_id TEXT NOT NULL, strategy_version TEXT,
      config_fingerprint TEXT, frozen_config_json TEXT NOT NULL,
      dataset_identity TEXT NOT NULL, data_revision TEXT,
      symbol TEXT, asset_portfolio_json TEXT, venue TEXT, market_type TEXT,
      timeframe TEXT, execution_mode TEXT NOT NULL,
      cost_model_version TEXT, classifier_version TEXT, software_revision TEXT,
      policy_version TEXT, validation_run_ids_json TEXT NOT NULL,
      evidence_quality TEXT NOT NULL, scope_json TEXT NOT NULL,
      created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
      UNIQUE(owner_id,candidate_id));
    CREATE INDEX IF NOT EXISTS idx_research_candidates_scope
      ON strategy_research_candidates(owner_id,symbol,timeframe,execution_mode,dataset_identity);
    CREATE TABLE IF NOT EXISTS research_champion_registry (
      designation_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope_key TEXT NOT NULL,
      candidate_id TEXT NOT NULL, selection_policy_version TEXT NOT NULL,
      reviewer_id TEXT NOT NULL, review_status TEXT NOT NULL,
      reason TEXT NOT NULL, evidence_reference TEXT,
      selected_at TEXT NOT NULL, payload_json TEXT NOT NULL,
      FOREIGN KEY(candidate_id) REFERENCES strategy_research_candidates(candidate_id));
    CREATE INDEX IF NOT EXISTS idx_research_champions_scope
      ON research_champion_registry(owner_id,scope_key,selected_at);
    CREATE TABLE IF NOT EXISTS strategy_comparisons (
      comparison_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL,
      owner_id TEXT NOT NULL, champion_candidate_id TEXT NOT NULL,
      challenger_candidate_id TEXT NOT NULL, scope_json TEXT NOT NULL,
      policy_version TEXT NOT NULL, statistical_method TEXT NOT NULL,
      random_seed INTEGER, status TEXT NOT NULL, recommendation TEXT NOT NULL,
      blocking_reasons_json TEXT NOT NULL, report_json TEXT NOT NULL,
      validation_run_ids_json TEXT NOT NULL, created_at TEXT NOT NULL,
      completed_at TEXT, payload_json TEXT NOT NULL, UNIQUE(owner_id,idempotency_key),
      FOREIGN KEY(champion_candidate_id) REFERENCES strategy_research_candidates(candidate_id),
      FOREIGN KEY(challenger_candidate_id) REFERENCES strategy_research_candidates(candidate_id));
    CREATE INDEX IF NOT EXISTS idx_strategy_comparisons_scope
      ON strategy_comparisons(owner_id,created_at,champion_candidate_id,challenger_candidate_id);
    CREATE TABLE IF NOT EXISTS research_campaigns (
      campaign_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, owner_id TEXT NOT NULL,
      strategy_family TEXT, selection_criteria_json TEXT NOT NULL, policy_versions_json TEXT NOT NULL,
      status TEXT NOT NULL, candidate_count INTEGER NOT NULL DEFAULT 0,
      comparison_attempts INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL, completed_at TEXT, UNIQUE(owner_id,idempotency_key));
    CREATE INDEX IF NOT EXISTS idx_research_campaigns_owner
      ON research_campaigns(owner_id,created_at,status);
    CREATE TABLE IF NOT EXISTS matched_experiments (
      experiment_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, owner_id TEXT NOT NULL,
      campaign_id TEXT, champion_candidate_id TEXT NOT NULL, challenger_candidate_ids_json TEXT NOT NULL,
      scope_json TEXT NOT NULL, frozen_spec_json TEXT NOT NULL, candidate_job_ids_json TEXT NOT NULL,
      validation_run_ids_json TEXT NOT NULL, holdout_status TEXT NOT NULL,
      status TEXT NOT NULL, recommendation TEXT NOT NULL, warnings_json TEXT NOT NULL,
      report_json TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
      payload_json TEXT NOT NULL, UNIQUE(owner_id,idempotency_key));
    CREATE INDEX IF NOT EXISTS idx_matched_experiments_owner
      ON matched_experiments(owner_id,created_at,status);
    CREATE TABLE IF NOT EXISTS matched_experiment_candidates (
      experiment_id TEXT NOT NULL, candidate_id TEXT NOT NULL, role TEXT NOT NULL,
      job_id TEXT, status TEXT NOT NULL, validation_run_id TEXT, error_message TEXT,
      updated_at TEXT NOT NULL, PRIMARY KEY(experiment_id,candidate_id));
    CREATE INDEX IF NOT EXISTS idx_matched_experiment_candidates_job
      ON matched_experiment_candidates(job_id,status);
    CREATE TABLE IF NOT EXISTS matched_experiment_events (
      event_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, from_status TEXT,
      to_status TEXT NOT NULL, reason TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_matched_experiment_events
      ON matched_experiment_events(experiment_id,created_at);
    CREATE TABLE IF NOT EXISTS holdout_exposure_events (
      exposure_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, campaign_id TEXT,
      experiment_id TEXT NOT NULL, dataset_identity TEXT NOT NULL, period_start TEXT,
      period_end TEXT, candidate_id TEXT, exposure_kind TEXT NOT NULL,
      selection_influence TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_holdout_exposure_identity
      ON holdout_exposure_events(owner_id,dataset_identity,period_start,period_end,created_at);
    CREATE TABLE IF NOT EXISTS research_agent_hypotheses (
      hypothesis_id TEXT PRIMARY KEY, campaign_id TEXT, owner_id TEXT NOT NULL,
      strategy_id TEXT NOT NULL, strategy_version TEXT, baseline_fingerprint TEXT,
      proposed_fingerprint TEXT, question TEXT NOT NULL, expected_effect TEXT,
      evidence_refs_json TEXT NOT NULL, uncertainties_json TEXT NOT NULL,
      methodology_json TEXT NOT NULL, dataset_scope_json TEXT NOT NULL,
      cost_assumptions_json TEXT NOT NULL, agent_version TEXT, status TEXT NOT NULL,
      plan_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      payload_json TEXT NOT NULL, UNIQUE(owner_id,hypothesis_id));
    CREATE INDEX IF NOT EXISTS idx_research_agent_hypotheses_owner
      ON research_agent_hypotheses(owner_id,created_at,status);
    CREATE TABLE IF NOT EXISTS research_agent_approvals (
      approval_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, owner_id TEXT NOT NULL,
      plan_fingerprint TEXT NOT NULL, reviewer_id TEXT NOT NULL, decision TEXT NOT NULL,
      approved_at TEXT NOT NULL, max_jobs INTEGER NOT NULL, resource_budget_json TEXT NOT NULL,
      payload_json TEXT NOT NULL, UNIQUE(owner_id,hypothesis_id,plan_fingerprint));
    CREATE TABLE IF NOT EXISTS research_agent_events (
      event_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, hypothesis_id TEXT,
      event_type TEXT NOT NULL, actor_id TEXT NOT NULL, payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_research_agent_events_owner
      ON research_agent_events(owner_id,created_at,event_type);
    """)
    # Lifecycle rows are mutable by their transition methods; evidence and
    # event rows remain append-only.
    for table in ("regime_classifier_versions", "market_context_snapshots", "intelligence_calculation_runs", "strategy_validation_runs", "strategy_research_candidates", "research_champion_registry", "strategy_comparisons", "matched_experiment_events", "holdout_exposure_events"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()}
              BEFORE {action} ON {table}
              BEGIN SELECT RAISE(ABORT, 'immutable intelligence metadata'); END""")
    connection.executescript((Path(__file__).parents[1] / "migrations" / "research" / "0001_dataset_retention.sql").read_text())
