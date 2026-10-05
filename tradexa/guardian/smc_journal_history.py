"""Bounded repeat scans of closed Agent journal rows, not broker accounting.

Trade rowids order scanning, not closure time. Every finite pass freezes its
upper rowid and the next pass starts at zero, so older open rows that later
close are eventually revisited. Checkpoints and evidence live only in Guardian.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from .events import GuardianEvent, _safe_json
from .health import component_health
from .lab_execution_observer import _NoRedirect
from .lab_fill_history import digest, identity
from .lab_observer import _timestamp

COMPONENT = "smc_journal_history"
PROBE = "guardian_smc_journal_history"
SCOPE = "CYCLIC_RETAINED_SMC_AGENT_CLOSED_TRADES"
PAGE_SIZE = 32
MAX_SEQUENCE = 2**63 - 1
INITIAL_CURSOR = {"cycle": 0, "after": 0, "upper": 0, "origin": "", "anchor": ""}
TEXT_FIELDS = ("id", "decision_id", "symbol", "timeframe", "direction", "setup_id",
               "proposal_id", "strategy_fingerprint", "order_id", "result", "close_reason")
TIME_FIELDS = ("opened_at", "closed_at")
NUMBER_FIELDS = ("entry", "stop", "target", "planned_rr", "size", "requested_size",
                 "risk_amount", "exit_price", "realised_r")
FIELDS = TEXT_FIELDS + TIME_FIELDS + NUMBER_FIELDS + ("size_capped",)
# Close fields MUST NOT enter cursor anchors: an older row can legitimately close.
ANCHOR_FIELDS = ("source_sequence", "id", "direction", "entry", "stop", "target", "planned_rr", "size")


def validate_cursor(cursor):
    if not isinstance(cursor, dict) or set(cursor) != set(INITIAL_CURSOR):
        raise ValueError("Invalid journal scan cursor")
    if any(type(cursor[k]) is not int or not 0 <= cursor[k] <= MAX_SEQUENCE
           for k in ("cycle", "after", "upper")):
        raise ValueError("Invalid journal scan sequence")
    if any(not isinstance(cursor[k], str) or
           (cursor[k] and not re.fullmatch(r"[0-9a-f]{64}", cursor[k])) for k in ("origin", "anchor")):
        raise ValueError("Invalid journal scan anchor")
    if (bool(cursor["after"]) != bool(cursor["upper"]) or
            bool(cursor["after"]) != bool(cursor["anchor"]) or
            (cursor["after"] and not 0 < cursor["after"] < cursor["upper"]) or
            (cursor["after"] and not cursor["origin"])):
        raise ValueError("Invalid journal scan boundary")
    return dict(cursor)


def project_trade(row):
    if not isinstance(row, dict) or set(row) != {"source_sequence", *FIELDS}:
        raise ValueError("Invalid journal trade projection")
    if type(row["source_sequence"]) is not int or not 1 <= row["source_sequence"] <= MAX_SEQUENCE:
        raise ValueError("Invalid journal row sequence")
    result = dict(row)
    for key in TEXT_FIELDS:
        if row[key] is not None and (not isinstance(row[key], str) or len(row[key]) > 256):
            raise ValueError("Invalid journal trade metadata")
    for key in ("id", "decision_id", "symbol", "timeframe"):
        identity(row[key])
    if row["direction"] not in {"long", "short"} or type(row["size_capped"]) is not int or row["size_capped"] not in {0, 1}:
        raise ValueError("Invalid journal trade direction or sizing flag")
    for key in TIME_FIELDS:
        if row[key] is not None or key == "opened_at":
            try:
                result[key] = _timestamp(row[key]).isoformat()
            except OverflowError as exc:
                raise ValueError("Journal timestamp out of range") from exc
    for key in NUMBER_FIELDS:
        if row[key] is None and key in {"requested_size", "risk_amount", "exit_price", "realised_r"}:
            continue
        if type(row[key]) not in (int, float) or not math.isfinite(row[key]):
            raise ValueError("Invalid journal trade number")
        if key not in {"realised_r", "risk_amount"} and row[key] <= 0:
            raise ValueError("Invalid journal trade price or size")
        if key == "risk_amount" and row[key] < 0:
            raise ValueError("Invalid journal risk amount")
        result[key] = float(row[key])
    if result["closed_at"] is not None:
        if (_timestamp(result["closed_at"]) < _timestamp(result["opened_at"]) or
                result["exit_price"] is None or result["realised_r"] is None):
            raise ValueError("Incomplete or reversed journal close")
        identity(result["result"])
        identity(result["close_reason"])
    return _safe_json(result)


def trade_anchor(row):
    return {k: row[k] for k in ANCHOR_FIELDS}


def source_origin(first):
    return digest(["smc-journal-origin-v1", trade_anchor(first)]) if first else ""


def scan_anchor(origin, upper, previous):
    return digest(["smc-journal-scan-v1", origin, trade_anchor(upper), trade_anchor(previous)])


def next_scan_cursor(cursor, origin, upper, rows, has_more):
    if has_more:
        if not rows or not upper:
            raise ValueError("Journal scan cannot advance without rows")
        return validate_cursor({"cycle": cursor["cycle"], "after": rows[-1]["source_sequence"],
                                "upper": upper["source_sequence"], "origin": origin,
                                "anchor": scan_anchor(origin, upper, rows[-1])})
    return validate_cursor({"cycle": cursor["cycle"] + 1, "after": 0, "upper": 0,
                            "origin": origin, "anchor": ""})


class GuardianSMCJournalHistory:
    def __init__(self, store, url, key, *, fetch=None, clock=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"app", "localhost", "127.0.0.1", "::1"}
                or parsed.port != 8000 or parsed.path != "/guardian/smc-journal"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Journal history URL must be an internal read endpoint")
        if not isinstance(key, str) or len(key) < 24:
            raise ValueError("Journal history requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.component, self.probe = COMPONENT, PROBE
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self, cursor):
        import json
        request = Request(self.url + "?" + urlencode(cursor), headers={"X-Guardian-Observer-Key": self.key})
        with build_opener(_NoRedirect()).open(request, timeout=3) as response:
            if response.status != 200:
                raise ValueError("Journal source unavailable")
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Journal response exceeds bound")
        return json.loads(raw)

    def poll(self):
        try:
            return self._poll()
        except Exception:
            self.store.record_heartbeat(PROBE, "FAILED", observed_at=self.clock(),
                                        reason="JOURNAL_HISTORY_UNAVAILABLE")
            raise

    def _poll(self):
        expected = self.store.observer_scan_cursor(COMPONENT)
        view = self.fetch(expected)
        if (not isinstance(view, dict) or type(view.get("schema_version")) is not int or
                view["schema_version"] != 1 or view.get("scope") != SCOPE or
                view.get("execution_integrity_verified") is not False):
            raise ValueError("Invalid journal history contract")
        observed, now = _timestamp(view.get("observed_at")), self.clock()
        if now.utcoffset() is None or not -5 <= (now - observed).total_seconds() <= 90:
            raise ValueError("Stale journal history observation")
        page = view.get("page")
        if (not isinstance(page, dict) or validate_cursor(page.get("cursor")) != expected or
                page.get("atomic_snapshot") is not True or type(page.get("has_more")) is not bool):
            raise ValueError("Invalid journal history page")
        raw = page.get("trades")
        if not isinstance(raw, list) or len(raw) > PAGE_SIZE or (page["has_more"] and not raw):
            raise ValueError("Journal history page exceeds bound")
        first = project_trade(page["first_trade"]) if page.get("first_trade") is not None else None
        upper = project_trade(page["upper_trade"]) if page.get("upper_trade") is not None else None
        origin = source_origin(first)
        if page.get("origin") != origin or (expected["origin"] and origin != expected["origin"]):
            raise ValueError("Journal source origin changed")
        if bool(first) != bool(upper) or (first and first["source_sequence"] > upper["source_sequence"]):
            raise ValueError("Journal scan boundary missing")
        previous = page.get("previous_trade")
        if expected["after"]:
            previous = project_trade(previous)
            if (not upper or upper["source_sequence"] != expected["upper"] or
                    previous["source_sequence"] != expected["after"] or
                    scan_anchor(origin, upper, previous) != expected["anchor"]):
                raise ValueError("Journal scan anchor changed")
        elif previous is not None:
            raise ValueError("Unexpected journal predecessor")
        sequence, rows, events, seen = expected["after"], [], [], set()
        for item in raw:
            row = project_trade(item)
            if (not upper or not sequence < row["source_sequence"] <= upper["source_sequence"] or
                    row["id"] in seen or (not rows and not sequence and row != first) or
                    (row["source_sequence"] == upper["source_sequence"] and row != upper)):
                raise ValueError("Journal page unordered or duplicated")
            sequence = row["source_sequence"]
            rows.append(row)
            seen.add(row["id"])
            if row["closed_at"] is None:
                continue
            when = _timestamp(row["closed_at"])
            if (when - observed).total_seconds() > 5:
                raise ValueError("Journal close ahead of observation")
            event = GuardianEvent(
                source_service=PROBE, source_component=COMPONENT,
                event_type="smc_closed_journal_observed", timestamp=when,
                event_id=digest(["smc-closed-journal-v1", origin, row["id"]]),
                lab_id="SMC", agent_id="smc_agent", symbol=row["symbol"], timeframe=row["timeframe"],
                order_id=row["order_id"], correlation_id=row["decision_id"], state_after="JOURNAL_CLOSED",
                reason="RECORDED_JOURNAL_CLOSE_ONLY",
                evidence={"trade": row, "journal_origin": origin, "broker_execution_verified": False,
                          "position_lifecycle_verified": False, "net_pnl_verified": False,
                          "currency_verified": False}, metadata={"coverage": SCOPE, "paper_only": True})
            event.canonical_json()
            events.append(event)
        if (bool(rows) != bool(upper) or
                (not page["has_more"] and upper and sequence != upper["source_sequence"])):
            raise ValueError("Journal scan incomplete boundary")
        next_cursor = next_scan_cursor(expected, origin, upper, rows, page["has_more"])
        if validate_cursor(page.get("next_cursor")) != next_cursor:
            raise ValueError("Invalid journal next checkpoint")
        count = self.store.append_observed_scan_page(COMPONENT, expected=expected,
                                                     next_cursor=next_cursor, events=events)
        self.store.record_heartbeat(PROBE, "DEGRADED" if page["has_more"] else "HEALTHY",
                                    observed_at=observed,
                                    reason="JOURNAL_PASS_IN_PROGRESS" if page["has_more"] else "JOURNAL_PASS_FINISHED")
        return count


def smc_journal_history_view(store, *, after=0, now=None):
    health = component_health(store.heartbeats(), (PROBE,), now=now)["components"][PROBE]
    state = {"HEALTHY": "PASS_COMPLETED_AT_LAST_POLL", "DEGRADED": "SCANNING"}.get(health["state"], "UNKNOWN")
    return {**store.smc_journal_history_page(after=after), "scan_cursor": store.observer_scan_cursor(COMPONENT),
            "history_state": state, "probe_state": health["state"], "probe_reason": health["reason"],
            "scope": SCOPE, "observation_age_seconds": health.get("age_seconds"),
            "whole_scan_atomic": False, "full_lifecycle_verified": False,
            "execution_integrity_verified": False, "net_pnl_verified": False,
            "currency_verified": False, "source_history_immutable_verified": False}
