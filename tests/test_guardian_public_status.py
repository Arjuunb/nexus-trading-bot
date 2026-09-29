"""Coarse public status is observed without a trading/control credential."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from tradexa.guardian.incidents import GuardianIncidentEngine
from tradexa.guardian.health import component_health
from tradexa.guardian.public_status import GuardianPublicStatusCollector
from tradexa.guardian.service import _public_status_monitor
from tradexa.guardian.store import GuardianStore

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
URL = "http://app:8000/status/public"


def _view(*, sample=NOW, workers="No workers scheduled", market_state="operational",
          market_detail="Candles current", market_since=NOW):
    return {
        "last_sample_at": sample.isoformat(), "interval_s": 60,
        "components": [
            {"id": "api", "state": "operational", "detail": "Answering requests",
             "since": NOW.isoformat()},
            {"id": "workers", "state": "operational", "detail": workers,
             "since": NOW.isoformat()},
            {"id": "market_data", "state": market_state, "detail": market_detail,
             "since": market_since.isoformat()},
            {"id": "database", "state": "operational", "detail": "Recording decisions and fills",
             "since": NOW.isoformat()},
        ],
    }


def test_public_status_poll_is_read_only_idempotent_and_no_workers_is_unknown(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    status = _view()
    collector = GuardianPublicStatusCollector(store, URL, fetch=lambda: status,
                                             clock=lambda: NOW + timedelta(seconds=20))
    assert collector.poll() == 3
    assert store.heartbeats()["trading_instances"]["state"] == "UNKNOWN"
    assert store.heartbeats()["instance_market_data"]["state"] == "HEALTHY"
    assert "market_data" not in store.heartbeats()  # PA/SMC are not covered.
    assert "database" not in store.heartbeats()  # This is only the instance ledger.
    assert store.heartbeats()["guardian_public_probe"]["state"] == "HEALTHY"
    # The upstream public feed is cached for 30 seconds; a later sample with
    # unchanged component transitions may not change immutable evidence.
    status["last_sample_at"] = (NOW + timedelta(seconds=15)).isoformat()
    assert collector.poll() == 0
    assert store.count() == 3
    assert all(event["source_service"] == "guardian_probe" for event in store.recent())


def test_stale_or_partial_status_cannot_refresh_good_component_heartbeats(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    status = _view()
    collector = GuardianPublicStatusCollector(store, URL, fetch=lambda: status,
                                             clock=lambda: NOW + timedelta(seconds=181))
    with pytest.raises(ValueError, match="stale"):
        collector.poll()
    assert store.heartbeats() == {}
    status["last_sample_at"] = (NOW + timedelta(seconds=180)).isoformat()
    status["components"].pop()
    with pytest.raises(ValueError, match="missing"):
        collector.poll()
    assert store.heartbeats() == {}


def test_invalid_later_transition_does_not_partially_apply_status(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    status = _view()
    status["components"][-1]["since"] = (NOW + timedelta(seconds=1)).isoformat()
    collector = GuardianPublicStatusCollector(store, URL, fetch=lambda: status,
                                             clock=lambda: NOW)
    with pytest.raises(ValueError, match="after sample"):
        collector.poll()
    assert store.heartbeats() == {}
    assert store.count() == 0


def test_accepted_old_source_sample_does_not_get_a_new_freshness_clock(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    collector = GuardianPublicStatusCollector(
        store, URL, fetch=lambda: _view(), clock=lambda: NOW + timedelta(seconds=120))
    collector.poll()
    health = component_health(store.heartbeats(), ("instance_market_data", "guardian_public_probe"),
                              now=NOW + timedelta(seconds=120))
    assert health["components"]["instance_market_data"]["state"] == "UNKNOWN"
    assert health["components"]["guardian_public_probe"]["state"] == "HEALTHY"


def test_warming_instance_feed_is_not_reported_as_healthy(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    collector = GuardianPublicStatusCollector(
        store, URL, fetch=lambda: _view(market_detail="Warming up after a start"),
        clock=lambda: NOW)
    collector.poll()
    assert store.heartbeats()["instance_market_data"]["state"] == "UNKNOWN"
    assert not any(row["source_component"] == "instance_market_data"
                   for row in store.recent())


def test_public_market_recovery_remains_unverified_even_after_current_label(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    status = _view(market_state="degraded", market_detail="Candles arriving late on 1 of 1 feeds")
    collector = GuardianPublicStatusCollector(store, URL, fetch=lambda: status,
                                             clock=lambda: NOW + timedelta(seconds=20))
    collector.poll()
    engine = GuardianIncidentEngine(store)
    engine.scan()
    [incident] = engine.list()
    assert incident["state"] == "OPEN"
    status["components"][2].update({"state": "operational", "detail": "Candles current",
                                      "since": (NOW + timedelta(seconds=10)).isoformat()})
    status["last_sample_at"] = (NOW + timedelta(seconds=20)).isoformat()
    collector.poll()
    engine.scan()
    [incident] = engine.list()
    assert incident["state"] == "RECOVERING"
    assert incident["root_cause"] == "Candle/quote freshness failed; upstream cause not proven"
    assert store.recent(1)[0]["evidence"]["closed_candle_continuity_verified"] is False


def test_collector_rejects_external_or_credential_bearing_urls(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    for url in ("https://example.com/status/public", "http://app:8000/control",
                "http://name:password@app:8000/status/public"):
        with pytest.raises(ValueError, match="internal"):
            GuardianPublicStatusCollector(store, url)


def test_monitor_failure_marks_only_observer_failed_not_trading(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    stopped = Event()

    def fail_once():
        stopped.set()
        raise RuntimeError("injected public feed failure")

    collector = GuardianPublicStatusCollector(store, URL, fetch=fail_once)
    _public_status_monitor(store, collector, stopped)
    assert store.heartbeats()["guardian_public_probe"]["state"] == "FAILED"
    assert "instance_market_data" not in store.heartbeats()
    assert store.count() == 0
