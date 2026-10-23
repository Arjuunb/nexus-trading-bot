"""Telegram notifier — fail-safe, gated, and the /notifications endpoints."""
import pytest
import urllib.error
import urllib.request

from services.notifier import Notifier


def test_not_configured_is_noop():
    n = Notifier("", "")
    assert n.configured is False
    assert n.send("hi") is False           # no network call, no crash
    n.dispatch("trade", "x", "y")          # silently does nothing


def test_dispatch_respects_event_flags(monkeypatch):
    n = Notifier("tok", "chat")
    sent = []
    monkeypatch.setattr(n, "send_async", lambda t: sent.append(t))
    n.notify_trades = False
    n.dispatch("trade", "open", "BTC")     # trades off -> nothing
    assert sent == []
    n.notify_trades = True
    n.dispatch("trade", "open", "BTC")
    assert sent and "open" in sent[0]
    n.notify_risk = False
    n.dispatch("risk", "halt", "")         # risk off -> nothing
    assert len(sent) == 1


def test_send_never_raises_on_bad_host(monkeypatch):
    # Exercise the transport failure locally; never contact Telegram.
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(True)
        raise urllib.error.URLError("isolated test transport unavailable")
    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    assert Notifier("bad", "bad").send("x") is False
    assert calls == [True]


@pytest.fixture()
def client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    monkeypatch.setattr(webhook_api, "notifier", Notifier("", ""))
    app = FastAPI()
    app.include_router(webhook_api.router)
    yield TestClient(app)
    webhook_api.engine.stop()


SECRET = "dev-control-key"


def test_notifications_endpoints(client):
    s = client.get("/notifications/status").json()
    assert "telegram_configured" in s and "notify_trades" in s
    assert client.post("/notifications", json={"notify_trades": False}).status_code == 401
    r = client.post("/notifications", json={"notify_trades": False}, headers={"X-Webhook-Secret": SECRET})
    assert r.json()["notify_trades"] is False
    # test send (not configured by default) -> sent False, no crash
    t = client.post("/notifications/test", headers={"X-Webhook-Secret": SECRET})
    assert t.status_code == 200 and "sent" in t.json()
    assert t.json()["sent"] is False
