"""Guardian's opt-in exporter must observe only saved, committed lab evidence."""
import json
import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import app as app_module
from config import settings, validate_credential_separation
from services.guardian_read_model import lab_decision_snapshot
from tradexa.guardian.lab_observer import GuardianLabObserver
from tradexa.guardian.store import GuardianStore

KEY = "guardian-observer-unit-test-key-12345"
NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc).isoformat()


def _lab_db(path, prefix, payload):
    with sqlite3.connect(path) as conn:
        conn.execute(f"CREATE TABLE {prefix}_sessions(id TEXT,status TEXT,started_at TEXT)")
        conn.execute(f"""CREATE TABLE {prefix}_evaluations(
            correlation_id TEXT, session_id TEXT, candle_time TEXT, created_at TEXT,
            updated_at TEXT, symbol TEXT, timeframe TEXT, strategy_id TEXT,
            strategy_version TEXT, {'model_id TEXT,' if prefix == 'smc' else ''}
            state TEXT, reason TEXT, missing_conditions_json TEXT, payload_json TEXT)""")
        conn.execute(f"INSERT INTO {prefix}_sessions VALUES('session-1','active',?)", (NOW,))
        columns = ("correlation_id,session_id,candle_time,created_at,updated_at,symbol,"
                   "timeframe,strategy_id,strategy_version,"
                   + ("model_id," if prefix == "smc" else "")
                   + "state,reason,missing_conditions_json,payload_json")
        values = ["decision-1", "session-1", NOW, NOW, NOW, "BTCUSDT", "5m",
                  "SMC_SOURCE_V1" if prefix == "smc" else "PA1_SR_REJECTION",
                  "1.0", *(["SMC_M1_SWEEP_REVERSAL"] if prefix == "smc" else []),
                  "WATCHING", "waiting for rejection", json.dumps(["rejection"]), json.dumps(payload)]
        conn.execute(f"INSERT INTO {prefix}_evaluations({columns}) VALUES({','.join('?' for _ in values)})", values)


def test_lab_snapshot_is_bounded_read_only_and_preserves_condition_results(tmp_path):
    smc = tmp_path / "smc.db"
    _lab_db(smc, "smc", {"source_evaluation": {"ordered_condition_results": [
        {"key": "htf_context", "status": "PASS", "detail": "4H bias"},
        {"key": "rejection", "status": "MISSING", "detail": "not yet"},
    ]}})
    result = lab_decision_snapshot(smc, "SMC")
    assert result["coverage"] == "LATEST_ACTIVE_SESSION_ONLY"
    [row] = result["evaluations"]
    assert row["conditions"] == [{"key": "htf_context", "status": "PASS"},
                                 {"key": "rejection", "status": "MISSING"}]
    assert row["missing_conditions"] == ["rejection"]
    assert row["strategy_version"] == "1.0"
    assert "detail" not in str(result)
    assert sqlite3.connect(smc).execute("SELECT COUNT(*) FROM smc_evaluations").fetchone()[0] == 1


def test_route_requires_separate_key_and_never_invokes_trading_runtime(tmp_path, monkeypatch):
    pa, smc = tmp_path / "pa.db", tmp_path / "smc.db"
    _lab_db(pa, "pa", {"trace": {"conditions": [
        {"key": "zone", "status": "PASS", "detail": "internal strategy detail"}]}})
    _lab_db(smc, "smc", {"source_evaluation": {"ordered_condition_results": []}})
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "price_action_paper_db", str(pa))
    monkeypatch.setattr(settings, "smc_paper_db", str(smc))
    client = TestClient(app_module.app)
    assert client.get("/guardian/observations").status_code == 401
    assert client.get("/guardian/observations", headers={
        "X-Guardian-Observer-Key": "wrong-key"}).status_code == 401
    assert client.get("/guardian/observations", headers={
        "X-Webhook-Secret": settings.admin_key}).status_code == 401
    assert client.get("/instances", headers={
        "X-Guardian-Observer-Key": KEY}).status_code == 401
    assert client.post("/guardian/observations", headers={
        "X-Guardian-Observer-Key": KEY}).status_code == 401
    response = client.get("/guardian/observations", headers={"X-Guardian-Observer-Key": KEY})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["feed_health_verified"] is False
    assert body["execution_integrity_verified"] is False
    assert [lab["lab"] for lab in body["labs"]] == ["PRICE_ACTION", "SMC"]
    assert body["labs"][0]["evaluations"][0]["conditions"] == [
        {"key": "zone", "status": "PASS"}]


def test_locked_source_returns_structured_503_not_partial_evidence(tmp_path, monkeypatch):
    pa, smc = tmp_path / "pa.db", tmp_path / "smc.db"
    _lab_db(pa, "pa", {"trace": {"conditions": []}})
    _lab_db(smc, "smc", {"source_evaluation": {"ordered_condition_results": []}})
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "price_action_paper_db", str(pa))
    monkeypatch.setattr(settings, "smc_paper_db", str(smc))
    lock = sqlite3.connect(pa, timeout=0.1)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        response = TestClient(app_module.app).get(
            "/guardian/observations", headers={"X-Guardian-Observer-Key": KEY})
    finally:
        lock.rollback()
        lock.close()
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert str(pa) not in response.text


def test_saved_decision_reaches_guardian_without_trading_write(tmp_path, monkeypatch):
    pa, smc = tmp_path / "pa.db", tmp_path / "smc.db"
    _lab_db(pa, "pa", {"trace": {"conditions": [
        {"key": "zone", "status": "PASS"}, {"key": "rejection", "status": "MISSING"}]}})
    _lab_db(smc, "smc", {"source_evaluation": {"ordered_condition_results": []}})
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "price_action_paper_db", str(pa))
    monkeypatch.setattr(settings, "smc_paper_db", str(smc))
    client = TestClient(app_module.app)
    guardian = GuardianStore(tmp_path / "guardian.db")

    def fetch():
        response = client.get("/guardian/observations", headers={
            "X-Guardian-Observer-Key": KEY})
        assert response.status_code == 200
        return response.json()

    collector = GuardianLabObserver(guardian, "http://app:8000/guardian/observations",
                                    KEY, fetch=fetch)
    assert collector.poll() == 2
    assert collector.poll() == 0
    assert guardian.count() == 2
    assert {row["source_component"] for row in guardian.recent()} == {"pa_lab", "smc_lab"}
    assert sqlite3.connect(pa).execute("SELECT COUNT(*) FROM pa_evaluations").fetchone()[0] == 1
    assert sqlite3.connect(smc).execute("SELECT COUNT(*) FROM smc_evaluations").fetchone()[0] == 1


def test_observer_key_cannot_reuse_control_credential(monkeypatch):
    monkeypatch.setattr(settings, "guardian_observer_key", settings.admin_key)
    try:
        validate_credential_separation(production=False)
    except RuntimeError as exc:
        assert "HUB_GUARDIAN_OBSERVER_KEY" in str(exc)
    else:
        raise AssertionError("shared Observer/Control key was accepted")
