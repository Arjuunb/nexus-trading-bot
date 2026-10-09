"""Source-local, immutable evidence of instance strategy and gate decisions.

The triggers are part of the decision statement's SQLite transaction. They
retain material states after the mutable decision row is pruned, without a
Guardian service dependency or any change to execution authority.
"""
from __future__ import annotations

import sqlite3


def install_decision_outbox(connection: sqlite3.Connection) -> None:
    def rules(prefix: str, column: str, status: str) -> str:
        return f"""(
        SELECT json_group_array(json_object(
            'key', substr(CASE WHEN type='object' THEN
                coalesce(json_extract(value, '$.key'), '') ELSE coalesce(value, '') END,1,100),
            'status','{status}'))
        FROM json_each(coalesce({prefix}.{column},'[]'))
        WHERE type IN ('text','object')
    )"""
    insert = f"""INSERT INTO guardian_decision_lifecycle(
        source_event_id,decision_id,decision_identity,instance_id,decision_time,event_time,
        symbol,timeframe,strategy,side,decision,final_state,gate_stage,
        blocker,reason,executed,passed_rules_json,failed_rules_json)
      VALUES(lower(hex(randomblob(16))),NEW.id,NEW.decision_identity,NEW.instance_id,
        NEW.ts,strftime('%Y-%m-%dT%H:%M:%fZ','now'),NEW.symbol,NEW.timeframe,
        NEW.strategy,NEW.side,NEW.decision,
        NEW.final_state,NEW.gate_stage,substr(NEW.blocker,1,500),
        substr(coalesce(NEW.reason,''),1,500),NEW.executed,
        {rules('NEW','passed_json','PASS')},
        {rules('NEW','failed_json','MISSING')});"""
    connection.executescript(f"""
      CREATE TABLE IF NOT EXISTS guardian_decision_lifecycle(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        source_event_id TEXT NOT NULL UNIQUE,
        decision_id INTEGER NOT NULL, decision_identity TEXT NOT NULL,
        instance_id TEXT NOT NULL, decision_time TEXT NOT NULL,
        event_time TEXT NOT NULL,
        symbol TEXT NOT NULL, timeframe TEXT, strategy TEXT, side TEXT,
        decision TEXT NOT NULL, final_state TEXT NOT NULL,
        gate_stage TEXT NOT NULL, blocker TEXT NOT NULL, reason TEXT NOT NULL,
        executed INTEGER NOT NULL, passed_rules_json TEXT NOT NULL,
        failed_rules_json TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS guardian_decision_lifecycle_decision
        ON guardian_decision_lifecycle(decision_id,sequence);
      CREATE TRIGGER IF NOT EXISTS guardian_decision_lifecycle_no_update
        BEFORE UPDATE ON guardian_decision_lifecycle
        BEGIN SELECT RAISE(ABORT,'decision evidence is immutable'); END;
      CREATE TRIGGER IF NOT EXISTS guardian_decision_lifecycle_no_delete
        BEFORE DELETE ON guardian_decision_lifecycle
        BEGIN SELECT RAISE(ABORT,'decision evidence is immutable'); END;
      CREATE TRIGGER IF NOT EXISTS decisions_guardian_insert
        AFTER INSERT ON decisions WHEN NEW.instance_id <> ''
        BEGIN {insert} END;
      CREATE TRIGGER IF NOT EXISTS decisions_guardian_update
        AFTER UPDATE ON decisions WHEN NEW.instance_id <> '' AND (
          NEW.instance_id IS NOT OLD.instance_id OR
          NEW.decision IS NOT OLD.decision OR
          NEW.final_state IS NOT OLD.final_state OR
          NEW.gate_stage IS NOT OLD.gate_stage OR
          NEW.blocker IS NOT OLD.blocker OR
          NEW.reason IS NOT OLD.reason OR
          NEW.executed IS NOT OLD.executed OR
          {rules('NEW','passed_json','PASS')} IS NOT
          {rules('OLD','passed_json','PASS')} OR
          {rules('NEW','failed_json','MISSING')} IS NOT
          {rules('OLD','failed_json','MISSING')})
        BEGIN {insert} END;
    """)
