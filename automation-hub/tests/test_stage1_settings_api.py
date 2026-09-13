import pytest


SECRET = "dev-control-key"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    from config import settings
    from data.ledger import SqliteLedger
    from services.trading_instances import TradingInstanceManager

    def factory(_key, _symbol):
        return object()

    manager = TradingInstanceManager(SqliteLedger(str(tmp_path / "instances.db")),
                                     strategy_factory=factory, live=False, live_poll_s=60)
    monkeypatch.setattr(webhook_api, "instance_manager", manager)
    monkeypatch.setattr(settings, "settings_path", str(tmp_path / "runtime.json"))
    app = FastAPI()
    app.include_router(webhook_api.router)
    return TestClient(app), manager, tmp_path


def test_platform_defaults_apply_only_to_new_instances(api):
    client, manager, _ = api
    old = manager.create(symbol="BTCUSDT", strategy_key="brain",
                         strategy_label="Decision Brain", strategy_version="v1",
                         timeframe="5m", risk_per_trade_pct=0.005,
                         capital_allocation=500)
    response = client.post("/instances/platform", headers={"X-Webhook-Secret": SECRET}, json={
        "default_symbol": "ETHUSDT", "default_timeframe": "5m",
        "default_strategy": "supertrend", "default_capital": 750,
        "default_risk_per_trade_pct": 0.004, "default_max_open_positions": 2,
        "default_entry_mode": "market", "default_fill_model": "PerfectFill",
        "max_instance_risk_per_trade_pct": 0.01,
    })
    assert response.status_code == 200, response.text
    created = client.post("/instances", headers={"X-Webhook-Secret": SECRET}, json={})
    assert created.status_code == 200, created.text
    row = created.json()["instance"]
    assert (row["symbol"], row["timeframe"], row["strategy_key"]) == ("ETHUSDT", "5m", "supertrend")
    assert row["capital_allocation"] == 750
    assert row["risk_per_trade_pct"] == 0.004
    assert row["entry_mode"] == "market"
    assert row["fill_model"] == "PerfectFill"
    assert manager.status(old.id)["symbol"] == "BTCUSDT"
    assert manager.status(old.id)["capital_allocation"] == 500


def test_global_instance_risk_ceiling_returns_field_error(api):
    client, _, _ = api
    saved = client.post("/instances/platform", headers={"X-Webhook-Secret": SECRET},
                        json={"max_instance_risk_per_trade_pct": 0.01})
    assert saved.status_code == 200
    rejected = client.post("/instances", headers={"X-Webhook-Secret": SECRET}, json={
        "symbol": "BTCUSDT", "strategy": "brain", "timeframe": "5m",
        "capital_allocation": 500, "risk_per_trade_pct": 0.02,
    })
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["field"] == "risk_per_trade_pct"


def test_fill_model_endpoint_persists_and_settings_return_no_secret_values(api, monkeypatch):
    client, _, tmp_path = api
    changed = client.post("/execution/fill-model", headers={"X-Webhook-Secret": SECRET},
                          json={"model": "realistic"})
    assert changed.status_code == 200
    from services.runtime_settings import load_overrides
    overrides = load_overrides(str(tmp_path / "runtime.json"))
    assert overrides["fill_model"] == "RealisticFill"
    import webhook_api
    from services.fill_model import PerfectFill, RealisticFill
    monkeypatch.setattr(webhook_api.paper, "fill_model", PerfectFill())
    webhook_api._apply_setting("fill_model", overrides["fill_model"])
    assert isinstance(webhook_api.paper.fill_model, RealisticFill)
    settings = client.get("/settings").json()
    assert settings["metadata"]["editable"]["scope"] == "legacy"
    assert settings["metadata"]["readonly"]["editable"] is False
    assert settings["metadata"]["readonly"]["restart_required"] is True
    assert SECRET not in str(settings)


def test_existing_stop_and_resume_controls_remain_functional(api):
    client, _, _ = api
    stopped = client.post("/controls/stop-all", headers={"X-Webhook-Secret": SECRET})
    assert stopped.status_code == 200
    assert client.get("/controls/state").json()["state"] == "Stopped"
    resumed = client.post("/controls/resume", headers={"X-Webhook-Secret": SECRET})
    assert resumed.status_code == 200
    assert client.get("/controls/state").json()["state"] == "Active"


def test_market_data_connection_endpoint_returns_probe_evidence(api, monkeypatch):
    client, _, _ = api
    import data.forward_market_data as forward
    monkeypatch.setattr(forward, "test_forward_connection", lambda symbol, timeframe, exchange: {
        "ok": True, "provider": f"live (ccxt:{exchange})", "market": "spot",
        "symbol": symbol, "timeframe": timeframe, "latency_ms": 4.2,
        "last_price_timestamp": "2026-08-22T00:00:00+00:00", "last_price": 123.45,
    })
    response = client.post("/market-data/test-connection", json={"symbol": "BTCUSDT", "timeframe": "5m"})
    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "provider": "live (ccxt:binance)", "market": "spot",
        "symbol": "BTCUSDT", "timeframe": "5m", "latency_ms": 4.2,
        "last_price_timestamp": "2026-08-22T00:00:00+00:00", "last_price": 123.45,
    }
