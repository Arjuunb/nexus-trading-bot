SET search_path TO nexus_workers,pg_catalog;
CREATE TABLE IF NOT EXISTS local_readiness_assessments(
 assessment_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,created_at TEXT NOT NULL,
 evidence_hash TEXT NOT NULL,payload_bytes INTEGER NOT NULL,payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_local_readiness_owner ON local_readiness_assessments(owner_id,created_at,assessment_id);
CREATE TABLE IF NOT EXISTS operational_retention_segments(
 segment_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,source_id TEXT NOT NULL,
 period_start TEXT NOT NULL,period_end TEXT NOT NULL,event_count INTEGER NOT NULL,
 content_hash TEXT NOT NULL,created_at TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_operational_retention_owner ON operational_retention_segments(owner_id,source_id,period_start);
CREATE TABLE IF NOT EXISTS paper_evidence_reviews(
 review_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,report_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL,created_at TEXT NOT NULL,payload_json TEXT NOT NULL,
 UNIQUE(owner_id,idempotency_key),FOREIGN KEY(report_id) REFERENCES paper_reconciliation_reports(report_id));
CREATE INDEX IF NOT EXISTS ix_paper_evidence_review_owner ON paper_evidence_reviews(owner_id,report_id,created_at);
DROP TRIGGER IF EXISTS immutable_worker_evidence ON paper_evidence_reviews;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON paper_evidence_reviews
 FOR EACH ROW EXECUTE FUNCTION immutable_metadata();
DROP TRIGGER IF EXISTS immutable_worker_evidence ON local_readiness_assessments;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON local_readiness_assessments
 FOR EACH ROW EXECUTE FUNCTION immutable_metadata();
DROP TRIGGER IF EXISTS immutable_worker_evidence ON operational_retention_segments;
CREATE TRIGGER immutable_worker_evidence BEFORE UPDATE OR DELETE ON operational_retention_segments
 FOR EACH ROW EXECUTE FUNCTION immutable_metadata();
