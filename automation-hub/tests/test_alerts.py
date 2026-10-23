"""Alerts System (#9): rule evaluation, channel status, fail-safe senders."""
import pytest

from services.alerts import evaluate_alerts, AlertChannels, send_discord, dispatch_alert


def test_high_drawdown_alert():
    a = evaluate_alerts({"drawdown_pct": 14.0})
    assert any(x["type"] == "high_drawdown" and x["severity"] == "critical" for x in a)
    assert evaluate_alerts({"drawdown_pct": 3.0}) == []


def test_fear_and_greed_extremes():
    assert any(x["type"] == "extreme_fear" for x in evaluate_alerts({"fear_greed": 12}))
    assert any(x["type"] == "extreme_greed" for x in evaluate_alerts({"fear_greed": 88}))
    assert evaluate_alerts({"fear_greed": 50}) == []


def test_funding_spike_and_liquidations():
    assert any(x["type"] == "funding_spike" for x in evaluate_alerts({"funding_rate_pct": 0.09}))
    assert evaluate_alerts({"funding_rate_pct": 0.01}) == []
    assert any(x["type"] == "large_liquidations" for x in evaluate_alerts({"liquidations_usd": 120_000_000}))


def test_trade_and_underperforming_events():
    a = evaluate_alerts({
        "events": [{"kind": "opened", "symbol": "BTCUSDT", "side": "long"},
                   {"kind": "closed", "symbol": "ETHUSDT", "pnl": -12.3}],
        "underperforming": [{"strategy": "EMA 8/30", "reason": "PF 0.6"}],
    })
    types = {x["type"] for x in a}
    assert {"trade_opened", "trade_closed", "strategy_underperforming"} <= types


def test_channel_status_no_secret_leak(tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_DISCORD_WEBHOOK", raising=False)
    ch = AlertChannels(notifier=None, path=str(tmp_path / "ch.json"))
    st = ch.status()
    assert st["telegram"]["connected"] is False
    assert st["discord"]["connected"] is False and st["email"]["connected"] is False
    # saving a webhook connects discord but never echoes the value
    ch.save({"discord_webhook": "https://discord.test/hook/SECRET"})
    assert ch.status()["discord"]["connected"] is True
    assert "SECRET" not in str(ch.status())


def test_senders_failsafe_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ALERT_DISCORD_WEBHOOK", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    assert send_discord("", "hi") is False                 # no webhook -> False, no raise
    ch = AlertChannels(notifier=None, path=None)
    res = dispatch_alert({"title": "x", "detail": "y", "severity": "info"}, ch)
    assert res["queued"] is True and "channels" in res


# ───────────────────────── endpoints ─────────────────────────
@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    from services.alerts import AlertChannels
    from services.notifier import Notifier
    monkeypatch.setattr(webhook_api, "alert_channels", AlertChannels(Notifier("", ""), str(tmp_path / "ch.json")))
    app = FastAPI(); app.include_router(webhook_api.router)
    return TestClient(app)


SECRET = "dev-control-key"


def test_alerts_endpoints(client):
    st = client.get("/alerts/channels").json()
    assert "telegram" in st["channels"] and "discord" in st["channels"] and "email" in st["channels"]
    chk = client.get("/alerts/check").json()
    assert "alerts" in chk and "context" in chk
    # saving channels is secret-gated
    assert client.post("/alerts/channels", json={"discord_webhook": "https://x/y"}).status_code == 401
    saved = client.post("/alerts/channels", json={"discord_webhook": "https://x/y"},
                        headers={"X-Webhook-Secret": SECRET}).json()
    assert saved["channels"]["discord"]["connected"] is True
