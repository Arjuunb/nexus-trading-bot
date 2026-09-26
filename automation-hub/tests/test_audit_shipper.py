"""Scheduled export of the audit log (services/audit_shipper.py)."""
import json

import pytest

from services import audit_log
from services.audit_log import AuditLog
from services.audit_shipper import AuditExporter, check_destination


class Collector:
    """A fake intake endpoint that records what it receives."""

    def __init__(self, codes=None):
        self.codes = list(codes or [])
        self.requests = []

    def __call__(self, url, body, headers, timeout):
        self.requests.append((url, body, headers))
        return self.codes.pop(0) if self.codes else 200

    @property
    def entries(self):
        return [json.loads(line) for _, body, _ in self.requests for line in body.decode().splitlines()]


@pytest.fixture()
def log(tmp_path):
    return AuditLog(tmp_path / "audit.db")


def _fill(log, n):
    for i in range(n):
        log.append(kind="request", actor=f"u{i}", method="POST", path=f"/p/{i}", status=200)


def test_only_https_or_localhost_destinations_are_accepted():
    assert check_destination("https://logs.example.com/intake") == ""
    assert check_destination("http://127.0.0.1:8080/in") == ""
    assert check_destination("http://logs.example.com/intake")
    assert check_destination("")
    assert AuditExporter(lambda: None, url="ftp://x").configured is False


def test_exports_in_order_with_continuity_headers_and_token(log):
    _fill(log, 5)
    sink = Collector()
    ex = AuditExporter(lambda: log, url="https://siem.example.com/in", token="tok-123", batch=2, post=sink)
    result = ex.export_once()
    assert result == {"ok": True, "sent": 5, "last_seq": 5, "error": ""}
    assert [e["seq"] for e in sink.entries] == [1, 2, 3, 4, 5]
    url, _, headers = sink.requests[-1]
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["Content-Type"] == "application/x-ndjson"
    assert headers["X-Audit-Last-Seq"] == "5" and headers["X-Audit-Head-Hash"] == log.head()["hash"]
    # the receiver can check each entry links to the previous one
    for prev, cur in zip(sink.entries, sink.entries[1:]):
        assert cur["prev_hash"] == prev["hash"]


def test_nothing_is_sent_twice_and_new_entries_follow(log):
    _fill(log, 3)
    sink = Collector()
    ex = AuditExporter(lambda: log, url="https://siem.example.com/in", post=sink)
    ex.export_once()
    assert ex.export_once()["sent"] == 0
    _fill(log, 2)
    assert ex.export_once()["sent"] == 2
    assert [e["seq"] for e in sink.entries] == [1, 2, 3, 4, 5]


def test_a_failed_batch_is_retried_from_the_same_entry(log):
    _fill(log, 4)
    sink = Collector(codes=[200, 503])
    ex = AuditExporter(lambda: log, url="https://siem.example.com/in", batch=2, post=sink)
    first = ex.export_once()
    assert first["sent"] == 2 and "HTTP 503" in first["error"]
    status = ex.status()
    assert status["last_exported_seq"] == 2 and status["pending"] == 2 and "503" in status["last_error"]
    second = ex.export_once()
    assert second["ok"] and second["sent"] == 2
    # the refused batch (3, 4) is sent again, and nothing else is repeated
    assert [e["seq"] for e in sink.entries] == [1, 2, 3, 4, 3, 4]
    assert ex.status()["pending"] == 0 and ex.status()["last_error"] == ""


def test_network_errors_do_not_move_the_cursor_or_leak_the_url(log):
    _fill(log, 2)

    def boom(*a):
        raise OSError("connection refused to https://user:secret@siem.example.com/in?api_key=zzz")
    ex = AuditExporter(lambda: log, url="https://user:secret@siem.example.com/in?api_key=zzz", post=boom)
    result = ex.export_once()
    assert not result["ok"] and result["sent"] == 0
    status = ex.status()
    assert status["last_exported_seq"] == 0
    assert "secret" not in json.dumps(status) and "zzz" not in json.dumps(status)
    assert status["destination"] == "https://siem.example.com"


def test_run_now_endpoint_and_status(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    log = AuditLog(tmp_path / "a.db")
    audit_log.set_default_log(log)
    try:
        _fill(log, 3)
        sink = Collector()
        monkeypatch.setattr(webhook_api, "audit_exporter",
                            AuditExporter(audit_log.default_log, url="https://siem.example.com/in", post=sink))
        app = FastAPI()
        app.include_router(webhook_api.router)
        client = TestClient(app)
        h = {"x-webhook-secret": "dev-control-key"}
        assert client.post("/security/audit/export/run").status_code == 401
        r = client.post("/security/audit/export/run", headers=h)
        assert r.status_code == 200 and r.json()["sent"] == 3
        status = client.get("/security/status", headers=h).json()["audit_export"]
        assert status["destination"] == "https://siem.example.com" and status["pending"] == 0
    finally:
        audit_log.set_default_log(None)
