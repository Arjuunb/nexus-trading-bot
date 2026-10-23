-- SQLite journal RESEARCH metadata only. No financial rows are rewritten.
CREATE TABLE IF NOT EXISTS retained_research_datasets(
 snapshot_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
 candles_json TEXT NOT NULL, payload_bytes INTEGER NOT NULL, candle_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS retained_research_dataset_refs(
 job_id TEXT PRIMARY KEY REFERENCES research_jobs(job_id), owner_id TEXT NOT NULL,
 snapshot_id TEXT NOT NULL REFERENCES retained_research_datasets(snapshot_id),
 manifest_json TEXT NOT NULL, retained_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_retained_dataset_owner ON retained_research_dataset_refs(owner_id,job_id);
CREATE TRIGGER IF NOT EXISTS retained_datasets_no_update BEFORE UPDATE ON retained_research_datasets
 BEGIN SELECT RAISE(ABORT,'immutable retained dataset'); END;
CREATE TRIGGER IF NOT EXISTS retained_datasets_no_delete BEFORE DELETE ON retained_research_datasets
 BEGIN SELECT RAISE(ABORT,'immutable retained dataset'); END;
CREATE TRIGGER IF NOT EXISTS retained_dataset_refs_no_update BEFORE UPDATE ON retained_research_dataset_refs
 BEGIN SELECT RAISE(ABORT,'immutable dataset reference'); END;
CREATE TRIGGER IF NOT EXISTS retained_dataset_refs_no_delete BEFORE DELETE ON retained_research_dataset_refs
 BEGIN SELECT RAISE(ABORT,'immutable dataset reference'); END;
