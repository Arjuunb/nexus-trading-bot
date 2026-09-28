"""Bot Health aggregation endpoint — one honest operational snapshot. Every
field comes from real engine / ledger / watchdog / skip-log state."""
import pytest


def test_health_bot_endpoint_shape_is_real():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    app = FastAPI(); app.include_router(webhook_api.router)
    client = TestClient(app)

    r = client.get("/health/bot").json()
    # all Bot Health sections present
    for key in ("engine", "data_source", "broker", "last_candle", "last_signal",
                "last_rejected", "open_positions", "daily_pnl", "risk",
                "watchdog", "errors"):
        assert key in r, f"missing {key}"

    # honest defaults: paper only, live locked, no faked broker
    assert r["broker"]["connected"] is False
    assert r["broker"]["live_locked"] is True
    assert r["engine"]["strategy"]  # real strategy label
    assert isinstance(r["errors"], list)
    assert isinstance(r["risk"]["exposure_pct"], (int, float))


def test_health_bot_surfaces_last_rejection_from_skip_log(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    from data.skipped_store import SkippedTradeStore

    assert webhook_api.pipeline.skipped is webhook_api.skipped_store
    skipped = SkippedTradeStore(str(tmp_path / "skipped.db"))
    monkeypatch.setattr(webhook_api, "skipped_store", skipped)
    monkeypatch.setattr(webhook_api.pipeline, "skipped", skipped)
    app = FastAPI(); app.include_router(webhook_api.router)
    client = TestClient(app)
    control = {"X-Webhook-Secret": webhook_api.settings.admin_key}
    webhook = {"X-Webhook-Secret": webhook_api.settings.webhook_secret}

    # force a rejection through the real pipeline, then it must appear in health
    assert client.get("/health/bot").json()["last_rejected"] is None
    assert client.post("/controls/stop-all", headers=control).status_code == 200
    try:
        response = client.post("/webhook/tradingview", headers=webhook, json={
            "alert_id": "health-rej", "symbol": "BTCUSDT", "side": "BUY",
            "entry": 100.0, "stop": 95.0})
        assert response.status_code == 200
        assert response.json()["stage"] == "controls"
        assert skipped.total() == 1
    finally:
        assert client.post("/controls/resume", headers=control).status_code == 200

    lr = client.get("/health/bot").json()["last_rejected"]
    assert lr is not None
    assert lr["stage"] == "controls" and "stopped" in lr["reason"].lower()
