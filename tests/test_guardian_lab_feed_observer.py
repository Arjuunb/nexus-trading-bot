"""Feed freshness is independently observed, not inferred from a decision row."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradexa.guardian.lab_feed_observer import GuardianLabFeedObserver
from tradexa.guardian.store import GuardianStore

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
KEY = "independent-guardian-feed-key-123456"
URL = "http://app:8000/guardian/lab-feeds"


def _view():
    return {"schema_version": 1, "scope": "CURRENT_LAB_FEED_STATUS",
            "execution_health_verified": False, "observed_at": NOW.isoformat(),
            "feeds": [
                {"lab": "PRICE_ACTION", "component_state": "HEALTHY",
                 "reason": "FEED_RECONCILED", "feed_state": "SYNCHRONIZED",
                 "reliable": True, "paper_only": True,
                 "execution_health_verified": False, "session_id": "pa-session",
                 "last_closed_update": NOW.isoformat(),
                 "closed_candle_age_seconds": 1,
                 "closed_candle_freshness_limit_seconds": 330},
                {"lab": "SMC", "component_state": "BLOCKED",
                 "reason": "FEED_NOT_SYNCHRONIZED", "feed_state": "STALE_CANDLES",
                 "reliable": False, "paper_only": True,
                 "execution_health_verified": False, "session_id": "smc-session",
                 "last_closed_update": None},
            ]}


def test_valid_feeds_have_separate_component_health_not_lab_execution_health(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianLabFeedObserver(store, URL, KEY, fetch=_view,
                                       clock=lambda: NOW + timedelta(seconds=2))
    assert observer.poll() == {"PRICE_ACTION": "HEALTHY", "SMC": "BLOCKED"}
    beats = store.heartbeats()
    assert beats["guardian_lab_feed_probe"]["state"] == "HEALTHY"
    assert beats["pa_feed"]["state"] == "HEALTHY"
    assert beats["smc_feed"]["state"] == "BLOCKED"
    assert "pa_lab" not in beats and "smc_lab" not in beats
    assert store.count() == 0


def test_invalid_or_stale_source_cannot_claim_feed_health(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    view = _view()
    view["feeds"][1]["component_state"] = "HEALTHY"
    with pytest.raises(ValueError, match="reliability"):
        GuardianLabFeedObserver(store, URL, KEY, fetch=lambda: view, clock=lambda: NOW).poll()
    assert store.heartbeats() == {}
    with pytest.raises(ValueError, match="stale"):
        GuardianLabFeedObserver(store, URL, KEY, fetch=_view,
                                clock=lambda: NOW + timedelta(minutes=3)).poll()
    with pytest.raises(ValueError, match="internal"):
        GuardianLabFeedObserver(store, "https://trade-logx.com/guardian/lab-feeds", KEY)
