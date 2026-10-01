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


def _execution_observation(store, event_id: str, key: str, code: str, **changes):
    evidence = {"execution_key": key, "integrity_code": code,
                "cross_database_atomic": False,
                "execution_integrity_verified": False,
                **changes.pop("evidence", {})}
    _append(store, event_id, "execution_integrity_observed",
            source_service=changes.pop("source_service", "guardian_smc_probe"),
            source_component=changes.pop("source_component", "smc_agent"),
            execution_id=key, reason=code, evidence=evidence,
            metadata={"paper_only": True, **changes.pop("metadata", {})},
            **changes)


def test_smc_execution_observation_groups_per_key_without_claiming_proof(guardian):
    store, engine = guardian
    _execution_observation(store, "smc_pending_0001", "decision-1", "ORDER_AWAITING_FILL")
    _execution_observation(store, "smc_mismatch_001", "decision-1", "AGENT_TRADE_PRECEDES_FILL")
    _execution_observation(store, "smc_mismatch_002", "decision-1", "JOURNAL_SIZE_EXCEEDS_BROKER_FILL")
    _execution_observation(store, "smc_mismatch_003", "decision-2", "FILLED_ORDER_JOURNAL_PENDING")
    engine.scan()

    incidents = {row["fingerprint"]: row for row in engine.list()}
    assert set(incidents) == {"smc_execution_integrity:decision-1",
                              "smc_execution_integrity:decision-2"}
    first = incidents["smc_execution_integrity:decision-1"]
    assert first["confidence"] == "POSSIBLE"
    assert first["severity"] == "WARNING"
    assert first["evidence_count"] == 2
    assert incidents["smc_execution_integrity:decision-2"]["severity"] == "HIGH"

    # A later consistent cross-database read is progress, not proof of repair.
    _execution_observation(store, "smc_consistent_01", "decision-1", "CONSISTENT")
    engine.scan()
    assert engine.get(first["incident_id"])["state"] == "RECOVERING"
    assert len(engine.timeline(first["incident_id"])) == 3
    assert GuardianIncidentEngine(store).scan() == 0


def test_smc_execution_observation_rejects_untrusted_or_unverified_contract(guardian):
    store, engine = guardian
    _execution_observation(store, "smc_no_incident_1", "decision-1", "PENDING")
    _execution_observation(store, "smc_no_incident_2", "decision-1", "CONSISTENT")
    _execution_observation(store, "smc_wrong_source_1", "decision-1", "DUPLICATE_EXECUTION_KEY",
                           source_service="smc_lab")
    _execution_observation(store, "smc_wrong_scope_1", "decision-1", "DUPLICATE_EXECUTION_KEY",
                           metadata={"paper_only": False})
    _execution_observation(store, "smc_wrong_claim_1", "decision-1", "DUPLICATE_EXECUTION_KEY",
                           evidence={"cross_database_atomic": True})
    _execution_observation(store, "smc_wrong_key_0001", "decision-1", "DUPLICATE_EXECUTION_KEY",
                           evidence={"execution_key": "decision-2"})
    engine.scan()
    assert engine.list() == []


def test_active_incident_count_is_not_truncated_to_visible_page(guardian):
    store, engine = guardian
    for number in range(55):
        _append(store, f"worker_crash_{number:04d}", "worker_crashed",
                source_service="smc_lab", source_component=f"worker_{number}")
    assert engine.scan() == 55
    assert len(engine.list(limit=50)) == 50
    assert engine.active_summary() == {
        "total": 55, "warning_or_higher": 55, "high_or_critical": 55}
