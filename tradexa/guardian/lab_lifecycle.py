"""Cursor-backed import of immutable, post-install PA/SMC lifecycle evidence."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .events import GuardianEvent
from .lab_observer import _timestamp
from .store import GuardianStore
from .provenance import provenance_metadata

_COMPONENTS = {"PRICE_ACTION": "pa_lifecycle", "SMC": "smc_lifecycle"}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_FILL_FIELDS = frozenset({"order_id", "symbol", "side", "quantity", "price", "fee", "realized_pnl"})


class GuardianLabLifecycle:
    def __init__(self, store: GuardianStore, url: str, key: str, *,
                 timeout_s: float = 3.0,
                 fetch: Callable[[str, int, str], dict] | None = None,
                 clock: Callable[[], datetime] | None = None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in
                {"app", "localhost", "127.0.0.1", "::1"} or parsed.port != 8000 or
                parsed.path != "/guardian/lifecycle" or parsed.username or parsed.password or
                parsed.query or parsed.fragment):
            raise ValueError("Guardian lifecycle URL must be an internal read endpoint")
        if not isinstance(key, str) or len(key) < 24 or timeout_s <= 0:
            raise ValueError("Guardian lifecycle requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.timeout_s = timeout_s
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self, lab: str, after: int, anchor: str) -> dict:
        request = Request(self.url + "?" + urlencode({"lab": lab, "after": after,
                                                       "anchor": anchor}),
                          headers={"X-Guardian-Observer-Key": self.key})
        with urlopen(request, timeout=self.timeout_s) as response:
            if response.status != 200:
                raise ValueError("lab lifecycle returned a non-success response")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("lab lifecycle response exceeds size limit")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("lab lifecycle response must be an object")
        return value

    def poll(self) -> int:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Guardian lifecycle clock must have a timezone")
        appended = 0
        behind = False
        for lab, component in _COMPONENTS.items():
            expected = self.store.observer_cursor(component)
            view = self.fetch(lab, *expected)
            if (not isinstance(view, dict) or view.get("schema_version") != 1 or
                    view.get("scope") != "POST_INSTALL_MATERIAL_LIFECYCLE" or
                    view.get("feed_health_verified") is not False or
                    view.get("execution_integrity_verified") is not False):
                raise ValueError("lab lifecycle contract is invalid")
            observed = _timestamp(view.get("observed_at"))
            if not -5 <= (now.astimezone(timezone.utc) - observed).total_seconds() <= 90:
                raise ValueError("lab lifecycle response is stale or ahead of clock")
            page = view.get("page")
            if (not isinstance(page, dict) or page.get("lab") != lab or
                    page.get("coverage") != "POST_INSTALL_MATERIAL_LIFECYCLE" or
                    page.get("after") != expected[0] or page.get("anchor") != expected[1] or
                    type(page.get("has_more")) is not bool):
                raise ValueError("lab lifecycle page identity is invalid")
            rows = page.get("transitions")
            if not isinstance(rows, list) or len(rows) > 32:
                raise ValueError("lab lifecycle page bound is invalid")
            events = []
            sequence = expected[0]
            for row in rows:
                if (not isinstance(row, dict) or type(row.get("source_sequence")) is not int or
                        row["source_sequence"] <= sequence or
                        not isinstance(row.get("source_event_id"), str) or
                        len(row["source_event_id"]) != 32 or
                        not isinstance(row.get("session_id"), str) or
                        not isinstance(row.get("correlation_id"), str) or
                        not isinstance(row.get("conditions"), list) or
                        not isinstance(row.get("missing_conditions"), list)):
                    raise ValueError("lab lifecycle row is invalid")
                sequence = row["source_sequence"]
                event_time = _timestamp(row.get("event_time"))
                if event_time > observed:
                    raise ValueError("lab lifecycle event is after observation")
                fill = row.get("fill")
                if fill is not None and not isinstance(fill, dict):
                    raise ValueError("lab lifecycle fill is invalid")
                event = GuardianEvent(
                    source_service="guardian_lab_lifecycle",
                    source_component=component, event_type="lab_lifecycle_observed",
                    event_id=row["source_event_id"], timestamp=event_time,
                    severity="INFO", lab_id=lab, session_id=row["session_id"],
                    correlation_id=row["correlation_id"],
                    strategy_id=row.get("strategy_id"),
                    strategy_version=row.get("strategy_version"),
                    symbol=row.get("symbol"), timeframe=row.get("timeframe"),
                    decision=row.get("state"), reason=row.get("reason"),
                    order_id=row.get("order_id"),
                    evidence={
                        "source_sequence": sequence,
                        "idempotency_key": row.get("idempotency_key"),
                        "source_event_time": event_time.isoformat(),
                        "conditions": row["conditions"],
                        "missing_conditions": row["missing_conditions"],
                        "condition_trace_available": bool(row["conditions"]),
                        "broker_event_order_id": row.get("broker_event_order_id"),
                        "fill": {key: value for key, value in (fill or {}).items()
                                 if key in _FILL_FIELDS} if fill else None,
                        "feed_health_verified": False,
                        "execution_integrity_verified": False,
                    },
                    metadata={"coverage": "POST_INSTALL_MATERIAL_LIFECYCLE",
                              "paper_only": True, "model_id": row.get("model_id"),
                              **provenance_metadata(row)},
                )
                event.canonical_json()
                events.append(event)
            next_cursor = (page.get("next_after"), page.get("next_anchor"))
            if (type(next_cursor[0]) is not int or not isinstance(next_cursor[1], str) or
                    next_cursor != ((sequence, rows[-1]["source_event_id"])
                                    if rows else expected) or
                    (page["has_more"] and not rows)):
                raise ValueError("lab lifecycle next cursor is invalid")
            appended += self.store.append_observed_page(
                component, expected=expected, next_cursor=next_cursor, events=events)
            behind = behind or page["has_more"]
        self.store.record_heartbeat(
            "guardian_lab_lifecycle", "DEGRADED" if behind else "HEALTHY",
            reason="LIFECYCLE_IMPORT_IN_PROGRESS" if behind else "", observed_at=now)
        return appended
