"""Committed lab evaluations are evidence; they are not proof of feed health."""
from datetime import datetime, timedelta, timezone

import pytest

from tradexa.guardian.lab_observer import GuardianLabObserver
from tradexa.guardian.store import GuardianStore

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
KEY = "independent-lab-observer-key-123456"
URL = "http://app:8000/guardian/observations"


def _view():
    row = {
        "correlation_id": "corr-1", "session_id": "session-1",
        "candle_time": (NOW - timedelta(minutes=5)).isoformat(),
        "strategy_id": "SMC_SOURCE_V1", "strategy_version": "1.0",
        "symbol": "BTCUSDT", "timeframe": "5m", "state": "WATCHING",
        "reason": "waiting for rejection", "conditions": [
            {"key": "htf_context", "status": "PASS"},
            {"key": "rejection", "status": "MISSING"},
        ], "missing_conditions": ["rejection"],
        "condition_trace_available": True,
    }
    return {
        "schema_version": 1, "scope": "BOUNDED_SAVED_LAB_DECISIONS",
        "observed_at": NOW.isoformat(), "feed_health_verified": False,
        "execution_integrity_verified": False,
        "labs": [
            {"lab": "PRICE_ACTION", "state": "UNKNOWN", "session_id": None,
             "coverage": "LATEST_ACTIVE_SESSION_ONLY", "evaluations": []},
            {"lab": "SMC", "state": "OBSERVED", "session_id": "session-1",
             "coverage": "LATEST_ACTIVE_SESSION_ONLY", "evaluations": [row]},
        ],
    }


def test_lab_observation_is_immutable_deduplicated_and_not_health_claim(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    view = _view()
    collector = GuardianLabObserver(store, URL, KEY, fetch=lambda: view,
                                    clock=lambda: NOW + timedelta(seconds=30))
    assert collector.poll() == 1
    view["observed_at"] = (NOW + timedelta(seconds=20)).isoformat()
    assert collector.poll() == 0
    assert store.count() == 1
    [event] = store.recent()
    assert event["source_component"] == "smc_lab"
    assert event["event_type"] == "lab_evaluation_observed"
    assert event["evidence"]["missing_conditions"] == ["rejection"]
    assert event["evidence"]["feed_health_verified"] is False
    assert "smc_lab" not in store.heartbeats()
    assert "pa_lab" not in store.heartbeats()
    assert store.heartbeats()["guardian_lab_probe"]["state"] == "HEALTHY"


def test_material_decision_state_change_adds_one_event_not_one_per_poll(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    view = _view()
    collector = GuardianLabObserver(store, URL, KEY, fetch=lambda: view, clock=lambda: NOW)
    assert collector.poll() == 1
    row = view["labs"][1]["evaluations"][0]
    row.update(state="SIGNAL_FOUND", reason="native proposal found", missing_conditions=[])
    assert collector.poll() == 1
    assert collector.poll() == 0
    assert store.count() == 2


def test_bad_later_lab_row_cannot_partially_write_earlier_evidence(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    view = _view()
    view["labs"].reverse()
    view["labs"][1]["evaluations"] = [{"session_id": None, "candle_time": "bad"}]
    with pytest.raises(ValueError, match="unknown lab"):
        GuardianLabObserver(store, URL, KEY, fetch=lambda: view, clock=lambda: NOW).poll()
    assert store.count() == 0
    assert store.heartbeats() == {}


def test_stale_or_unsafe_observer_is_rejected(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    with pytest.raises(ValueError, match="internal"):
        GuardianLabObserver(store, "https://trade-logx.com/guardian/observations", KEY)
    with pytest.raises(ValueError, match="independent long key"):
        GuardianLabObserver(store, URL, "short")
    with pytest.raises(ValueError, match="stale"):
        GuardianLabObserver(store, URL, KEY, fetch=_view,
                            clock=lambda: NOW + timedelta(minutes=3)).poll()
    assert store.count() == 0


def test_future_decision_candle_cannot_be_recorded_as_evidence(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    view = _view()
    view["labs"][1]["evaluations"][0]["candle_time"] = (
        NOW + timedelta(minutes=5)).isoformat()
    with pytest.raises(ValueError, match="after observation"):
        GuardianLabObserver(store, URL, KEY, fetch=lambda: view, clock=lambda: NOW).poll()
    assert store.count() == 0
