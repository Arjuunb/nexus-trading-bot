"""Resumable observation of retained fills, never execution or trade accounting.

A fill is not a position lifecycle, journal trade or verified net P&L. Source
rowids order ingestion, not market time. No trading database is opened here.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from .events import GuardianEvent
from .health import component_health
from .lab_execution_observer import _NoRedirect
from .lab_observer import _timestamp

LABS = {"PRICE_ACTION": "PA_LAB", "SMC": "SMC_LAB"}
COMPONENTS = {"PRICE_ACTION": "pa_fill_history", "SMC": "smc_fill_history"}
SCOPE = "RETAINED_ISOLATED_PAPER_FILLS"
PAGE_SIZE = 32
MAX_SEQUENCE = 2**63 - 1
TEXT_FIELDS = ("id", "order_id", "symbol", "side", "account_id", "execution_engine",
               "strategy", "strategy_version", "timeframe", "candle_id", "fill_key",
               "quote_event_id", "market_data_source")
TIME_FIELDS = ("timestamp", "signal_timestamp", "decision_timestamp", "order_timestamp", "fill_timestamp")
NUMBER_FIELDS = ("quantity", "price", "fee", "realized_pnl", "commission", "funding",
                 "stop_loss", "take_profit", "risk_amount")
FIELDS = TEXT_FIELDS + TIME_FIELDS + NUMBER_FIELDS


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def identity(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("Invalid paper fill identity")
    return value


def project_fill(row, lab, account_id):
    if not isinstance(row, dict) or set(row) != {"source_sequence", *FIELDS}:
        raise ValueError("Invalid paper fill fields")
    sequence = row["source_sequence"]
    if type(sequence) is not int or not 1 <= sequence <= MAX_SEQUENCE:
        raise ValueError("Invalid paper fill sequence")
    result = dict(row)
    for key in TEXT_FIELDS:
        # Legacy/manual and synthetic protective fills may have blank optional
        # provenance. Preserve it as recorded, without inventing a strategy.
        if row[key] is not None and (not isinstance(row[key], str) or len(row[key]) > 256):
            raise ValueError("Invalid paper fill metadata")
    for key in ("id", "order_id", "symbol"):
        identity(row[key])
    if (lab not in LABS or row["account_id"] != identity(account_id) or
            row["execution_engine"] != LABS[lab] or row["side"] not in {"buy", "sell"}):
        raise ValueError("Fill account or engine mismatch")
    for key in TIME_FIELDS:
        if row[key] is not None or key == "timestamp":
            try:
                result[key] = _timestamp(row[key]).isoformat()
            except OverflowError as exc:
                # UTC conversion can overflow for a syntactically valid date
                # at year 1/9999. Treat it as malformed source evidence (503).
                raise ValueError("Paper fill timestamp is out of range") from exc
    for key in NUMBER_FIELDS:
        value = row[key]
        if value is None and key not in {"quantity", "price", "fee", "realized_pnl"}:
            continue
        if type(value) not in (float, int) or not math.isfinite(value):
            raise ValueError("Invalid paper fill number")
        if key in {"quantity", "price", "stop_loss", "take_profit"} and value <= 0:
            raise ValueError("Invalid paper fill price or quantity")
        if key == "risk_amount" and value < 0:
            raise ValueError("Invalid paper fill risk amount")
        result[key] = float(value)
    return result


def cursor_anchor(lab, account_id, first, last):
    # Both ends include material fill content, not just an easily reused rowid.
    return digest(["lab-fill-cursor-v1", lab, account_id, first, last])


class GuardianLabFillHistory:
    def __init__(self, store, url, key, lab, *, fetch=None, clock=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"app", "localhost", "127.0.0.1", "::1"}
                or parsed.port != 8000 or parsed.path != "/guardian/lab-fills"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Fill history URL must be an internal read endpoint")
        if lab not in LABS or not isinstance(key, str) or len(key) < 24:
            raise ValueError("Fill history requires a lab and independent long key")
        self.store, self.url, self.key, self.lab = store, url, key, lab
        self.component = COMPONENTS[lab]
        self.probe = "guardian_" + self.component
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self, after, anchor):
        request = Request(self.url + "?" + urlencode({"lab": self.lab, "after": after, "anchor": anchor}),
                          headers={"X-Guardian-Observer-Key": self.key})
        with build_opener(_NoRedirect()).open(request, timeout=3) as response:
            if response.status != 200:
                raise ValueError("Fill history source unavailable")
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Fill history response exceeds bound")
        return json.loads(raw)

    def poll(self):
        try:
            return self._poll()
        except Exception:
            self.store.record_heartbeat(self.probe, "FAILED", observed_at=self.clock(),
                                        reason="FILL_HISTORY_UNAVAILABLE")
            raise

    def _poll(self):
        expected = self.store.observer_cursor(self.component)
        view = self.fetch(*expected)
        if (not isinstance(view, dict) or type(view.get("schema_version")) is not int or
                view["schema_version"] != 1 or view.get("scope") != SCOPE or
                view.get("execution_integrity_verified") is not False):
            raise ValueError("Invalid fill history contract")
        now, observed = self.clock(), _timestamp(view.get("observed_at"))
        if now.utcoffset() is None or not -5 <= (now - observed).total_seconds() <= 90:
            raise ValueError("Stale fill history observation")
        page = view.get("page")
        if (not isinstance(page, dict) or page.get("lab") != self.lab or
                page.get("account_type") != LABS[self.lab] or page.get("atomic_snapshot") is not True or
                type(page.get("after")) is not int or page["after"] != expected[0] or
                page.get("anchor") != expected[1] or type(page.get("has_more")) is not bool):
            raise ValueError("Invalid fill history page identity")
        account = identity(page.get("account_id"))
        rows = page.get("fills")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or (page["has_more"] and not rows):
            raise ValueError("Invalid fill history page bound")
        first = page.get("first_fill")
        first = project_fill(first, self.lab, account) if first is not None else None
        previous = page.get("previous_fill")
        if expected[0]:
            previous = project_fill(previous, self.lab, account)
            if (not first or previous["source_sequence"] != expected[0] or
                    first["source_sequence"] > expected[0] or
                    cursor_anchor(self.lab, account, first, previous) != expected[1]):
                raise ValueError("Fill history source cursor changed")
        elif previous is not None:
            raise ValueError("Unexpected previous fill")
        if bool(first) != bool(rows or expected[0]):
            raise ValueError("Fill history start identity missing")
        sequence, events, seen = expected[0], [], set()
        for row in rows:
            row = project_fill(row, self.lab, account)
            if row["source_sequence"] <= sequence or row["id"] in seen:
                raise ValueError("Fill history page is duplicated or unordered")
            if not events and not expected[0] and row != first:
                raise ValueError("Fill history does not start at retained origin")
            sequence = row["source_sequence"]
            seen.add(row["id"])
            event_time = _timestamp(row["timestamp"])
            if (event_time - observed).total_seconds() > 5:
                raise ValueError("Fill is ahead of observation")
            event = GuardianEvent(
                source_service=self.probe, source_component=self.component,
                event_type="lab_paper_fill_observed", timestamp=event_time,
                event_id=digest(["lab-fill-event-v1", self.lab, account, row["id"]]),
                lab_id=self.lab, symbol=row["symbol"], order_id=row["order_id"],
                strategy_id=row["strategy"], strategy_version=row["strategy_version"],
                timeframe=row["timeframe"], reason="RETAINED_PAPER_FILL_OBSERVED",
                evidence={"account_id": account, "account_type": LABS[self.lab], "fill": row,
                          "journal_trade_verified": False, "position_lifecycle_verified": False,
                          "execution_integrity_verified": False, "currency_verified": False},
                metadata={"coverage": SCOPE, "paper_only": True})
            event.canonical_json()
            events.append(event)
        next_cursor = ((sequence, cursor_anchor(self.lab, account, first,
                                               events[-1].evidence["fill"])) if rows else expected)
        if (type(page.get("next_after")) is not int or
                (page["next_after"], page.get("next_anchor")) != next_cursor):
            raise ValueError("Invalid fill history next cursor")
        count = self.store.append_observed_page(self.component, expected=expected,
                                                next_cursor=next_cursor, events=events)
        self.store.record_heartbeat(self.probe, "DEGRADED" if page["has_more"] else "HEALTHY",
                                    observed_at=observed,
                                    reason="FILL_IMPORT_IN_PROGRESS" if page["has_more"] else "")
        return count


def lab_fill_history_view(store, lab, *, after=0, now=None):
    if lab not in COMPONENTS or type(after) is not int or not 0 <= after <= MAX_SEQUENCE:
        raise ValueError("Invalid lab fill history request")
    probe = "guardian_" + COMPONENTS[lab]
    health = component_health(store.heartbeats(), (probe,), now=now)["components"][probe]
    state = {"HEALTHY": "CAUGHT_UP_AT_LAST_POLL", "DEGRADED": "IMPORTING"}.get(health["state"], "UNKNOWN")
    return {**store.lab_fill_history_page(probe, after=after), "lab": lab,
            "history_state": state, "probe_state": health["state"], "probe_reason": health["reason"],
            "observation_age_seconds": health.get("age_seconds"), "scope": SCOPE,
            "full_lifecycle_verified": False, "source_history_immutable_verified": False,
            "currency_verified": False, "net_pnl_verified": False,
            "execution_integrity_verified": False}
