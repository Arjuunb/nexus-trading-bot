"""The Guardian event: one immutable shape for everything Guardian observes.

Every event carries the same fields (PRD §6), so an instance's stale feed,
a lab's reconnect and Guardian's own health change can be filtered and put
on one timeline. Events are values: built once, validated, stripped of
secrets, and never edited afterwards -- the store refuses UPDATE and DELETE.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from services.redaction import redact, scrub_text

SEVERITIES = ("INFO", "WATCH", "WARNING", "HIGH", "CRITICAL")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

#: PRD §19. BLOCKED means "cannot work because something it depends on is
#: down" -- never a verdict on the component itself.
HEALTH_STATES = ("HEALTHY", "DEGRADED", "BLOCKED", "FAILED", "UNKNOWN")

_CATALOGUE = {
    "market_data": (
        "websocket_connected", "websocket_disconnected", "websocket_reconnected",
        "candle_received", "candle_closed", "candle_missing", "stale_candle",
        "stale_htf_candle", "missing_htf_candle", "htf_candle_recovered",
        "quote_received", "mark_price_received", "abnormal_latency", "sequence_gap"),
    "strategy": (
        "evaluation_started", "evaluation_completed", "condition_passed",
        "condition_failed", "setup_detected", "setup_rejected", "setup_expired",
        "signal_generated", "signal_invalidated"),
    "risk": (
        "risk_check_started", "risk_check_passed", "risk_check_failed",
        "size_calculated", "exposure_limit_hit", "daily_limit_hit",
        "drawdown_limit_hit"),
    "execution": (
        "intent_created", "order_submitted", "order_acknowledged", "order_rejected",
        "order_filled", "partial_fill", "execution_uncertain",
        "reconciliation_started", "reconciliation_completed",
        "integrity_violation", "integrity_resolved"),
    "position": (
        "position_opened", "stop_updated", "target_updated", "position_closed",
        "stop_hit", "target_hit"),
    "journal": ("journal_requested", "journal_written", "journal_failed", "journal_reconciled"),
    "infrastructure": (
        "worker_started", "worker_stopped", "worker_crashed", "worker_restarted",
        "database_error", "api_error", "resource_warning", "deployment_detected",
        "configuration_changed", "instance_lifecycle"),
    # Guardian's own observations about the platform and about itself.
    "guardian": (
        "health_changed", "heartbeat_missed", "collector_failed", "collector_recovered",
        "guardian_started", "guardian_stopped",
        "incident_opened", "incident_updated", "incident_recovered", "incident_closed",
        "anomaly_detected", "anomaly_cleared",
        "hypothesis_created", "report_issued"),
}
EVENT_TYPES: dict[str, str] = {t: cat for cat, types in _CATALOGUE.items() for t in types}

#: PRD §6, in order. ``category`` and ``received_at`` are added by Guardian.
FIELDS = (
    "event_id", "timestamp", "received_at", "source_service", "source_component",
    "agent_id", "instance_id", "lab_id", "strategy_id", "strategy_version",
    "symbol", "timeframe", "event_type", "category", "severity", "state_before",
    "state_after", "decision", "reason", "evidence", "correlation_id", "session_id",
    "execution_id", "order_id", "position_id", "latency_ms", "metadata",
)
_TEXT_LIMIT = 2000


class InvalidEvent(ValueError):
    """An event Guardian will not store: its evidence would be ambiguous."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class GuardianEvent:
    event_type: str
    source_service: str
    source_component: str
    severity: str = "INFO"
    timestamp: str = field(default_factory=utcnow)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    category: str = ""
    agent_id: Optional[str] = None
    instance_id: Optional[str] = None
    lab_id: Optional[str] = None
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    state_before: Optional[str] = None
    state_after: Optional[str] = None
    decision: Optional[str] = None
    reason: Optional[str] = None
    evidence: Any = None
    correlation_id: Optional[str] = None
    session_id: Optional[str] = None
    execution_id: Optional[str] = None
    order_id: Optional[str] = None
    position_id: Optional[str] = None
    latency_ms: Optional[float] = None
    metadata: Any = None

    def to_dict(self) -> dict:
        return asdict(self)


def _text(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return scrub_text(str(value))[:_TEXT_LIMIT]


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    # Round-trip so what is stored is exactly what can be read back.
    return json.loads(json.dumps(redact(value), default=str))


def make_event(event_type: str, *, source_service: str, source_component: str,
               severity: str = "INFO", **fields: Any) -> GuardianEvent:
    """Validate and build one event. Secrets are removed from every text and
    structured field before the event exists, so nothing downstream -- store,
    API, report -- can leak one."""
    if event_type not in EVENT_TYPES:
        raise InvalidEvent(f"unknown event type {event_type!r}")
    if severity not in SEVERITY_RANK:
        raise InvalidEvent(f"unknown severity {severity!r}")
    if not source_service or not source_component:
        raise InvalidEvent("an event must name its source service and component")
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise InvalidEvent(f"unknown event fields {sorted(unknown)}")
    for name in ("event_id", "received_at", "category"):
        fields.pop(name, None)             # Guardian's to assign, never the caller's
    latency = fields.pop("latency_ms", None)
    evidence, metadata = fields.pop("evidence", None), fields.pop("metadata", None)
    text = {k: _text(v) for k, v in fields.items()}
    if text.get("timestamp") is None:
        text.pop("timestamp", None)
    return GuardianEvent(
        event_type=event_type, category=EVENT_TYPES[event_type],
        source_service=_text(source_service), source_component=_text(source_component),
        severity=severity, evidence=_json_safe(evidence), metadata=_json_safe(metadata),
        latency_ms=float(latency) if latency is not None else None, **text)


def worse(a: str, b: str) -> str:
    """The more severe of two severities."""
    return a if SEVERITY_RANK[a] >= SEVERITY_RANK[b] else b
