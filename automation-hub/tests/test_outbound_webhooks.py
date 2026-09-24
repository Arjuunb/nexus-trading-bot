"""Signed outbound webhooks (services/outbound_webhooks.py)."""
import json

import pytest

from services.outbound_webhooks import RETRY_WINDOW_S, WebhookService, sign, verify

TENANT = "__owner__"


class Clock:
    def __init__(self):
        self.t = 1_780_000_000.0

    def __call__(self):
        return self.t


class Receiver:
    def __init__(self, codes=None):
        self.codes, self.got = list(codes or []), []

    def __call__(self, url, body, headers, timeout):
        self.got.append((url, body, headers))
        return self.codes.pop(0) if self.codes else 200


class Decisions:
    def __init__(self):
        self.rows = []

    def add(self, verdict):
        self.rows.append({"id": len(self.rows) + 1, "decision": verdict, "ts": "2026-09-24T00:00:00Z",
                          "symbol": "BTCUSDT"})

    def after(self, after_id, limit):
        return [r for r in self.rows if r["id"] > after_id][:limit]

    def max_id(self):
        return max((r["id"] for r in self.rows), default=0)


@pytest.fixture()
def rig(tmp_path):
    clock, rx, dec = Clock(), Receiver(), Decisions()
    svc = WebhookService(tmp_path / "wh.db", decision_source=dec.after, latest_decision_id=dec.max_id,
                         render=lambda d: {"id": f"dec_{d['id']}", "verdict": d["decision"]},
                         post=rx, clock=clock)
    return svc, clock, rx, dec


def test_signatures_verify_and_reject_tampering_and_replays():
    body = b'{"a":1}'
    header = sign("whsec_x", body, 1_780_000_000)
    assert verify("whsec_x", body, header, now=1_780_000_010)
    assert not verify("whsec_x", b'{"a":2}', header, now=1_780_000_010)
    assert not verify("whsec_y", body, header, now=1_780_000_010)
    assert not verify("whsec_x", body, header, now=1_780_000_000 + 3600)  # stale
    assert not verify("whsec_x", body, "garbage")


def test_subscriptions_need_https_and_known_events_and_hide_the_secret(rig):
    svc, *_ = rig
    with pytest.raises(ValueError):
        svc.subscribe(TENANT, "http://example.com/hook", None)
    with pytest.raises(ValueError):
        svc.subscribe(TENANT, "https://example.com/hook", ["order.filled"])
    created = svc.subscribe(TENANT, "https://example.com/hook", ["decision.rejected"])
    assert created["secret"].startswith("whsec_")
    assert "secret" not in svc.list(TENANT)[0]


def test_a_new_subscription_starts_from_now_and_gets_only_its_events(rig):
    svc, _, rx, dec = rig
    dec.add("rejected")  # history: never sent
    sub = svc.subscribe(TENANT, "https://example.com/hook", ["decision.rejected"])
    dec.add("accepted")
    dec.add("rejected")
    result = svc.run_once()
    assert result["queued"] == 1 and result["delivered"] == 1
    url, body, headers = rx.got[0]
    event = json.loads(body)
    assert event["type"] == "decision.rejected" and event["id"] == "evt_dec_3"
    assert event["idempotency_key"] == "dec_3:rejected" and event["data"]["verdict"] == "rejected"
    assert headers["Nexus-Event-Type"] == "decision.rejected"
    assert verify(sub["secret"], body, headers["Nexus-Signature"], now=rig[1].t)
    assert svc.run_once()["queued"] == 0  # nothing sent twice


def test_failures_retry_with_backoff_then_give_up_after_24_hours(rig):
    svc, clock, rx, dec = rig
    rx.codes = [500] * 1000
    sub = svc.subscribe(TENANT, "https://example.com/hook", None)
    dec.add("rejected")
    svc.run_once()
    first = svc.deliveries(TENANT, sub["id"])[0]
    assert first["status"] == "pending" and first["attempts"] == 1 and first["last_error"] == "HTTP 500"
    svc.deliver_due()
    assert svc.deliveries(TENANT, sub["id"])[0]["attempts"] == 1  # not due yet: backing off
    clock.t += RETRY_WINDOW_S + 7200
    for _ in range(3):
        svc.deliver_due()
        clock.t += 3600
    assert svc.deliveries(TENANT, sub["id"])[0]["status"] == "failed"


def test_a_recovered_endpoint_gets_the_same_event_and_a_disabled_one_gets_nothing(rig):
    svc, clock, rx, dec = rig
    rx.codes = [503]
    sub = svc.subscribe(TENANT, "https://example.com/hook", None)
    dec.add("accepted")
    svc.run_once()
    clock.t += 60
    svc.deliver_due()
    assert [json.loads(b)["id"] for _, b, _ in rx.got] == ["evt_dec_1", "evt_dec_1"]
    assert svc.deliveries(TENANT, sub["id"])[0]["status"] == "delivered"
    svc.disable(TENANT, sub["id"])
    dec.add("accepted")
    svc.run_once()
    assert len(rx.got) == 2


def test_webhook_endpoints_show_the_secret_once(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import app as hub_app
    import webhook_api
    rx = Receiver()
    svc = WebhookService(tmp_path / "wh.db", post=rx)
    monkeypatch.setattr(webhook_api, "outbound_webhooks", svc)
    c = TestClient(hub_app.app)
    h = {"x-webhook-secret": "dev-control-key"}
    created = c.post("/security/webhooks", json={"url": "https://example.com/hook"}, headers=h)
    assert created.status_code == 200 and created.json()["secret"].startswith("whsec_")
    listed = c.get("/security/webhooks", headers=h)
    assert created.json()["secret"] not in listed.text
    tested = c.post(f"/security/webhooks/{created.json()['id']}/test", headers=h).json()
    assert tested["deliveries"][0]["status"] == "delivered" and tested["deliveries"][0]["event_type"] == "webhook.test"
    assert c.post("/security/webhooks", json={"url": "ftp://x"}, headers=h).status_code == 400


def test_a_redirect_is_a_failed_attempt_not_a_delivery():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from services.outbound_webhooks import _post

    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(("POST", self.path))
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def do_GET(self):
            hits.append(("GET", self.path))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code = _post(f"http://127.0.0.1:{server.server_port}/hook", b"{}", {}, 5)
    finally:
        server.shutdown()
    assert code == 302 and hits == [("POST", "/hook")]
