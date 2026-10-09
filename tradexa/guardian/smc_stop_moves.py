"""Retained Agent stop attempts; not broker-confirmed or current protection."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from .events import GuardianEvent, _safe_json
from .health import component_health
from .lab_execution_observer import _NoRedirect
from .lab_fill_history import digest
from .lab_observer import _timestamp
from .smc_intent_history import validate_cursor

COMPONENT = "smc_stop_moves"
PROBE = "guardian_smc_stop_moves"
SCOPE = "RETAINED_SMC_AGENT_STOP_MOVE_RECORDS"
PAGE_SIZE = 32
MAX_RESPONSE_BYTES = 128 * 1024
MAX_EVENT_BYTES = 8192
TEXT_FIELDS = ("id", "trade_id", "at", "candle_time", "symbol", "reason_code")
FIELDS = {*TEXT_FIELDS, "source_sequence", "from_price", "to_price", "progress_r",
          "applied", "error_recorded", "reason_recorded"}
FLAGS = {"execution_integrity_verified": False, "full_lifecycle_verified": False,
         "source_history_immutable_verified": False, "current_protection_verified": False,
         "broker_stop_application_verified": False, "paper_account_binding_verified": False,
         "journal_trade_binding_verified": False, "complete_stop_history_verified": False}


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous stop history field")
        result[key] = value
    return result


def project_move(row):
    if not isinstance(row, dict) or set(row) != FIELDS:
        raise ValueError("Invalid stop history projection")
    if type(row["source_sequence"]) is not int or not 1 <= row["source_sequence"] <= 2**63-1:
        raise ValueError("Invalid stop history sequence")
    for key in TEXT_FIELDS:
        value = row[key]
        if (not isinstance(value, str) or len(value) > 256 or
                (key != "candle_time" and not value)):
            raise ValueError("Invalid stop history identity")
        value.encode("utf-8")
    for key in ("applied", "error_recorded", "reason_recorded"):
        if type(row[key]) is not bool:
            raise ValueError("Invalid stop history flag")
    for key in ("from_price", "to_price", "progress_r"):
        value = row[key]
        if key == "progress_r" and value is None:
            continue
        try:
            valid = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid or (key != "progress_r" and value <= 0):
            raise ValueError("Invalid stop history number")
    result = dict(row)
    result["at"] = _timestamp(row["at"]).isoformat()
    if row["candle_time"]:
        result["candle_time"] = _timestamp(row["candle_time"]).isoformat()
    return _safe_json(result)


def source_origin(first):
    return digest(["smc-stop-origin-v1", first]) if first else ""


def cursor_anchor(first, previous):
    return digest(["smc-stop-cursor-v1", first, previous])


def event_for(row, origin):
    return GuardianEvent(source_service=PROBE, source_component=COMPONENT,
        event_type="smc_stop_move_observed", timestamp=_timestamp(row["at"]),
        event_id=digest(["smc-stop-event-v1", origin, row["id"]]),
        lab_id="SMC", agent_id="smc_agent", symbol=row["symbol"],
        state_after="REPORTED_APPLIED" if row["applied"] else "REPORTED_NOT_APPLIED",
        reason="RECORDED_AGENT_STOP_ATTEMPT_ONLY",
        evidence={"move": row, "stop_history_origin": origin},
        metadata={"coverage": SCOPE, "paper_only": True, **FLAGS})


def project_event(event):
    if not isinstance(event, dict):
        raise ValueError("Invalid stop history event")
    raw = {k: v for k, v in event.items() if k not in ("guardian_sequence", "received_at")}
    evidence = raw.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"move", "stop_history_origin"}:
        raise ValueError("Invalid stop history evidence")
    origin = evidence["stop_history_origin"]
    if not isinstance(origin, str) or len(origin) != 64 or any(c not in "0123456789abcdef" for c in origin):
        raise ValueError("Invalid stop history origin")
    row = project_move(evidence["move"])
    expected = event_for(row, origin).canonical_json()
    # Every header, flag, field and identity is part of the read contract.
    actual = GuardianEvent.from_payload(raw).canonical_json()
    if set(raw) != set(json.loads(expected)) or actual != expected:
        raise ValueError("Contradictory stop history event")
    return row


class GuardianSMCStopMoves:
    def __init__(self, store, url, key, *, fetch=None, clock=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"app", "localhost", "127.0.0.1", "::1"}
                or parsed.port != 8000 or parsed.path != "/guardian/smc-stop-moves"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Stop history URL must be an internal read endpoint")
        if not isinstance(key, str) or len(key) < 24:
            raise ValueError("Stop history requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.component, self.probe = COMPONENT, PROBE
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self, after, anchor):
        request = Request(self.url+"?"+urlencode({"after": after, "anchor": anchor}),
                          headers={"X-Guardian-Observer-Key": self.key})
        with build_opener(_NoRedirect()).open(request, timeout=3) as response:
            if response.status != 200:
                raise ValueError("Stop history source unavailable")
            raw = response.read(MAX_RESPONSE_BYTES+1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Stop history response exceeds bound")
        return json.loads(raw, object_pairs_hook=unique_fields)

    def poll(self):
        try:
            return self._poll()
        except Exception:
            self.store.record_heartbeat(PROBE, "FAILED", observed_at=self.clock(),
                                        reason="STOP_HISTORY_SOURCE_UNAVAILABLE")
            raise

    def _poll(self):
        expected = self.store.observer_cursor(COMPONENT)
        validate_cursor(*expected)
        view = self.fetch(*expected)
        if len(json.dumps(view, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            raise ValueError("Stop history response exceeds bound")
        if (not isinstance(view, dict) or set(view) != {
                "schema_version", "scope", "observed_at", "execution_integrity_verified", "page"} or
                type(view["schema_version"]) is not int or view["schema_version"] != 1 or
                view["scope"] != SCOPE or view["execution_integrity_verified"] is not False):
            raise ValueError("Invalid stop history contract")
        observed, now = _timestamp(view["observed_at"]), self.clock()
        if now.utcoffset() is None or not -5 <= (now-observed).total_seconds() <= 90:
            raise ValueError("Stale stop history observation")
        page = view["page"]
        if (not isinstance(page, dict) or set(page) != {
                "after", "anchor", "origin", "atomic_snapshot", "first_move", "previous_move",
                "moves", "has_more", "next_after", "next_anchor"} or
                type(page["after"]) is not int or page["after"] != expected[0] or
                page["anchor"] != expected[1] or page["atomic_snapshot"] is not True or
                type(page["has_more"]) is not bool):
            raise ValueError("Invalid stop history page")
        rows = page["moves"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or (page["has_more"] and not rows):
            raise ValueError("Invalid stop history page bound")
        first = project_move(page["first_move"]) if page["first_move"] is not None else None
        origin = source_origin(first)
        if page["origin"] != origin:
            raise ValueError("Stop history origin changed")
        previous = page["previous_move"]
        if expected[0]:
            previous = project_move(previous)
            if (not first or first["source_sequence"] > expected[0] or
                    previous["source_sequence"] != expected[0] or cursor_anchor(first, previous) != expected[1]):
                raise ValueError("Stop history source cursor changed")
        elif previous is not None:
            raise ValueError("Unexpected stop history predecessor")
        if bool(first) != bool(rows or expected[0]):
            raise ValueError("Stop history origin missing")
        sequence, last, seen, events = expected[0], None, set(), []
        for value in rows:
            row = project_move(value)
            if (row["source_sequence"] <= sequence or row["id"] in seen or
                    (not events and not expected[0] and row != first)):
                raise ValueError("Stop history page unordered or duplicated")
            if (_timestamp(row["at"])-observed).total_seconds() > 5:
                raise ValueError("Stop history record ahead of observation")
            seen.add(row["id"])
            sequence, last = row["source_sequence"], row
            event = event_for(row, origin)
            if len(event.canonical_json().encode()) > MAX_EVENT_BYTES:
                raise ValueError("Stop history event exceeds bound")
            events.append(event)
        next_cursor = (sequence, cursor_anchor(first, last)) if events else expected
        if type(page["next_after"]) is not int or (page["next_after"], page["next_anchor"]) != next_cursor:
            raise ValueError("Invalid stop history checkpoint")
        count = self.store.append_observed_page(COMPONENT, expected=expected,
                                                next_cursor=next_cursor, events=events)
        self.store.record_heartbeat(PROBE, "DEGRADED" if page["has_more"] else "HEALTHY",
            observed_at=observed, reason="STOP_HISTORY_IMPORT_IN_PROGRESS" if page["has_more"] else "")
        return count


def smc_stop_moves_view(store, *, after=0, now=None):
    page = store.smc_stop_moves_page(after=after)
    for event in page["events"]:
        project_event(event)
    health = component_health(page.pop("heartbeats"), (PROBE,), now=now)["components"][PROBE]
    state = {"HEALTHY": "CAUGHT_UP_AT_LAST_POLL", "DEGRADED": "IMPORTING"}.get(health["state"], "UNKNOWN")
    return {**page, "scope": SCOPE, "history_state": state, "probe_state": health["state"],
            "probe_reason": health["reason"], "observation_age_seconds": health.get("age_seconds"), **FLAGS}
