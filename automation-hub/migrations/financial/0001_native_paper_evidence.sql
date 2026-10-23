-- Additive ORIGINAL paper transaction evidence. Never a second balance authority.
CREATE TABLE IF NOT EXISTS v2_source_evidence(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL, tenant_id TEXT,
 event_type TEXT NOT NULL, event_timestamp TEXT NOT NULL,
 source_transaction_id TEXT NOT NULL, evidence_hash TEXT NOT NULL,
 payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_v2_source_evidence_account_clock
 ON v2_source_evidence(account_id,event_timestamp,sequence);
CREATE INDEX IF NOT EXISTS ix_v2_source_evidence_owner_kind
 ON v2_source_evidence(tenant_id,event_type,sequence);
CREATE TABLE IF NOT EXISTS v2_episode_origins(
 episode_id TEXT PRIMARY KEY,entry_event_id TEXT NOT NULL,
 context_json TEXT NOT NULL,opened_at TEXT NOT NULL,
 FOREIGN KEY(entry_event_id) REFERENCES v2_source_evidence(event_id));
CREATE TRIGGER IF NOT EXISTS v2_episode_origins_no_update BEFORE UPDATE ON v2_episode_origins
 BEGIN SELECT RAISE(ABORT,'immutable original paper episode'); END;
CREATE TRIGGER IF NOT EXISTS v2_episode_origins_no_delete BEFORE DELETE ON v2_episode_origins
 BEGIN SELECT RAISE(ABORT,'immutable original paper episode'); END;
CREATE TRIGGER IF NOT EXISTS v2_source_evidence_immutable_update
 BEFORE UPDATE ON v2_source_evidence BEGIN SELECT RAISE(ABORT,'immutable native paper evidence'); END;
CREATE TRIGGER IF NOT EXISTS v2_source_evidence_immutable_delete
 BEFORE DELETE ON v2_source_evidence BEGIN SELECT RAISE(ABORT,'immutable native paper evidence'); END;
