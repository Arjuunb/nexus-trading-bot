"""One structured shape for every Trading Instance lifecycle event.

Instance logs were free-form strings scattered across the manager, the engine
and the hub, so answering "what happened to the BTCUSDT worker at 04:12?"
meant grepping several different formats and hoping the instance id was in the
line at all. Every event emitted through here carries the same key set --
instance_id, symbol, strategy_id, exchange, market_type, timeframe, event,
status, timestamp -- so instance logs can be filtered and correlated.

Emission is best-effort by construction. Telemetry must never be the reason a
worker stops, so a failing store is swallowed here and nowhere else.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

#: Lifecycle events with a defined meaning. Emitting anything else is allowed
#: (the set is not enforced) but these are the ones an operator can rely on.
EVENTS = (
    "INSTANCE_CREATED", "INSTANCE_STARTING", "INSTANCE_RESTORED",
    "MARKET_CONNECTING", "MARKET_CONNECTED", "MARKET_STALE",
    "MARKET_DISCONNECTED", "MARKET_RECONNECTED",
    "SUBSCRIPTION_CREATED", "SUBSCRIPTION_REUSED",
    "WARMUP_STARTED", "WARMUP_COMPLETE",
    "STRATEGY_READY", "STRATEGY_BLOCKED",
    "SIGNAL_GENERATED", "ORDER_CREATED", "ORDER_REJECTED",
    "INSTANCE_PAUSED", "INSTANCE_STOPPED", "INSTANCE_ERROR",
    "SUPERVISOR_ERROR",
)

_ERROR_STATUSES = {"error", "failed", "blocked"}
_WARNING_STATUSES = {"stale", "disconnected", "retrying", "repairing", "degraded"}


def event_payload(instance, event: str, *, status: str = "", detail: str = "",
                  **extra) -> dict:
    """The canonical key set for one instance event."""
    payload = {
        "event": str(event),
        "status": str(status or ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instance_id": getattr(instance, "id", None),
        "symbol": getattr(instance, "symbol", None),
        "strategy_id": getattr(instance, "strategy_key", None),
        "strategy_version": getattr(instance, "strategy_version", None),
        "exchange": getattr(instance, "exchange", None),
        "market_type": getattr(instance, "instrument_type", None),
        "timeframe": getattr(instance, "timeframe", None),
    }
    if detail:
        payload["detail"] = str(detail)[:500]
    payload.update(extra)
    return payload


def format_event(payload: dict) -> str:
    """One line, stable key order, machine-parseable as JSON after the tag."""
    return "instance_event " + json.dumps(payload, sort_keys=True, default=str)


def log_event(manager, instance, event: str, *, status: str = "",
              detail: str = "", **extra) -> dict:
    """Persist one lifecycle event to the instance engine log. Never raises."""
    payload = event_payload(instance, event, status=status, detail=detail, **extra)
    level = ("error" if status in _ERROR_STATUSES else
             "warning" if status in _WARNING_STATUSES else "info")
    instance_id = payload.get("instance_id")
    store = getattr(manager, "store", None)
    if store is not None and instance_id:
        try:
            store.append_engine_log(instance_id, level=level,
                                    message=format_event(payload))
        except Exception:  # observability must not stop execution
            pass
    return payload
