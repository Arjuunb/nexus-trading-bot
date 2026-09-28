"""Guardian: the platform's independent observer (PRD "Nexus Guardian", Phase 1).

Trading code talks to Guardian through exactly one function, :func:`emit`.
It builds an event and queues it on the installed bus; with no bus installed
it does nothing. It never blocks, never raises and returns only whether the
event was queued -- nothing in trading may depend on Guardian (PRD §42).
"""
from __future__ import annotations

from typing import Any, Optional

from services.guardian.schema import InvalidEvent, make_event

_bus = None


def install(bus) -> None:
    global _bus
    _bus = bus


def uninstall() -> None:
    install(None)


def installed():
    return _bus


def emit(event_type: str, **fields: Any) -> bool:
    bus = _bus
    if bus is None:
        return False
    try:
        event = make_event(event_type, **fields)
    except InvalidEvent:
        bus.reject()
        return False
    except Exception:  # noqa: BLE001 -- observability must never reach the caller
        return False
    return bus.publish(event)


# Instance lifecycle events (services/instance_telemetry.py) in Guardian's
# vocabulary. Anything not listed is kept as ``instance_lifecycle`` with its
# original name, never dropped and never renamed into something it is not.
_LIFECYCLE = {
    "MARKET_CONNECTED": ("websocket_connected", "INFO"),
    "MARKET_RECONNECTED": ("websocket_reconnected", "INFO"),
    "MARKET_DISCONNECTED": ("websocket_disconnected", "WARNING"),
    "MARKET_STALE": ("stale_candle", "WARNING"),
    "INSTANCE_STARTING": ("worker_started", "INFO"),
    "INSTANCE_RESTORED": ("worker_started", "INFO"),
    "INSTANCE_STOPPED": ("worker_stopped", "INFO"),
    "INSTANCE_ERROR": ("worker_crashed", "HIGH"),
    "SUPERVISOR_ERROR": ("worker_crashed", "HIGH"),
    "SIGNAL_GENERATED": ("signal_generated", "INFO"),
    "ORDER_CREATED": ("intent_created", "INFO"),
    "ORDER_REJECTED": ("order_rejected", "WARNING"),
}
_STATUS_SEVERITY = {"error": "HIGH", "failed": "HIGH", "blocked": "WARNING", "stale": "WARNING",
                    "disconnected": "WARNING", "retrying": "WATCH", "repairing": "WATCH",
                    "degraded": "WATCH"}


def observe_instance_event(payload: dict, *, lab_id: Optional[str] = None) -> bool:
    """Forward one instance lifecycle event to Guardian. Never raises."""
    try:
        name = str(payload.get("event") or "")
        event_type, severity = _LIFECYCLE.get(name, ("instance_lifecycle", None))
        severity = severity or _STATUS_SEVERITY.get(str(payload.get("status") or "").lower(), "INFO")
        extra = {k: v for k, v in payload.items() if k not in (
            "event", "status", "timestamp", "instance_id", "symbol", "strategy_id",
            "strategy_version", "timeframe", "detail")}
        return emit(event_type, source_service="trading_instances",
                    source_component=f"instance:{payload.get('instance_id')}",
                    severity=severity, timestamp=payload.get("timestamp"),
                    instance_id=payload.get("instance_id"), lab_id=lab_id,
                    symbol=payload.get("symbol"), timeframe=payload.get("timeframe"),
                    strategy_id=payload.get("strategy_id"),
                    strategy_version=payload.get("strategy_version"),
                    state_after=payload.get("status") or None, reason=payload.get("detail"),
                    metadata={"lifecycle_event": name, **extra})
    except Exception:  # noqa: BLE001
        return False


__all__ = ["emit", "install", "uninstall", "installed", "observe_instance_event"]
