"""Cursor-backed import of persisted Trading Instance decision/gate transitions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .events import GuardianEvent
from .lab_observer import _timestamp
from .store import GuardianStore
from .instance_provenance import instance_provenance_metadata

_COMPONENT = "instance_decisions"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class GuardianInstanceDecisions:
    def __init__(self, store: GuardianStore, url: str, key: str, *,
                 timeout_s: float = 3.0,
                 fetch: Callable[[int, str], dict] | None = None,
                 clock: Callable[[], datetime] | None = None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in
                {"app", "localhost", "127.0.0.1", "::1"} or parsed.port != 8000 or
                parsed.path != "/guardian/instance-decisions" or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Guardian instance decision URL must be internal")
        if not isinstance(key, str) or len(key) < 24 or timeout_s <= 0:
            raise ValueError("Guardian instance decisions require an independent long key")
        self.store, self.url, self.key = store, url, key
        self.timeout_s = timeout_s
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self, after: int, anchor: str) -> dict:
        request = Request(self.url + "?" + urlencode({"after": after, "anchor": anchor}),
                          headers={"X-Guardian-Observer-Key": self.key})
        with urlopen(request, timeout=self.timeout_s) as response:
            if response.status != 200:
                raise ValueError("instance decisions returned a non-success response")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("instance decision response exceeds size limit")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("instance decision response must be an object")
        return value

    def poll(self) -> int:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Guardian instance decision clock must have a timezone")
        expected = self.store.observer_cursor(_COMPONENT)
        view = self.fetch(*expected)
        if (not isinstance(view, dict) or view.get("schema_version") != 1 or
                view.get("scope") != "POST_INSTALL_INSTANCE_DECISION_LIFECYCLE" or
                view.get("feed_health_verified") is not False or
                view.get("execution_integrity_verified") is not False):
            raise ValueError("instance decision contract is invalid")
        observed = _timestamp(view.get("observed_at"))
        if not -5 <= (now.astimezone(timezone.utc) - observed).total_seconds() <= 90:
            raise ValueError("instance decision response is stale or ahead of clock")
        page = view.get("page")
        if (not isinstance(page, dict) or
                page.get("coverage") != "POST_INSTALL_INSTANCE_DECISION_LIFECYCLE" or
                page.get("after") != expected[0] or page.get("anchor") != expected[1] or
                type(page.get("has_more")) is not bool):
            raise ValueError("instance decision page identity is invalid")
        rows = page.get("transitions")
        if not isinstance(rows, list) or len(rows) > 32:
            raise ValueError("instance decision page bound is invalid")
        events = []
        sequence = expected[0]
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("source_sequence")) is not int or
                    row["source_sequence"] <= sequence or
                    not isinstance(row.get("source_event_id"), str) or
                    len(row["source_event_id"]) != 32 or
                    type(row.get("decision_id")) is not int or
                    not isinstance(row.get("passed_rules"), list) or
                    not isinstance(row.get("failed_rules"), list) or
                    type(row.get("executed")) is not bool):
                raise ValueError("instance decision row is invalid")
            sequence = row["source_sequence"]
            event_time = _timestamp(row.get("event_time"))
            if event_time > observed:
                raise ValueError("instance decision event is after observation")
            _timestamp(row.get("decision_time"))
            instance_id = row.get("instance_id")
            if not isinstance(instance_id, str):
                raise ValueError("instance decision owner is invalid")
            provenance = instance_provenance_metadata(row)
            config = provenance["instance_provenance"]["saved_config"] or {}
            event = GuardianEvent(
                source_service="guardian_instance_decisions",
                source_component=_COMPONENT,
                event_type="instance_decision_observed",
                event_id=row["source_event_id"], timestamp=event_time,
                severity="INFO", instance_id=instance_id or None,
                strategy_id=config.get("strategy_key") or row.get("strategy"),
                strategy_version=config.get("strategy_version"), symbol=row.get("symbol"),
                timeframe=row.get("timeframe"),
                decision=row.get("final_state"), reason=row.get("reason"),
                state_after=row.get("final_state"),
                evidence={
                    "source_sequence": sequence,
                    "decision_id": row["decision_id"],
                    "decision_identity": row.get("decision_identity"),
                    "decision_time": row["decision_time"],
                    "strategy_verdict": row.get("strategy_verdict"),
                    "strategy_label": row.get("strategy"),
                    "side": row.get("side"),
                    "gate_stage": row.get("gate_stage"),
                    "blocker": row.get("blocker"),
                    "executed": row["executed"],
                    "passed_rules": row["passed_rules"],
                    "failed_rules": row["failed_rules"],
                    "feed_health_verified": False,
                    "execution_integrity_verified": False,
                },
                metadata={"coverage": "POST_INSTALL_INSTANCE_DECISION_LIFECYCLE",
                          "instance_attributed": bool(instance_id), **provenance},
            )
            event.canonical_json()
            events.append(event)
        next_cursor = (page.get("next_after"), page.get("next_anchor"))
        if (type(next_cursor[0]) is not int or not isinstance(next_cursor[1], str) or
                next_cursor != ((sequence, rows[-1]["source_event_id"])
                                if rows else expected) or
                (page["has_more"] and not rows)):
            raise ValueError("instance decision next cursor is invalid")
        appended = self.store.append_observed_page(
            _COMPONENT, expected=expected, next_cursor=next_cursor, events=events)
        self.store.record_heartbeat(
            "guardian_instance_decisions", "DEGRADED" if page["has_more"] else "HEALTHY",
            reason="INSTANCE_DECISION_IMPORT_IN_PROGRESS" if page["has_more"] else "",
            observed_at=now)
        return appended
