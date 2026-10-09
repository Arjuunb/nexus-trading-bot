"""Cached paper risk cannot remain current after the probe fails or ages."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.instance_ledger_view import instance_ledger_view
from tradexa.guardian.store import GuardianStore

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _save(store):
    store.append_observed_snapshot("instance_ledger", "a" * 64, GuardianEvent(
        source_service="guardian_instance_ledger_probe", source_component="instance_ledger",
        event_type="instance_paper_ledger_observed", timestamp=NOW,
        evidence={"atomic_snapshot": True, "source_coverage_verified": True,
                  "instances": [{"instance_id": "i1", "open_positions": 1,
                                 "open_trades": 1, "risk_complete": True,
                                 "risk_amount": 10}], "findings": []}))


def test_absent_or_stale_probe_masks_cached_risk(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    assert instance_ledger_view(store, now=NOW)["observation_state"] == "UNKNOWN"
    _save(store)
    missing = instance_ledger_view(store, now=NOW)
    assert missing["instances"][0]["risk_amount"] is None
    store.record_heartbeat("guardian_instance_ledger_probe", "HEALTHY", observed_at=NOW)
    current = instance_ledger_view(store, now=NOW + timedelta(seconds=10))
    assert current["observation_state"] == "CURRENT"
    assert current["instances"][0]["risk_amount"] == 10
    assert current["global_risk_amount"] is None
    assert current["currency_verified"] is False
    stale = instance_ledger_view(store, now=NOW + timedelta(minutes=2))
    assert stale["observation_state"] == "UNKNOWN"
    assert stale["instances"][0]["risk_amount"] is None
    assert stale["instances"][0]["risk_complete"] is False


def test_revalidated_unchanged_snapshot_is_current_without_another_event(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    _save(store)
    later = NOW + timedelta(days=1)
    store.record_heartbeat("guardian_instance_ledger_probe", "HEALTHY", observed_at=later)
    assert instance_ledger_view(store, now=later)["instances"][0]["risk_amount"] == 10
    assert store.count() == 1
    store.record_heartbeat("guardian_instance_ledger_probe", "FAILED", observed_at=later)
    assert instance_ledger_view(store, now=later)["instances"][0]["risk_amount"] is None
