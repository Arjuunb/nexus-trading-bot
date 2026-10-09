"""Bounded, read-only collection of committed PA/SMC decision evidence."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .events import GuardianEvent
from .store import GuardianStore

_MAX_RESPONSE_BYTES = 1024 * 1024
_LAB_COMPONENTS = {"PRICE_ACTION": "pa_lab", "SMC": "smc_lab"}


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("lab observation timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("lab observation timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("lab observation timestamp must have a timezone")
    return parsed.astimezone(timezone.utc)


class GuardianLabObserver:
    """Poll a separately keyed, GET-only app projection; never control a lab."""

    def __init__(self, store: GuardianStore, url: str, key: str, *,
                 timeout_s: float = 3.0, fetch: Callable[[], dict] | None = None,
                 clock: Callable[[], datetime] | None = None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in
                {"app", "localhost", "127.0.0.1", "::1"} or
                parsed.port != 8000 or parsed.path != "/guardian/observations" or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Guardian lab URL must be an internal observation endpoint")
        if not isinstance(key, str) or len(key) < 24 or timeout_s <= 0:
            raise ValueError("Guardian lab observer requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.timeout_s = timeout_s
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self) -> dict:
        request = Request(self.url, headers={"X-Guardian-Observer-Key": self.key})
        with urlopen(request, timeout=self.timeout_s) as response:
            if response.status != 200:
                raise ValueError("lab observer returned a non-success response")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("lab observation response exceeds size limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("lab observation response must be an object")
        return result

    def poll(self) -> int:
        view = self.fetch()
        if not isinstance(view, dict) or view.get("schema_version") != 1 or \
                view.get("scope") != "BOUNDED_SAVED_LAB_DECISIONS" or \
                view.get("feed_health_verified") is not False or \
                view.get("execution_integrity_verified") is not False:
            raise ValueError("lab observation contract is invalid")
        observed = _timestamp(view.get("observed_at"))
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Guardian observer clock must have a timezone")
        age_s = (now.astimezone(timezone.utc) - observed).total_seconds()
        if not -5 <= age_s <= 90:
            raise ValueError("lab observation is stale or ahead of clock")
        labs = view.get("labs")
        if not isinstance(labs, list) or len(labs) != len(_LAB_COMPONENTS):
            raise ValueError("lab observations are incomplete")
        projected = []
        seen = set()
        for lab in labs:
            if not isinstance(lab, dict) or lab.get("lab") not in _LAB_COMPONENTS or \
                    lab["lab"] in seen or lab.get("coverage") != "LATEST_ACTIVE_SESSION_ONLY" or \
                    lab.get("state") not in {"OBSERVED", "UNKNOWN"}:
                raise ValueError("invalid lab observation")
            seen.add(lab["lab"])
            rows = lab.get("evaluations")
            if not isinstance(rows, list) or len(rows) > 32:
                raise ValueError("lab evaluation window is invalid")
            if lab["state"] == "UNKNOWN" and rows:
                raise ValueError("unknown lab cannot contain evaluation evidence")
            for row in rows:
                if not isinstance(row, dict) or row.get("session_id") != lab.get("session_id"):
                    raise ValueError("lab evaluation session identity is inconsistent")
                conditions = row.get("conditions")
                missing = row.get("missing_conditions")
                if not isinstance(conditions, list) or not isinstance(missing, list):
                    raise ValueError("lab condition trace is invalid")
                evidence = {"conditions": conditions, "missing_conditions": missing,
                            "condition_trace_available": row.get("condition_trace_available") is True,
                            "source_row_state": row.get("state"),
                            "full_history_coverage": False,
                            "feed_health_verified": False,
                            "execution_integrity_verified": False}
                identity = json.dumps({
                    "lab": lab["lab"], "correlation_id": row.get("correlation_id"),
                    "session_id": row.get("session_id"), "state": row.get("state"),
                    "reason": row.get("reason"), "conditions": conditions,
                    "missing": missing,
                }, sort_keys=True, separators=(",", ":"))
                candle_time = _timestamp(row.get("candle_time"))
                if candle_time > observed:
                    raise ValueError("lab decision candle is after observation")
                event = GuardianEvent(
                    source_service="guardian_lab_probe",
                    source_component=_LAB_COMPONENTS[lab["lab"]],
                    event_type="lab_evaluation_observed",
                    event_id=hashlib.sha256(identity.encode()).hexdigest()[:32],
                    timestamp=candle_time,
                    severity="INFO", lab_id=lab["lab"],
                    session_id=row.get("session_id"),
                    correlation_id=row.get("correlation_id"),
                    strategy_id=row.get("strategy_id"),
                    strategy_version=row.get("strategy_version"),
                    symbol=row.get("symbol"), timeframe=row.get("timeframe"),
                    decision=row.get("state"), reason=row.get("reason"),
                    evidence=evidence,
                    metadata={"coverage": "LATEST_ACTIVE_SESSION_ONLY", "paper_only": True,
                              "model_id": row.get("model_id")},
                )
                event.canonical_json()
                projected.append(event)
        if seen != set(_LAB_COMPONENTS):
            raise ValueError("lab observations are incomplete")
        appended = sum(self.store.append(event) for event in projected)
        # A successful API read is a probe heartbeat, *not* proof that either
        # lab's feed, strategy, journal, or broker is healthy.
        self.store.record_heartbeat("guardian_lab_probe", "HEALTHY", observed_at=observed)
        return appended
