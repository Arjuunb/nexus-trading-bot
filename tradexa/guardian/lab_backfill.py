"""Read-only, cursor-backed import of retained PA/SMC evaluation identities."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .events import GuardianEvent
from .lab_observer import _timestamp
from .store import GuardianStore

_LAB_COMPONENTS = {"PRICE_ACTION": "pa_lab", "SMC": "smc_lab"}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class GuardianLabBackfill:
    """One bounded page per lab per poll; never touches a trading database."""

    def __init__(self, store: GuardianStore, url: str, key: str, *,
                 timeout_s: float = 3.0,
                 fetch: Callable[[str, int, str], dict] | None = None,
                 clock: Callable[[], datetime] | None = None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in
                {"app", "localhost", "127.0.0.1", "::1"} or
                parsed.port != 8000 or parsed.path != "/guardian/evaluations" or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Guardian backfill URL must be an internal read endpoint")
        if not isinstance(key, str) or len(key) < 24 or timeout_s <= 0:
            raise ValueError("Guardian backfill requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.timeout_s = timeout_s
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self, lab: str, after: int, anchor: str) -> dict:
        query = urlencode({"lab": lab, "after": after, "anchor": anchor})
        request = Request(self.url + "?" + query,
                          headers={"X-Guardian-Observer-Key": self.key})
        with urlopen(request, timeout=self.timeout_s) as response:
            if response.status != 200:
                raise ValueError("lab backfill returned a non-success response")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("lab backfill response exceeds size limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("lab backfill response must be an object")
        return result

    def poll(self) -> int:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Guardian backfill clock must have a timezone")
        appended = 0
        behind = False
        for lab, component in _LAB_COMPONENTS.items():
            expected = self.store.observer_cursor(component)
            view = self.fetch(lab, *expected)
            if (not isinstance(view, dict) or view.get("schema_version") != 1 or
                    view.get("scope") != "RETAINED_LAB_DECISION_IDENTITIES" or
                    view.get("feed_health_verified") is not False or
                    view.get("execution_integrity_verified") is not False):
                raise ValueError("lab backfill contract is invalid")
            observed = _timestamp(view.get("observed_at"))
            age_s = (now.astimezone(timezone.utc) - observed).total_seconds()
            if not -5 <= age_s <= 90:
                raise ValueError("lab backfill response is stale or ahead of clock")
            page = view.get("page")
            if (not isinstance(page, dict) or page.get("lab") != lab or
                    page.get("coverage") != "ALL_RETAINED_EVALUATION_IDENTITIES" or
                    page.get("after") != expected[0] or page.get("anchor") != expected[1] or
                    type(page.get("has_more")) is not bool):
                raise ValueError("lab backfill page identity is invalid")
            rows = page.get("evaluations")
            if not isinstance(rows, list) or len(rows) > 32:
                raise ValueError("lab backfill page bound is invalid")
            events = []
            sequence = expected[0]
            for row in rows:
                if not isinstance(row, dict) or type(row.get("source_sequence")) is not int or \
                        row["source_sequence"] <= sequence:
                    raise ValueError("lab backfill sequence is invalid")
                sequence = row["source_sequence"]
                correlation = row.get("correlation_id")
                if not isinstance(correlation, str) or not 1 <= len(correlation) <= 128 or \
                        not isinstance(row.get("session_id"), str) or \
                        not isinstance(row.get("conditions"), list) or \
                        not isinstance(row.get("missing_conditions"), list):
                    raise ValueError("lab backfill decision identity is invalid")
                candle_time = _timestamp(row.get("candle_time"))
                if candle_time > observed:
                    raise ValueError("lab decision candle is after observation")
                identity = f"{lab}:{correlation}"
                event = GuardianEvent(
                    source_service="guardian_lab_backfill",
                    source_component=component,
                    event_type="lab_evaluation_backfilled",
                    event_id=hashlib.sha256(identity.encode()).hexdigest()[:32],
                    timestamp=candle_time,
                    severity="INFO", lab_id=lab,
                    session_id=row["session_id"], correlation_id=correlation,
                    strategy_id=row.get("strategy_id"),
                    strategy_version=row.get("strategy_version"),
                    symbol=row.get("symbol"), timeframe=row.get("timeframe"),
                    decision=row.get("state"), reason=row.get("reason"),
                    evidence={
                        "conditions": row["conditions"],
                        "missing_conditions": row["missing_conditions"],
                        "condition_trace_available": row.get("condition_trace_available") is True,
                        "source_row_state": row.get("state"),
                        "source_sequence": sequence,
                        "decision_existence_coverage": "RETAINED_ROWS_ONLY",
                        "lifecycle_history_complete": False,
                        "feed_health_verified": False,
                        "execution_integrity_verified": False,
                    },
                    metadata={"coverage": "ALL_RETAINED_EVALUATION_IDENTITIES",
                              "paper_only": True, "model_id": row.get("model_id")},
                )
                event.canonical_json()
                events.append(event)
            next_cursor = (page.get("next_after"), page.get("next_anchor"))
            if (type(next_cursor[0]) is not int or not isinstance(next_cursor[1], str)
                    or next_cursor != ((sequence, rows[-1]["correlation_id"]) if rows else expected)
                    or (page["has_more"] and not rows)):
                raise ValueError("lab backfill next cursor is invalid")
            appended += self.store.append_observed_page(
                component, expected=expected, next_cursor=next_cursor, events=events)
            behind = behind or page["has_more"]
        self.store.record_heartbeat(
            "guardian_lab_backfill", "DEGRADED" if behind else "HEALTHY",
            reason="BACKFILL_IN_PROGRESS" if behind else "",
            observed_at=now)
        return appended
