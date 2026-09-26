"""Security checkup (services/security_checkup.py)."""
from datetime import datetime, timezone

import pytest

from services.security_checkup import run

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _good(**over):
    base = dict(
        https=True, local=False,
        vault={"configured": True, "master_key_id": "abcd1234", "tenants_under_other_master_key": 0},
        default_session_secret=False, default_control_key=False, two_factor=True,
        backups={"latest": {"snapshot": "20260924T080000Z", "encrypted": True}, "unencrypted_kept": 0, "keep": 7},
        audit_available=True, audit_export={"configured": True, "last_error": ""},
        exchange_keys=[{"status": "active", "scope": {"ip_restricted": True}}],
        api_keys=[{"scopes": ["read", "control"], "created_at": "2026-09-01T00:00:00+00:00",
                   "last_used_at": "2026-09-20T00:00:00+00:00", "revoked_at": None}],
        webhooks=[{"active": True, "secret_encrypted": True}], live_routing_locked=True, now=NOW)
    base.update(over)
    return run(**base)


def _status(result, cid):
    return {c["id"]: c for c in result["checks"]}[cid]


def test_a_well_configured_deployment_passes_every_check_with_no_fixes():
    r = _good()
    assert r["counts"] == {"pass": r["total"], "warn": 0, "fail": 0}
    assert all(c["fix"] == "" for c in r["checks"])


@pytest.mark.parametrize("over, cid, status", [
    ({"https": False}, "https", "fail"),
    ({"https": False, "local": True}, "https", "warn"),
    ({"vault": {"configured": False, "problem": "HUB_MASTER_KEY is not set"}}, "master_key", "fail"),
    ({"vault": {"configured": True, "tenants_under_other_master_key": 1}}, "master_key", "fail"),
    ({"default_control_key": True}, "defaults", "fail"),
    ({"two_factor": False}, "two_factor", "warn"),
    ({"two_factor": None}, "two_factor", "warn"),
    ({"backups": {"latest": None}}, "backups", "warn"),
    ({"backups": {"latest": {"snapshot": "20260924T080000Z", "encrypted": False}}}, "backups", "fail"),
    ({"backups": {"latest": {"snapshot": "20260920T080000Z", "encrypted": True}}}, "backups", "warn"),
    ({"audit_available": False}, "audit", "fail"),
    ({"audit_export": {"configured": False}}, "audit_export", "warn"),
    ({"exchange_keys": [{"status": "active", "scope": {"ip_restricted": False}}]}, "exchange_keys", "warn"),
    ({"api_keys": [{"scopes": ["control"], "created_at": "2026-01-01T00:00:00+00:00",
                    "last_used_at": None, "revoked_at": None}]}, "api_keys", "warn"),
    ({"webhooks": [{"active": True, "secret_encrypted": False}]}, "webhooks", "warn"),
])
def test_each_gap_is_reported_with_a_fix(over, cid, status):
    check = _status(_good(**over), cid)
    assert check["status"] == status and check["fix"]


def test_idle_read_only_or_revoked_keys_are_not_flagged():
    r = _good(api_keys=[
        {"scopes": ["read"], "created_at": "2026-01-01T00:00:00+00:00", "last_used_at": None, "revoked_at": None},
        {"scopes": ["control"], "created_at": "2026-01-01T00:00:00+00:00", "last_used_at": None,
         "revoked_at": "2026-02-01T00:00:00+00:00"}])
    assert _status(r, "api_keys")["status"] == "pass"


def test_the_checkup_endpoint_reports_real_state():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import app as hub_app
    c = TestClient(hub_app.app)
    r = c.get("/security/checkup", headers={"x-webhook-secret": "dev-control-key"})
    assert r.status_code == 200
    body = r.json()
    ids = {x["id"] for x in body["checks"]}
    assert {"https", "master_key", "defaults", "backups", "audit", "live_routing"} <= ids
    assert {x["id"]: x for x in body["checks"]}["defaults"]["status"] == "fail"  # the test server runs on dev defaults
    assert c.get("/security/checkup").status_code in (401, 403)
