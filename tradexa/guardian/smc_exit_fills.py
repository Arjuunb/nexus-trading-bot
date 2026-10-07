"""Retained SMC exit facts; never broker, journal or trading authority.

The read decoder mirrors the pure source v1 contract. The standalone Guardian
image never imports execution, services, the broker, or a source writer.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from .events import GuardianEvent, _safe_json
from .health import component_health
from .lab_execution_observer import _NoRedirect
from .lab_fill_history import digest
from .lab_observer import _timestamp
from .smc_intent_history import validate_cursor
from .smc_fill_positions import POSITION_FIELDS, number as _number, text as _text

text, number = _text, _number

COMPONENT = "smc_exit_fills"
PROBE = "guardian_smc_exit_fills"
SCOPE = "RETAINED_SMC_PAPER_EXIT_FILL_EVIDENCE"
PAYLOAD_SCOPE = "SMC_PAPER_EXIT_FILL"
PAGE_SIZE = 32
MAX_RESPONSE_BYTES = 384 * 1024
MAX_BYTES = 8192
KINDS = {"POSITION_STOP_LOSS", "POSITION_TAKE_PROFIT", "POSITION_TRAILING_STOP",
         "ORDER_TRAILING_STOP", "ORDER_REDUCE_ONLY", "NETTING_FILL",
         "LEGACY_POSITION_REMEDIATION", "PAPER_LIQUIDATION"}
PROTECTION_FIELDS = {"stop_loss", "take_profit", "trailing_offset", "peak_price"}
ORDER_FIELDS = {"type", "limit_price", "stop_price", "trailing_offset"}
OBSERVATION_FIELDS = {"timestamp", "quote_event_id", "open", "high", "low", "close", "bid", "ask"}
PAYLOAD_FIELDS = {"schema_version", "scope", "account_id", "fill_id", "order_id", "symbol", "side",
          "quantity", "closed_quantity", "price", "raw_reference_price", "reduce_only",
          "persisted_order", "position", "protection", "order", "trigger_kind", "trigger_price",
          "effective_stop", "effective_peak", "fill_source", "observation"}

FIELDS = {"source_sequence", "fill_id", "order_id", "timestamp", "symbol", "side", "quantity", "price", "exit_evidence", "capture_state"}


def decode_exit_fill(raw):
    """Strict read contract; cannot certify source history or current risk."""
    if not isinstance(raw, str):
        raise ValueError("Invalid exit provenance payload")
    try:
        if len(raw.encode("utf-8")) > MAX_BYTES:
            raise ValueError("Exit provenance exceeds bound")
        value = json.loads(raw, object_pairs_hook=_unique_fields)
    except (UnicodeError, RecursionError, ValueError) as exc:
        raise ValueError("Invalid exit provenance JSON") from exc
    if (not isinstance(value, dict) or set(value) != PAYLOAD_FIELDS or
            type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["scope"] != PAYLOAD_SCOPE):
        raise ValueError("Invalid exit provenance contract")
    for key in ("account_id", "fill_id", "order_id", "symbol"):
        _text(value[key])
    if (value["side"] not in ("buy", "sell") or
            any(type(value[key]) is not bool for key in ("reduce_only", "persisted_order"))):
        raise ValueError("Invalid exit provenance flags")
    for key in ("quantity", "closed_quantity", "price", "raw_reference_price"):
        _number(value[key])
    pos = value["position"]
    if not isinstance(pos, dict) or set(pos) != POSITION_FIELDS or pos["side"] not in ("long", "short"):
        raise ValueError("Invalid exit position")
    for key in ("position_id", "entry_order_id", "entry_execution_key", "entry_timeframe"):
        if pos[key] is not None:
            _text(pos[key])
    _number(pos["size"])
    _number(pos["entry_price"])
    if ((pos["side"] == "long") == (value["side"] == "buy") or
            value["closed_quantity"] != min(value["quantity"], pos["size"]) or
            (value["reduce_only"] and value["quantity"] > pos["size"])):
        raise ValueError("Exit quantity/direction contradicts original position")
    for group, fields in (("protection", PROTECTION_FIELDS), ("order", ORDER_FIELDS),
                          ("observation", OBSERVATION_FIELDS)):
        item = value[group]
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("Invalid exit evidence projection")
        for key in fields - {"type", "timestamp", "quote_event_id"}:
            if item[key] is not None:
                _number(item[key])
    if value["order"]["type"] not in ("market", "limit", "stop", "stop_limit", "trailing_stop"):
        raise ValueError("Invalid exit order type")
    for key in ("trigger_price", "effective_stop", "effective_peak"):
        if value[key] is not None:
            _number(value[key])
    stamp = value["observation"]["timestamp"]
    if stamp is not None:
        _text(stamp)
        try:
            if datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("Unidentified exit observation timezone")
        except ValueError as exc:
            raise ValueError("Invalid exit observation time") from exc
    quote_id = value["observation"]["quote_event_id"]
    if quote_id is not None:
        _text(quote_id)
    kind, source = value["trigger_kind"], value["fill_source"]
    if not isinstance(kind, str) or kind not in KINDS or source not in ("CANDLE", "TICK", "MARK"):
        raise ValueError("Invalid exit trigger/source")
    if kind == "NETTING_FILL":
        if value["reduce_only"] or not value["persisted_order"] or source == "MARK":
            raise ValueError("Netting fill is not a protective exit")
    elif not value["reduce_only"]:
        raise ValueError("Exit trigger requires a reduce-only fill")
    if kind.startswith("POSITION_"):
        if value["persisted_order"] or source == "MARK" or value["trigger_price"] is None:
            raise ValueError("Invalid position protection trigger")
        if kind == "POSITION_TAKE_PROFIT":
            if value["trigger_price"] != value["protection"]["take_profit"]:
                raise ValueError("Target trigger disagrees with protection snapshot")
        elif value["trigger_price"] != value["effective_stop"]:
            raise ValueError("Stop trigger disagrees with effective stop")
        if kind == "POSITION_STOP_LOSS" and value["effective_stop"] != value["protection"]["stop_loss"]:
            raise ValueError("Static stop trigger disagrees with stored stop")
        if kind == "POSITION_TRAILING_STOP" and (
                source != "CANDLE" or value["protection"]["trailing_offset"] is None or
                value["effective_peak"] is None):
            raise ValueError("Unidentified trailing stop")
        if kind == "POSITION_TRAILING_STOP":
            delta = value["protection"]["trailing_offset"]
            stored = value["protection"]["stop_loss"]
            effective = value["effective_peak"] + (-delta if pos["side"] == "long" else delta)
            effective = (max(stored, effective) if pos["side"] == "long" else min(stored, effective)) if stored is not None else effective
            if value["effective_stop"] != effective or value["effective_stop"] == stored:
                raise ValueError("Trailing trigger disagrees with recorded inputs")
    if kind.startswith("ORDER_") and (not value["persisted_order"] or source == "MARK"):
        raise ValueError("Invalid explicit order exit")
    if kind == "ORDER_TRAILING_STOP" and (
            source != "CANDLE" or value["order"]["type"] != "trailing_stop" or value["trigger_price"] is None):
        raise ValueError("Invalid explicit trailing order")
    if kind in ("PAPER_LIQUIDATION", "LEGACY_POSITION_REMEDIATION") and (
            value["persisted_order"] or source != "MARK"):
        raise ValueError("Invalid synthetic mark exit")
    return value


def _unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Ambiguous duplicate exit evidence field")
        value[key] = item
    return value


def project_exit_fill(row, account):
    text(account)
    if not isinstance(row, dict) or set(row) != FIELDS or type(row["source_sequence"]) is not int or not 1 <= row["source_sequence"] <= 2**63-1:
        raise ValueError("Invalid exit-fill row")
    for key in ("fill_id", "order_id", "symbol"):
        text(row[key])
    if row["side"] not in ("buy", "sell"):
        raise ValueError("Invalid exit-fill side")
    number(row["quantity"])
    number(row["price"])
    result = dict(row)
    result["timestamp"] = _timestamp(row["timestamp"]).isoformat()
    value = row["exit_evidence"]
    if value is None:
        if row["capture_state"] != "UNVERIFIED_NO_EXIT_CAPTURE":
            raise ValueError("Missing exit evidence")
        return _safe_json(result)
    if row["capture_state"] != "RECORDED_SOURCE_EXIT":
        raise ValueError("Invalid exit capture state")
    value = decode_exit_fill(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))
    if value["account_id"] != account or any(value[k] != row[k] for k in ("fill_id", "order_id", "symbol", "side", "quantity", "price")):
        raise ValueError("Exit evidence disagrees with fill")
    result["exit_evidence"] = value
    return _safe_json(result)


def cursor_anchor(account, first, previous):
    return digest(["smc-exit-fill-cursor-v1", account, first, previous])


def project_event(event):
    _safe_json(event)
    GuardianEvent.from_payload({k: v for k, v in event.items()
                                if k not in ("guardian_sequence", "received_at")})
    evidence = event.get("evidence", {})
    metadata = event.get("metadata", {})
    if (not isinstance(evidence, dict) or not isinstance(metadata, dict) or
            set(evidence) != {"account_id", "account_type", "fill"} or
            set(metadata) != {"coverage", "paper_only", "full_lifecycle_verified"} or
            type(event.get("schema_version")) is not int or event["schema_version"] != 1 or
            event.get("source_service") != PROBE or event.get("source_component") != COMPONENT or
            event.get("event_type") != "smc_exit_evidence_observed" or event.get("lab_id") != "SMC" or
            event.get("reason") != "RECORDED_PAPER_EXIT_EVIDENCE_ONLY" or
            evidence.get("account_type") != "SMC_LAB" or metadata.get("coverage") != SCOPE or
            metadata.get("paper_only") is not True or metadata.get("full_lifecycle_verified") is not False):
        raise ValueError("Invalid retained exit-fill event")
    if event.get("severity") != "INFO" or any(event.get(k) is not None for k in (
            "execution_id", "position_id", "session_id", "decision", "state_before", "state_after",
            "timeframe", "agent_id", "correlation_id", "instance_id", "strategy_id", "strategy_version", "latency_ms")):
        raise ValueError("Retained position snapshot cannot assert execution state")
    row = project_exit_fill(evidence.get("fill"), evidence.get("account_id"))
    if (event.get("order_id") != row["order_id"] or event.get("symbol") != row["symbol"] or
            _timestamp(event.get("timestamp")) != _timestamp(row["timestamp"]) or
            event.get("event_id") != digest(["smc-exit-fill-event-v1", evidence["account_id"], row["fill_id"]])):
        raise ValueError("Retained exit-fill envelope identity mismatch")
    return row


class GuardianSMCExitFills:
    def __init__(self, store, url, key, *, fetch=None, clock=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"app", "localhost", "127.0.0.1", "::1"} or
                parsed.port != 8000 or parsed.path != "/guardian/smc-exit-fills" or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Exit-fill URL must be an internal read endpoint")
        if not isinstance(key, str) or len(key) < 24:
            raise ValueError("Exit-fill import requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.component, self.probe = COMPONENT, PROBE
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self, after, anchor):
        request = Request(self.url + "?" + urlencode({"after": after, "anchor": anchor}),
                          headers={"X-Guardian-Observer-Key": self.key})
        with build_opener(_NoRedirect()).open(request, timeout=3) as response:
            if response.status != 200:
                raise ValueError("Exit-fill source unavailable")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Exit-fill response exceeds bound")
        return json.loads(raw, object_pairs_hook=_unique_fields)

    def poll(self):
        try:
            return self._poll()
        except Exception:
            self.store.record_heartbeat(PROBE, "FAILED", observed_at=self.clock(), reason="EXIT_FILL_SOURCE_UNAVAILABLE")
            raise

    def _poll(self):
        expected = self.store.observer_cursor(COMPONENT)
        validate_cursor(*expected)
        view = self.fetch(*expected)
        if len(json.dumps(view, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
            raise ValueError("Exit-fill response exceeds bound")
        if (not isinstance(view, dict) or set(view) != {"schema_version", "scope", "observed_at", "execution_integrity_verified", "page"} or
                type(view.get("schema_version")) is not int or view["schema_version"] != 1 or
                view.get("scope") != SCOPE or view.get("execution_integrity_verified") is not False):
            raise ValueError("Invalid exit-fill response")
        observed, now = _timestamp(view.get("observed_at")), self.clock()
        if now.utcoffset() is None or not -5 <= (now-observed).total_seconds() <= 90:
            raise ValueError("Stale exit-fill observation")
        page = view.get("page")
        if (not isinstance(page, dict) or set(page) != {"account_id", "account_type", "atomic_snapshot", "source_capture_post_install_only", "full_lifecycle_verified", "after", "anchor", "first_fill", "previous_fill", "fills", "has_more", "next_after", "next_anchor"} or
                page.get("account_type") != "SMC_LAB" or page.get("atomic_snapshot") is not True or
                page.get("full_lifecycle_verified") is not False or page.get("source_capture_post_install_only") is not True or
                type(page.get("after")) is not int or page["after"] != expected[0] or page.get("anchor") != expected[1] or
                type(page.get("has_more")) is not bool):
            raise ValueError("Invalid exit-fill page")
        account = text(page.get("account_id"))
        rows = page.get("fills")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or (page["has_more"] and not rows):
            raise ValueError("Invalid exit-fill page bound")
        first = project_exit_fill(page["first_fill"], account) if page.get("first_fill") is not None else None
        previous = page.get("previous_fill")
        if expected[0]:
            previous = project_exit_fill(previous, account)
            if (not first or first["source_sequence"] > expected[0] or previous["source_sequence"] != expected[0] or
                    cursor_anchor(account, first, previous) != expected[1]):
                raise ValueError("Exit-fill source cursor changed")
        elif previous is not None:
            raise ValueError("Unexpected exit-fill predecessor")
        if bool(first) != bool(rows or expected[0]):
            raise ValueError("Exit-fill source origin missing")
        sequence, events, seen, last = expected[0], [], set(), None
        for item in rows:
            row = project_exit_fill(item, account)
            if row["source_sequence"] <= sequence or row["fill_id"] in seen or (not events and not expected[0] and row != first):
                raise ValueError("Exit-fill page unordered or duplicated")
            sequence, last = row["source_sequence"], row
            seen.add(row["fill_id"])
            when = _timestamp(row["timestamp"])
            if (when-observed).total_seconds() > 5:
                raise ValueError("Fill ahead of observation")
            event = GuardianEvent(source_service=PROBE, source_component=COMPONENT,
                event_type="smc_exit_evidence_observed", timestamp=when,
                event_id=digest(["smc-exit-fill-event-v1", account, row["fill_id"]]),
                lab_id="SMC", symbol=row["symbol"], order_id=row["order_id"],
                reason="RECORDED_PAPER_EXIT_EVIDENCE_ONLY",
                evidence={"account_id": account, "account_type": "SMC_LAB", "fill": row},
                metadata={"coverage": SCOPE, "paper_only": True, "full_lifecycle_verified": False})
            event.canonical_json()
            events.append(event)
        next_cursor = (sequence, cursor_anchor(account, first, last)) if events else expected
        if type(page.get("next_after")) is not int or (page["next_after"], page.get("next_anchor")) != next_cursor:
            raise ValueError("Invalid exit-fill next checkpoint")
        count = self.store.append_observed_page(COMPONENT, expected=expected, next_cursor=next_cursor, events=events)
        self.store.record_heartbeat(PROBE, "DEGRADED" if page["has_more"] else "HEALTHY", observed_at=observed,
                                    reason="EXIT_FILL_IMPORT_IN_PROGRESS" if page["has_more"] else "")
        return count


def smc_exit_fills_view(store, *, after=0, now=None):
    page = store.smc_exit_fills_page(after=after)
    for event in page["events"]:
        project_event(event)
    health = component_health(page.pop("heartbeats"), (PROBE,), now=now)["components"][PROBE]
    state = {"HEALTHY": "CAUGHT_UP_AT_LAST_POLL", "DEGRADED": "IMPORTING"}.get(health["state"], "UNKNOWN")
    return {**page, "scope": SCOPE, "history_state": state, "probe_state": health["state"],
            "probe_reason": health["reason"], "observation_age_seconds": health.get("age_seconds"),
            "execution_integrity_verified": False, "full_lifecycle_verified": False,
            "source_history_immutable_verified": False, "position_lifecycle_verified": False,
            "journal_close_verified": False, "current_protection_verified": False,
            "paper_account_binding_verified": False, "net_pnl_verified": False, "currency_binding_verified": False}
