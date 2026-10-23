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
CREATE TRIGGER IF NOT EXISTS operational_source_no_update BEFORE UPDATE ON operational_sources
 BEGIN SELECT RAISE(ABORT,'immutable operational source'); END;
CREATE TRIGGER IF NOT EXISTS operational_source_no_delete BEFORE DELETE ON operational_sources
 BEGIN SELECT RAISE(ABORT,'immutable operational source'); END;
CREATE TRIGGER IF NOT EXISTS operational_event_no_update BEFORE UPDATE ON operational_events
 BEGIN SELECT RAISE(ABORT,'immutable operational evidence'); END;
CREATE TRIGGER IF NOT EXISTS operational_event_no_delete BEFORE DELETE ON operational_events
 BEGIN SELECT RAISE(ABORT,'immutable operational evidence'); END;
