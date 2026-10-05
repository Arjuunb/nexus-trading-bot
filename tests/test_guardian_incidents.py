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


@pytest.mark.parametrize("findings", [None, ["bad-row"], [{"code": "EXIT_NOT_REDUCE_ONLY"}],
                                    [{"code": ["bad"], "record_type": "order", "record_id": "id", "confidence": "UNVERIFIED"}]])
def test_malformed_lab_execution_event_does_not_poison_incident_cursor(guardian, findings):
    store, engine = guardian
    _append(store, "invalid_lab_evidence", "lab_paper_execution_observed",
            source_service="guardian_smc_paper_execution_probe", source_component="smc_paper_execution",
            metadata={"paper_only": True}, evidence={"lab": "SMC", "atomic_snapshot": True,
                                                     "account_id": "smc-paper", "findings": findings})
    _append(store, "next_valid_crash", "worker_crashed", source_service="smc_lab", source_component="agent")
    assert engine.scan() == 2
    assert len(engine.list()) == 1
    assert engine.list()[0]["root_component"] == "agent"


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


def test_smc_account_and_key_delimiters_cannot_merge_unrelated_incidents(guardian):
    store, engine = guardian
    _execution_observation(store, "smc_account_scope_01", "b:c", "EXECUTION_UNCERTAIN",
                           evidence={"broker_account_id": "a"})
    _execution_observation(store, "smc_account_scope_02", "c", "EXECUTION_UNCERTAIN",
                           evidence={"broker_account_id": "a:b"})
    engine.scan()
    assert {row["fingerprint"] for row in engine.list()} == {
        "smc_execution_integrity:a:b%3Ac", "smc_execution_integrity:a%3Ab:c"}


@pytest.mark.parametrize("damage", ["source", "component", "paper", "atomic", "identity", "count", "code"])
def test_untrusted_open_journal_link_does_not_create_an_incident(guardian, damage):
    store, engine = guardian
    values = dict(source_service="guardian_smc_probe", source_component="smc_agent_journal",
                  reason="JOURNAL_INTENT_NOT_FOUND", metadata={"paper_only": True}, evidence={
                      "trade_id": "trade-1", "broker_account_id": "paper-account",
                      "execution_keys": [], "matching_intent_count": 0,
                      "integrity_code": "JOURNAL_INTENT_NOT_FOUND",
                      "cross_database_atomic": False, "execution_integrity_verified": False})
    if damage == "source":
        values["source_service"] = "smc_lab"
    elif damage == "component":
        values["source_component"] = "smc_agent"
    elif damage == "paper":
        values["metadata"]["paper_only"] = False
    elif damage == "atomic":
        values["evidence"]["cross_database_atomic"] = True
    elif damage == "identity":
        values["evidence"]["trade_id"] = ""
    elif damage == "count":
        values["evidence"]["matching_intent_count"] = 1
    elif damage == "code":
        values["evidence"]["integrity_code"] = "INTENT_LINK_FOUND"
    _append(store, f"smc_journal_bad_{damage}", "agent_journal_integrity_observed", **values)
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


def _paper_ledger_observation(store, event_id, findings, *, atomic=True,
                              source="guardian_instance_ledger_probe"):
    _append(store, event_id, "instance_paper_ledger_observed",
            source_service=source, source_component="instance_ledger",
            metadata={"paper_only": True}, evidence={
                "scope": "INSTANCE_ATTRIBUTED_PAPER_LEDGER_ONLY",
                "atomic_snapshot": atomic, "source_coverage_verified": atomic,
                "broker_fill_verified": False, "live_exposure_verified": False,
                "findings": findings})


def test_instance_integrity_groups_by_owner_and_never_certifies_repair(guardian):
    store, engine = guardian
    _paper_ledger_observation(store, "instance_pair_issue_1", [
        {"instance_id": "i1", "codes": ["MISSING_STOP"]},
        {"instance_id": "i1", "codes": ["SIZE_MISMATCH"]},
        {"instance_id": "i2", "codes": ["OPEN_POSITION_TRADE_UNVERIFIED"]},
    ])
    assert engine.scan() == 1
    incidents = {row["fingerprint"]: row for row in engine.list()}
    assert set(incidents) == {"instance_paper_ledger:i1", "instance_paper_ledger:i2"}
    assert all(row["confidence"] == "CONFIRMED" for row in incidents.values())
    assert all(row["severity"] == "HIGH" for row in incidents.values())
    assert all(row["evidence_count"] == 1 for row in incidents.values())
    _paper_ledger_observation(store, "instance_pair_read_2", [
        {"instance_id": "i1", "codes": []}, {"instance_id": "i2", "codes": []}])
    engine.scan()
    assert all(row["state"] == "RECOVERING" for row in engine.list())
    assert GuardianIncidentEngine(store).scan() == 0


def test_legacy_unknown_or_wrong_source_is_not_an_integrity_incident(guardian):
    store, engine = guardian
    _paper_ledger_observation(store, "legacy_pair_read_1", [
        {"instance_id": "i1", "codes": ["EXECUTION_LINK_UNVERIFIED"]},
        {"instance_id": "i1", "codes": ["OPEN_TRADE_POSITION_UNVERIFIED"]},
    ])
    _paper_ledger_observation(store, "wrong_pair_source_1", [
        {"instance_id": "i1", "codes": ["MISSING_STOP"]}], source="smc_lab")
    _paper_ledger_observation(store, "empty_pair_read_001", [])
    engine.scan()
    assert engine.list() == []
    _paper_ledger_observation(store, "racing_pair_read_1", [
        {"instance_id": "i1", "codes": ["MISSING_STOP"]}], atomic=False)
    engine.scan()
    assert engine.list()[0]["confidence"] == "POSSIBLE"


def test_multi_instance_analysis_failure_rolls_back_all_incidents(guardian, monkeypatch):
    store, engine = guardian
    _paper_ledger_observation(store, "multi_pair_issue_01", [
        {"instance_id": "i1", "codes": ["MISSING_STOP"]},
        {"instance_id": "i2", "codes": ["MISSING_STOP"]}])
    original = engine._apply
    calls = []
    def fail_second(conn, event, received_at, signal):
        original(conn, event, received_at, signal)
        calls.append(signal.fingerprint)
        if len(calls) == 2:
            raise RuntimeError("injected second instance failure")
    monkeypatch.setattr(engine, "_apply", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        engine.scan()
    assert engine.list() == []
    monkeypatch.setattr(engine, "_apply", original)
    assert engine.scan() == 1
    assert len(engine.list()) == 2
    assert all(row["evidence_count"] == 1 for row in engine.list())
