import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.strategy_intelligence import create_router
from test_shadow_router import setup

BASE = "/api/v2/strategy-intelligence/shadow"


@pytest.fixture
def client(setup):
    router, _, _, _, _, _ = setup
    app = FastAPI()
    app.include_router(create_router(shadow_provider=lambda: router, owner_resolver=lambda request: request.headers.get("x-test-owner")))
    return TestClient(app)


def test_api_authentication_and_explicit_authorization(client, setup):
    _, _, _, reg, _, request = setup
    assert client.get(BASE + "/policy").status_code == 401
    headers = {"x-test-owner": "alice"}
    assert client.get(BASE + "/policy", headers=headers).json()["orders_allowed"] is False
    assert client.post(BASE + "/registrations", json=request, headers=headers).status_code == 201
    assert client.post(BASE + "/registrations", json={**request, "leverage": 20}, headers=headers).status_code == 422
    assert client.get(BASE + "/requirements/" + request["candidate_id"], headers=headers).json()["valid"] is True
    rid = reg["registration_id"]
    assert client.post(BASE + f"/registrations/{rid}/place_order", json={"reason": "forged"}, headers=headers).status_code == 403


@pytest.mark.parametrize("suffix", ["", "/decisions", "/coverage", "/comparison", "/audit"])
def test_all_reads_are_owner_scoped(client, setup, suffix):
    _, _, _, reg, _, _ = setup
    path = BASE + "/registrations/" + reg["registration_id"] + suffix
    assert client.get(path, headers={"x-test-owner": "bob"}).status_code == 404
    assert client.get(path, headers={"x-test-owner": "alice"}).status_code == 200


def test_forbidden_tenant_actions_and_bounded_lists(client, setup):
    _, _, _, reg, _, _ = setup
    headers = {"x-test-owner": "bob"}
    assert client.post(BASE + f'/registrations/{reg["registration_id"]}/pause', json={"reason": "forged"}, headers=headers).status_code == 404
    assert client.get(BASE + "/registrations", headers=headers).json()["rows"] == []
    assert client.get(BASE + "/registrations?limit=100000", headers=headers).status_code == 422
    data = client.get(BASE + "/registrations", headers={"x-test-owner": "alice"}).json()
    assert "lease_token" not in data["rows"][0]


def test_lifecycle_routes_audit_real_transitions(client, setup):
    router, store, _, reg, _, _ = setup
    path = BASE + "/registrations/" + reg["registration_id"]
    headers = {"x-test-owner": "alice"}
    assert client.post(path + "/pause", json={"reason": "pause reviewed observation"}, headers=headers).json()["status"] == "PAUSED"
    assert client.post(path + "/resume", json={"reason": "resume observation"}, headers=headers).status_code == 200
    assert client.post(path + "/stop", json={"reason": "stop observation"}, headers=headers).json()["status"] == "STOPPED"
    assert client.post(path + "/resume", json={"reason": "illegal resume"}, headers=headers).status_code == 409
    assert any(e["action"] == "STOP" for e in store.events(reg["registration_id"], owner_id="alice"))
