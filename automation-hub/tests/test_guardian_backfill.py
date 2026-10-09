"""Guardian backfill must cover retained decisions without trading writes."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
from config import settings
from services.guardian_read_model import lab_decision_page
from tradexa.guardian.lab_backfill import GuardianLabBackfill
from tradexa.guardian.store import GuardianStore

KEY = "guardian-backfill-independent-key-12345"
URL = "http://app:8000/guardian/evaluations"
NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc).isoformat()


def _source(path, prefix, count):
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE {prefix}_evaluations("
                     "correlation_id TEXT PRIMARY KEY,session_id TEXT,candle_time TEXT,"
                     "created_at TEXT,updated_at TEXT,symbol TEXT,timeframe TEXT,"
                     "strategy_id TEXT,strategy_version TEXT,"
                     + ("model_id TEXT," if prefix == "smc" else "")
                     + "state TEXT,reason TEXT,missing_conditions_json TEXT,payload_json TEXT)")
        for i in range(count):
            session = "old-session" if i < count // 2 else "current-session"
            conn.execute(
                f"INSERT INTO {prefix}_evaluations VALUES ({','.join('?' for _ in range(14 if prefix == 'smc' else 13))})",
                (f"{prefix}-{i}", session, NOW, NOW, NOW, "BTCUSDT", "5m",
                 "SMC_SOURCE_V1" if prefix == "smc" else "PA1_SR_REJECTION", "1.0",
                 *(("SMC_M1_SWEEP_REVERSAL",) if prefix == "smc" else ()),
                 "WATCHING", "waiting", '["rejection"]',
                 json.dumps({"source_evaluation" if prefix == "smc" else "trace": {
                     "ordered_condition_results" if prefix == "smc" else "conditions": [
                         {"key": "rejection", "status": "MISSING"}]}})),
            )


def _collector(client, store):
    def fetch(lab, after, anchor):
        response = client.get("/guardian/evaluations", params={
            "lab": lab, "after": after, "anchor": anchor},
            headers={"X-Guardian-Observer-Key": KEY})
        assert response.status_code == 200, response.text
        return response.json()

    return GuardianLabBackfill(store, URL, KEY, fetch=fetch)


def _configured_client(tmp_path, monkeypatch, *, pa_count=41, smc_count=2):
    pa, smc = tmp_path / "pa.db", tmp_path / "smc.db"
    _source(pa, "pa", pa_count)
    _source(smc, "smc", smc_count)
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "price_action_paper_db", str(pa))
    monkeypatch.setattr(settings, "smc_paper_db", str(smc))
    return TestClient(app_module.app), pa, smc


def test_cursor_pages_include_inactive_sessions_and_reject_source_rewind(tmp_path):
    pa = tmp_path / "pa.db"
    _source(pa, "pa", 41)
    first = lab_decision_page(pa, "PRICE_ACTION")
    assert len(first["evaluations"]) == 32
    assert first["has_more"] is True
    assert first["evaluations"][0]["session_id"] == "old-session"
    second = lab_decision_page(pa, "PRICE_ACTION", after=first["next_after"],
                               anchor=first["next_anchor"])
    assert len(second["evaluations"]) == 9
    assert second["has_more"] is False
    assert {row["correlation_id"] for page in (first, second)
            for row in page["evaluations"]} == {f"pa-{i}" for i in range(41)}
    with sqlite3.connect(pa) as conn:
        conn.execute("DELETE FROM pa_evaluations WHERE correlation_id=?",
                     (first["next_anchor"],))
    with pytest.raises(ValueError, match="cursor is invalid"):
        lab_decision_page(pa, "PRICE_ACTION", after=first["next_after"],
                          anchor=first["next_anchor"])


def test_backfill_route_is_read_only_and_separately_authenticated(tmp_path, monkeypatch):
    client, pa, _ = _configured_client(tmp_path, monkeypatch)
    assert client.get("/guardian/evaluations?lab=PRICE_ACTION").status_code == 401
    assert client.get("/guardian/evaluations?lab=PRICE_ACTION", headers={
        "X-Webhook-Secret": settings.admin_key}).status_code == 401
    response = client.get("/guardian/evaluations?lab=PRICE_ACTION", headers={
        "X-Guardian-Observer-Key": KEY})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["page"]["has_more"] is True
    assert response.json()["feed_health_verified"] is False
    assert client.post("/guardian/evaluations?lab=PRICE_ACTION", headers={
        "X-Guardian-Observer-Key": KEY}).status_code == 401
    assert sqlite3.connect(pa).execute("SELECT COUNT(*) FROM pa_evaluations").fetchone()[0] == 41


def test_source_lock_is_structured_and_retry_after_release_preserves_cursor(
        tmp_path, monkeypatch):
    client, pa, _ = _configured_client(tmp_path, monkeypatch, pa_count=2, smc_count=1)
    store = GuardianStore(tmp_path / "guardian.db")
    lock = sqlite3.connect(pa, timeout=0.1)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        response = client.get("/guardian/evaluations?lab=PRICE_ACTION", headers={
            "X-Guardian-Observer-Key": KEY})
    finally:
        lock.rollback()
        lock.close()
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert str(pa) not in response.text
    assert store.observer_cursor("pa_lab") == (0, "")
    assert _collector(client, store).poll() == 3
    assert store.observer_cursor("pa_lab") == (2, "pa-1")


def test_long_outage_backfill_restart_and_duplicate_poll_are_idempotent(tmp_path, monkeypatch):
    client, pa, smc = _configured_client(tmp_path, monkeypatch)
    store = GuardianStore(tmp_path / "guardian.db")
    collector = _collector(client, store)
    assert collector.poll() == 34
    assert store.heartbeats()["guardian_lab_backfill"]["state"] == "DEGRADED"
    assert store.observer_cursor("pa_lab") == (32, "pa-31")
    restarted = _collector(client, GuardianStore(tmp_path / "guardian.db"))
    assert restarted.poll() == 9
    assert restarted.poll() == 0
    assert store.heartbeats()["guardian_lab_backfill"]["state"] == "HEALTHY"
    assert store.count() == 43
    assert len({event["correlation_id"] for event in store.recent(50)}) == 43
    assert all(event["evidence"]["lifecycle_history_complete"] is False
               for event in store.recent(50))
    assert sqlite3.connect(pa).execute("SELECT COUNT(*) FROM pa_evaluations").fetchone()[0] == 41
    assert sqlite3.connect(smc).execute("SELECT COUNT(*) FROM smc_evaluations").fetchone()[0] == 2


def test_failed_second_event_rolls_back_page_and_checkpoint(tmp_path, monkeypatch):
    client, _, _ = _configured_client(tmp_path, monkeypatch, pa_count=2, smc_count=1)
    store = GuardianStore(tmp_path / "guardian.db")
    second_id = hashlib.sha256("PRICE_ACTION:pa-1".encode()).hexdigest()[:32]
    with sqlite3.connect(store.path) as conn:
        conn.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON events
                        WHEN NEW.event_id='""" + second_id + "' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        _collector(client, store).poll()
    assert store.count() == 0
    assert store.observer_cursor("pa_lab") == (0, "")
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TRIGGER fail_second")
    assert _collector(client, store).poll() == 3
    assert store.observer_cursor("pa_lab") == (2, "pa-1")
    assert store.count() == 3


def test_restart_after_page_commit_but_before_probe_heartbeat_is_idempotent(
        tmp_path, monkeypatch):
    client, _, _ = _configured_client(tmp_path, monkeypatch, pa_count=2, smc_count=1)
    store = GuardianStore(tmp_path / "guardian.db")
    collector = _collector(client, store)
    original = store.record_heartbeat

    def fail_after_commit(component, state, **kwargs):
        if component == "guardian_lab_backfill":
            raise sqlite3.OperationalError("injected heartbeat failure")
        return original(component, state, **kwargs)

    monkeypatch.setattr(store, "record_heartbeat", fail_after_commit)
    with pytest.raises(sqlite3.OperationalError, match="injected heartbeat"):
        collector.poll()
    assert store.count() == 3
    assert store.observer_cursor("pa_lab") == (2, "pa-1")
    assert store.observer_cursor("smc_lab") == (1, "smc-0")
    recovered = GuardianStore(tmp_path / "guardian.db")
    assert _collector(client, recovered).poll() == 0
    assert recovered.count() == 3
