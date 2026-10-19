-- SQLite only: additive advisory namespace in the existing shadow database.
CREATE TABLE IF NOT EXISTS forward_qualification_policies(
 policy_version TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS forward_observation_windows(
 window_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, source_id TEXT NOT NULL,
 scope_key TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 evidence_hash TEXT NOT NULL, payload_bytes INTEGER NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_forward_window_owner_scope
 ON forward_observation_windows(owner_id,scope_key,period_start,period_end);
CREATE TABLE IF NOT EXISTS forward_qualification_reports(
 report_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, source_id TEXT NOT NULL,
 window_id TEXT NOT NULL REFERENCES forward_observation_windows(window_id),
 policy_version TEXT NOT NULL REFERENCES forward_qualification_policies(policy_version),
 created_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_forward_report_owner_source
 ON forward_qualification_reports(owner_id,source_id,created_at,report_id);
CREATE TABLE IF NOT EXISTS forward_qualification_requests(
 owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL,
 report_id TEXT NOT NULL REFERENCES forward_qualification_reports(report_id),
 PRIMARY KEY(owner_id,idempotency_key));
CREATE TABLE IF NOT EXISTS forward_qualification_reviews(
 review_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 report_id TEXT NOT NULL REFERENCES forward_qualification_reports(report_id),
 reviewer_id TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
 created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
 UNIQUE(owner_id,idempotency_key));
CREATE INDEX IF NOT EXISTS ix_forward_review_owner_report
 ON forward_qualification_reviews(owner_id,report_id,created_at,review_id);
CREATE TABLE IF NOT EXISTS forward_qualification_events(
 event_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, report_id TEXT NOT NULL
 REFERENCES forward_qualification_reports(report_id), action TEXT NOT NULL,
 created_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_forward_event_owner_report
 ON forward_qualification_events(owner_id,report_id,created_at,event_id);
CREATE TRIGGER IF NOT EXISTS forward_policy_no_update BEFORE UPDATE ON forward_qualification_policies
 BEGIN SELECT RAISE(ABORT,'immutable forward policy'); END;
CREATE TRIGGER IF NOT EXISTS forward_policy_no_delete BEFORE DELETE ON forward_qualification_policies
 BEGIN SELECT RAISE(ABORT,'immutable forward policy'); END;
CREATE TRIGGER IF NOT EXISTS forward_window_no_update BEFORE UPDATE ON forward_observation_windows
 BEGIN SELECT RAISE(ABORT,'immutable forward window'); END;
CREATE TRIGGER IF NOT EXISTS forward_window_no_delete BEFORE DELETE ON forward_observation_windows
 BEGIN SELECT RAISE(ABORT,'immutable forward window'); END;
CREATE TRIGGER IF NOT EXISTS forward_report_no_update BEFORE UPDATE ON forward_qualification_reports
 BEGIN SELECT RAISE(ABORT,'immutable forward report'); END;
CREATE TRIGGER IF NOT EXISTS forward_report_no_delete BEFORE DELETE ON forward_qualification_reports
 BEGIN SELECT RAISE(ABORT,'immutable forward report'); END;
CREATE TRIGGER IF NOT EXISTS forward_request_no_update BEFORE UPDATE ON forward_qualification_requests
 BEGIN SELECT RAISE(ABORT,'immutable forward request'); END;
CREATE TRIGGER IF NOT EXISTS forward_request_no_delete BEFORE DELETE ON forward_qualification_requests
 BEGIN SELECT RAISE(ABORT,'immutable forward request'); END;
CREATE TRIGGER IF NOT EXISTS forward_review_no_update BEFORE UPDATE ON forward_qualification_reviews
 BEGIN SELECT RAISE(ABORT,'append-only forward review'); END;
CREATE TRIGGER IF NOT EXISTS forward_review_no_delete BEFORE DELETE ON forward_qualification_reviews
 BEGIN SELECT RAISE(ABORT,'append-only forward review'); END;
CREATE TRIGGER IF NOT EXISTS forward_event_no_update BEFORE UPDATE ON forward_qualification_events
 BEGIN SELECT RAISE(ABORT,'append-only forward audit'); END;
CREATE TRIGGER IF NOT EXISTS forward_event_no_delete BEFORE DELETE ON forward_qualification_events
 BEGIN SELECT RAISE(ABORT,'append-only forward audit'); END;
