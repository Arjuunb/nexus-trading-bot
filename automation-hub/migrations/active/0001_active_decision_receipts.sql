-- SQLite only, source-owned evidence namespace; never financial rows.
CREATE TABLE IF NOT EXISTS active_decision_receipts(
 decision_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, tenant_id TEXT,
 source_kind TEXT NOT NULL, candle_time TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 persisted_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS active_receipt_tenant_session_candle
 ON active_decision_receipts(tenant_id,session_id,candle_time);
CREATE TABLE IF NOT EXISTS active_evidence_recovery(
 id INTEGER PRIMARY KEY CHECK(id=1), last_success_at TEXT, status TEXT NOT NULL,
 backlog INTEGER);
CREATE TABLE IF NOT EXISTS active_evidence_supersessions(
 event_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
 tenant_id TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
 replacement_reference TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS active_supersession_decision ON active_evidence_supersessions(decision_id);
CREATE TRIGGER IF NOT EXISTS active_receipt_no_update BEFORE UPDATE ON active_decision_receipts
 BEGIN SELECT RAISE(ABORT,'active receipt immutable'); END;
CREATE TRIGGER IF NOT EXISTS active_receipt_no_delete BEFORE DELETE ON active_decision_receipts
 BEGIN SELECT RAISE(ABORT,'active receipt immutable'); END;
CREATE TRIGGER IF NOT EXISTS active_supersession_no_update BEFORE UPDATE ON active_evidence_supersessions
 BEGIN SELECT RAISE(ABORT,'active supersession immutable'); END;
CREATE TRIGGER IF NOT EXISTS active_supersession_no_delete BEFORE DELETE ON active_evidence_supersessions
 BEGIN SELECT RAISE(ABORT,'active supersession immutable'); END;
-- Legacy order lifecycle may continue changing its original fields. Capture
-- cannot be added retrospectively or rewritten. Global reset semantics stay
-- unchanged; materialized receipts survive source-row reset without FK cascade.
CREATE TRIGGER IF NOT EXISTS __PREFIX___capture_immutable BEFORE UPDATE ON __PREFIX___evaluations
 WHEN (CASE WHEN json_valid(OLD.payload_json) THEN json_extract(OLD.payload_json,'$.active_evidence') END)
      IS NOT (CASE WHEN json_valid(NEW.payload_json) THEN json_extract(NEW.payload_json,'$.active_evidence') END)
   OR (json_valid(OLD.payload_json) AND json_extract(OLD.payload_json,'$.active_evidence') IS NOT NULL
       AND (OLD.correlation_id IS NOT NEW.correlation_id OR OLD.session_id IS NOT NEW.session_id
        OR OLD.candle_time IS NOT NEW.candle_time OR OLD.symbol IS NOT NEW.symbol
        OR OLD.timeframe IS NOT NEW.timeframe OR OLD.strategy_id IS NOT NEW.strategy_id
        OR OLD.strategy_version IS NOT NEW.strategy_version OR OLD.created_at IS NOT NEW.created_at))
 BEGIN SELECT RAISE(ABORT,'active capture immutable'); END;
