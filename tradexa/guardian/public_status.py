"""Read-only, coarse public-status observation from outside the trading app.

The public endpoint deliberately does not expose individual lab decisions.
This collector must not turn its aggregate status into invented trade evidence.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import urlopen

from .events import GuardianEvent
from .store import GuardianStore

_COMPONENTS = {
    "api": "api",
    "workers": "trading_instances",
    "market_data": "instance_market_data",
    "database": "instance_ledger",
}
_STATES = {
    "operational": "HEALTHY", "degraded": "DEGRADED",
    "outage": "FAILED", "unknown": "UNKNOWN",
}
_MAX_RESPONSE_BYTES = 512 * 1024


def _aware_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("public status timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("public status timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("public status timestamp must have a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in
            {"app", "localhost", "127.0.0.1", "::1"} or
            parsed.port != 8000 or parsed.path != "/status/public" or
            parsed.username or parsed.password or
            parsed.query or parsed.fragment):
        raise ValueError("Guardian public status URL must be an internal /status/public endpoint")


class GuardianPublicStatusCollector:
    """Observe an existing coarse status feed without a trading credential."""

    def __init__(self, store: GuardianStore, url: str, *, timeout_s: float = 3.0,
                 fetch: Callable[[], dict] | None = None,
                 clock: Callable[[], datetime] | None = None):
        _validate_url(url)
        if timeout_s <= 0:
            raise ValueError("public status timeout must be positive")
        self.store = store
        self.url = url
        self.timeout_s = timeout_s
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self) -> dict:
        with urlopen(self.url, timeout=self.timeout_s) as response:
            if response.status != 200:
                raise ValueError("public status returned a non-success response")
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("public status response exceeds size limit")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("public status response must be an object")
        return result

    def poll(self) -> int:
        """Persist one transition per public component; never write trading state.

        The source's stable ``since`` timestamp makes event IDs replay-safe
        across collector restarts. Fresh heartbeats are current state only.
        """
        view = self.fetch()
        if not isinstance(view, dict):
            raise ValueError("public status response must be an object")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Guardian observer clock must have a timezone")
        now = now.astimezone(timezone.utc)
        sample = _aware_timestamp(view.get("last_sample_at"))
        interval = view.get("interval_s")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= interval <= 3600:
            raise ValueError("public status sampling interval is invalid")
        age_s = (now - sample).total_seconds()
        if not -5 <= age_s <= max(90, 3 * interval):
            raise ValueError("public status sample is stale or ahead of clock")
        rows = view.get("components")
        if not isinstance(rows, list):
            raise ValueError("public status components are missing")
        by_id = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("id") not in _COMPONENTS or row["id"] in by_id:
                raise ValueError("public status has an invalid or duplicate component")
            if row.get("state") not in _STATES:
                raise ValueError("public status component state is invalid")
            by_id[row["id"]] = row
        if set(by_id) != set(_COMPONENTS):
            raise ValueError("public status is missing a required component")

        # Validate the entire upstream snapshot before touching Guardian's
        # store. A malformed later component must not make earlier components
        # look freshly observed while the probe itself fails.
        projected = []
        for source_name, component in _COMPONENTS.items():
            row = by_id[source_name]
            state = _STATES[row["state"]]
            # The upstream monitor calls zero scheduled workers operational.
            # Guardian must not translate that into evidence of live workers.
            no_workers = (source_name == "workers" and row.get("detail") == "No workers scheduled")
            warming = (source_name == "market_data" and
                       row.get("detail") == "Warming up after a start")
            if no_workers or warming:
                state = "UNKNOWN"
            reason = ("NO_WORKERS_SCHEDULED" if no_workers else
                      "INSTANCE_FEED_WARMING_UP" if warming else
                      "UPSTREAM_STATUS_" + row["state"].upper())
            if state == "UNKNOWN":
                projected.append((component, state, reason, None))
                continue
            since = _aware_timestamp(row.get("since"))
            if since > sample:
                raise ValueError("public status component transition is after sample")
            event_type = "component_status_changed"
            if source_name == "market_data" and state in {"DEGRADED", "FAILED"} and \
                    str(row.get("detail") or "").startswith("Candles arriving late"):
                event_type = "stale_candle"
            elif source_name == "market_data" and state == "HEALTHY" and \
                    row.get("detail") == "Candles current":
                event_type = "feed_synchronized"
            # An aggregate report does not prove websocket state or closed-bar
            # continuity, even when its label is operational.
            identity = (f"public-status-v1|{source_name}|{row['state']}|"
                        f"{event_type}|{since.isoformat()}")
            event = GuardianEvent(
                source_service="guardian_probe", source_component=component,
                event_type=event_type, event_id=hashlib.sha256(identity.encode()).hexdigest()[:32],
                timestamp=since, severity="HIGH" if state == "FAILED" else
                "WARNING" if state == "DEGRADED" else "INFO",
                state_after=state, reason=reason,
                evidence={"public_state": row["state"],
                          "closed_candle_continuity_verified": False},
                metadata={"venue": "binance_usdm", "scope": "instances"}
                if source_name == "market_data" else {},
            )
            event.canonical_json()
            projected.append((component, state, reason, event))

        appended = 0
        for component, state, reason, event in projected:
            if event is not None and self.store.append(event):
                appended += 1
            self.store.record_heartbeat(component, state, reason=reason,
                                        observed_at=sample)
        self.store.record_heartbeat("guardian_public_probe", "HEALTHY",
                                    observed_at=now)
        return appended
