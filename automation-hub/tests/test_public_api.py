"""Personal API keys (services/api_keys.py) and the public /v1 API
(routers/public_api.py)."""
import json
import time

import pytest

from services import api_keys, audit_log
from services.api_keys import ApiKeyStore, CURRENT_VERSION
from services.audit_log import AuditLog
from services.ratelimit import limiter

TENANT = "__owner__"
H = {"x-webhook-secret": "dev-control-key"}


# ------------------------------------------------------------------ key store
def test_a_key_is_shown_once_and_only_its_hash_is_stored(tmp_path):
    store = ApiKeyStore(tmp_path / "k.db")
    created = store.create(TENANT, "ci", ["read"], created_by="owner")
    token = created["token"]
    assert token.startswith("nxs_") and created["hint"].startswith("nxs_") and token not in created["hint"]
    assert token.split("_", 2)[2].encode() not in (tmp_path / "k.db").read_bytes()
    assert "token" not in store.list(TENANT)[0]
    assert store.authenticate(token)["name"] == "ci"
    assert store.authenticate(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    assert store.authenticate("nxs_bad") is None and store.authenticate("") is None


def test_scopes_are_validated_and_control_implies_read(tmp_path):
    store = ApiKeyStore(tmp_path / "k.db")
    assert store.create(TENANT, "ops", ["control"], created_by="o")["scopes"] == ["control", "read"]
    with pytest.raises(ValueError):
        store.create(TENANT, "x", ["trade_live"], created_by="o")
    with pytest.raises(ValueError):
        store.create(TENANT, " ", ["read"], created_by="o")


def test_revoked_keys_never_authenticate_again(tmp_path):
    store = ApiKeyStore(tmp_path / "k.db")
    created = store.create(TENANT, "ci", ["read"], created_by="o")
    store.revoke(TENANT, created["id"])
    assert store.authenticate(created["token"]) is None
    assert store.list(TENANT)[0]["active"] is False


# ----------------------------------------------------------------------- API
@pytest.fixture()
def env(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import app as hub_app
    import webhook_api
    from data.decision_store import DecisionStore

    store = ApiKeyStore(tmp_path / "keys.db")
    api_keys.set_default_store(store)
    audit = AuditLog(tmp_path / "audit.db")
    audit_log.set_default_log(audit)
    decisions = DecisionStore(str(tmp_path / "decisions.db"))
    for i in range(5):
        decisions.record({"ts": f"2026-09-2{i}T00:00:00+00:00", "symbol": "BTCUSDT", "timeframe": "15m",
                          "strategy": "Decision Brain", "side": "long",
                          "decision": "rejected" if i % 2 else "accepted",
                          "reason": f"reason {i}", "setup_quality_score": 50 + i})
    monkeypatch.setattr(webhook_api, "decision_store", decisions)
    read = store.create(TENANT, "reader", ["read"], created_by="owner")["token"]
    ctrl = store.create(TENANT, "operator bot", ["control"], created_by="owner")["token"]
    limiter.reset()
    yield {"client": TestClient(hub_app.app), "read": read, "ctrl": ctrl, "audit": audit, "store": store}
    api_keys.set_default_store(None)
    audit_log.set_default_log(None)
    limiter.reset()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_authentication_scopes_and_versions(env):
    c = env["client"]
    assert c.get("/v1/strategies").json()["error"]["code"] == "unauthenticated"
    assert c.get("/v1/strategies", headers=auth("nxs_00000000_nope")).status_code == 401
    r = c.get("/v1/strategies", headers=auth(env["read"]))
    assert r.status_code == 200
    assert r.headers["nexus-version"] == CURRENT_VERSION and r.headers["x-ratelimit-limit"] == "600"
    bad = c.get("/v1/strategies", headers={**auth(env["read"]), "Nexus-Version": "1999-01-01"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_request"
    denied = c.post("/v1/strategies/decision_brain/promote", json={"mode": "paper"}, headers=auth(env["read"]))
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "insufficient_scope"


def test_strategies_are_paper_and_promotion_to_live_is_refused(env):
    c, ctrl = env["client"], env["ctrl"]
    data = c.get("/v1/strategies", headers=auth(env["read"])).json()
    assert data["live_routing"] == "locked" and all(s["mode"] == "paper" for s in data["data"])
    sid = data["data"][0]["id"]
    live = c.post(f"/v1/strategies/{sid}/promote", json={"mode": "live"}, headers=auth(ctrl))
    assert live.status_code == 409 and live.json()["error"]["code"] == "live_routing_locked"
    assert c.post(f"/v1/strategies/{sid}/promote", json={"mode": "paper"}, headers=auth(ctrl)).json()["changed"] is False
    assert c.post("/v1/strategies/nope/promote", json={"mode": "paper"}, headers=auth(ctrl)).status_code == 404


def test_decisions_page_with_a_cursor_and_filter(env):
    c, h = env["client"], auth(env["read"])
    first = c.get("/v1/decisions?limit=2", headers=h).json()
    assert [d["id"] for d in first["data"]] == ["dec_5", "dec_4"] and first["next_cursor"] == "dec_4"
    second = c.get(f"/v1/decisions?limit=2&cursor={first['next_cursor']}", headers=h).json()
    assert [d["id"] for d in second["data"]] == ["dec_3", "dec_2"]
    rejected = c.get("/v1/decisions?verdict=rejected", headers=h).json()["data"]
    assert rejected and all(d["verdict"] == "rejected" for d in rejected)
    assert c.get("/v1/decisions?verdict=maybe", headers=h).status_code == 400
    assert c.get("/v1/decisions/dec_3", headers=h).json()["quality_score"] == 52
    assert c.get("/v1/decisions/dec_3/replay", headers=h).json()["error"]["code"] == "not_available"
    assert c.get("/v1/decisions/dec_999", headers=h).status_code == 404


def test_rate_limit_answers_429_with_retry_after(env):
    c, h = env["client"], auth(env["read"])
    codes = [c.get("/v1/strategies", headers=h).status_code for _ in range(25)]
    assert codes[:20] == [200] * 20 and 429 in codes[20:]
    blocked = c.get("/v1/strategies", headers=h)
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1


def test_positions_and_close(env, monkeypatch):
    from routers import public_api
    import webhook_api
    rows = [{"id": "p1", "instance_id": "i1", "symbol": "BTCUSDT", "side": "long", "size": 0.1,
             "entry": 100.0, "mark": 110.0, "mark_available": True, "unrealized_pnl": 1.0,
             "r_multiple": 2.0, "protective": {"stop": 95.0, "target": 115.0, "managed_by": "engine"},
             "opened_at": None, "mode": "paper"}]
    monkeypatch.setattr(public_api, "_open_positions", lambda: rows)
    calls = []

    class Manager:
        def close_open_positions(self, instance_id, initiated_by):
            calls.append((instance_id, initiated_by))
            return {"closed": [{"position_id": "p1", "exit": 110.0, "pnl": 1.0}], "remaining": []}
    monkeypatch.setattr(webhook_api, "instance_manager", Manager())
    c = env["client"]
    listed = c.get("/v1/positions", headers=auth(env["read"])).json()["data"]
    assert listed[0]["protective"]["managed_by"] == "engine"
    assert c.post("/v1/positions/p1/close", json={}, headers=auth(env["read"])).status_code == 403
    closed = c.post("/v1/positions/p1/close", json={"reason": "flatten"}, headers=auth(env["ctrl"]))
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    assert calls == [("i1", "api-key:operator bot")]
    assert c.post("/v1/positions/zzz/close", json={}, headers=auth(env["ctrl"])).status_code == 404
    # the close is in the audit log, attributed to the key
    entry = next(e for e in env["audit"].list() if e["path"] == "/v1/positions/p1/close")
    assert entry["actor"].startswith("api-key:operator bot") and entry["auth"] == "api_key"


def test_backtests_queue_and_report_gross_and_net(env, monkeypatch):
    import services.replay as replay
    stats = {"gross": {"trades": 10, "win_rate": 50.0, "expectancy_r": 0.2, "net_r": 2.0,
                       "profit_factor": 1.4, "max_drawdown_r": 1.5},
             "net": {"trades": 10, "win_rate": 40.0, "expectancy_r": 0.05, "net_r": 0.5,
                     "profit_factor": 1.1, "max_drawdown_r": 2.0}}

    def fake(symbol, tf, bars, strategy, fill_cost_pct=0.0):
        which = "net" if fill_cost_pct else "gross"
        return {"stats": stats[which], "meta": {"bars": bars, "data_is_real": True, "timeframe": tf}}
    monkeypatch.setattr(replay, "build_replay", fake)
    c = env["client"]
    strategy = c.get("/v1/strategies", headers=auth(env["read"])).json()["data"][0]["id"]
    assert c.post("/v1/backtests", json={"strategy": strategy}, headers=auth(env["read"])).status_code == 403
    assert c.post("/v1/backtests", json={"strategy": "nope"}, headers=auth(env["ctrl"])).status_code == 400
    queued = c.post("/v1/backtests", json={"strategy": strategy, "symbol": "BTC/USDT"}, headers=auth(env["ctrl"]))
    assert queued.status_code == 202
    job_id = queued.json()["id"]
    for _ in range(50):
        job = c.get(f"/v1/backtests/{job_id}", headers=auth(env["ctrl"])).json()
        if job["status"] in ("complete", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "complete", job
    assert job["result"]["gross"]["net_r"] == 2.0 and job["result"]["net"]["net_r"] == 0.5
    assert job["result"]["costs"]["net_r_drag"] == 1.5
    assert job["request"]["symbol"] == "BTCUSDT"
    # another key cannot read it
    assert c.get(f"/v1/backtests/{job_id}", headers=auth(env["read"])).status_code == 404


def test_key_management_shows_the_key_once_and_revoke_takes_effect(env):
    c = env["client"]
    created = c.post("/security/api-keys", json={"name": "laptop", "scopes": ["read"]}, headers=H)
    assert created.status_code == 200
    token = created.json()["token"]
    assert token.startswith("nxs_")  # not redacted on the one response meant to show it
    listed = c.get("/security/api-keys", headers=H)
    assert token not in listed.text and any(k["name"] == "laptop" for k in listed.json()["keys"])
    assert c.get("/v1/strategies", headers=auth(token)).status_code == 200
    key_id = created.json()["id"]
    assert c.delete(f"/security/api-keys/{key_id}", headers=H).json()["key"]["active"] is False
    assert c.get("/v1/strategies", headers=auth(token)).status_code == 401
    # the token never reaches the audit log
    assert token not in json.dumps(env["audit"].list(limit=100))


def test_a_backtest_without_data_fails_instead_of_reporting_zeros(env, monkeypatch):
    import services.replay as replay
    monkeypatch.setattr(replay, "build_replay", lambda *a, **k: {
        "stats": {"trades": 0, "net_r": 0}, "meta": {"bars": 0, "data_warning": "Historical data missing."}})
    c = env["client"]
    strategy = c.get("/v1/strategies", headers=auth(env["read"])).json()["data"][0]["id"]
    job_id = c.post("/v1/backtests", json={"strategy": strategy}, headers=auth(env["ctrl"])).json()["id"]
    for _ in range(50):
        job = c.get(f"/v1/backtests/{job_id}", headers=auth(env["ctrl"])).json()
        if job["status"] in ("complete", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "failed" and "Historical data missing" in job["result"]["error"]
