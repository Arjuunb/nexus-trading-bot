"""Reports/alerts stay evidence-backed, immutable, replay-safe and Guardian-only."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.incidents import GuardianIncidentEngine
from tradexa.guardian.notifications import GuardianNotifications
from tradexa.guardian.reports import GuardianReports
from tradexa.guardian.store import GuardianStore

START = datetime(2026, 9, 28, tzinfo=timezone.utc)  # Monday
NOW = START + timedelta(days=8)


def decision(**changes):
    return replace(GuardianEvent(
        event_id="lab_decision_original", timestamp=START + timedelta(hours=12),
        source_service="guardian_lab_probe", source_component="smc_lab",
        event_type="lab_evaluation_observed", lab_id="SMC", session_id="session_1",
        correlation_id="same_decision", strategy_id="SMC", strategy_version="1.0",
        symbol="BTCUSDT", timeframe="5m", decision="WATCHING", reason="REJECTION_MISSING",
        evidence={"conditions": [{"key": "htf", "status": "PASS"},
                                 {"key": "rejection", "status": "MISSING"}],
                  "missing_conditions": ["rejection"], "condition_trace_available": True}), **changes)


def test_empty_report_is_unknown_not_zero_trades_and_replay_is_idempotent(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    reports = GuardianReports(store)
    result = reports.generate("DAILY", START, now=NOW)
    assert result["coverage"]["observed_events"] == 0
    assert result["coverage"]["all_evaluations_verified"] is False
    assert all(value is None for value in result["unavailable_metrics"].values())
    for _ in range(100):
        assert reports.generate("DAILY", START, now=NOW + timedelta(hours=1)) == result
    assert len(reports.list()) == store.count() == 1
    restarted = GuardianReports(GuardianStore(store.path))
    assert restarted.generate("DAILY", START, now=NOW)["report_id"] == result["report_id"]


def test_decision_snapshots_deduplicate_and_durable_state_wins(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    reports = GuardianReports(store)
    store.append(decision())
    store.append(decision(event_id="lab_decision_backfill", source_service="guardian_lab_backfill",
                          event_type="lab_evaluation_backfilled"))
    store.append(decision(event_id="lab_decision_lifecycle", source_service="guardian_lab_lifecycle",
                          event_type="lab_lifecycle_observed", decision="STAGED",
                          evidence={"source_sequence": 2, "missing_conditions": []}))
    result = reports.generate("DAILY", START, now=NOW)
    assert result["coverage"]["observed_events"] == 3
    assert len(result["strategies"]) == 1
    group = result["strategies"][0]
    assert group["observed_decisions"] == 1
    assert group["decisions_by_state"] == {"STAGED": 1}
    assert group["unproven_near_valid_candidates"] == 0
    assert group["exact_version_verified"] is False


def test_versions_owners_symbols_and_configurations_are_not_pooled(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    reports = GuardianReports(store)
    variants = [dict(), {"strategy_version": "2.0"}, {"symbol": "ETHUSDT"},
                {"session_id": "session_2"}, {"timeframe": "15m"},
                {"metadata": {"config_hash": "a" * 64, "code_commit": "b" * 40}}]
    for i, changes in enumerate(variants):
        store.append(decision(event_id=f"separate_decision_{i}", correlation_id=f"decision_{i}", **changes))
    result = reports.generate("DAILY", START, now=NOW)
    assert len(result["strategies"]) == 6
    assert all(row["observed_decisions"] == 1 for row in result["strategies"])
    assert sum(row["exact_version_verified"] for row in result["strategies"]) == 1
    assert all(row["performance_verified"] is False for row in result["strategies"])
    assert sum(row["unproven_near_valid_candidates"] for row in result["strategies"]) == 6


def test_late_evidence_creates_immutable_revision_not_rewrite(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    reports = GuardianReports(store)
    old = reports.generate("DAILY", START, now=NOW)
    store.append(decision())
    new = reports.generate("DAILY", START, now=NOW + timedelta(minutes=1))
    assert new["report_id"] != old["report_id"]
    assert old["coverage"]["observed_events"] == 0 and new["coverage"]["observed_events"] == 1
    assert len(reports.list()) == 2
    with sqlite3.connect(store.path) as conn:
        for query in ("DELETE FROM guardian_reports", "UPDATE guardian_reports SET kind='WEEKLY'"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(query)


@pytest.mark.parametrize("kind,start,now", [
    ("DAILY", START, START + timedelta(hours=23)),
    ("DAILY", START + timedelta(minutes=1), NOW),
    ("WEEKLY", START + timedelta(days=1), NOW),
    ("MONTHLY", START, NOW), ("DAILY", START.replace(tzinfo=None), NOW),
])
def test_forming_or_invalid_utc_windows_fail_closed(tmp_path, kind, start, now):
    reports = GuardianReports(GuardianStore(tmp_path / "guardian.db"))
    with pytest.raises(ValueError):
        reports.generate(kind, start, now=now)
    assert reports.list() == []


def test_window_left_closed_right_open_and_due_periods(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    reports = GuardianReports(store)
    for i, timestamp in enumerate([START - timedelta(seconds=1), START,
                                   START + timedelta(days=1) - timedelta(microseconds=1),
                                   START + timedelta(days=1)]):
        store.append(decision(event_id=f"boundary_event_{i}", timestamp=timestamp,
                              correlation_id=f"decision_{i}"))
    assert reports.generate("DAILY", START, now=NOW)["coverage"]["observed_events"] == 2
    due = reports.generate_due(now=START + timedelta(days=7, hours=1))
    assert due[0]["window_start"] == (START + timedelta(days=6)).isoformat()
    assert due[1]["window_start"] == START.isoformat()
    assert due[1]["kind"] == "WEEKLY"


def test_report_scan_limits_are_explicit_and_no_false_complete_claim(tmp_path, monkeypatch):
    import tradexa.guardian.reports as module
    monkeypatch.setattr(module, "_SCAN_BYTES", 1500)
    store = GuardianStore(tmp_path / "guardian.db")
    for i in range(3):
        store.append(decision(event_id=f"large_evidence_{i}", correlation_id=f"decision_{i}"))
    report = GuardianReports(store).generate("DAILY", START, now=NOW)
    assert report["coverage"]["scan_truncated"] is True
    assert report["coverage"]["received_events_in_window"] == 3
    assert report["coverage"]["observed_events"] < 3


def test_report_and_audit_are_atomic_on_failure_retry_and_concurrency(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    reports = GuardianReports(store)
    with sqlite3.connect(store.path) as conn:
        conn.execute("CREATE TRIGGER fail_report_audit BEFORE INSERT ON events "
                     "WHEN NEW.event_type='report_created' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        reports.generate("DAILY", START, now=NOW)
    assert store.count() == 0 and reports.list() == []
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TRIGGER fail_report_audit")
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: reports.generate("DAILY", START, now=NOW), range(3)))
    assert len({result["report_id"] for result in results}) == 1
    assert len(reports.list()) == store.count() == 1


def outage(**changes):
    return replace(GuardianEvent(source_service="market_feed", source_component="market_data",
        event_type="websocket_disconnected", severity="WARNING",
        metadata={"venue": "binance_usdm", "scope": "shared"}), **changes)


def test_two_hundred_outage_updates_create_one_notice_and_restart_no_duplicates(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    incidents, notifications = GuardianIncidentEngine(store), GuardianNotifications(store)
    for i in range(200):
        store.append(outage(event_id=f"outage_update_{i}"))
    incidents.scan()
    assert notifications.scan() == 200
    assert len(notifications.list()) == 1
    notice = notifications.list()[0]
    assert notice["category"] == "OPEN" and notice["channel"] == "IN_APP"
    assert notice["remediation_performed"] is False
    assert GuardianNotifications(GuardianStore(store.path)).scan() == 0
    assert len(notifications.list()) == 1
    assert sum(event["event_type"] == "notification_created" for event in store.recent(500)) == 1


def test_only_escalation_and_verified_recovery_notify_not_transport_alone(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    incidents, notices = GuardianIncidentEngine(store), GuardianNotifications(store)
    store.append(outage())
    store.append(outage(event_type="sequence_gap"))
    store.append(outage(event_type="websocket_reconnected"))
    store.append(outage(event_type="feed_synchronized", evidence={}))
    store.append(outage(event_type="feed_synchronized", evidence={"closed_candle_continuity_verified": True}))
    incidents.scan()
    notices.scan()
    assert {item["category"] for item in notices.list()} == {"OPEN", "ESCALATED", "RECOVERED"}
    assert len(notices.list()) == 3
    recovery = next(item for item in notices.list() if item["category"] == "RECOVERED")
    assert recovery["confidence"] == "CONFIRMED"


def test_no_setup_or_near_valid_candidate_does_not_notify(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    incidents, notifications = GuardianIncidentEngine(store), GuardianNotifications(store)
    store.append(decision())
    store.append(outage(event_type="condition_failed", reason="NO_SETUP"))
    incidents.scan()
    assert notifications.scan() == 0
    assert notifications.list() == []


def test_notification_cursor_notice_and_audit_commit_together(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    incidents, notices = GuardianIncidentEngine(store), GuardianNotifications(store)
    store.append(outage())
    incidents.scan()
    with sqlite3.connect(store.path) as conn:
        conn.execute("CREATE TRIGGER fail_notice_audit BEFORE INSERT ON events "
                     "WHEN NEW.event_type='notification_created' BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        notices.scan()
    assert notices.list() == [] and store.count() == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT last_update_sequence FROM guardian_notification_cursor").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_notice_audit")
    assert notices.scan() == 1
    assert len(notices.list()) == 1 and store.count() == 2
    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM guardian_notifications")
