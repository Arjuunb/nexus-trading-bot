"""Phase 3 observes evidence; it cannot turn a correlation into trading authority."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tradexa.guardian import anomalies, dependencies, investigations
from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.incidents import GuardianIncidentEngine
from tradexa.guardian.store import GuardianStore

NOW = datetime(2026, 10, 4, 12, 10, 30, tzinfo=timezone.utc)
CUTOFF = NOW.replace(second=0)
META = {"code_commit": "a" * 40, "config_hash": "b" * 64,
        "latency_kind": "strategy_evaluation", "venue": "binance_usdm", "scope": "smc"}


@pytest.fixture
def store(tmp_path):
    return GuardianStore(tmp_path / "guardian.db")


def persist(store, event, *, received_at=None):
    with closing(store._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        store._append_in_transaction(conn, event, (received_at or CUTOFF - timedelta(seconds=1)).isoformat())
        conn.commit()


def sample(store, name, timestamp, value=10, **changes):
    event = replace(GuardianEvent(
        event_id=name, timestamp=timestamp, source_service="smc_lab", source_component="strategy",
        event_type="evaluation_completed", latency_ms=value, metadata=META,
        lab_id="SMC", session_id="session-a", symbol="BTCUSDT", timeframe="5m",
        strategy_id="smc-frozen", strategy_version="1.0"), **changes)
    persist(store, event)
    return event


def series(store, *, current=100, baseline=10, **changes):
    for index in range(20):
        sample(store, f"baseline_{index:03}", CUTOFF - timedelta(minutes=6 + index), baseline, **changes)
    for index in range(5):
        sample(store, f"current_{index:03}", CUTOFF - timedelta(seconds=1 + index), current, **changes)


def heartbeat(state, **changes):
    return {"state": state, "observed_at": NOW.isoformat(), "reason": "SOURCE_OBSERVED", **changes}


def node(view, name):
    return next(row for row in view["nodes"] if row["component"] == name)


def test_feed_failure_blocks_smc_dependency_not_strategy_and_does_not_mix_pa_or_instances():
    view = dependencies.dependency_map({"smc_feed": heartbeat("FAILED"), "smc_lab": heartbeat("HEALTHY"),
        "pa_feed": heartbeat("HEALTHY"), "pa_lab": heartbeat("HEALTHY"),
        "instance_market_data": heartbeat("HEALTHY"), "trading_instances": heartbeat("HEALTHY")},
        ("smc_lab", "pa_lab", "trading_instances"), now=NOW)
    assert view["required_dependency_readiness"] == "BLOCKED_BY_DEPENDENCY"
    smc = node(view, "smc_lab")
    assert smc["observed_state"] == "HEALTHY"
    assert smc["dependency_readiness"] == "BLOCKED_BY_DEPENDENCY"
    assert smc["blocked_by"] == ["smc_feed"]
    assert smc["strategy_failure_verified"] is False
    assert smc["trading_gate_verified"] is False
    assert node(view, "pa_lab")["dependency_readiness"] == "OBSERVED_AVAILABLE"
    assert node(view, "trading_instances")["dependency_readiness"] == "OBSERVED_AVAILABLE"
    assert node(view, "smc_paper_execution")["blocked_by"] == ["smc_feed"]
    assert view["runtime_topology_verified"] is False


def test_healthy_probe_cannot_certify_its_subject_or_missing_parent():
    view = dependencies.dependency_map({"guardian_lab_feed_probe": heartbeat("HEALTHY"),
        "guardian_smc_execution_probe": heartbeat("HEALTHY"), "smc_lab": heartbeat("HEALTHY")},
        ("smc_lab",), now=NOW)
    assert view["required_dependency_readiness"] == "UNKNOWN"
    assert node(view, "smc_feed")["observed_state"] == "UNKNOWN"
    assert node(view, "smc_lab")["unknown_dependencies"] == ["smc_feed"]
    assert node(view, "smc_paper_execution")["observed_state"] == "UNKNOWN"


def test_smc_automatic_path_does_not_silently_depend_on_independent_agent():
    beats = {name: heartbeat("HEALTHY") for name in ("smc_feed", "smc_lab", "smc_paper_journal", "smc_paper_execution")}
    beats.update({"smc_agent": heartbeat("FAILED"), "smc_agent_journal": heartbeat("FAILED")})
    view = dependencies.dependency_map(beats, ("smc_paper_execution",), now=NOW)
    assert node(view, "smc_paper_execution")["dependency_readiness"] == "OBSERVED_AVAILABLE"
    assert node(view, "smc_agent_paper_execution")["dependency_readiness"] == "BLOCKED_BY_DEPENDENCY"
    assert node(view, "smc_paper_execution")["execution_path_selected_verified"] is False


@pytest.mark.parametrize("age", [91, -6])
def test_stale_or_future_dependency_is_unknown(age):
    view = dependencies.dependency_map({"smc_feed": heartbeat("HEALTHY",
        observed_at=(NOW - timedelta(seconds=age)).isoformat()), "smc_lab": heartbeat("HEALTHY")},
        ("smc_lab",), now=NOW)
    assert node(view, "smc_lab")["dependency_readiness"] == "UNKNOWN"


def test_dependency_cycle_is_rejected(monkeypatch):
    monkeypatch.setattr(dependencies, "DEPENDENCIES", {"a": ("b",), "b": ("a",)})
    with pytest.raises(ValueError, match="cycle"):
        dependencies.dependency_map({}, ("a",), now=NOW)


def test_dependency_degradation_is_not_silently_available():
    view = dependencies.dependency_map({"smc_feed": heartbeat("DEGRADED"), "smc_lab": heartbeat("HEALTHY")},
        ("smc_lab",), now=NOW)
    assert node(view, "smc_lab")["dependency_readiness"] == "DEPENDENCY_DEGRADED"
    assert view["required_dependency_readiness"] == "OBSERVED_UNAVAILABLE"


def test_latency_deviation_is_watch_not_failure_and_current_not_in_baseline(store):
    series(store)
    [row] = anomalies.latency_anomalies(store, now=NOW)["anomalies"]
    assert (row["baseline_samples"], row["current_samples"]) == (20, 5)
    assert row["baseline_median_ms"] == 10
    assert row["current_median_ms"] == 100
    assert row["threshold_ms"] == 50
    assert row["state"] == "LATENCY_DEVIATION" and row["severity"] == "WATCH"
    assert row["failure_verified"] is False and row["financial_risk_verified"] is False
    assert len(row["current_evidence_ids"]) == 5


def test_normal_latency_is_observation_not_healthy_and_empty_not_normal(store):
    empty = anomalies.latency_anomalies(store, now=NOW)
    assert empty["anomalies"] == [] and empty["coverage_state"] == "INSUFFICIENT_EVIDENCE"
    series(store, current=11)
    view = anomalies.latency_anomalies(store, now=NOW)
    assert view["anomalies"][0]["state"] == "NO_DEVIATION_OBSERVED"
    assert view["all_metrics_observed"] is False and view["automatic_action_allowed"] is False


def test_single_outlier_does_not_equal_sustained_deviation(store):
    series(store, current=10)
    sample(store, "outlier_000", CUTOFF - timedelta(seconds=20), 10000)
    assert anomalies.latency_anomalies(store, now=NOW)["anomalies"][0]["state"] == "NO_DEVIATION_OBSERVED"


@pytest.mark.parametrize("changes", [
    {"symbol": "ETHUSDT"}, {"session_id": "session-b"}, {"lab_id": "PRICE_ACTION"},
    {"instance_id": "instance-2"}, {"strategy_version": "2.0"}, {"timeframe": "15m"},
    {"metadata": {**META, "venue": "kraken_spot"}},
    {"metadata": {**META, "code_commit": "c" * 40}},
    {"metadata": {**META, "config_hash": "d" * 64}},
    {"metadata": {**META, "latency_kind": "paper_fill"}},
    {"source_service": "pa_lab"}, {"source_component": "journal"},
    {"event_type": "order_accepted"},
])
def test_baselines_never_pool_unrelated_contexts(store, changes):
    for index in range(20):
        sample(store, f"baseline_{index:03}", CUTOFF - timedelta(minutes=6 + index))
    for index in range(5):
        sample(store, f"current_{index:03}", CUTOFF - timedelta(seconds=1 + index), 100, **changes)
    rows = anomalies.latency_anomalies(store, now=NOW)["anomalies"]
    assert len(rows) == 2
    assert all(row["state"] == "INSUFFICIENT_EVIDENCE" for row in rows)


def test_unversioned_latency_cannot_claim_baseline_normal_or_deviation(store):
    series(store, metadata={"latency_kind": "strategy_evaluation"})
    [row] = anomalies.latency_anomalies(store, now=NOW)["anomalies"]
    assert row["state"] == "INSUFFICIENT_EVIDENCE"
    assert row["reasons"] == ["SOURCE_VERSION_OR_CONFIGURATION_UNVERIFIED"]
    assert row["threshold_ms"] is None


def test_extreme_latency_and_clock_ahead_of_receipt_are_not_fitted(store):
    sample(store, "extreme_latency", CUTOFF - timedelta(seconds=1), 1e308)
    ahead = sample(store, "receipt_ahead0", CUTOFF - timedelta(seconds=1))
    # Store receipt is authoritative; construct a separate event received early.
    persist(store, replace(ahead, event_id="early_receipt0"), received_at=CUTOFF - timedelta(seconds=10))
    view = anomalies.latency_anomalies(store, now=NOW)
    assert view["excluded_untyped_events"] == 2
    assert view["anomalies"][0]["current_samples"] == 1


def test_forming_future_late_import_and_before_window_samples_are_excluded(store):
    sample(store, "forming_000", CUTOFF, 1000)
    sample(store, "future_0000", CUTOFF + timedelta(hours=1), 1000)
    sample(store, "too_old_000", CUTOFF - timedelta(hours=25), 1000)
    late = replace(GuardianEvent(source_service="smc_lab", source_component="strategy",
        event_type="evaluation_completed", event_id="late_import_000", timestamp=CUTOFF - timedelta(minutes=1)),
        latency_ms=1000, metadata=META)
    persist(store, late, received_at=CUTOFF)
    assert anomalies.latency_anomalies(store, now=NOW)["observed_events"] == 0


def test_baseline_current_half_open_boundary_and_utc_normalization(store):
    boundary = CUTOFF - timedelta(minutes=5)
    sample(store, "boundary_a0", boundary)
    sample(store, "boundary_b0", boundary - timedelta(microseconds=1))
    sample(store, "boundary_c0", boundary.astimezone(timezone(timedelta(hours=3))))
    [row] = anomalies.latency_anomalies(store, now=NOW)["anomalies"]
    assert (row["baseline_samples"], row["current_samples"]) == (1, 2)


@pytest.mark.parametrize("metadata", [{"latency_kind": ["strategy_evaluation"]},
    {"latency_kind": "strategy_evaluation", "scope": {}}, {}, {"latency_kind": "unspecified"}])
def test_untyped_or_malformed_metric_identity_is_excluded_not_crash(store, metadata):
    sample(store, "untyped_000", CUTOFF - timedelta(seconds=1), metadata=metadata)
    view = anomalies.latency_anomalies(store, now=NOW)
    assert view["anomalies"] == [] and view["excluded_untyped_events"] == 1


@pytest.mark.parametrize("bound", ["_SCAN_LIMIT", "_SCAN_BYTES"])
def test_truncated_scan_cannot_claim_anomaly_or_normal_baseline(store, monkeypatch, bound):
    series(store)
    monkeypatch.setattr(anomalies, bound, 1 if bound == "_SCAN_LIMIT" else 2048)
    view = anomalies.latency_anomalies(store, now=NOW)
    assert view["scan_truncated"] is True and view["coverage_state"] == "INSUFFICIENT_EVIDENCE"
    assert all(row["state"] == "INSUFFICIENT_EVIDENCE" for row in view["anomalies"])


def event(store, name, kind, minute, **changes):
    value = replace(GuardianEvent(source_service="market_data", source_component="public_stream",
        event_type=kind, event_id=name, timestamp=NOW - timedelta(minutes=minute),
        metadata={"venue": "binance_usdm", "scope": "shared", "dependency_id": "native_streams_1"}), **changes)
    persist(store, value)
    return value


def incident(store):
    engine = GuardianIncidentEngine(store)
    engine.scan()
    return engine.list()[0]["incident_id"]


def test_investigation_sorts_source_not_arrival_and_shared_chain_remains_possible(store):
    event(store, "smc_stale_000", "stale_candle", 5, source_service="smc_lab", lab_id="SMC")
    event(store, "pa_stale_0000", "stale_candle", 4, source_service="pa_lab", lab_id="PRICE_ACTION")
    event(store, "disconnect_00", "websocket_disconnected", 6)
    event(store, "reconnect_000", "websocket_reconnected", 3)
    event(store, "sync_0000000", "feed_synchronized", 2, evidence={"closed_candle_continuity_verified": False})
    view = investigations.incident_investigation(store, incident(store), now=NOW)
    assert [row["event_id"] for row in view["timeline"]] == ["disconnect_00", "smc_stale_000", "pa_stale_0000", "reconnect_000", "sync_0000000"]
    [chain] = view["possible_chains"]
    assert chain["upstream_event_id"] == "disconnect_00"
    assert chain["downstream_event_ids"] == ["smc_stale_000", "pa_stale_0000"]
    assert chain["confidence"] == "POSSIBLE" and chain["causal_link_verified"] is False
    root = next(row for row in view["root_cause_candidates"] if row["evidence_ids"] == ["disconnect_00"])
    assert root["failure_fact_confidence"] == "CONFIRMED" and root["causal_confidence"] == "POSSIBLE"
    assert view["incident"]["state"] == "RECOVERING"
    assert view["strategy_defect_verified"] is False
    assert view["causal_chain_verified"] is False and view["automatic_action_allowed"] is False


def test_coincidence_without_explicit_dependency_and_wrong_time_do_not_prove_chain(store):
    meta = {"venue": "binance_usdm", "scope": "shared"}
    event(store, "earlier_stale", "stale_candle", 6, metadata=meta)
    event(store, "later_down_00", "websocket_disconnected", 5, metadata=meta)
    event(store, "later_stale_0", "stale_candle", 4, metadata=meta)
    view = investigations.incident_investigation(store, incident(store), now=NOW)
    assert view["possible_chains"] == []
    assert all(row["causal_confidence"] == "UNKNOWN" for row in view["root_cause_candidates"])


def test_same_execution_context_reconstructed_but_unrelated_owner_session_venue_not_invented(store):
    common = {"source_service": "smc_lab", "source_component": "paper_broker", "lab_id": "SMC",
              "session_id": "session-a", "execution_id": "execution-a", "symbol": "BTCUSDT", "timeframe": "5m"}
    event(store, "order_commit0", "paper_order_committed", 6, **common)
    event(store, "order_fill_00", "paper_order_filled", 5, **common)
    event(store, "journal_fail0", "journal_failed", 4, **common)
    event(store, "other_owner_0", "paper_order_filled", 3, **{**common, "lab_id": "PRICE_ACTION"})
    event(store, "other_session", "paper_order_filled", 3, **{**common, "session_id": "session-b"})
    event(store, "other_key_000", "paper_order_filled", 3, **{**common, "execution_id": "execution-b"})
    event(store, "other_venue_0", "paper_order_filled", 3, **common,
          metadata={"venue": "kraken_spot", "scope": "shared", "dependency_id": "native_streams_1"})
    view = investigations.incident_investigation(store, incident(store), now=NOW)
    assert [row["event_id"] for row in view["timeline"]] == ["order_commit0", "order_fill_00", "journal_fail0"]
    assert view["timeline"][0]["relationship"] == "SAME_EXECUTION_ID"
    assert all(row["execution_id"] == "execution-a" for row in view["timeline"])
    assert len(view["root_cause_candidates"]) == 1


def test_unrelated_venue_cannot_join_shared_dependency_context(store):
    event(store, "down_binance0", "websocket_disconnected", 6)
    event(store, "stale_kraken0", "stale_candle", 5,
          metadata={"venue": "kraken_spot", "scope": "shared", "dependency_id": "native_streams_1"})
    engine = GuardianIncidentEngine(store)
    engine.scan()
    target = next(row for row in engine.list() if "binance_usdm" in row["fingerprint"])
    view = investigations.incident_investigation(store, target["incident_id"], now=NOW)
    assert [row["event_id"] for row in view["timeline"]] == ["down_binance0"]


def test_normal_condition_failure_not_incident_or_root_cause(store):
    common = {"source_service": "smc_lab", "source_component": "strategy", "lab_id": "SMC", "correlation_id": "decision-a"}
    event(store, "worker_down00", "worker_crashed", 5, **common)
    event(store, "ordinary_wait", "condition_failed", 4, reason="NO_SETUP", **common)
    view = investigations.incident_investigation(store, incident(store), now=NOW)
    assert len(view["timeline"]) == 2 and len(view["root_cause_candidates"]) == 1
    assert view["root_cause_candidates"][0]["evidence_ids"] == ["worker_down00"]


def test_future_failure_is_visible_as_invalid_clock_not_root_cause(store):
    event(store, "future_down00", "websocket_disconnected", -60)
    view = investigations.incident_investigation(store, incident(store), now=NOW)
    assert view["timeline"][0]["clock_valid"] is False
    assert view["root_cause_candidates"] == [] and view["possible_chains"] == []
    assert view["coverage"]["clock_invalid_events"] == 1


def test_nonatomic_smc_findings_remain_possible_not_verified(store):
    event(store, "nonatomic_smc", "execution_integrity_observed", 3, source_service="guardian_smc_probe",
        source_component="smc_agent", execution_id="execution-a", reason="FILLED_ORDER_JOURNAL_PENDING",
        metadata={"paper_only": True}, evidence={"cross_database_atomic": False,
        "execution_integrity_verified": False, "execution_key": "execution-a", "integrity_code": "FILLED_ORDER_JOURNAL_PENDING"})
    view = investigations.incident_investigation(store, incident(store), now=NOW)
    assert view["root_cause_candidates"][0]["failure_fact_confidence"] == "POSSIBLE"
    assert view["causal_chain_verified"] is False


def test_long_incident_keeps_opening_anchor_and_reports_bounds(store, monkeypatch):
    event(store, "opening_down0", "websocket_disconnected", 100)
    for index in range(5):
        event(store, f"stale_{index:06}", "stale_candle", 5 - index)
    target = incident(store)
    monkeypatch.setattr(investigations, "_DIRECT_LIMIT", 2)
    view = investigations.incident_investigation(store, target, now=NOW)
    assert view["coverage"]["direct_evidence_truncated"] is True
    assert view["timeline"][0]["event_id"] == "opening_down0"
    assert len(view["timeline"]) == 3


def test_100_dashboard_refreshes_and_restart_create_no_revisions_or_incidents(store):
    series(store)
    event(store, "worker_down00", "worker_crashed", 5)
    target = incident(store)
    before = store.count()
    first = anomalies.latency_anomalies(store, now=NOW)
    investigation = investigations.incident_investigation(store, target, now=NOW)
    for _ in range(100):
        assert anomalies.latency_anomalies(store, now=NOW) == first
        assert investigations.incident_investigation(store, target, now=NOW) == investigation
    restarted = GuardianStore(store.path)
    assert anomalies.latency_anomalies(restarted, now=NOW) == first
    assert GuardianIncidentEngine(restarted).scan() == 0
    assert restarted.count() == before


def test_analysis_remains_available_during_short_wal_write(store):
    series(store)
    with closing(store._connect()) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO heartbeats VALUES('pending','FAILED','not committed',?)", (NOW.isoformat(),))
        assert anomalies.latency_anomalies(store, now=NOW)["anomalies"][0]["state"] == "LATENCY_DEVIATION"
        assert "pending" not in store.heartbeats()
        writer.rollback()


def test_missing_incident_and_naive_analysis_clock_fail_closed(store):
    GuardianIncidentEngine(store)
    assert investigations.incident_investigation(store, "0" * 32, now=NOW) is None
    for operation in (lambda: anomalies.latency_anomalies(store, now=NOW.replace(tzinfo=None)),
                      lambda: investigations.incident_investigation(store, "0" * 32, now=NOW.replace(tzinfo=None))):
        with pytest.raises(ValueError, match="aware"):
            operation()
