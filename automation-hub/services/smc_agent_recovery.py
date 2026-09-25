"""Recover execution ownership from the durable request; never mutate broker state."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from services.smc_strategy_lab import STRATEGY_VERSION


def recover_order_metadata(account, journal, execution_key: str, evidence: dict) -> None:
    order = evidence["order"]
    intent = journal.execution_intent(execution_key)
    request = journal.execution_order_request(execution_key)
    with account._lock:
        existing = account._db.execute(
            "SELECT * FROM smc_order_meta WHERE order_id=?", (order["id"],)).fetchone()
        if not request:
            # Older safety builds did not snapshot the prepared request.
            # They can recover only if the original ownership row survived.
            if not existing:
                raise RuntimeError("broker order exists but durable lab ownership evidence is missing")
            return
        session = request["session"]
        args = request["arguments"]
        if intent["session_id"] != session["id"] or order["candle_id"] != execution_key:
            raise ValueError("execution/session identity mismatch")
        if existing and (existing["session_id"] != session["id"]
                         or existing["idempotency_key"] != execution_key):
            raise ValueError("existing broker ownership conflicts with intent")
        now = datetime.now(timezone.utc).isoformat()
        entry = float(order["requested_price"])
        stop, t1, t2 = args["stop_loss"], args["target_1"], args["target_2"]
        risk = abs(entry - float(stop))
        config = {"reference_price": entry, "stop_loss": stop,
                  "target_1": t1, "target_2": t2,
                  "target_1_r": abs(float(t1) - entry) / risk,
                  "target_2_r": abs(float(t2) - entry) / risk,
                  "rules": args["rules"], "correlation_id": args.get("correlation_id"),
                  "idempotency_key": execution_key, "execution_mode": "PAPER",
                  "live_execution_allowed": False}
        status = {"open": "ORDER_PENDING", "triggered": "ORDER_PENDING",
                  "partially_filled": "PARTIALLY_FILLED", "filled": "ENTERED",
                  "cancelled": "CANCELLED", "rejected": "REJECTED"}.get(order["status"])
        if status is None:
            raise ValueError("unknown broker order state")
        account._db.execute("BEGIN IMMEDIATE")
        try:
            if not existing:
                account._db.execute(
                    "INSERT INTO smc_order_meta(order_id,session_id,ownership,idempotency_key,"
                    "proposal_id,setup_id,poi_id,model_id,model_version,direction,entry,stop,"
                    "target_1,target_2,risk_pct,creation_candle,expiry_candle,status,reason,"
                    "config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (order["id"], session["id"], args["ownership"], execution_key,
                     args.get("proposal_id"), args.get("setup_id"), args.get("poi_id"),
                     args.get("model_id"), STRATEGY_VERSION if args.get("model_id") else None,
                     "bullish" if order["side"] == "buy" else "bearish", entry, stop, t1, t2,
                     args.get("risk_pct"), args.get("creation_candle"), args.get("expiry_candle"),
                     status, "recovered committed paper execution",
                     json.dumps(config, sort_keys=True), order["created_at"], now))
            account._db.execute(
                "UPDATE smc_candidates SET status='ORDER_CREATED',reason=?,updated_at=? "
                "WHERE session_id=? AND proposal_id=? AND status='PENDING_APPROVAL'",
                ("reconciled committed paper order", now, session["id"], args.get("proposal_id")))
            account._db.commit()
        except BaseException:
            account._db.rollback()
            raise
