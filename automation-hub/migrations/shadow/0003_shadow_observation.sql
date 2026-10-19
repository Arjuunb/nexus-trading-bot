-- SQLite ONLY, applied explicitly to the isolated shadow database, never PG.
-- Sprint 7 decision-only namespace in the existing physically separate shadow DB.
CREATE TABLE IF NOT EXISTS shadow_registrations(
 registration_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL, frozen_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('DRAFT','READY','OBSERVING','PAUSED','BLOCKED','STOPPED','FAILED')),
 readiness TEXT NOT NULL, desired_observing INTEGER NOT NULL DEFAULT 0,
 observation_start TEXT, resume_after TEXT, last_decision_time TEXT,
 heartbeat REAL, lease_token TEXT, lease_owner TEXT, lease_expiry REAL,
 generation INTEGER NOT NULL DEFAULT 0, runtime_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, UNIQUE(owner_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_shadow_registration_owner ON shadow_registrations(owner_id,created_at);
CREATE INDEX IF NOT EXISTS ix_shadow_registration_recovery ON shadow_registrations(desired_observing,lease_expiry);
CREATE TABLE IF NOT EXISTS shadow_observations(
 decision_id TEXT PRIMARY KEY, registration_id TEXT NOT NULL,
 owner_id TEXT NOT NULL, candle_close TEXT NOT NULL, candle_hash TEXT,
 context_hash TEXT, status TEXT NOT NULL, payload_json TEXT NOT NULL,
 UNIQUE(registration_id,candle_close),
 FOREIGN KEY(registration_id) REFERENCES shadow_registrations(registration_id)
);
CREATE INDEX IF NOT EXISTS ix_shadow_observation_scope ON shadow_observations(owner_id,registration_id,candle_close);
CREATE TABLE IF NOT EXISTS shadow_observation_events(
 event_id TEXT PRIMARY KEY, registration_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 action TEXT NOT NULL, timestamp TEXT NOT NULL, payload_json TEXT NOT NULL,
 FOREIGN KEY(registration_id) REFERENCES shadow_registrations(registration_id)
);
CREATE INDEX IF NOT EXISTS ix_shadow_observation_event_scope ON shadow_observation_events(owner_id,registration_id,timestamp);
CREATE TRIGGER IF NOT EXISTS shadow_registration_frozen BEFORE UPDATE OF frozen_json,request_json,owner_id,idempotency_key,registration_id,created_at ON shadow_registrations
 BEGIN SELECT RAISE(ABORT,'immutable shadow registration'); END;
CREATE TRIGGER IF NOT EXISTS shadow_observations_no_update BEFORE UPDATE ON shadow_observations
 BEGIN SELECT RAISE(ABORT,'immutable shadow decision'); END;
CREATE TRIGGER IF NOT EXISTS shadow_observations_no_delete BEFORE DELETE ON shadow_observations
 BEGIN SELECT RAISE(ABORT,'immutable shadow decision'); END;
CREATE TRIGGER IF NOT EXISTS shadow_observation_events_no_update BEFORE UPDATE ON shadow_observation_events
 BEGIN SELECT RAISE(ABORT,'append-only shadow audit'); END;
CREATE TRIGGER IF NOT EXISTS shadow_observation_events_no_delete BEFORE DELETE ON shadow_observation_events
 BEGIN SELECT RAISE(ABORT,'append-only shadow audit'); END;
