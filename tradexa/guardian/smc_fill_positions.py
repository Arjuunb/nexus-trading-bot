"""Retained source fill-position evidence; no trading imports or reconciliation.

The v1 payload validator mirrors the source's pure contract. Guardian's image
contains only tradexa/guardian: importing a broker or source writer is forbidden.
"""
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

COMPONENT = "smc_fill_positions"
PROBE = "guardian_smc_fill_positions"
SCOPE = "RETAINED_SMC_PAPER_FILL_POSITION_TRANSITIONS"
PAYLOAD_SCOPE = "SMC_PAPER_FILL_POSITION_TRANSITION"
PAGE_SIZE = 32
MAX_RESPONSE_BYTES = 384 * 1024
POSITION_FIELDS = {"position_id", "entry_order_id", "entry_execution_key", "entry_timeframe", "side", "size", "entry_price"}
PAYLOAD_FIELDS = {"schema_version", "scope", "account_id", "fill_id", "order_id", "symbol", "side",
                  "quantity", "price", "reduce_only", "persisted_order", "before", "after", "effect"}
FIELDS = {"source_sequence", "fill_id", "order_id", "timestamp", "symbol", "side", "quantity", "price", "transition", "capture_state"}


def text(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise ValueError("Invalid fill-position identity")
    value.encode("utf-8")
    return value


def number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Invalid fill-position number")


def effect(before, after):
    if before is None:
        return "OPEN" if after is not None else "UNCHANGED"
    if after is None:
        return "CLOSE"
    if before["position_id"] != after["position_id"] or before["side"] != after["side"]:
        return "REVERSE"
    return "INCREASE" if after["size"] > before["size"] else "REDUCE" if after["size"] < before["size"] else "UNCHANGED"


def project_fill_position(row, account):
    text(account)
    if not isinstance(row, dict) or set(row) != FIELDS or type(row["source_sequence"]) is not int or not 1 <= row["source_sequence"] <= 2**63-1:
        raise ValueError("Invalid fill-position row")
    for key in ("fill_id", "order_id", "symbol"):
        text(row[key])
    if row["side"] not in {"buy", "sell"}:
        raise ValueError("Invalid fill-position side")
    number(row["quantity"])
    number(row["price"])
    result = dict(row)
    result["timestamp"] = _timestamp(row["timestamp"]).isoformat()
    value = row["transition"]
    if value is None:
        if row["capture_state"] != "UNVERIFIED_LEGACY_FILL":
            raise ValueError("Missing fill-position evidence")
        return _safe_json(result)
    if (row["capture_state"] != "RECORDED_SOURCE_TRANSITION" or not isinstance(value, dict) or set(value) != PAYLOAD_FIELDS or
            type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["scope"] != PAYLOAD_SCOPE):
        raise ValueError("Invalid source transition contract")
    if len(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()) > 8192:
        raise ValueError("Fill-position payload exceeds bound")
    for key in ("account_id", "fill_id", "order_id", "symbol"):
        text(value[key])
    for key in ("quantity", "price"):
        number(value[key])
    if value["account_id"] != account or any(value[k] != row[k] for k in ("fill_id", "order_id", "symbol", "side", "quantity", "price")):
        raise ValueError("Fill-position identity disagrees with fill")
    if any(type(value[k]) is not bool for k in ("reduce_only", "persisted_order")):
        raise ValueError("Invalid fill-position flags")
    for key in ("before", "after"):
        position = value[key]
        if position is None:
            continue
        if not isinstance(position, dict) or set(position) != POSITION_FIELDS or position["side"] not in {"long", "short"}:
            raise ValueError("Invalid position snapshot")
        for field in ("position_id", "entry_order_id", "entry_execution_key", "entry_timeframe"):
            if position[field] is not None:
                text(position[field])
        number(position["size"])
        number(position["entry_price"])
    if value["effect"] != effect(value["before"], value["after"]):
        raise ValueError("Position effect disagrees with snapshots")
    return _safe_json(result)


def cursor_anchor(account, first, previous):
    return digest(["smc-fill-position-cursor-v1", account, first, previous])


def project_event(event):
    _safe_json(event)
    evidence = event.get("evidence", {})
    metadata = event.get("metadata", {})
    if (not isinstance(evidence, dict) or not isinstance(metadata, dict) or
            set(evidence) != {"account_id", "account_type", "fill"} or
            set(metadata) != {"coverage", "paper_only", "full_lifecycle_verified"} or
            type(event.get("schema_version")) is not int or event["schema_version"] != 1 or
            event.get("source_service") != PROBE or event.get("source_component") != COMPONENT or
            event.get("event_type") != "smc_fill_position_observed" or event.get("lab_id") != "SMC" or
            event.get("reason") != "RECORDED_PAPER_POSITION_TRANSITION_ONLY" or
            evidence.get("account_type") != "SMC_LAB" or metadata.get("coverage") != SCOPE or
            metadata.get("paper_only") is not True or metadata.get("full_lifecycle_verified") is not False):
        raise ValueError("Invalid retained fill-position event")
    if any(event.get(k) is not None for k in ("execution_id", "position_id", "session_id", "decision", "state_before", "state_after")):
        raise ValueError("Retained position snapshot cannot assert execution state")
    row = project_fill_position(evidence.get("fill"), evidence.get("account_id"))
    if (event.get("order_id") != row["order_id"] or event.get("symbol") != row["symbol"] or
            _timestamp(event.get("timestamp")) != _timestamp(row["timestamp"]) or
            event.get("event_id") != digest(["smc-fill-position-event-v1", evidence["account_id"], row["fill_id"]])):
        raise ValueError("Retained fill-position envelope identity mismatch")
    return row


class GuardianSMCFillPositions:
    def __init__(self, store, url, key, *, fetch=None, clock=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"app", "localhost", "127.0.0.1", "::1"} or
                parsed.port != 8000 or parsed.path != "/guardian/smc-fill-transitions" or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Fill-position URL must be an internal read endpoint")
        if not isinstance(key, str) or len(key) < 24:
            raise ValueError("Fill-position import requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.component, self.probe = COMPONENT, PROBE
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self, after, anchor):
        request = Request(self.url + "?" + urlencode({"after": after, "anchor": anchor}),
                          headers={"X-Guardian-Observer-Key": self.key})
        with build_opener(_NoRedirect()).open(request, timeout=3) as response:
            if response.status != 200:
                raise ValueError("Fill-position source unavailable")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Fill-position response exceeds bound")
        return json.loads(raw)

    def poll(self):
        try:
            return self._poll()
        except Exception:
            self.store.record_heartbeat(PROBE, "FAILED", observed_at=self.clock(), reason="FILL_POSITION_SOURCE_UNAVAILABLE")
            raise

    def _poll(self):
        expected = self.store.observer_cursor(COMPONENT)
        validate_cursor(*expected)
        view = self.fetch(*expected)
        if len(json.dumps(view, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            raise ValueError("Fill-position response exceeds bound")
        if (not isinstance(view, dict) or type(view.get("schema_version")) is not int or view["schema_version"] != 1 or
                view.get("scope") != SCOPE or view.get("execution_integrity_verified") is not False):
            raise ValueError("Invalid fill-position response")
        observed, now = _timestamp(view.get("observed_at")), self.clock()
        if now.utcoffset() is None or not -5 <= (now-observed).total_seconds() <= 90:
            raise ValueError("Stale fill-position observation")
        page = view.get("page")
        if (not isinstance(page, dict) or page.get("account_type") != "SMC_LAB" or page.get("atomic_snapshot") is not True or
                page.get("full_lifecycle_verified") is not False or page.get("source_capture_post_install_only") is not True or
                type(page.get("after")) is not int or page["after"] != expected[0] or page.get("anchor") != expected[1] or
                type(page.get("has_more")) is not bool):
            raise ValueError("Invalid fill-position page")
        account = text(page.get("account_id"))
        rows = page.get("fills")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or (page["has_more"] and not rows):
            raise ValueError("Invalid fill-position page bound")
        first = project_fill_position(page["first_fill"], account) if page.get("first_fill") is not None else None
        previous = page.get("previous_fill")
        if expected[0]:
            previous = project_fill_position(previous, account)
            if (not first or first["source_sequence"] > expected[0] or previous["source_sequence"] != expected[0] or
                    cursor_anchor(account, first, previous) != expected[1]):
                raise ValueError("Fill-position source cursor changed")
        elif previous is not None:
            raise ValueError("Unexpected fill-position predecessor")
        if bool(first) != bool(rows or expected[0]):
            raise ValueError("Fill-position source origin missing")
        sequence, events, seen, last = expected[0], [], set(), None
        for item in rows:
            row = project_fill_position(item, account)
            if row["source_sequence"] <= sequence or row["fill_id"] in seen or (not events and not expected[0] and row != first):
                raise ValueError("Fill-position page unordered or duplicated")
            sequence, last = row["source_sequence"], row
            seen.add(row["fill_id"])
            when = _timestamp(row["timestamp"])
            if (when-observed).total_seconds() > 5:
                raise ValueError("Fill ahead of observation")
            event = GuardianEvent(source_service=PROBE, source_component=COMPONENT,
                event_type="smc_fill_position_observed", timestamp=when,
                event_id=digest(["smc-fill-position-event-v1", account, row["fill_id"]]),
                lab_id="SMC", symbol=row["symbol"], order_id=row["order_id"],
                reason="RECORDED_PAPER_POSITION_TRANSITION_ONLY",
                evidence={"account_id": account, "account_type": "SMC_LAB", "fill": row},
                metadata={"coverage": SCOPE, "paper_only": True, "full_lifecycle_verified": False})
            event.canonical_json()
            events.append(event)
        next_cursor = (sequence, cursor_anchor(account, first, last)) if events else expected
        if type(page.get("next_after")) is not int or (page["next_after"], page.get("next_anchor")) != next_cursor:
            raise ValueError("Invalid fill-position next checkpoint")
        count = self.store.append_observed_page(COMPONENT, expected=expected, next_cursor=next_cursor, events=events)
        self.store.record_heartbeat(PROBE, "DEGRADED" if page["has_more"] else "HEALTHY", observed_at=observed,
                                    reason="FILL_POSITION_IMPORT_IN_PROGRESS" if page["has_more"] else "")
        return count


def smc_fill_positions_view(store, *, after=0, now=None):
    page = store.smc_fill_positions_page(after=after)
    for event in page["events"]:
        project_event(event)
    health = component_health(page.pop("heartbeats"), (PROBE,), now=now)["components"][PROBE]
    state = {"HEALTHY": "CAUGHT_UP_AT_LAST_POLL", "DEGRADED": "IMPORTING"}.get(health["state"], "UNKNOWN")
    return {**page, "scope": SCOPE, "history_state": state, "probe_state": health["state"],
            "probe_reason": health["reason"], "observation_age_seconds": health.get("age_seconds"),
            "execution_integrity_verified": False, "full_lifecycle_verified": False,
            "source_history_immutable_verified": False, "position_lifecycle_verified": False}
