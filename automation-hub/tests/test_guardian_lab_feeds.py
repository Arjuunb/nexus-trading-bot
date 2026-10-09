"""Guardian feed probes read local status; they cannot start or trade a lab."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app as app_module
import webhook_api
from config import settings
from services.guardian_lab_feed_read_model import lab_feed_snapshot

KEY = "independent-guardian-feed-key-123456"


class Stream:
    def __init__(self, **changes):
        self.calls = 0
        self.data = {
            "state": "SYNCHRONIZED", "reliable": True,
            "symbol": "BTCUSDT", "timeframe": "5m",
            "reconciliation_complete": True, "unresolved_missing_candles": 0,
            "last_closed_update": "2026-09-29T12:00:00+00:00",
            "last_update": "2026-09-29T12:00:01+00:00",
            "closed_candle_age_seconds": 1,
            "freshness_thresholds_seconds": {"completed_candle": 330},
            "failing_dependency": None,
        } | changes

    def status(self):
        self.calls += 1
        return dict(self.data)


def _db(path, prefix):
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"CREATE TABLE {prefix}_sessions(id TEXT,status TEXT,started_at TEXT,"
            "symbol TEXT,timeframe TEXT,mode TEXT,operating_mode TEXT)")
        connection.execute(
            f"INSERT INTO {prefix}_sessions VALUES "
            "('session-1','active','2026-09-29T12:00:00+00:00',"
            "'BTCUSDT','5m','LIVE_PAPER','automatic')")


def test_feed_health_requires_source_reconciliation_and_matching_market(tmp_path):
    path = tmp_path / "smc.db"
    _db(path, "smc")
    stream = Stream()
    reconciled = {"state": "SYNCHRONIZED", "reliable": True,
                  "last_closed_update": stream.data["last_closed_update"]}
    healthy = lab_feed_snapshot(path, "SMC", stream, reconciled=reconciled)
    assert healthy["component_state"] == "HEALTHY"
    assert healthy["execution_health_verified"] is False
    assert stream.calls == 1
    assert lab_feed_snapshot(path, "SMC", stream)["reason"] == "CHART_RECONCILIATION_UNVERIFIED"
    assert lab_feed_snapshot(path, "SMC", stream, reconciled={
        **reconciled, "last_closed_update": "2026-09-29T11:55:00+00:00"})["reliable"] is False
    stream.data["closed_candle_age_seconds"] = 360
    assert lab_feed_snapshot(path, "SMC", stream, reconciled=reconciled)["reliable"] is False
    stream.data["closed_candle_age_seconds"] = 1
    stream.data["symbol"] = "ETHUSDT"
    mismatch = lab_feed_snapshot(path, "SMC", stream, reconciled=reconciled)
    assert mismatch["component_state"] == "BLOCKED"
    assert mismatch["reason"] == "STREAM_IDENTITY_MISMATCH"


def test_stale_pa_stream_never_reports_healthy(tmp_path):
    path = tmp_path / "pa.db"
    _db(path, "pa")
    stale = lab_feed_snapshot(path, "PRICE_ACTION", Stream(
        state="STALE_CANDLES", reliable=False,
        failing_dependency="BINANCE_USDM_KLINE_STREAM"))
    assert stale["component_state"] == "BLOCKED"
    assert stale["failing_dependency"] == "BINANCE_USDM_KLINE_STREAM"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE pa_sessions SET mode='HISTORICAL_REPLAY'")
    stream = Stream()
    replay = lab_feed_snapshot(path, "PRICE_ACTION", stream)
    assert replay["component_state"] == "UNKNOWN"
    assert replay["reason"] == "SESSION_NOT_LIVE_PAPER"
    assert stream.calls == 0


def test_feed_route_uses_separate_key_and_only_status_snapshots(tmp_path, monkeypatch):
    pa, smc = tmp_path / "pa.db", tmp_path / "smc.db"
    _db(pa, "pa")
    _db(smc, "smc")
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "price_action_paper_db", str(pa))
    monkeypatch.setattr(settings, "smc_paper_db", str(smc))
    pa_stream, smc_stream = Stream(), Stream()
    monkeypatch.setattr(webhook_api, "price_action_runtime", SimpleNamespace(stream=pa_stream))
    monkeypatch.setattr(webhook_api, "smc_runtime", SimpleNamespace(
        stream=smc_stream, last_market_health={
            "state": "SYNCHRONIZED", "reliable": True,
            "last_closed_update": smc_stream.data["last_closed_update"]}))
    client = TestClient(app_module.app)
    assert client.get("/guardian/lab-feeds").status_code == 401
    assert client.get("/guardian/lab-feeds", headers={
        "X-Webhook-Secret": settings.admin_key}).status_code == 401
    response = client.get("/guardian/lab-feeds", headers={
        "X-Guardian-Observer-Key": KEY})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["execution_health_verified"] is False
    assert [feed["component_state"] for feed in body["feeds"]] == ["HEALTHY", "HEALTHY"]
    assert pa_stream.calls == smc_stream.calls == 1
    assert "quote" not in response.text and "last_error" not in response.text


def test_locked_session_source_fails_closed_without_leaking_path(tmp_path, monkeypatch):
    pa, smc = tmp_path / "pa.db", tmp_path / "smc.db"
    _db(pa, "pa")
    _db(smc, "smc")
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "price_action_paper_db", str(pa))
    monkeypatch.setattr(settings, "smc_paper_db", str(smc))
    monkeypatch.setattr(webhook_api, "price_action_runtime", SimpleNamespace(stream=Stream()))
    monkeypatch.setattr(webhook_api, "smc_runtime", SimpleNamespace(stream=Stream(),
                         last_market_health={}))
    lock = sqlite3.connect(pa, timeout=0.1)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        response = TestClient(app_module.app).get(
            "/guardian/lab-feeds", headers={"X-Guardian-Observer-Key": KEY})
    finally:
        lock.rollback()
        lock.close()
    assert response.status_code == 503
    assert response.json()["detail"]["state"] == "PERSISTENCE_BLOCKED"
    assert str(pa) not in response.text
