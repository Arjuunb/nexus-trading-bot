"""Instance gate evidence is immutable, bounded, and independent of trading."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
from config import settings
from data.decision_store import DecisionStore
from services.guardian_instance_read_model import instance_decision_page
from tradexa.guardian.instance_decisions import GuardianInstanceDecisions
from tradexa.guardian.store import GuardianStore


KEY = "guardian-instance-decision-observer-key-12345"
DECISION_TIME = "2026-10-02T12:00:00+00:00"


def _decision(*, identity="instance-1:BTCUSDT:5m:2026-10-02T12:00:00Z",
              instance_id="instance-1"):
    return {
        "ts": DECISION_TIME, "symbol": "BTCUSDT", "timeframe": "5m",
        "strategy": "Adaptive MTF Trend Pullback", "side": "long",
        "decision": "accepted", "reason": "strategy setup accepted",
        "passed_rules": ["higher timeframe closed", "risk reward pass"],
        "failed_rules": [], "components": {"rolling_candles": list(range(500))},
        "instance_id": instance_id, "decision_identity": identity,
    }


def _count(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM guardian_decision_lifecycle").fetchone()[0]


def test_material_states_are_exactly_once_and_transient_updates_do_not_amplify(tmp_path):
    path = tmp_path / "decisions.db"
    store = DecisionStore(str(path))
    did = store.record(_decision())
    assert store.record(_decision()) == did
    assert _count(path) == 1
    for index in range(100):
        store._c.execute(
            "UPDATE decisions SET components_json=?, passed_json=? WHERE id=?",
            (f'{{"display_quote":{index},"heartbeat":{index}}}',
             '["higher timeframe closed", "risk reward pass"]', did),
        )
    store._c.commit()
    assert _count(path) == 1
    store.finalize(did, final_state="GATE_REJECTED", gate_stage="correlation",
                   reason="correlation limit", blocker="GATE_REJECTED: CORRELATION_LIMIT")
    store.finalize(did, final_state="GATE_REJECTED", gate_stage="correlation",
                   reason="correlation limit", blocker="GATE_REJECTED: CORRELATION_LIMIT")
    assert _count(path) == 2
    page = instance_decision_page(path)
    assert [row["final_state"] for row in page["transitions"]] == [
        "QUALIFIED", "GATE_REJECTED"]
    assert page["transitions"][1]["gate_stage"] == "correlation"
    assert page["transitions"][1]["blocker"] == "GATE_REJECTED: CORRELATION_LIMIT"
    assert page["transitions"][0]["passed_rules"] == [
        {"key": "higher timeframe closed", "status": "PASS"},
        {"key": "risk reward pass", "status": "PASS"},
    ]
    assert "rolling_candles" not in str(page)
    assert "display_quote" not in str(page)
    store.mark_executed(did)
    store.mark_executed(did)
    assert _count(path) == 3
    assert instance_decision_page(path)["transitions"][-1]["executed"] is True


def test_only_attributed_instances_are_exported_and_pruning_keeps_evidence(tmp_path):
    path = tmp_path / "decisions.db"
    store = DecisionStore(str(path))
    store.record(_decision(identity="legacy", instance_id=""))
    assert _count(path) == 0
    store.record(_decision(identity="instance-one"))
    store.record(_decision(identity="instance-two"))
    assert store.prune(keep=1) == 2
    assert _count(path) == 2
    page = instance_decision_page(path)
    with pytest.raises(ValueError, match="cursor is invalid"):
        instance_decision_page(path, after=page["next_after"], anchor="wrong")
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM guardian_decision_lifecycle WHERE sequence=1")


def test_outbox_failure_prevents_source_transition_then_retry_succeeds(tmp_path):
    path = tmp_path / "decisions.db"
    store = DecisionStore(str(path))
    store._c.execute("""CREATE TRIGGER reject_guardian_evidence
        BEFORE INSERT ON guardian_decision_lifecycle
        BEGIN SELECT RAISE(ABORT,'injected evidence failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected evidence failure"):
        store.record(_decision())
    assert store.count() == 0
    store._c.execute("DROP TRIGGER reject_guardian_evidence")
    did = store.record(_decision())
    assert _count(path) == 1
    store._c.execute("""CREATE TRIGGER reject_guardian_evidence
        BEFORE INSERT ON guardian_decision_lifecycle
        BEGIN SELECT RAISE(ABORT,'injected evidence failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected evidence failure"):
        store.finalize(did, final_state="GATE_REJECTED", gate_stage="risk",
                       reason="risk cap", blocker="GATE_REJECTED: RISK_CAP")
    assert store.get(did)["final_state"] == "QUALIFIED"
    assert _count(path) == 1
    store._c.execute("DROP TRIGGER reject_guardian_evidence")
    store.finalize(did, final_state="GATE_REJECTED", gate_stage="risk",
                   reason="risk cap", blocker="GATE_REJECTED: RISK_CAP")
    assert _count(path) == 2


def test_observer_route_is_key_scoped_read_only_and_structured_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    DecisionStore(str(path)).record(_decision())
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "decisions_db", str(path))
    client = TestClient(app_module.app)
    assert client.get("/guardian/instance-decisions").status_code == 401
    assert client.get("/guardian/instance-decisions", headers={
        "X-Webhook-Secret": settings.admin_key}).status_code == 401
    response = client.get("/guardian/instance-decisions", headers={
        "X-Guardian-Observer-Key": KEY})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["page"]["transitions"][0]["instance_id"] == "instance-1"
    assert client.post("/guardian/instance-decisions", headers={
        "X-Guardian-Observer-Key": KEY}).status_code in (401, 405)
    monkeypatch.setattr(settings, "decisions_db", str(tmp_path / "missing.db"))
    missing = client.get("/guardian/instance-decisions", headers={
        "X-Guardian-Observer-Key": KEY})
    assert missing.status_code == 503
    assert missing.json()["detail"]["code"] == "INSTANCE_DECISION_EVIDENCE_UNAVAILABLE"
    assert str(tmp_path) not in missing.text


def test_observer_read_during_source_write_and_persistent_lock(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    source = DecisionStore(str(path))
    source.record(_decision())
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "decisions_db", str(path))
    client = TestClient(app_module.app)
    headers = {"X-Guardian-Observer-Key": KEY}
    writer = sqlite3.connect(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE decisions SET components_json='{}' WHERE id=1")
    try:
        assert client.get("/guardian/instance-decisions", headers=headers).status_code == 200
    finally:
        writer.rollback()
        writer.close()
    writer = sqlite3.connect(path)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        blocked = client.get("/guardian/instance-decisions", headers=headers)
        assert blocked.status_code == 503
        assert blocked.json()["detail"]["state"] == "PERSISTENCE_BLOCKED"
    finally:
        writer.rollback()
        writer.close()
    assert client.get("/guardian/instance-decisions", headers=headers).status_code == 200


def test_restart_import_is_idempotent_and_cursor_is_atomic(tmp_path):
    path = tmp_path / "decisions.db"
    source = DecisionStore(str(path))
    did = source.record(_decision())
    source.finalize(did, final_state="GATE_REJECTED", gate_stage="risk",
                    reason="risk cap", blocker="GATE_REJECTED: RISK_CAP")
    guardian = GuardianStore(tmp_path / "guardian.db")

    def fetch(after, anchor):
        return {"schema_version": 1,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "scope": "POST_INSTALL_INSTANCE_DECISION_LIFECYCLE",
                "feed_health_verified": False, "execution_integrity_verified": False,
                "page": instance_decision_page(path, after=after, anchor=anchor)}

    collector = GuardianInstanceDecisions(
        guardian, "http://app:8000/guardian/instance-decisions", KEY, fetch=fetch)
    assert collector.poll() == 2
    assert collector.poll() == 0
    restarted = GuardianInstanceDecisions(
        GuardianStore(guardian.path), "http://app:8000/guardian/instance-decisions",
        KEY, fetch=fetch)
    assert restarted.poll() == 0
    assert guardian.count() == 2
    assert guardian.observer_cursor("instance_decisions") == (
        instance_decision_page(path)["next_after"],
        instance_decision_page(path)["next_anchor"])
    events = guardian.recent(source_service="guardian_instance_decisions")
    assert events[0]["evidence"]["blocker"] == "GATE_REJECTED: RISK_CAP"
    assert events[0]["evidence"]["strategy_verdict"] == "accepted"
    assert events[0]["evidence"]["execution_integrity_verified"] is False


def test_failed_guardian_import_does_not_advance_cursor(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    DecisionStore(str(path)).record(_decision())
    guardian = GuardianStore(tmp_path / "guardian.db")

    def fetch(after, anchor):
        return {"schema_version": 1,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "scope": "POST_INSTALL_INSTANCE_DECISION_LIFECYCLE",
                "feed_health_verified": False, "execution_integrity_verified": False,
                "page": instance_decision_page(path, after=after, anchor=anchor)}

    collector = GuardianInstanceDecisions(
        guardian, "http://app:8000/guardian/instance-decisions", KEY, fetch=fetch)
    actual_append = guardian.append_observed_page

    def failed_append(*args, **kwargs):
        raise sqlite3.IntegrityError("injected Guardian write failure")

    monkeypatch.setattr(guardian, "append_observed_page", failed_append)
    with pytest.raises(sqlite3.IntegrityError, match="injected Guardian write failure"):
        collector.poll()
    assert guardian.observer_cursor("instance_decisions") == (0, "")
    assert guardian.count() == 0
    monkeypatch.setattr(guardian, "append_observed_page", actual_append)
    assert collector.poll() == 1
    assert guardian.count() == 1
