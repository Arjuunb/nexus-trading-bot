"""Separate-key, read-only PA/SMC feed-state polling.

Feed evidence must never be confused with execution, strategy, or journal
health. In particular, a healthy feed does not arm a lab.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .lab_observer import _timestamp
from .store import GuardianStore

_COMPONENTS = {"PRICE_ACTION": "pa_feed", "SMC": "smc_feed"}
_MAX_RESPONSE_BYTES = 16 * 1024


class GuardianLabFeedObserver:
    def __init__(self, store: GuardianStore, url: str, key: str, *,
                 timeout_s: float = 3.0, fetch: Callable[[], dict] | None = None,
                 clock: Callable[[], datetime] | None = None):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in
                {"app", "localhost", "127.0.0.1", "::1"} or parsed.port != 8000 or
                parsed.path != "/guardian/lab-feeds" or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Guardian lab feed URL must be an internal read endpoint")
        if not isinstance(key, str) or len(key) < 24 or timeout_s <= 0:
            raise ValueError("Guardian lab feed observer requires an independent long key")
        self.store, self.url, self.key = store, url, key
        self.timeout_s = timeout_s
        self.fetch = fetch or self._fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self) -> dict:
        request = Request(self.url, headers={"X-Guardian-Observer-Key": self.key})
        with urlopen(request, timeout=self.timeout_s) as response:
            if response.status != 200:
                raise ValueError("lab feed observer returned a non-success response")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("lab feed observation exceeds size limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("lab feed response must be an object")
        return result

    def poll(self) -> dict[str, str]:
        view = self.fetch()
        if (not isinstance(view, dict) or view.get("schema_version") != 1 or
                view.get("scope") != "CURRENT_LAB_FEED_STATUS" or
                view.get("execution_health_verified") is not False):
            raise ValueError("lab feed observation contract is invalid")
        observed = _timestamp(view.get("observed_at"))
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Guardian lab feed clock must have a timezone")
        if not -5 <= (now.astimezone(timezone.utc) - observed).total_seconds() <= 90:
            raise ValueError("lab feed observation is stale or ahead of clock")
        feeds = view.get("feeds")
        if not isinstance(feeds, list) or len(feeds) != 2:
            raise ValueError("lab feed observations are incomplete")
        states = {}
        for item in feeds:
            if (not isinstance(item, dict) or item.get("lab") not in _COMPONENTS or
                    item["lab"] in states or item.get("paper_only") is not True or
                    item.get("execution_health_verified") is not False or
                    item.get("component_state") not in
                    {"HEALTHY", "BLOCKED", "FAILED", "DEGRADED", "UNKNOWN"} or
                    not isinstance(item.get("reason"), str) or
                    len(item["reason"]) > 100):
                raise ValueError("invalid lab feed observation")
            state = item["component_state"]
            age = item.get("closed_candle_age_seconds")
            limit = item.get("closed_candle_freshness_limit_seconds")
            fresh = (type(age) in (int, float) and type(limit) in (int, float) and
                     math.isfinite(age) and math.isfinite(limit) and
                     0 <= age <= limit and limit > 0)
            if ((state == "HEALTHY") != (item.get("reliable") is True) or
                    (state == "HEALTHY" and (item.get("feed_state") != "SYNCHRONIZED" or
                     not item.get("session_id") or not item.get("last_closed_update") or
                     not fresh))):
                raise ValueError("lab feed reliability claim is inconsistent")
            states[item["lab"]] = state
        if set(states) != set(_COMPONENTS):
            raise ValueError("lab feed observations are incomplete")
        for item in feeds:
            self.store.record_heartbeat(
                _COMPONENTS[item["lab"]], item["component_state"],
                reason=item["reason"], observed_at=observed)
        self.store.record_heartbeat("guardian_lab_feed_probe", "HEALTHY",
                                    observed_at=observed)
        return states
