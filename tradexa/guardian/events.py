"""Versioned Guardian evidence envelope; no commands or trading authority."""
from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Mapping

SCHEMA_VERSION = 1
SEVERITIES = frozenset({"INFO", "WATCH", "WARNING", "HIGH", "CRITICAL"})
MAX_EVENT_BYTES = 64 * 1024
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE_KEY = re.compile(r"password|secret|token|api[_-]?key|authorization|private[_-]?key", re.I)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:\bbearer\s+\S+|\b(?:password|secret|token|api[_-]?key)\s*[:=]\s*\S+)")


class GuardianEventError(ValueError):
    """Invalid or unsafe supplementary telemetry; never a trading rejection."""


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Take a bounded JSON snapshot, rejecting obvious credential material."""
    if depth > 8:
        raise GuardianEventError("event evidence is too deeply nested")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GuardianEventError("event evidence contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > 2048 or _SENSITIVE_VALUE.search(value):
            raise GuardianEventError("event evidence contains oversized or credential-like text")
        return value
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise GuardianEventError("event evidence has too many fields")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 80 or _SENSITIVE_KEY.search(key):
                raise GuardianEventError("event evidence contains an unsafe field name")
            result[key] = _safe_json(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise GuardianEventError("event evidence has too many items")
        return [_safe_json(item, depth=depth + 1) for item in value]
    raise GuardianEventError(f"event evidence contains unsupported type {type(value).__name__}")


@dataclass(frozen=True)
class GuardianEvent:
    source_service: str
    source_component: str
    event_type: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    severity: str = "INFO"
    agent_id: str | None = None
    instance_id: str | None = None
    lab_id: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    state_before: str | None = None
    state_after: str | None = None
    decision: str | None = None
    reason: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None
    session_id: str | None = None
    execution_id: str | None = None
    order_id: str | None = None
    position_id: str | None = None
    latency_ms: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> GuardianEvent:
        """Decode one versioned producer event without accepting store-owned fields."""
        if not isinstance(payload, Mapping):
            raise GuardianEventError("event must be a JSON object")
        values = dict(payload)
        version = values.pop("schema_version", None)
        if type(version) is not int or version != SCHEMA_VERSION:
            raise GuardianEventError("unsupported Guardian event schema_version")
        allowed = {item.name for item in fields(cls)}
        if set(values) - allowed:
            raise GuardianEventError("event contains unknown or store-owned fields")
        for required in ("source_service", "source_component", "event_type", "event_id", "timestamp"):
            if required not in values:
                raise GuardianEventError(f"event is missing {required}")
        raw_time = values["timestamp"]
        if not isinstance(raw_time, str):
            raise GuardianEventError("event timestamp must be an ISO-8601 string")
        try:
            values["timestamp"] = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GuardianEventError("event timestamp is invalid") from exc
        event = cls(**values)
        event.canonical_json()
        return event

    def canonical_json(self) -> str:
        """Serialize once before persistence; received_at belongs to the store."""
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None \
                or self.timestamp.utcoffset() is None:
            raise GuardianEventError("event timestamp must include a timezone")
        if not all(isinstance(name, str) and _NAME.fullmatch(name) for name in
                   (self.source_service, self.source_component, self.event_type)):
            raise GuardianEventError("source and event type must be lowercase identifiers")
        if not isinstance(self.event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", self.event_id):
            raise GuardianEventError("event_id must be a stable opaque identifier")
        if not isinstance(self.severity, str) or self.severity not in SEVERITIES:
            raise GuardianEventError("unknown Guardian severity")
        if self.latency_ms is not None and (isinstance(self.latency_ms, bool)
                                            or not isinstance(self.latency_ms, (int, float))
                                            or not math.isfinite(self.latency_ms)
                                            or self.latency_ms < 0):
            raise GuardianEventError("latency_ms must be finite and nonnegative")
        if not isinstance(self.evidence, Mapping) or not isinstance(self.metadata, Mapping):
            raise GuardianEventError("event evidence and metadata must be objects")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "event_id": self.event_id,
            "timestamp": self.timestamp.astimezone(timezone.utc).isoformat(),
            "source_service": self.source_service,
            "source_component": self.source_component,
            "event_type": self.event_type,
            "severity": self.severity,
        }
        for name in ("agent_id", "instance_id", "lab_id", "strategy_id",
                     "strategy_version", "symbol", "timeframe", "state_before",
                     "state_after", "decision", "reason", "correlation_id",
                     "session_id", "execution_id", "order_id", "position_id"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise GuardianEventError(f"{name} must be text")
            payload[name] = _safe_json(value)
        for name in ("latency_ms",):
            payload[name] = _safe_json(getattr(self, name))
        payload["evidence"] = _safe_json(self.evidence)
        payload["metadata"] = _safe_json(self.metadata)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise GuardianEventError("event exceeds the Guardian evidence size limit")
        return encoded
