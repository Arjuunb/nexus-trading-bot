"""Atomic, material-only PA/SMC evaluation evidence for the read-only Guardian.

SQLite triggers run in the evaluation statement's transaction.  A failed
outbox insert therefore cannot leave a changed evaluation without its matching
evidence.  No Guardian process, network call, or broker call is made here.
"""
from __future__ import annotations

import sqlite3


def install_lab_lifecycle_outbox(connection: sqlite3.Connection, lab: str) -> None:
    if lab not in {"pa", "smc"}:
        raise ValueError("unknown lab outbox")
    source = f"{lab}_evaluations"
    outbox = f"{lab}_guardian_lifecycle"
    model = "NEW.model_id" if lab == "smc" else "NULL"
    condition_path = ("$.source_evaluation.ordered_condition_results" if lab == "smc"
                      else "$.trace.conditions")
    condition_projection = f"""(
        SELECT json_group_array(json_object(
            'key', substr(coalesce(json_extract(value, '$.key'), ''), 1, 80),
            'status', substr(coalesce(json_extract(value, '$.status'), 'UNKNOWN'), 1, 32)))
        FROM json_each(coalesce(json_extract(NEW.payload_json, '{condition_path}'), '[]'))
    )"""
    latest = "$.lifecycle[#-1]"
    order = f"json_extract(NEW.payload_json, '{latest}.order_id')"
    broker_order = f"json_extract(NEW.payload_json, '{latest}.broker_event_order_id')"
    fill = f"json_extract(NEW.payload_json, '{latest}.fill')"
    material_changed = " OR ".join((
        "NEW.state IS NOT OLD.state",
        "NEW.reason IS NOT OLD.reason",
        "NEW.missing_conditions_json IS NOT OLD.missing_conditions_json",
        f"{order} IS NOT json_extract(OLD.payload_json, '{latest}.order_id')",
        f"{broker_order} IS NOT json_extract(OLD.payload_json, '{latest}.broker_event_order_id')",
        f"{fill} IS NOT json_extract(OLD.payload_json, '{latest}.fill')",
    ))
    insert = f"""INSERT INTO {outbox}(
        source_event_id,correlation_id,session_id,idempotency_key,candle_time,
        symbol,timeframe,strategy_id,strategy_version,model_id,state,reason,
        missing_conditions_json,conditions_json,order_id,broker_event_order_id,
        fill_json,event_time)
      VALUES (lower(hex(randomblob(16))),NEW.correlation_id,NEW.session_id,
        NEW.idempotency_key,NEW.candle_time,NEW.symbol,NEW.timeframe,
        NEW.strategy_id,NEW.strategy_version,{model},NEW.state,
        substr(NEW.reason,1,500),NEW.missing_conditions_json,
        {condition_projection},{order},{broker_order},{fill},NEW.updated_at);"""
    connection.executescript(f"""
      CREATE TABLE IF NOT EXISTS {outbox}(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        source_event_id TEXT NOT NULL UNIQUE,
        correlation_id TEXT NOT NULL, session_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, candle_time TEXT NOT NULL,
        symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
        strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
        model_id TEXT, state TEXT NOT NULL, reason TEXT NOT NULL,
        missing_conditions_json TEXT NOT NULL, conditions_json TEXT NOT NULL,
        order_id TEXT, broker_event_order_id TEXT, fill_json TEXT,
        event_time TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS {outbox}_correlation
        ON {outbox}(correlation_id,sequence);
      CREATE TRIGGER IF NOT EXISTS {outbox}_immutable_update
        BEFORE UPDATE ON {outbox} BEGIN SELECT RAISE(ABORT,'guardian lifecycle is immutable'); END;
      CREATE TRIGGER IF NOT EXISTS {outbox}_immutable_delete
        BEFORE DELETE ON {outbox} BEGIN SELECT RAISE(ABORT,'guardian lifecycle is immutable'); END;
      CREATE TRIGGER IF NOT EXISTS {source}_guardian_insert
        AFTER INSERT ON {source} BEGIN {insert} END;
      CREATE TRIGGER IF NOT EXISTS {source}_guardian_update
        AFTER UPDATE ON {source} WHEN {material_changed} BEGIN {insert} END;
    """)
