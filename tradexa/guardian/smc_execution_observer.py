"""Independent observation of SMC Agent paper execution relationships."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .events import GuardianEvent
from .lab_observer import _timestamp
from .lab_execution_observer import _NoRedirect
from .store import GuardianStore

_MAX_RESPONSE_BYTES = 1024 * 1024
COVERAGE = "ALL_OUTSTANDING_AND_OPEN_JOURNAL_PLUS_RECENT_TERMINAL"
_CODES = frozenset({
    "CONSISTENT", "PENDING", "EXECUTION_UNCERTAIN", "ORDER_ID_NOT_FOUND",
    "ORDER_IDENTITY_MISMATCH", "TRADE_ID_NOT_FOUND", "TRADE_IDENTITY_MISMATCH",
    "ENTRY_MARKED_REDUCE_ONLY", "FAILED_INTENT_WITH_ORDER_ID",
    "ORDER_ID_UNRECORDED", "FILLED_ORDER_JOURNAL_PENDING",
    "COMPLETE_INTENT_TRADE_UNRECORDED", "OPEN_TRADE_POSITION_UNVERIFIED",
    "BROKER_ORDER_UNRECORDED", "DUPLICATE_EXECUTION_KEY",
    "AGENT_TRADE_PRECEDES_FILL", "ORDER_AWAITING_FILL",
    "JOURNAL_SIZE_EXCEEDS_BROKER_FILL",
    "MULTIPLE_INTENTS_FOR_TRADE", "POSITION_SIDE_MISMATCH",
    "CLOSED_TRADE_POSITION_STILL_OPEN",
})
_LINK_CODES = {0: "JOURNAL_INTENT_NOT_FOUND", 1: "INTENT_LINK_FOUND"}
_STATES = {"DECISION_APPROVED", "EXECUTION_PENDING", "EXECUTED", "RECONCILED",
           "COMPLETE", "EXECUTION_FAILED", "EXECUTION_UNCERTAIN"}


def _text(value, *, optional=False):
    if optional and value in (None, ""):
        return value
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise ValueError("SMC observation identity is invalid")
    return value


def _count(value, bound):
    if type(value) is not int or not 0 <= value <= bound:
        raise ValueError("SMC observation count is invalid")
    return value


def _quantity(value, *, optional=False):
    if optional and value is None:
        return value
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("SMC observation quantity is invalid")
    return value


class GuardianSMCExecutionObserver:
    """A probe, not a trading authority or a confirmed cross-DB reconciler."""

    def __init__(self, store: GuardianStore, url: str, key: str, *,
                 timeout_s: float = 3.0, fetch: Callable[[], dict] | None = None,
                 clock: Callable[[], datetime] | None = None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in
                {"app", "localhost", "127.0.0.1", "::1"} or
                parsed.port != 8000 or parsed.path != "/guardian/smc-execution" or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("SMC execution observer URL must be internal")
        if not isinstance(key, str) or len(key) < 24 or timeout_s <= 0:
            raise ValueError("SMC execution observer requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.timeout_s = timeout_s
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self) -> dict:
        request = Request(self.url, headers={"X-Guardian-Observer-Key": self.key})
        with build_opener(_NoRedirect()).open(request, timeout=self.timeout_s) as response:
            if response.status != 200:
                raise ValueError("SMC execution observer returned a non-success response")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("SMC execution observer response exceeds size limit")
        view = json.loads(raw)
        if not isinstance(view, dict):
            raise ValueError("SMC execution observer response must be an object")
        return view

    def poll(self) -> int:
        try:
            return self._poll()
        except Exception:
            self.store.record_heartbeat("guardian_smc_execution_probe", "FAILED",
                                        observed_at=self.clock(),
                                        reason="SMC_EXECUTION_EVIDENCE_UNAVAILABLE")
            raise

    def _poll(self) -> int:
        view = self.fetch()
        if (not isinstance(view, dict) or view.get("schema_version") != 2 or
                view.get("scope") != "SMC_AGENT_PAPER_ONLY" or
                view.get("coverage") != COVERAGE or
                view.get("cross_database_atomic") is not False or
                view.get("broker_account_type") != "SMC_LAB" or
                view.get("feed_health_verified") is not False or
                view.get("execution_integrity_verified") is not False):
            raise ValueError("SMC execution observation contract is invalid")
        observed = _timestamp(view.get("observed_at"))
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None or \
                not -5 <= (now.astimezone(timezone.utc) - observed).total_seconds() <= 90:
            raise ValueError("SMC execution observation is stale or ahead of clock")
        account_id = _text(view.get("broker_account_id"))
        outstanding = _count(view.get("outstanding_count"), 64)
        terminal = _count(view.get("terminal_sample_count"), 16)
        extra = _count(view.get("extra_open_intent_count"), 64)
        open_count = _count(view.get("open_journal_trade_count"), 64)
        rows, links = view.get("executions"), view.get("open_journal_trades")
        if (not isinstance(rows, list) or len(rows) > 144 or
                len(rows) != outstanding + terminal + extra or
                not isinstance(links, list) or len(links) != open_count):
            raise ValueError("SMC execution observation bound is invalid")
        events = []
        seen = set()
        rows_by_key = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("integrity_code") not in _CODES or \
                    not isinstance(row.get("execution_key"), str) or \
                    not row["execution_key"] or row["execution_key"] in seen:
                raise ValueError("SMC execution observation identity is invalid")
            seen.add(row["execution_key"])
            rows_by_key[row["execution_key"]] = row
            for field in ("execution_key", "symbol", "timeframe"):
                _text(row.get(field))
            # The journal permits these links to be unassigned on an early
            # durable intent. Retain the missing links, never invent them.
            for field in ("decision_id", "session_id", "broker_order_id", "discovered_broker_order_id",
                          "trade_id", "broker_order_status"):
                _text(row.get(field), optional=True)
            if row.get("state") not in _STATES:
                raise ValueError("SMC intent state is invalid")
            for field in ("broker_order_found", "journal_trade_found", "trade_closed",
                          "open_position_matches_order"):
                if row.get(field) is not None and type(row[field]) is not bool:
                    raise ValueError("SMC observation relationship is invalid")
            _count(row.get("matching_broker_order_count"), 2**63 - 1)
            _count(row.get("matching_journal_intent_count"), 2**63 - 1)
            _quantity(row.get("broker_filled_quantity"), optional=True)
            _quantity(row.get("journal_trade_size"), optional=True)
            updated = _timestamp(row.get("updated_at"))
            if updated > observed:
                raise ValueError("SMC intent update is after observation")
            material = {
                "execution_key": row["execution_key"],
                "broker_account_id": account_id,
                **{field: row[field] for field in ("decision_id", "session_id", "symbol", "timeframe")},
                "state": row.get("state"),
                "integrity_code": row["integrity_code"],
                "broker_order_id": row.get("broker_order_id"),
                "discovered_broker_order_id": row.get("discovered_broker_order_id"),
                "matching_broker_order_count": row.get("matching_broker_order_count"),
                "matching_journal_intent_count": row.get("matching_journal_intent_count"),
                "broker_order_found": row.get("broker_order_found"),
                "broker_order_status": row.get("broker_order_status"),
                "broker_filled_quantity": row.get("broker_filled_quantity"),
                "trade_id": row.get("trade_id"),
                "journal_trade_found": row.get("journal_trade_found"),
                "trade_closed": row.get("trade_closed"),
                "journal_trade_size": row.get("journal_trade_size"),
                "open_position_matches_order": row.get("open_position_matches_order"),
            }
            # A source intent may receive a new durable update with the same
            # projected fields. Its timestamp then changes too, so reusing
            # the prior ID would be a conflicting replay rather than a valid
            # second observation. Polling the unchanged source row still
            # produces exactly the same event ID and payload.
            identity = json.dumps({"contract": 2, "material": material,
                                   "source_updated_at": updated.isoformat()},
                                  sort_keys=True, separators=(",", ":"))
            event = GuardianEvent(
                source_service="guardian_smc_probe",
                source_component="smc_agent",
                event_type="execution_integrity_observed",
                event_id=hashlib.sha256(identity.encode()).hexdigest()[:32],
                timestamp=updated,
                severity="INFO" if row["integrity_code"] in {
                    "CONSISTENT", "PENDING", "ORDER_AWAITING_FILL"} else "WATCH",
                session_id=row.get("session_id"),
                execution_id=row["execution_key"],
                correlation_id=row.get("decision_id"),
                order_id=(row.get("discovered_broker_order_id") or
                          row.get("broker_order_id")),
                symbol=row.get("symbol"), timeframe=row.get("timeframe"),
                decision=row.get("state"), reason=row["integrity_code"],
                evidence={**material, "cross_database_atomic": False,
                          "execution_integrity_verified": False},
                metadata={"coverage": COVERAGE,
                          "paper_only": True},
            )
            event.canonical_json()
            events.append(event)
        if sum(row["state"] not in {"COMPLETE", "EXECUTION_FAILED"} for row in rows) != outstanding:
            raise ValueError("SMC outstanding execution coverage disagrees")
        seen_trades = set()
        for link in links:
            if not isinstance(link, dict):
                raise ValueError("SMC open journal link is invalid")
            trade_id = _text(link.get("trade_id"))
            if trade_id in seen_trades:
                raise ValueError("SMC open journal identity is duplicated")
            seen_trades.add(trade_id)
            keys = link.get("execution_keys")
            count = _count(link.get("matching_intent_count"), 64)
            if (not isinstance(keys, list) or len(keys) != count or
                    any(not isinstance(key, str) or key not in rows_by_key for key in keys) or
                    len(set(keys)) != len(keys)):
                raise ValueError("SMC journal execution links are incomplete")
            for key in keys:
                row = rows_by_key[key]
                if (row.get("trade_id") != trade_id or row.get("trade_closed") is not False or
                        row.get("journal_trade_found") is not True or
                        row.get("journal_trade_size") != link.get("journal_trade_size") or
                        row.get("matching_journal_intent_count") != count):
                    raise ValueError("SMC journal execution back-reference disagrees")
            expected = _LINK_CODES.get(count, "MULTIPLE_INTENTS_FOR_TRADE")
            if link.get("integrity_code") != expected:
                raise ValueError("SMC journal link finding is invalid")
            opened = _timestamp(link.get("opened_at"))
            if opened > observed:
                raise ValueError("SMC journal open time is after observation")
            for field in ("decision_id", "symbol", "timeframe", "direction"):
                _text(link.get(field))
            _text(link.get("order_id"), optional=True)
            _quantity(link.get("journal_trade_size"))
            material = {"broker_account_id": account_id, **{field: link[field] for field in (
                "trade_id", "decision_id", "symbol", "timeframe", "direction", "opened_at",
                "order_id", "journal_trade_size", "execution_keys", "matching_intent_count", "integrity_code")}}
            material["execution_keys"] = sorted(keys)
            identity = json.dumps({"contract": 2, "journal_link": material}, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False)
            event = GuardianEvent(
                source_service="guardian_smc_probe", source_component="smc_agent_journal",
                event_type="agent_journal_integrity_observed",
                event_id=hashlib.sha256(identity.encode()).hexdigest()[:32], timestamp=opened,
                severity="INFO" if expected == "INTENT_LINK_FOUND" else "WATCH",
                # Do not invent an execution identity for an unlinked trade.
                correlation_id=link["decision_id"], order_id=link["order_id"],
                symbol=link["symbol"], timeframe=link["timeframe"], reason=expected,
                evidence={**material, "cross_database_atomic": False,
                          "execution_integrity_verified": False},
                metadata={"coverage": COVERAGE, "paper_only": True},
            )
            event.canonical_json()
            events.append(event)
        if any(row.get("journal_trade_found") is True and row.get("trade_closed") is False and
               row.get("trade_id") not in seen_trades for row in rows):
            raise ValueError("SMC open journal coverage omits an observed trade")
        appended = sum(self.store.append(event) for event in events)
        # The probe can be reachable while a trading incident exists. Never
        # turn this into a smc_agent or paper-broker HEALTHY heartbeat.
        self.store.record_heartbeat("guardian_smc_execution_probe", "HEALTHY",
                                    observed_at=observed)
        return appended
