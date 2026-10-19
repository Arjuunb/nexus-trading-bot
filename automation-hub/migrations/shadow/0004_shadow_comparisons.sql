-- SQLite only; isolated decision/research DB, no financial schema changes.
CREATE TABLE IF NOT EXISTS shadow_comparison_artifacts(
 comparison_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
 registration_id TEXT NOT NULL REFERENCES shadow_registrations(registration_id),
 active_source_id TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 calculation_version TEXT NOT NULL, input_watermark TEXT NOT NULL,
 created_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_shadow_comparison_owner ON shadow_comparison_artifacts(owner_id,created_at,comparison_id);
CREATE TABLE IF NOT EXISTS shadow_comparison_requests(
 owner_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL,
 comparison_id TEXT NOT NULL REFERENCES shadow_comparison_artifacts(comparison_id),
 PRIMARY KEY(owner_id,idempotency_key)
);
CREATE TRIGGER IF NOT EXISTS shadow_comparisons_no_update BEFORE UPDATE ON shadow_comparison_artifacts
 BEGIN SELECT RAISE(ABORT,'immutable shadow comparison'); END;
CREATE TRIGGER IF NOT EXISTS shadow_comparisons_no_delete BEFORE DELETE ON shadow_comparison_artifacts
 BEGIN SELECT RAISE(ABORT,'immutable shadow comparison'); END;
CREATE TRIGGER IF NOT EXISTS shadow_comparison_requests_no_update BEFORE UPDATE ON shadow_comparison_requests
 BEGIN SELECT RAISE(ABORT,'immutable comparison request'); END;
CREATE TRIGGER IF NOT EXISTS shadow_comparison_requests_no_delete BEFORE DELETE ON shadow_comparison_requests
 BEGIN SELECT RAISE(ABORT,'immutable comparison request'); END;
