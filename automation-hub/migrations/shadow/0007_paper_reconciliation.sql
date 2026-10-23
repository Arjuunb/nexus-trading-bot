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
CREATE TRIGGER IF NOT EXISTS paper_reconciliation_policy_no_update BEFORE UPDATE ON paper_reconciliation_policies
 BEGIN SELECT RAISE(ABORT,'immutable reconciliation policy'); END;
CREATE TRIGGER IF NOT EXISTS paper_reconciliation_policy_no_delete BEFORE DELETE ON paper_reconciliation_policies
 BEGIN SELECT RAISE(ABORT,'immutable reconciliation policy'); END;
CREATE TRIGGER IF NOT EXISTS paper_reconciliation_report_no_update BEFORE UPDATE ON paper_reconciliation_reports
 BEGIN SELECT RAISE(ABORT,'immutable reconciliation report'); END;
CREATE TRIGGER IF NOT EXISTS paper_reconciliation_report_no_delete BEFORE DELETE ON paper_reconciliation_reports
 BEGIN SELECT RAISE(ABORT,'immutable reconciliation report'); END;
CREATE TRIGGER IF NOT EXISTS paper_reconciliation_request_no_update BEFORE UPDATE ON paper_reconciliation_requests
 BEGIN SELECT RAISE(ABORT,'immutable reconciliation binding'); END;
CREATE TRIGGER IF NOT EXISTS paper_reconciliation_request_no_delete BEFORE DELETE ON paper_reconciliation_requests
 BEGIN SELECT RAISE(ABORT,'immutable reconciliation binding'); END;
