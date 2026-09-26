"""The hash-chained, append-only security audit log (services/audit_log.py),
its request middleware, and the /security/audit endpoints."""
import json
import sqlite3

import pytest

from services import audit_log, redaction
from services.audit_log import GENESIS, AuditLog, entry_hash

SECRET = "dev-control-key"
LIVE = "live-exchange-secret-4f9a1c2b"


@pytest.fixture()
def log(tmp_path):
    return AuditLog(tmp_path / "audit.db")


@pytest.fixture()
def default_log(tmp_path):
    log = AuditLog(tmp_path / "default-audit.db")
    audit_log.set_default_log(log)
    yield log
    audit_log.set_default_log(None)


def _fill(log, n=3):
    for i in range(n):
        log.append(kind="request", actor=f"user{i}", method="POST", path=f"/p/{i}", status=200)


def test_chain_links_every_entry_to_the_one_before(log):
    _fill(log)
    rows = list(reversed(log.list()))
    assert rows[0]["prev_hash"] == GENESIS
    assert rows[1]["prev_hash"] == rows[0]["hash"] and rows[2]["prev_hash"] == rows[1]["hash"]
    assert all(entry_hash(r) == r["hash"] for r in rows)
    assert log.verify() == {"ok": True, "entries": 3, "first_bad_seq": None, "reason": "",
                            "head_hash": rows[2]["hash"]}


def test_the_application_cannot_update_or_delete_an_entry(log):
    _fill(log, 1)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        log._c.execute("UPDATE audit_entries SET actor='someone-else'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        log._c.execute("DELETE FROM audit_entries")
    assert log.verify()["ok"]


def _raw(tmp_path):
    """A SQLite shell on the file, with the protections dropped -- what an
    attacker with disk access could do."""
    c = sqlite3.connect(tmp_path / "audit.db", isolation_level=None)
    c.execute("DROP TRIGGER audit_entries_no_update")
    c.execute("DROP TRIGGER audit_entries_no_delete")
    return c


def test_editing_a_row_outside_the_app_is_detected(log, tmp_path):
    _fill(log)
    _raw(tmp_path).execute("UPDATE audit_entries SET actor='innocent' WHERE seq=2")
    result = log.verify()
    assert not result["ok"] and result["first_bad_seq"] == 2
    assert "content altered" in result["reason"]


def test_deleting_a_row_outside_the_app_is_detected(log, tmp_path):
    _fill(log)
    _raw(tmp_path).execute("DELETE FROM audit_entries WHERE seq=2")
    result = log.verify()
    assert not result["ok"] and result["first_bad_seq"] == 3 and "gap" in result["reason"]


def test_a_forged_row_with_a_recomputed_hash_breaks_the_next_link(log, tmp_path):
    _fill(log)
    raw = _raw(tmp_path)
    raw.row_factory = sqlite3.Row
    row = dict(raw.execute("SELECT * FROM audit_entries WHERE seq=2").fetchone())
    row["actor"] = "innocent"
    raw.execute("UPDATE audit_entries SET actor=?, hash=? WHERE seq=2", (row["actor"], entry_hash(row)))
    result = log.verify()
    assert not result["ok"] and result["first_bad_seq"] == 3 and "link broken" in result["reason"]


def test_secrets_never_reach_the_log(log, monkeypatch):
    monkeypatch.setenv("HUB_EXCHANGE_API_SECRET", LIVE)
    redaction.refresh_known_secrets()
    try:
        log.append(kind="request", actor="owner", path=f"/x?token={LIVE}", query=f"secret={LIVE}",
                   detail={"body": {"api_secret": "abcdefabcdef", "note": f"used {LIVE}"}})
        stored = json.dumps(log.list())
        assert LIVE not in stored and "abcdefabcdef" not in stored
        assert log.verify()["ok"]
    finally:
        monkeypatch.delenv("HUB_EXCHANGE_API_SECRET")
        redaction.refresh_known_secrets()


def test_export_is_the_full_chain_with_hashes(log):
    _fill(log)
    lines = [json.loads(line) for line in log.export_jsonl()]
    assert [r["seq"] for r in lines] == [1, 2, 3]
    assert lines[-1]["hash"] == log.head()["hash"]


# ------------------------------------------------------------------ middleware
@pytest.fixture()
def hub_client(default_log):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import app as hub_app
    return TestClient(hub_app.app)


def test_refused_state_changes_are_recorded_with_redacted_bodies(hub_client, default_log):
    r = hub_client.post("/settings", json={"risk_per_trade_pct": 0.5, "password": "hunter2hunter2"},
                        headers={"User-Agent": "audit-test"})
    assert r.status_code == 401
    entry = default_log.list(limit=1)[0]
    assert entry["actor"] == "anonymous" and entry["auth"] == "none"
    assert entry["method"] == "POST" and entry["path"] == "/settings" and entry["status"] == 401
    assert entry["user_agent"] == "audit-test"
    body = json.loads(entry["detail"])["body"]
    assert body == {"risk_per_trade_pct": 0.5, "password": redaction.REDACTED}


def test_reads_are_not_recorded_and_control_key_callers_are_named(hub_client, default_log):
    hub_client.get("/api/version")
    assert default_log.list() == []
    hub_client.post("/security/audit", headers={"x-webhook-secret": SECRET})  # 405: still a write attempt
    entry = default_log.list(limit=1)[0]
    assert entry["actor"] == "control-key" and entry["auth"] == "control_key"
    assert entry["status"] == 405
    assert default_log.verify()["ok"]


# ------------------------------------------------------------------- endpoints
@pytest.fixture()
def api(default_log):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    app = FastAPI()
    app.include_router(webhook_api.router)
    return TestClient(app)


def test_audit_endpoints_require_the_control_credential(api):
    for path in ("/security/audit", "/security/audit/verify", "/security/audit/export"):
        assert api.get(path).status_code == 401


def test_audit_endpoints_list_verify_and_export(api, default_log):
    _fill(default_log)
    h = {"x-webhook-secret": SECRET}
    listed = api.get("/security/audit?limit=2", headers=h).json()
    assert [e["seq"] for e in listed["entries"]] == [3, 2] and listed["head"]["seq"] == 3
    assert api.get("/security/audit/verify", headers=h).json()["ok"] is True
    exported = api.get("/security/audit/export", headers=h)
    assert exported.headers["x-audit-head-hash"] == default_log.head()["hash"]
    assert len(exported.text.strip().splitlines()) == 3


def test_settings_changes_record_the_previous_and_new_value(tmp_path, default_log):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    from config import settings
    from data.ledger import SqliteLedger
    from execution.paper_engine import PaperExecutionEngine
    from services.controls import TradingControl
    from services.signal_pipeline import SignalPipeline
    from services.auto_engine import AutoStrategyEngine

    settings.settings_path = str(tmp_path / "runtime.json")
    led = SqliteLedger(":memory:")
    webhook_api.ledger = led
    webhook_api.controls = TradingControl()
    webhook_api.paper = PaperExecutionEngine(led, 10_000)
    webhook_api.pipeline = SignalPipeline(led, webhook_api.paper, webhook_api.controls,
                                          equity=10_000, risk_per_trade_pct=0.01,
                                          exposure_limit_pct=0.05, max_drawdown_pct=0.20)
    webhook_api.engine = AutoStrategyEngine(webhook_api.pipeline, webhook_api.paper, led,
                                            symbols=["BTCUSDT"], interval=0.01)
    app = FastAPI()
    app.include_router(webhook_api.router)
    try:
        r = TestClient(app).post("/settings", json={"risk_per_trade_pct": 0.02},
                                 headers={"x-webhook-secret": SECRET})
        assert r.status_code == 200
        change = default_log.list(kind="change")[0]
        assert change["action"] == "settings.update"
        assert json.loads(change["detail"])["before"] == {"risk_per_trade_pct": 0.01}
        assert json.loads(change["detail"])["after"] == {"risk_per_trade_pct": 0.02}
    finally:
        webhook_api.engine.stop()


def test_middleware_replays_a_preread_body_and_passes_a_disconnect_on():
    import asyncio
    from services.audit_middleware import AuditMiddleware

    seen = []

    async def app(scope, receive, send):
        seen.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    class Sink:
        entries = []

        def append(self, **kw):
            self.entries.append(kw)

    sink = Sink()
    mw = AuditMiddleware(app, log_factory=lambda: sink, identify=lambda s, h: ("t", "none"))
    body = json.dumps({"a": 1}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]

    async def run(messages):
        queue = list(messages)

        async def receive():
            return queue.pop(0)

        async def send(_):
            pass
        await mw({"type": "http", "method": "POST", "path": "/x", "headers": headers}, receive, send)

    asyncio.run(run([{"type": "http.request", "body": body[:3], "more_body": True},
                     {"type": "http.request", "body": body[3:], "more_body": False}]))
    assert seen[-1] == {"type": "http.request", "body": body, "more_body": False}
    assert sink.entries[-1]["detail"]["body"] == {"a": 1}

    asyncio.run(run([{"type": "http.request", "body": body[:3], "more_body": True},
                     {"type": "http.disconnect"}]))
    assert seen[-1] == {"type": "http.disconnect"}


def test_sign_in_attempts_name_the_account_tried(hub_client, default_log):
    hub_client.post("/auth/login", data={"username": "admin", "password": "not-the-password"})
    entry = default_log.list(limit=1)[0]
    assert entry["actor"] == "admin" and entry["auth"] == "password"
    assert entry["status"] == 401
    assert "not-the-password" not in entry["detail"]
