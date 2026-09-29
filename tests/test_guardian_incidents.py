"""Incidents are derived only from immutable, source-bound Guardian evidence."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.incidents import GuardianIncidentEngine
from tradexa.guardian.store import GuardianStore


@pytest.fixture
def guardian(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    return store, GuardianIncidentEngine(store)


def _append(store, event_id: str, kind: str, **values):
    store.append(GuardianEvent(
        source_service=values.pop("source_service", "market_data"),
        source_component=values.pop("source_component", "public_stream"),
        event_type=kind, event_id=event_id,
        timestamp=datetime(2026, 9, 29, tzinfo=timezone.utc), **values))


def test_normal_strategy_inactivity_is_not_an_incident(guardian):
    store, engine = guardian
    _append(store, "condition_failed_0001", "condition_failed",
            source_service="smc_lab", source_component="strategy",
            reason="WAITING_REJECTION")
    assert engine.scan() == 1
    assert engine.list() == []


def test_one_feed_outage_groups_downstream_symptoms_and_requires_continuity(guardian):
    store, engine = guardian
    meta = {"venue": "binance_usdm"}
    _append(store, "feed_down_00000001", "websocket_disconnected", metadata=meta)
    _append(store, "candle_stale_0001", "stale_candle", source_service="smc_lab",
            source_component="candle_pipeline", metadata=meta)
    _append(store, "htf_stale_000001", "stale_htf_candle", source_service="pa_lab",
            source_component="candle_pipeline", metadata=meta)
    assert engine.scan() == 3
    [incident] = engine.list()
    assert incident["state"] == "OPEN"
    assert incident["severity"] == "HIGH"
    assert incident["confidence"] == "CONFIRMED"
    assert incident["root_cause"] == "Public websocket disconnected"
    assert incident["evidence_count"] == 3
    assert len(engine.timeline(incident["incident_id"])) == 3

    _append(store, "feed_reconnect_01", "websocket_reconnected", metadata=meta)
    engine.scan()
    assert engine.list()[0]["state"] == "RECOVERING"
    assert engine.list()[0]["root_cause"] == "Public websocket disconnected"

    _append(store, "feed_sync_unverified", "feed_synchronized", metadata=meta)
    engine.scan()
    assert engine.list()[0]["state"] == "RECOVERING"

    _append(store, "feed_sync_000001", "feed_synchronized", metadata=meta,
            evidence={"closed_candle_continuity_verified": True})
    engine.scan()
    [recovered] = engine.list()
    assert recovered["state"] == "RECOVERED"
    assert len(engine.timeline(recovered["incident_id"])) == 6

    _append(store, "feed_down_00000002", "websocket_disconnected", metadata=meta)
    engine.scan()
    incidents = engine.list()
    assert len(incidents) == 2
    assert {row["state"] for row in incidents} == {"OPEN", "RECOVERED"}


def test_restart_scan_is_idempotent(guardian):
    store, engine = guardian
    _append(store, "worker_crash_0001", "worker_crashed",
            source_service="smc_lab", source_component="agent")
    assert engine.scan() == 1
    restarted = GuardianIncidentEngine(store)
    assert restarted.scan() == 0
    [incident] = restarted.list()
    assert incident["evidence_count"] == 1
    assert len(restarted.timeline(incident["incident_id"])) == 1

    _append(store, "worker_restart_01", "worker_restarted",
            source_service="smc_lab", source_component="agent")
    restarted.scan()
    assert restarted.list()[0]["state"] == "RECOVERING"
    _append(store, "worker_heartbeat1", "worker_heartbeat",
            source_service="smc_lab", source_component="agent")
    restarted.scan()
    assert restarted.list()[0]["state"] == "RECOVERING"
    _append(store, "worker_verified_01", "worker_heartbeat",
            source_service="smc_lab", source_component="agent",
            evidence={"worker_operational_verified": True})
    restarted.scan()
    assert restarted.list()[0]["state"] == "RECOVERED"


def test_journal_and_execution_identity_never_mix(guardian):
    store, engine = guardian
    _append(store, "exec_uncertain_01", "execution_uncertain",
            source_service="smc_lab", source_component="paper_broker",
            execution_id="execution_001")
    _append(store, "exec_uncertain_02", "execution_uncertain",
            source_service="smc_lab", source_component="paper_broker",
            execution_id="execution_002")
    _append(store, "reconcile_unverified", "reconciliation_completed",
            source_service="smc_lab", source_component="paper_broker",
            execution_id="execution_001", evidence={"verified": False})
    _append(store, "journal_fail_0001", "journal_failed",
            source_service="smc_lab", source_component="journal",
            execution_id="execution_001")
    assert engine.scan() == 4
    incidents = engine.list()
    assert len(incidents) == 3
    assert next(row for row in incidents if row["fingerprint"] == "execution:execution_001")["state"] == "RECOVERING"

    _append(store, "reconcile_verified1", "reconciliation_completed",
            source_service="smc_lab", source_component="paper_broker",
            execution_id="execution_001", evidence={"verified": True})
    _append(store, "journal_reconciled1", "journal_reconciled",
            source_service="smc_lab", source_component="journal",
            execution_id="execution_001", evidence={"journal_consistency_verified": True})
    engine.scan()
    states = {row["fingerprint"]: row["state"] for row in engine.list()}
    assert states == {
        "execution:execution_001": "RECOVERED",
        "execution:execution_002": "OPEN",
        "journal:execution_001": "RECOVERED",
    }


def test_failed_analysis_rolls_back_cursor_and_retries(guardian, monkeypatch):
    store, engine = guardian
    _append(store, "feed_down_00000001", "websocket_disconnected")
    original = engine._apply

    def fail_after_write(conn, event, received_at, signal):
        original(conn, event, received_at, signal)
        raise RuntimeError("injected analyzer crash")

    monkeypatch.setattr(engine, "_apply", fail_after_write)
    with pytest.raises(RuntimeError, match="injected"):
        engine.scan()
    assert engine.list() == []
    monkeypatch.setattr(engine, "_apply", original)
    assert engine.scan() == 1
    assert len(engine.list()) == 1


def test_recovery_without_observed_failure_creates_no_incident(guardian):
    store, engine = guardian
    _append(store, "feed_sync_000001", "feed_synchronized")
    assert engine.scan() == 1
    assert engine.list() == []
