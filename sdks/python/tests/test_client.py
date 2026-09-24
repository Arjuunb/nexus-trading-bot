"""Exercises the client against a local fake of the API."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from tradelogx_nexus import Client, NexusError, verify_webhook

DECISIONS = [{"id": f"dec_{i}", "symbol": "BTCUSDT", "verdict": "rejected"} for i in range(5, 0, -1)]


class Fake(BaseHTTPRequestHandler):
    calls = []
    fail_once = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        Fake.calls.append((method, url.path, q, body, dict(self.headers)))
        if self.headers.get("Authorization") != "Bearer nxs_test_key":
            return self._send(401, {"error": {"code": "unauthenticated", "message": "no"}})
        if Fake.fail_once.pop(url.path, False):
            return self._send(429, {"error": {"code": "rate_limited", "message": "slow"}}, {"Retry-After": "0"})
        if url.path == "/v1/decisions":
            limit = int(q.get("limit", 50))
            start = 0
            if "cursor" in q:
                start = next(i for i, d in enumerate(DECISIONS) if d["id"] == q["cursor"]) + 1
            page = DECISIONS[start:start + limit]
            nxt = page[-1]["id"] if len(page) == limit and start + limit < len(DECISIONS) else None
            return self._send(200, {"data": page, "next_cursor": nxt})
        if url.path == "/v1/strategies/decision_brain/promote":
            if body.get("mode") == "live":
                return self._send(409, {"error": {"code": "live_routing_locked", "message": "locked"}})
            return self._send(200, {"id": "decision_brain", "mode": "paper", "changed": False})
        if url.path == "/v1/backtests":
            return self._send(202, {"id": "bt_1", "status": "queued"})
        if url.path == "/v1/backtests/bt_1":
            return self._send(200, {"id": "bt_1", "status": "complete", "result": {"net": {"net_r": 0.5}}})
        return self._send(404, {"error": {"code": "not_found", "message": url.path}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


@pytest.fixture()
def client():
    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Fake.calls.clear()
    yield Client("nxs_test_key", base_url=f"http://127.0.0.1:{server.server_port}", backoff=0)
    server.shutdown()


def test_decisions_follow_cursors_and_respect_limit(client):
    assert [d["id"] for d in client.decisions.list(page_size=2)] == [d["id"] for d in DECISIONS]
    assert len(list(client.decisions.list(limit=3, page_size=2))) == 3
    first = Fake.calls[0]
    assert first[4]["Nexus-Version"] == "2026-09-24"


def test_errors_carry_the_stable_code(client):
    with pytest.raises(NexusError) as err:
        client.strategies.promote("decision_brain", "live")
    assert err.value.status == 409 and err.value.code == "live_routing_locked"
    with pytest.raises(NexusError) as err:
        Client("nxs_wrong", base_url=client.base_url).strategies.list()
    assert err.value.code == "unauthenticated"


def test_429_is_retried_after_the_server_says(client):
    Fake.fail_once["/v1/decisions"] = True
    assert len(list(client.decisions.list(limit=1))) == 1


def test_backtest_run_waits_for_completion(client):
    job = client.backtests.run("decision_brain", poll_interval=0)
    assert job["status"] == "complete" and job["result"]["net"]["net_r"] == 0.5


def test_a_key_is_required(monkeypatch):
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    with pytest.raises(ValueError):
        Client()


def test_webhook_signatures():
    body, header = b'{"id":"evt_1"}', "t=1780000000,v1=a6cfe2114f40ec5193d0304c1e682c55fdabf04be55f4190b0c065017cb094af"
    assert verify_webhook("whsec_test", body, header, now=1780000000)
    assert not verify_webhook("whsec_test", b'{"id":"evt_2"}', header, now=1780000000)
    assert not verify_webhook("whsec_test", body, header, now=1780000000 + 3600)
