"""Immutable metadata outbox inside the existing paper accounting transaction.

No financial transaction is replayed by this module. Its rows preserve exact
committed IDs, producer context and execution receipts for journal recovery.
The SQLite migration is additive and leaves old executions without invented
outbox history. Downgrade by stopping evidence consumers/producers and retaining
this table. A primary ledger backup must include it with paper_executions.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import sqlite3


SCHEMA_VERSION = 1
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_evidence_outbox (
  execution_id TEXT PRIMARY KEY, action TEXT NOT NULL CHECK(action IN ('OPEN','REDUCE','CLOSE')),
  instance_id TEXT NOT NULL DEFAULT '', simulation_session_id TEXT NOT NULL DEFAULT '',
  trade_id TEXT NOT NULL, position_id TEXT NOT NULL,
  parent_trade_id TEXT, parent_position_id TEXT,
  remainder_trade_id TEXT, remainder_position_id TEXT,
  observed_at TEXT, context_json TEXT, receipt_json TEXT NOT NULL,
  created_at TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS idx_paper_evidence_outbox_scope
  ON paper_evidence_outbox(instance_id,simulation_session_id,created_at,execution_id);
CREATE TRIGGER IF NOT EXISTS immutable_paper_evidence_outbox_update
  BEFORE UPDATE ON paper_evidence_outbox
  BEGIN SELECT RAISE(ABORT, 'immutable paper evidence outbox'); END;
CREATE TRIGGER IF NOT EXISTS immutable_paper_evidence_outbox_delete
  BEFORE DELETE ON paper_evidence_outbox
  BEGIN SELECT RAISE(ABORT, 'immutable paper evidence outbox'); END;
"""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      default=lambda item: str(item) if isinstance(item, Decimal) else _unsupported(item))


def _unsupported(item):
    raise TypeError(f"unsupported metadata type: {type(item).__name__}")


def append_outbox(connection: sqlite3.Connection, *, execution_id: str, action: str,
                  instance_id: str, simulation_session_id: str,
                  trade_id: str, position_id: str, created_at: str,
                  receipt: dict, evidence: dict | None = None,
                  parent_trade_id: str | None = None, parent_position_id: str | None = None,
                  remainder_trade_id: str | None = None, remainder_position_id: str | None = None):
    """Append inside the caller's accounting transaction; never commit here."""
    evidence = evidence or {}
    captured_receipt = {**(evidence.get("receipt") or {}), **receipt}
    # The producer's gross receipt can carry more precise pre-subtraction facts.
    if (evidence.get("receipt") or {}).get("gross_pnl") is not None:
        captured_receipt["gross_pnl"] = evidence["receipt"]["gross_pnl"]
    connection.execute("""INSERT INTO paper_evidence_outbox
      (execution_id,action,instance_id,simulation_session_id,trade_id,position_id,
       parent_trade_id,parent_position_id,remainder_trade_id,remainder_position_id,
       observed_at,context_json,receipt_json,created_at,schema_version)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        execution_id, action, instance_id or "", simulation_session_id or "", trade_id, position_id,
        parent_trade_id, parent_position_id, remainder_trade_id, remainder_position_id,
        evidence.get("observed_at"),
        canonical_json(evidence["context"]) if evidence.get("context") is not None else None,
        canonical_json(captured_receipt), created_at, SCHEMA_VERSION))


def decode_row(row) -> dict:
    row = dict(row)
    context = row.pop("context_json", None)
    receipt = row.pop("receipt_json", "{}")
    row["context"] = json.loads(context) if isinstance(context, str) else context
    row["receipt"] = json.loads(receipt) if isinstance(receipt, str) else receipt
    return row


def watermark(snapshot: dict) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
