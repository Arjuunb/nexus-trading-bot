"""Independent observation of SMC Agent paper execution relationships."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .events import GuardianEvent
from .lab_observer import _timestamp
from .store import GuardianStore

_MAX_RESPONSE_BYTES = 1024 * 1024
_CODES = frozenset({
    "CONSISTENT", "PENDING", "EXECUTION_UNCERTAIN", "ORDER_ID_NOT_FOUND",
    "ORDER_IDENTITY_MISMATCH", "TRADE_ID_NOT_FOUND", "TRADE_IDENTITY_MISMATCH",
    "ENTRY_MARKED_REDUCE_ONLY", "FAILED_INTENT_WITH_ORDER_ID",
    "ORDER_ID_UNRECORDED", "FILLED_ORDER_JOURNAL_PENDING",
    "COMPLETE_INTENT_TRADE_UNRECORDED", "OPEN_TRADE_POSITION_UNVERIFIED",
    "BROKER_ORDER_UNRECORDED", "DUPLICATE_EXECUTION_KEY",
    "AGENT_TRADE_PRECEDES_FILL", "ORDER_AWAITING_FILL",
    "JOURNAL_SIZE_EXCEEDS_BROKER_FILL",
})


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
        with urlopen(request, timeout=self.timeout_s) as response:
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
        view = self.fetch()
        if (not isinstance(view, dict) or view.get("schema_version") != 1 or
                view.get("scope") != "SMC_AGENT_PAPER_ONLY" or
                view.get("coverage") != "ALL_OUTSTANDING_PLUS_RECENT_TERMINAL" or
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
        rows = view.get("executions")
        if (not isinstance(rows, list) or len(rows) > 80 or
                type(view.get("outstanding_count")) is not int or
                not 0 <= view["outstanding_count"] <= 64 or
                type(view.get("terminal_sample_count")) is not int or
                not 0 <= view["terminal_sample_count"] <= 16 or
                len(rows) != view["outstanding_count"] + view["terminal_sample_count"]):
            raise ValueError("SMC execution observation bound is invalid")
        events = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("integrity_code") not in _CODES or \
                    not isinstance(row.get("execution_key"), str) or \
                    not row["execution_key"] or row["execution_key"] in seen:
                raise ValueError("SMC execution observation identity is invalid")
            seen.add(row["execution_key"])
            updated = _timestamp(row.get("updated_at"))
            if updated > observed:
                raise ValueError("SMC intent update is after observation")
            material = {
                "execution_key": row["execution_key"],
                "state": row.get("state"),
                "integrity_code": row["integrity_code"],
                "broker_order_id": row.get("broker_order_id"),
                "discovered_broker_order_id": row.get("discovered_broker_order_id"),
                "matching_broker_order_count": row.get("matching_broker_order_count"),
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
            identity = json.dumps({"material": material,
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
                metadata={"coverage": "ALL_OUTSTANDING_PLUS_RECENT_TERMINAL",
                          "paper_only": True},
            )
            event.canonical_json()
            events.append(event)
        appended = sum(self.store.append(event) for event in events)
        # The probe can be reachable while a trading incident exists. Never
        # turn this into a smc_agent or paper-broker HEALTHY heartbeat.
        self.store.record_heartbeat("guardian_smc_execution_probe", "HEALTHY",
                                    observed_at=observed)
        return appended
