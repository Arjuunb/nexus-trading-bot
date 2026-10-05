"""Retained execution-intent event observation, not reconciliation or accounting.

Immutable event order/trade links are never replaced by today's intent state.
Parent metadata is restricted to frozen execution identity columns; late-bound
decision IDs, payloads, error text and broker/fill inference are excluded.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from .events import GuardianEvent, _safe_json
from .health import component_health
from .lab_execution_observer import _NoRedirect
from .lab_fill_history import digest, identity
from .lab_observer import _timestamp

COMPONENT = "smc_intent_history"
PROBE = "guardian_smc_intent_history"
SCOPE = "RETAINED_SMC_AGENT_EXECUTION_INTENT_EVENTS"
PAGE_SIZE = 32
MAX_SEQUENCE = 2**63 - 1
STATES = frozenset({"DECISION_APPROVED", "EXECUTION_PENDING", "EXECUTED", "EXECUTION_FAILED",
                    "EXECUTION_UNCERTAIN", "RECONCILED", "COMPLETE"})
TEXT_FIELDS = ("id", "execution_key", "state", "broker_order_id", "trade_id", "created_at",
               "intent_id", "session_id", "symbol", "timeframe", "candle_time", "proposal_id")
FIELDS = TEXT_FIELDS + ("error_recorded",)


def validate_cursor(after, anchor):
    if (type(after) is not int or not 0 <= after <= MAX_SEQUENCE or not isinstance(anchor, str) or
            (anchor and not re.fullmatch(r"[0-9a-f]{64}", anchor)) or bool(after) != bool(anchor)):
        raise ValueError("Invalid intent history cursor")


def project_transition(row):
    if not isinstance(row, dict) or set(row) != {"source_sequence", *FIELDS}:
        raise ValueError("Invalid intent transition projection")
    if type(row["source_sequence"]) is not int or not 1 <= row["source_sequence"] <= MAX_SEQUENCE:
        raise ValueError("Invalid intent transition sequence")
    for key in TEXT_FIELDS:
        if row[key] is not None and (not isinstance(row[key], str) or len(row[key]) > 256):
            raise ValueError("Invalid intent transition metadata")
    for key in ("id", "execution_key", "intent_id", "symbol", "timeframe"):
        identity(row[key])
    if row["state"] not in STATES or type(row["error_recorded"]) is not bool:
        raise ValueError("Invalid intent transition state or error flag")
    result = dict(row)
    try:
        result["created_at"] = _timestamp(row["created_at"]).isoformat()
        if row["candle_time"]:
            result["candle_time"] = _timestamp(row["candle_time"]).isoformat()
    except OverflowError as exc:
        raise ValueError("Intent timestamp out of range") from exc
    return _safe_json(result)


def source_origin(first):
    return digest(["smc-intent-origin-v1", first]) if first else ""


def cursor_anchor(first, previous):
    return digest(["smc-intent-cursor-v1", first, previous])


class GuardianSMCIntentHistory:
    def __init__(self, store, url, key, *, fetch=None, clock=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"app", "localhost", "127.0.0.1", "::1"}
                or parsed.port != 8000 or parsed.path != "/guardian/smc-intent-events"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Intent history URL must be an internal read endpoint")
        if not isinstance(key, str) or len(key) < 24:
            raise ValueError("Intent history requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.component, self.probe = COMPONENT, PROBE
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self, after, anchor):
        request = Request(self.url + "?" + urlencode({"after": after, "anchor": anchor}),
                          headers={"X-Guardian-Observer-Key": self.key})
        with build_opener(_NoRedirect()).open(request, timeout=3) as response:
            if response.status != 200:
                raise ValueError("Intent history source unavailable")
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Intent history response exceeds bound")
        return json.loads(raw)

    def poll(self):
        try:
            return self._poll()
        except Exception:
            self.store.record_heartbeat(PROBE, "FAILED", observed_at=self.clock(),
                                        reason="INTENT_HISTORY_UNAVAILABLE")
            raise

    def _poll(self):
        expected = self.store.observer_cursor(COMPONENT)
        validate_cursor(*expected)
        view = self.fetch(*expected)
        if (not isinstance(view, dict) or type(view.get("schema_version")) is not int or
                view["schema_version"] != 1 or view.get("scope") != SCOPE or
                view.get("execution_integrity_verified") is not False):
            raise ValueError("Invalid intent history contract")
        observed, now = _timestamp(view.get("observed_at")), self.clock()
        if now.utcoffset() is None or not -5 <= (now - observed).total_seconds() <= 90:
            raise ValueError("Stale intent history observation")
        page = view.get("page")
        if (not isinstance(page, dict) or type(page.get("after")) is not int or page["after"] != expected[0] or
                page.get("anchor") != expected[1] or page.get("atomic_snapshot") is not True or
                type(page.get("has_more")) is not bool):
            raise ValueError("Invalid intent history page")
        raw = page.get("transitions")
        if not isinstance(raw, list) or len(raw) > PAGE_SIZE or (page["has_more"] and not raw):
            raise ValueError("Intent history page exceeds bound")
        first = project_transition(page["first_transition"]) if page.get("first_transition") is not None else None
        origin = source_origin(first)
        if page.get("origin") != origin:
            raise ValueError("Intent history origin mismatch")
        previous = page.get("previous_transition")
        if expected[0]:
            previous = project_transition(previous)
            if (not first or first["source_sequence"] > expected[0] or
                    previous["source_sequence"] != expected[0] or cursor_anchor(first, previous) != expected[1]):
                raise ValueError("Intent history source cursor changed")
        elif previous is not None:
            raise ValueError("Unexpected intent predecessor")
        if bool(first) != bool(raw or expected[0]):
            raise ValueError("Intent history origin missing")
        sequence, events, seen, last = expected[0], [], set(), None
        for item in raw:
            row = project_transition(item)
            if (row["source_sequence"] <= sequence or row["id"] in seen or
                    (not events and not expected[0] and row != first)):
                raise ValueError("Intent history page unordered or duplicated")
            sequence, last = row["source_sequence"], row
            seen.add(row["id"])
            when = _timestamp(row["created_at"])
            if (when - observed).total_seconds() > 5:
                raise ValueError("Intent event ahead of observation")
            event = GuardianEvent(
                source_service=PROBE, source_component=COMPONENT,
                event_type="smc_intent_transition_observed", timestamp=when,
                event_id=digest(["smc-intent-event-v1", origin, row["id"]]),
                lab_id="SMC", agent_id="smc_agent", symbol=row["symbol"], timeframe=row["timeframe"],
                session_id=row["session_id"], execution_id=row["execution_key"], order_id=row["broker_order_id"],
                state_after=row["state"], reason="RECORDED_EXECUTION_INTENT_EVENT_ONLY",
                evidence={"transition": row, "journal_origin": origin, "broker_execution_verified": False,
                          "journal_trade_verified": False, "position_lifecycle_verified": False},
                metadata={"coverage": SCOPE, "paper_only": True})
            event.canonical_json()
            events.append(event)
        next_cursor = (sequence, cursor_anchor(first, last)) if events else expected
        if (type(page.get("next_after")) is not int or
                (page["next_after"], page.get("next_anchor")) != next_cursor):
            raise ValueError("Invalid intent next checkpoint")
        count = self.store.append_observed_page(COMPONENT, expected=expected,
                                                next_cursor=next_cursor, events=events)
        self.store.record_heartbeat(PROBE, "DEGRADED" if page["has_more"] else "HEALTHY",
                                    observed_at=observed,
                                    reason="INTENT_IMPORT_IN_PROGRESS" if page["has_more"] else "")
        return count


def smc_intent_history_view(store, *, after=0, now=None):
    health = component_health(store.heartbeats(), (PROBE,), now=now)["components"][PROBE]
    state = {"HEALTHY": "CAUGHT_UP_AT_LAST_POLL", "DEGRADED": "IMPORTING"}.get(health["state"], "UNKNOWN")
    cursor = store.observer_cursor(COMPONENT)
    return {**store.smc_intent_history_page(after=after), "source_cursor": {"after": cursor[0], "anchor": cursor[1]},
            "history_state": state, "probe_state": health["state"], "probe_reason": health["reason"],
            "scope": SCOPE, "observation_age_seconds": health.get("age_seconds"),
            "execution_integrity_verified": False, "full_lifecycle_verified": False,
            "source_history_immutable_verified": False}
