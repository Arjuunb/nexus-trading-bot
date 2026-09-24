"""Exchange-key custody (services/key_vault.py), the venue scope check
(services/key_scope.py) and the /security/keys endpoints."""
import base64
import json
import os
import sqlite3
from urllib.parse import parse_qs, urlparse
import hashlib
import hmac

import pytest

from services import key_scope
from services.key_vault import KeyRefused, KeyVault, VaultNotConfigured, parse_master_key

TENANT = "__owner__"
KEY, SECRET = "binance-api-key-AAAA1234", "binance-api-secret-ZZZZ9876"
SAFE = {"allowed": True, "refusals": [], "warnings": [], "read_only": False,
        "can_trade": ["enableFutures"], "ip_restricted": True, "can_read": True, "checked_at": 0}


def safe_checker(venue, api_key, api_secret):
    return {"venue": venue, **SAFE}


@pytest.fixture()
def master():
    return os.urandom(32)


@pytest.fixture()
def vault(tmp_path, master):
    return KeyVault(tmp_path / "vault.db", master)


# ------------------------------------------------------------------ scope
def test_scope_refuses_any_fund_movement_permission():
    for flag in ("enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer"):
        decision = key_scope.evaluate({"enableReading": True, "enableFutures": True, flag: True,
                                       "ipRestrict": True})
        assert decision["allowed"] is False and decision["refusals"]
    ok = key_scope.evaluate({"enableReading": True, "enableFutures": True, "ipRestrict": False,
                             "enableWithdrawals": False})
    assert ok["allowed"] is True and ok["read_only"] is False
    assert ok["warnings"]  # no IP binding is flagged, not refused


def test_scope_request_is_signed_with_the_key_and_sent_with_its_header():
    seen = {}

    def fake_get(url, headers, timeout):
        seen["url"], seen["headers"] = url, headers
        return 200, json.dumps({"enableReading": True, "enableWithdrawals": False,
                                "ipRestrict": True}).encode()

    key_scope.binance_restrictions(KEY, SECRET, http_get=fake_get, now_ms=1700000000000)
    query = urlparse(seen["url"]).query
    params = parse_qs(query)
    unsigned = query.split("&signature=")[0]
    expected = hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    assert params["signature"] == [expected] and seen["headers"] == {"X-MBX-APIKEY": KEY}
    assert seen["url"].startswith(key_scope.BINANCE_RESTRICTIONS_URL)


@pytest.mark.parametrize("response", [(401, b'{"code":-2015,"msg":"Invalid API-key"}'),
                                      (200, b'{"unexpected":true}'), (503, b"")])
def test_scope_that_cannot_be_confirmed_raises(response):
    with pytest.raises(key_scope.ScopeCheckUnavailable):
        key_scope.binance_restrictions(KEY, SECRET, http_get=lambda *a: response)

    def boom(*a):
        raise OSError("network down")
    with pytest.raises(key_scope.ScopeCheckUnavailable):
        key_scope.binance_restrictions(KEY, SECRET, http_get=boom)


def test_unsupported_venues_are_refused_not_stored_unchecked():
    with pytest.raises(key_scope.ScopeCheckUnavailable, match="only Binance"):
        key_scope.check("okx", KEY, SECRET)


# ------------------------------------------------------------------- vault
def test_attached_keys_are_encrypted_at_rest_and_listed_without_secrets(vault, tmp_path):
    meta = vault.attach(TENANT, "binance", KEY, SECRET, checker=safe_checker)
    assert meta["key_hint"] == "…1234" and meta["status"] == "active"
    assert KEY not in json.dumps(vault.list(TENANT)) and SECRET not in json.dumps(vault.list(TENANT))
    raw = (tmp_path / "vault.db").read_bytes()
    assert KEY.encode() not in raw and SECRET.encode() not in raw
    assert vault.load_active(TENANT, "binance") == (KEY, SECRET)


def test_withdrawal_enabled_or_unconfirmable_keys_are_never_stored(vault):
    def withdraws(venue, k, s):
        return {"venue": venue, **key_scope.evaluate({"enableWithdrawals": True, "enableReading": True})}

    def unreachable(venue, k, s):
        raise key_scope.ScopeCheckUnavailable("Binance could not be reached")

    for checker in (withdraws, unreachable):
        with pytest.raises(KeyRefused):
            vault.attach(TENANT, "binance", KEY, SECRET, checker=checker)
    assert vault.list(TENANT) == [] and vault.load_active(TENANT, "binance") is None


def test_no_master_key_means_nothing_is_stored(tmp_path):
    vault = KeyVault(tmp_path / "v.db", None)
    assert vault.status()["configured"] is False and "HUB_MASTER_KEY" in vault.status()["problem"]
    with pytest.raises(VaultNotConfigured):
        vault.attach(TENANT, "binance", KEY, SECRET, checker=safe_checker)


def test_the_wrong_master_key_decrypts_nothing(tmp_path, master):
    KeyVault(tmp_path / "v.db", master).attach(TENANT, "binance", KEY, SECRET, checker=safe_checker)
    other = KeyVault(tmp_path / "v.db", os.urandom(32))
    with pytest.raises(VaultNotConfigured):
        other.load_active(TENANT, "binance")


def test_a_ciphertext_moved_to_another_tenant_does_not_decrypt(vault, tmp_path):
    vault.attach(TENANT, "binance", KEY, SECRET, checker=safe_checker)
    vault.attach("tenant-b", "binance", "other-key-BBBB5678", "other-secret-BBBB5678",
                 checker=safe_checker)
    c = sqlite3.connect(tmp_path / "vault.db")
    a = c.execute("SELECT api_key_ct, api_key_nonce FROM credentials WHERE tenant=?", (TENANT,)).fetchone()
    c.execute("UPDATE credentials SET api_key_ct=?, api_key_nonce=? WHERE tenant='tenant-b'", a)
    c.commit()
    with pytest.raises(VaultNotConfigured, match="failed authentication"):
        vault.load_active("tenant-b", "binance")


def test_attaching_a_new_key_rotates_the_old_one_and_revoke_takes_effect(vault):
    first = vault.attach(TENANT, "binance", KEY, SECRET, checker=safe_checker)
    second = vault.attach(TENANT, "binance", "new-key-CCCC0001", "new-secret-CCCC0001",
                          checker=safe_checker)
    statuses = {k["id"]: k["status"] for k in vault.list(TENANT)}
    assert statuses == {first["id"]: "rotated", second["id"]: "active"}
    assert vault.load_active(TENANT, "binance") == ("new-key-CCCC0001", "new-secret-CCCC0001")
    vault.revoke(TENANT, second["id"])
    assert vault.load_active(TENANT, "binance") is None


def test_recheck_revokes_a_key_that_gained_withdrawal_permission(vault):
    meta = vault.attach(TENANT, "binance", KEY, SECRET, checker=safe_checker)

    def now_withdraws(venue, k, s):
        return {"venue": venue, **key_scope.evaluate({"enableWithdrawals": True})}
    assert vault.recheck(TENANT, meta["id"], checker=now_withdraws)["status"] == "revoked"
    assert vault.load_active(TENANT, "binance") is None


def test_master_key_rotation_rewraps_data_keys_only(vault, tmp_path):
    vault.attach(TENANT, "binance", KEY, SECRET, checker=safe_checker)
    new = os.urandom(32)
    assert vault.rewrap(new) == 1
    assert KeyVault(tmp_path / "vault.db", new).load_active(TENANT, "binance") == (KEY, SECRET)


def test_master_key_parsing():
    raw = os.urandom(32)
    assert parse_master_key(base64.b64encode(raw).decode()) == raw
    assert parse_master_key(raw.hex()) == raw
    assert parse_master_key(None) is None
    with pytest.raises(VaultNotConfigured):
        parse_master_key("too-short")


# --------------------------------------------------------------- endpoints
@pytest.fixture()
def api(tmp_path, master, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    from services import audit_log, key_vault
    from services.audit_log import AuditLog

    vault = KeyVault(tmp_path / "vault.db", master)
    key_vault.set_default_vault(vault)
    audit = AuditLog(tmp_path / "audit.db")
    audit_log.set_default_log(audit)
    monkeypatch.setattr(key_scope, "check", safe_checker)
    app = FastAPI()
    app.include_router(webhook_api.router)
    yield TestClient(app), vault, audit
    key_vault.set_default_vault(None)
    audit_log.set_default_log(None)


H = {"x-webhook-secret": "dev-control-key"}


def test_key_endpoints_require_the_control_credential(api):
    client, _, _ = api
    assert client.get("/security/keys").status_code == 401
    assert client.post("/security/keys", json={}).status_code == 401
    assert client.get("/security/status").status_code == 401


def test_attach_list_and_revoke_through_the_api_never_echo_secrets(api, monkeypatch):
    client, vault, audit = api
    r = client.post("/security/keys", headers=H,
                    json={"venue": "binance", "api_key": KEY, "api_secret": SECRET, "label": "Main"})
    assert r.status_code == 200, r.text
    assert KEY not in r.text and SECRET not in r.text
    listed = client.get("/security/keys", headers=H)
    assert KEY not in listed.text and SECRET not in listed.text
    cred_id = listed.json()["keys"][0]["id"]
    assert client.delete(f"/security/keys/{cred_id}", headers=H).json()["key"]["status"] == "revoked"
    actions = [e["action"] for e in audit.list(kind="change")]
    assert actions == ["key.revoke", "key.attach"]
    assert KEY not in json.dumps(audit.list()) and SECRET not in json.dumps(audit.list())


def test_a_refused_key_returns_422_with_the_reason(api, monkeypatch):
    client, vault, _ = api

    def withdraws(venue, k, s):
        return {"venue": venue, **key_scope.evaluate({"enableWithdrawals": True})}
    monkeypatch.setattr(key_scope, "check", withdraws)
    r = client.post("/security/keys", headers=H,
                    json={"venue": "binance", "api_key": KEY, "api_secret": SECRET})
    assert r.status_code == 422 and "withdraw" in r.json()["detail"]["error"]
    assert vault.list(TENANT) == []


def test_status_reports_vault_audit_and_lock(api):
    client, _, _ = api
    body = client.get("/security/status", headers=H).json()
    assert body["vault"]["configured"] is True and body["audit"]["available"] is True
    assert body["live_routing_locked"] is True


def test_live_broker_refuses_a_real_money_key_that_can_withdraw(monkeypatch, tmp_path):
    from execution import live_readiness
    from services import key_vault
    key_vault.set_default_vault(KeyVault(tmp_path / "empty.db", None))
    monkeypatch.setenv("HUB_ENABLE_EXTERNAL_LIVE", "1")
    monkeypatch.setenv("HUB_EXCHANGE_API_KEY", KEY)
    monkeypatch.setenv("HUB_EXCHANGE_API_SECRET", SECRET)
    monkeypatch.setenv("HUB_TESTNET", "0")
    asked = []

    def withdraws(venue, k, s):
        asked.append((venue, k))
        return {"venue": venue, **key_scope.evaluate({"enableWithdrawals": True})}
    monkeypatch.setattr(key_scope, "check", withdraws)
    try:
        with pytest.raises(RuntimeError, match="refused"):
            live_readiness.make_live_broker()
        assert asked == [("binance", KEY)]
    finally:
        key_vault.set_default_vault(None)


def test_live_broker_prefers_the_vault_key_over_the_environment(monkeypatch, tmp_path, master):
    from execution import live_readiness
    from services import key_vault
    vault = KeyVault(tmp_path / "v.db", master)
    vault.attach(TENANT, "binance", "vault-key-VVVV0001", "vault-secret-VVVV0001", checker=safe_checker)
    key_vault.set_default_vault(vault)
    monkeypatch.setenv("HUB_EXCHANGE_API_KEY", KEY)
    try:
        assert live_readiness.exchange_credentials("binance") == ("vault-key-VVVV0001", "vault-secret-VVVV0001")
    finally:
        key_vault.set_default_vault(None)
