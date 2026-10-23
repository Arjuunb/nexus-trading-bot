-- PostgreSQL-only isolated research/shadow metadata. Never apply to a financial database.
CREATE SCHEMA IF NOT EXISTS nexus_workers;
SET search_path TO nexus_workers,pg_catalog;
CREATE TABLE IF NOT EXISTS worker_store_identity(purpose TEXT PRIMARY KEY,authority_id TEXT NOT NULL,version INTEGER NOT NULL,writer_role TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS research_jobs (
      job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, owner_id TEXT NOT NULL,
      account_id TEXT, instance_id TEXT, simulation_session_id TEXT,
      job_type TEXT NOT NULL, strategy_id TEXT NOT NULL, strategy_version TEXT,
      config_fingerprint TEXT, frozen_config_json TEXT NOT NULL, request_json TEXT NOT NULL,
      status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, error_class TEXT,
      error_message TEXT, result_json TEXT, requested_at TEXT NOT NULL,
      started_at TEXT, completed_at TEXT, lease_until TEXT, attempt_number INTEGER NOT NULL DEFAULT 0, worker_id TEXT, lease_token TEXT, heartbeat_at TEXT, lease_expires_at TEXT, cancel_requested_at TEXT, retry_at TEXT, max_attempts INTEGER NOT NULL DEFAULT 3, logical_result_key TEXT, result_reference TEXT,
      UNIQUE(owner_id,idempotency_key));

CREATE TABLE IF NOT EXISTS research_job_attempts (
      attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_number INTEGER NOT NULL,
      worker_id TEXT NOT NULL, lease_token TEXT NOT NULL, status TEXT NOT NULL,
      claimed_at TEXT NOT NULL, started_at TEXT, heartbeat_at TEXT, lease_expires_at TEXT,
      completed_at TEXT, error_class TEXT, error_message TEXT,
      UNIQUE(job_id,attempt_number), UNIQUE(job_id,lease_token),
      FOREIGN KEY(job_id) REFERENCES research_jobs(job_id));

CREATE TABLE IF NOT EXISTS research_job_events (
      event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, from_status TEXT,
      to_status TEXT NOT NULL, attempt_number INTEGER NOT NULL DEFAULT 0,
      worker_id TEXT, reason TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(job_id) REFERENCES research_jobs(job_id), rowid BIGSERIAL);

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

CREATE TABLE IF NOT EXISTS retained_research_datasets(
 snapshot_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
 candles_json TEXT NOT NULL, payload_bytes INTEGER NOT NULL, candle_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS retained_research_dataset_refs(
 job_id TEXT PRIMARY KEY REFERENCES research_jobs(job_id), owner_id TEXT NOT NULL,
 snapshot_id TEXT NOT NULL REFERENCES retained_research_datasets(snapshot_id),
 manifest_json TEXT NOT NULL, retained_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_validation_scope
      ON strategy_validation_runs(owner_id,account_id,instance_id,simulation_session_id,created_at);

CREATE INDEX IF NOT EXISTS idx_strategy_validation_identity
      ON strategy_validation_runs(strategy_id,strategy_version,config_fingerprint,dataset_identity,method);

CREATE INDEX IF NOT EXISTS idx_research_jobs_scope
      ON research_jobs(owner_id,account_id,instance_id,simulation_session_id,requested_at);

CREATE INDEX IF NOT EXISTS idx_research_jobs_status
      ON research_jobs(status,lease_until,requested_at);

CREATE INDEX IF NOT EXISTS idx_research_jobs_claimable
      ON research_jobs(status,retry_at,lease_expires_at,requested_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_research_jobs_result_key
      ON research_jobs(logical_result_key) WHERE logical_result_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_research_job_attempts_job
      ON research_job_attempts(job_id,attempt_number);

CREATE INDEX IF NOT EXISTS idx_research_job_events_job
      ON research_job_events(job_id,created_at,event_id);

CREATE INDEX IF NOT EXISTS ix_retained_dataset_owner ON retained_research_dataset_refs(owner_id,job_id);

CREATE TABLE IF NOT EXISTS shadow_registrations(
 registration_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL, frozen_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('DRAFT','READY','OBSERVING','PAUSED','BLOCKED','STOPPED','FAILED')),
 readiness TEXT NOT NULL, desired_observing INTEGER NOT NULL DEFAULT 0,
 observation_start TEXT, resume_after TEXT, last_decision_time TEXT,
 heartbeat DOUBLE PRECISION, lease_token TEXT, lease_owner TEXT, lease_expiry DOUBLE PRECISION,
 generation INTEGER NOT NULL DEFAULT 0, runtime_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, UNIQUE(owner_id,idempotency_key)
);

CREATE TABLE IF NOT EXISTS shadow_observations(
 decision_id TEXT PRIMARY KEY, registration_id TEXT NOT NULL,
 owner_id TEXT NOT NULL, candle_close TEXT NOT NULL, candle_hash TEXT,
 context_hash TEXT, status TEXT NOT NULL, payload_json TEXT NOT NULL,
 UNIQUE(registration_id,candle_close),
 FOREIGN KEY(registration_id) REFERENCES shadow_registrations(registration_id)
);

CREATE TABLE IF NOT EXISTS shadow_observation_events(
 event_id TEXT PRIMARY KEY, registration_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 action TEXT NOT NULL, timestamp TEXT NOT NULL, payload_json TEXT NOT NULL,
 FOREIGN KEY(registration_id) REFERENCES shadow_registrations(registration_id)
, rowid BIGSERIAL);

CREATE TABLE IF NOT EXISTS shadow_comparison_artifacts(
 comparison_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
 registration_id TEXT NOT NULL REFERENCES shadow_registrations(registration_id),
 active_source_id TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 calculation_version TEXT NOT NULL, input_watermark TEXT NOT NULL,
 created_at TEXT NOT NULL, payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_comparison_requests(
 owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL,
 comparison_id TEXT NOT NULL REFERENCES shadow_comparison_artifacts(comparison_id),
 PRIMARY KEY(owner_id,idempotency_key)
);

CREATE TABLE IF NOT EXISTS forward_qualification_policies(
 policy_version TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS forward_observation_windows(
 window_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, source_id TEXT NOT NULL,
 scope_key TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 evidence_hash TEXT NOT NULL, payload_bytes INTEGER NOT NULL, payload_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS forward_qualification_reports(
 report_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, source_id TEXT NOT NULL,
 window_id TEXT NOT NULL REFERENCES forward_observation_windows(window_id),
 policy_version TEXT NOT NULL REFERENCES forward_qualification_policies(policy_version),
 created_at TEXT NOT NULL, payload_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS forward_qualification_requests(
 owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL,
 report_id TEXT NOT NULL REFERENCES forward_qualification_reports(report_id),
 PRIMARY KEY(owner_id,idempotency_key));

CREATE TABLE IF NOT EXISTS forward_qualification_reviews(
 review_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 report_id TEXT NOT NULL REFERENCES forward_qualification_reports(report_id),
 reviewer_id TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
 UNIQUE(owner_id,idempotency_key), rowid BIGSERIAL);

CREATE TABLE IF NOT EXISTS forward_qualification_events(
 event_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, report_id TEXT NOT NULL
 REFERENCES forward_qualification_reports(report_id), action TEXT NOT NULL,
 created_at TEXT NOT NULL, payload_json TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS ix_shadow_registration_owner ON shadow_registrations(owner_id,created_at);

CREATE INDEX IF NOT EXISTS ix_shadow_registration_recovery ON shadow_registrations(desired_observing,lease_expiry);

CREATE INDEX IF NOT EXISTS ix_shadow_observation_scope ON shadow_observations(owner_id,registration_id,candle_close);

CREATE INDEX IF NOT EXISTS ix_shadow_observation_event_scope ON shadow_observation_events(owner_id,registration_id,timestamp);

CREATE INDEX IF NOT EXISTS ix_shadow_comparison_owner ON shadow_comparison_artifacts(owner_id,created_at,comparison_id);

CREATE INDEX IF NOT EXISTS ix_forward_window_owner_scope
 ON forward_observation_windows(owner_id,scope_key,period_start,period_end);

CREATE INDEX IF NOT EXISTS ix_forward_report_owner_source
 ON forward_qualification_reports(owner_id,source_id,created_at,report_id);

CREATE INDEX IF NOT EXISTS ix_forward_review_owner_report
 ON forward_qualification_reviews(owner_id,report_id,created_at,review_id);

CREATE INDEX IF NOT EXISTS ix_forward_event_owner_report
 ON forward_qualification_events(owner_id,report_id,created_at,event_id);

CREATE OR REPLACE FUNCTION immutable_metadata() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE_WORKER_EVIDENCE'; END $$;

DROP TRIGGER IF EXISTS immutable_worker_evidence ON strategy_validation_runs;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON strategy_validation_runs FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON retained_research_datasets;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON retained_research_datasets FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON retained_research_dataset_refs;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON retained_research_dataset_refs FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON shadow_observations;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON shadow_observations FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON shadow_observation_events;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON shadow_observation_events FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON research_job_events;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON research_job_events FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON shadow_comparison_artifacts;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON shadow_comparison_artifacts FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON shadow_comparison_requests;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON shadow_comparison_requests FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON forward_qualification_policies;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON forward_qualification_policies FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON forward_observation_windows;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON forward_observation_windows FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON forward_qualification_reports;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON forward_qualification_reports FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON forward_qualification_requests;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON forward_qualification_requests FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON forward_qualification_reviews;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON forward_qualification_reviews FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON forward_qualification_events;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON forward_qualification_events FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

CREATE OR REPLACE FUNCTION frozen_shadow_registration() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF ROW(OLD.frozen_json,OLD.request_json,OLD.owner_id,OLD.registration_id,OLD.idempotency_key,OLD.created_at) IS DISTINCT FROM ROW(NEW.frozen_json,NEW.request_json,NEW.owner_id,NEW.registration_id,NEW.idempotency_key,NEW.created_at) THEN RAISE EXCEPTION 'IMMUTABLE_SHADOW_IDENTITY'; END IF; RETURN NEW; END $$;
DROP TRIGGER IF EXISTS frozen_shadow_identity ON shadow_registrations;
CREATE TRIGGER frozen_shadow_identity BEFORE UPDATE ON shadow_registrations FOR EACH ROW EXECUTE FUNCTION frozen_shadow_registration();

-- PostgreSQL has no hidden rowid; preserve deterministic validation ordering.
ALTER TABLE strategy_validation_runs ADD COLUMN IF NOT EXISTS rowid BIGSERIAL;

CREATE OR REPLACE FUNCTION frozen_research_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(OLD.job_id,OLD.owner_id,OLD.idempotency_key,OLD.strategy_id,OLD.strategy_version,
        OLD.config_fingerprint,OLD.frozen_config_json,OLD.request_json,OLD.account_id,OLD.instance_id,OLD.simulation_session_id,OLD.job_type)
 IS DISTINCT FROM
    ROW(NEW.job_id,NEW.owner_id,NEW.idempotency_key,NEW.strategy_id,NEW.strategy_version,
        NEW.config_fingerprint,NEW.frozen_config_json,NEW.request_json,NEW.account_id,NEW.instance_id,NEW.simulation_session_id,NEW.job_type)
 THEN RAISE EXCEPTION 'IMMUTABLE_RESEARCH_IDENTITY'; END IF;
 IF OLD.logical_result_key IS DISTINCT FROM NEW.logical_result_key AND NOT
    (OLD.logical_result_key IS NULL AND NEW.logical_result_key='validation:'||OLD.job_id AND OLD.attempt_number=0 AND OLD.status='QUEUED')
 THEN RAISE EXCEPTION 'IMMUTABLE_RESEARCH_RESULT_KEY'; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS frozen_research_job ON research_jobs;
CREATE TRIGGER frozen_research_job BEFORE UPDATE ON research_jobs
 FOR EACH ROW EXECUTE FUNCTION frozen_research_identity();

-- Existing campaign/candidate/agent metadata follows the same selected backend.
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

DROP TRIGGER IF EXISTS immutable_worker_evidence ON strategy_research_candidates; CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON strategy_research_candidates FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

CREATE TABLE IF NOT EXISTS research_champion_registry (
      designation_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope_key TEXT NOT NULL,
      candidate_id TEXT NOT NULL, selection_policy_version TEXT NOT NULL,
      reviewer_id TEXT NOT NULL, review_status TEXT NOT NULL,
      reason TEXT NOT NULL, evidence_reference TEXT,
      selected_at TEXT NOT NULL, payload_json TEXT NOT NULL,
      FOREIGN KEY(candidate_id) REFERENCES strategy_research_candidates(candidate_id));

DROP TRIGGER IF EXISTS immutable_worker_evidence ON research_champion_registry; CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON research_champion_registry FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

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

DROP TRIGGER IF EXISTS immutable_worker_evidence ON strategy_comparisons; CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON strategy_comparisons FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

CREATE TABLE IF NOT EXISTS research_campaigns (
      campaign_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, owner_id TEXT NOT NULL,
      strategy_family TEXT, selection_criteria_json TEXT NOT NULL, policy_versions_json TEXT NOT NULL,
      status TEXT NOT NULL, candidate_count INTEGER NOT NULL DEFAULT 0,
      comparison_attempts INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL, completed_at TEXT, UNIQUE(owner_id,idempotency_key));

CREATE TABLE IF NOT EXISTS matched_experiments (
      experiment_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL, owner_id TEXT NOT NULL,
      campaign_id TEXT, champion_candidate_id TEXT NOT NULL, challenger_candidate_ids_json TEXT NOT NULL,
      scope_json TEXT NOT NULL, frozen_spec_json TEXT NOT NULL, candidate_job_ids_json TEXT NOT NULL,
      validation_run_ids_json TEXT NOT NULL, holdout_status TEXT NOT NULL,
      status TEXT NOT NULL, recommendation TEXT NOT NULL, warnings_json TEXT NOT NULL,
      report_json TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
      payload_json TEXT NOT NULL, UNIQUE(owner_id,idempotency_key));

CREATE TABLE IF NOT EXISTS matched_experiment_candidates (
      experiment_id TEXT NOT NULL, candidate_id TEXT NOT NULL, role TEXT NOT NULL,
      job_id TEXT, status TEXT NOT NULL, validation_run_id TEXT, error_message TEXT,
      updated_at TEXT NOT NULL, PRIMARY KEY(experiment_id,candidate_id));

CREATE TABLE IF NOT EXISTS matched_experiment_events (
      event_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, from_status TEXT,
      to_status TEXT NOT NULL, reason TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, rowid BIGSERIAL);

DROP TRIGGER IF EXISTS immutable_worker_evidence ON matched_experiment_events; CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON matched_experiment_events FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

CREATE TABLE IF NOT EXISTS holdout_exposure_events (
      exposure_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, campaign_id TEXT,
      experiment_id TEXT NOT NULL, dataset_identity TEXT NOT NULL, period_start TEXT,
      period_end TEXT, candidate_id TEXT, exposure_kind TEXT NOT NULL,
      selection_influence TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL, rowid BIGSERIAL);

DROP TRIGGER IF EXISTS immutable_worker_evidence ON holdout_exposure_events; CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON holdout_exposure_events FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

CREATE TABLE IF NOT EXISTS research_agent_hypotheses (
      hypothesis_id TEXT PRIMARY KEY, campaign_id TEXT, owner_id TEXT NOT NULL,
      strategy_id TEXT NOT NULL, strategy_version TEXT, baseline_fingerprint TEXT,
      proposed_fingerprint TEXT, question TEXT NOT NULL, expected_effect TEXT,
      evidence_refs_json TEXT NOT NULL, uncertainties_json TEXT NOT NULL,
      methodology_json TEXT NOT NULL, dataset_scope_json TEXT NOT NULL,
      cost_assumptions_json TEXT NOT NULL, agent_version TEXT, status TEXT NOT NULL,
      plan_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      payload_json TEXT NOT NULL, UNIQUE(owner_id,hypothesis_id));

CREATE TABLE IF NOT EXISTS research_agent_approvals (
      approval_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL, owner_id TEXT NOT NULL,
      plan_fingerprint TEXT NOT NULL, reviewer_id TEXT NOT NULL, decision TEXT NOT NULL,
      approved_at TEXT NOT NULL, max_jobs INTEGER NOT NULL, resource_budget_json TEXT NOT NULL,
      payload_json TEXT NOT NULL, UNIQUE(owner_id,hypothesis_id,plan_fingerprint));

DROP TRIGGER IF EXISTS immutable_worker_evidence ON research_agent_approvals; CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON research_agent_approvals FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

CREATE TABLE IF NOT EXISTS research_agent_events (
      event_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, hypothesis_id TEXT,
      event_type TEXT NOT NULL, actor_id TEXT NOT NULL, payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL, rowid BIGSERIAL);

DROP TRIGGER IF EXISTS immutable_worker_evidence ON research_agent_events; CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON research_agent_events FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

CREATE INDEX IF NOT EXISTS idx_research_candidates_scope
      ON strategy_research_candidates(owner_id,symbol,timeframe,execution_mode,dataset_identity);

CREATE INDEX IF NOT EXISTS idx_research_champions_scope
      ON research_champion_registry(owner_id,scope_key,selected_at);

CREATE INDEX IF NOT EXISTS idx_strategy_comparisons_scope
      ON strategy_comparisons(owner_id,created_at,champion_candidate_id,challenger_candidate_id);

CREATE INDEX IF NOT EXISTS idx_research_campaigns_owner
      ON research_campaigns(owner_id,created_at,status);

CREATE INDEX IF NOT EXISTS idx_matched_experiments_owner
      ON matched_experiments(owner_id,created_at,status);

CREATE INDEX IF NOT EXISTS idx_matched_experiment_candidates_job
      ON matched_experiment_candidates(job_id,status);

CREATE INDEX IF NOT EXISTS idx_matched_experiment_events
      ON matched_experiment_events(experiment_id,created_at);

CREATE INDEX IF NOT EXISTS idx_holdout_exposure_identity
      ON holdout_exposure_events(owner_id,dataset_identity,period_start,period_end,created_at);

CREATE INDEX IF NOT EXISTS idx_research_agent_hypotheses_owner
      ON research_agent_hypotheses(owner_id,created_at,status);

CREATE INDEX IF NOT EXISTS idx_research_agent_events_owner
      ON research_agent_events(owner_id,created_at,event_type);

-- Shared operational evidence, never a financial data source.
-- Operational observations only, installed in the isolated shadow metadata DB.
CREATE TABLE IF NOT EXISTS operational_sources(
 owner_id TEXT NOT NULL,source_id TEXT NOT NULL,scope_json TEXT NOT NULL,
 created_at TEXT NOT NULL,PRIMARY KEY(owner_id,source_id));
CREATE TABLE IF NOT EXISTS operational_events(
 event_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,source_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL,event_type TEXT NOT NULL,occurred_at TEXT NOT NULL,
 committed_at TEXT NOT NULL,evidence_hash TEXT NOT NULL,payload_bytes INTEGER NOT NULL,
 payload_json TEXT NOT NULL,UNIQUE(owner_id,source_id,idempotency_key),
 FOREIGN KEY(owner_id,source_id) REFERENCES operational_sources(owner_id,source_id));
CREATE INDEX IF NOT EXISTS ix_operational_owner_clock ON operational_events(owner_id,source_id,occurred_at,event_id);

DROP TRIGGER IF EXISTS immutable_worker_evidence ON operational_sources;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON operational_sources FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON operational_events;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON operational_events FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

-- Reconciliation artifacts are separate from the financial authority.
-- Read-only financial reconciliation artifacts in the isolated metadata store.
CREATE TABLE IF NOT EXISTS paper_reconciliation_policies(
 policy_version TEXT PRIMARY KEY,payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_reconciliation_reports(
 report_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,source_id TEXT NOT NULL,
 created_at TEXT NOT NULL,evidence_hash TEXT NOT NULL,payload_bytes INTEGER NOT NULL,
 snapshot_json TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_paper_reconciliation_owner ON paper_reconciliation_reports(owner_id,source_id,created_at,report_id);
CREATE TABLE IF NOT EXISTS paper_reconciliation_requests(
 owner_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_json TEXT NOT NULL,
 report_id TEXT NOT NULL REFERENCES paper_reconciliation_reports(report_id),PRIMARY KEY(owner_id,idempotency_key));

DROP TRIGGER IF EXISTS immutable_worker_evidence ON paper_reconciliation_policies;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON paper_reconciliation_policies FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON paper_reconciliation_reports;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON paper_reconciliation_reports FOR EACH ROW EXECUTE FUNCTION immutable_metadata();

DROP TRIGGER IF EXISTS immutable_worker_evidence ON paper_reconciliation_requests;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON paper_reconciliation_requests FOR EACH ROW EXECUTE FUNCTION immutable_metadata();
