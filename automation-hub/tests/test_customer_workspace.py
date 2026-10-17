"""No-network customer workspace behavior and legacy-engine boundary checks.

The fake models rows selected by a caller's verified access token.  It is not a
replacement for database RLS tests: it proves that the HTTP layer delegates to
the dedicated token-scoped store instead of the deployment-wide SQLite store.
"""
from copy import deepcopy
from functools import wraps

import pytest
from fastapi.testclient import TestClient

import app as hub_app
from services.supabase_auth import Principal, SupabaseAuthError


USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"
PRINCIPALS = {
    "access-a": Principal(id=USER_A, email="a@example.invalid", email_confirmed=True,
                          full_name="A", role="user"),
    "access-b": Principal(id=USER_B, email="b@example.invalid", email_confirmed=True,
                          full_name="B", role="user"),
    "unconfirmed": Principal(id=USER_A, email="a@example.invalid", email_confirmed=False,
                             full_name="A", role="user"),
}


class FakeCustomerWorkspaces:
    def __init__(self):
        self.rows = {
            ("access-a", "settings-center"): {"theme": "dark"},
            ("access-b", "settings-center"): {"theme": "light"},
        }
        self.calls = []
        self.failure = None

    def get(self, token, namespace):
        self.calls.append(("get", token, namespace))
        if self.failure:
            raise self.failure
        return deepcopy(self.rows.get((token, namespace), {}))

    def put(self, token, namespace, data):
        self.calls.append(("put", token, namespace, deepcopy(data)))
        if self.failure:
            raise self.failure
        self.rows[(token, namespace)] = deepcopy(data)


@pytest.fixture
def workspace_api(monkeypatch):
    workspaces = FakeCustomerWorkspaces()
    monkeypatch.setattr(hub_app.settings, "auth_mode", "supabase")
    monkeypatch.setattr(hub_app.settings, "admin_key", "internal-control-canary")

    def principal(token):
        if token not in PRINCIPALS:
            raise SupabaseAuthError("Invalid session")
        return PRINCIPALS[token]

    def forbidden_legacy_store(*_args, **_kwargs):
        pytest.fail("Supabase workspace requests must never use legacy SQLite or its mirror")

    monkeypatch.setattr(hub_app.supabase_auth, "principal", principal)
    monkeypatch.setattr(hub_app, "customer_workspaces", workspaces, raising=False)
    for method in ("get_user_settings", "set_user_settings", "delete_user_settings"):
        monkeypatch.setattr(hub_app.store, method, forbidden_legacy_store)

    # Direct route tests avoid application startup, which starts unrelated
    # services. All provider/store calls used by these requests are fakes.
    return TestClient(hub_app.app, base_url="https://testserver"), workspaces


def bearer(token="access-a"):
    return {"Authorization": f"Bearer {token}"}


def test_confirmed_customer_reads_own_workspace(workspace_api):
    client, workspaces = workspace_api
    response = client.get("/user/settings", headers=bearer())
    assert response.status_code == 200
    assert response.json() == {"ns": "settings-center", "data": {"theme": "dark"}}
    assert workspaces.calls == [("get", "access-a", "settings-center")]


def test_confirmed_customer_saves_then_reads_own_workspace(workspace_api):
    client, workspaces = workspace_api
    response = client.post("/user/settings", headers=bearer(),
                           json={"ns": "settings-center", "data": {"theme": "system"}})
    assert response.status_code == 200
    assert response.json() == {"saved": True, "ns": "settings-center"}
    assert client.get("/user/settings", headers=bearer()).json()["data"] == {"theme": "system"}
    assert workspaces.calls[0] == ("put", "access-a", "settings-center", {"theme": "system"})


def test_client_user_id_cannot_select_or_overwrite_another_workspace(workspace_api):
    client, workspaces = workspace_api
    response = client.post("/user/settings", headers=bearer(),
                           json={"user_id": USER_B, "ns": "settings-center", "data": {"theme": "system"}})
    assert response.status_code == 200
    own = client.get("/user/settings", params={"user_id": USER_B}, headers=bearer())
    other = client.get("/user/settings", headers=bearer("access-b"))
    assert own.status_code == other.status_code == 200
    assert own.json()["data"] == {"theme": "system"}
    assert other.json()["data"] == {"theme": "light"}
    assert all(call[1] in {"access-a", "access-b"} for call in workspaces.calls)


@pytest.mark.parametrize("token,status", [(None, 401), ("expired", 401), ("unconfirmed", 403)])
@pytest.mark.parametrize("method", ["get", "post"])
def test_workspace_requires_confirmed_verified_principal(workspace_api, token, status, method):
    client, workspaces = workspace_api
    kwargs = {"headers": bearer(token)} if token else {}
    if method == "post":
        kwargs["json"] = {"data": {"theme": "dark"}}
    response = getattr(client, method)("/user/settings", **kwargs)
    assert response.status_code == status
    assert workspaces.calls == []


def test_control_key_cannot_substitute_for_customer_principal(workspace_api):
    client, workspaces = workspace_api
    response = client.get("/user/settings", headers={"X-Webhook-Secret": "internal-control-canary"})
    assert response.status_code == 401
    assert workspaces.calls == []


@pytest.mark.parametrize("namespace", ["profile", "account", "dashboard", "preferences", "engine", "../preferences", "", "unknown"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_non_workspace_namespaces_are_rejected_before_store_access(workspace_api, namespace, method):
    client, workspaces = workspace_api
    kwargs = {"headers": bearer()}
    if method == "get":
        kwargs["params"] = {"ns": namespace}
    else:
        kwargs["json"] = {"ns": namespace, "data": {}}
    response = getattr(client, method)("/user/settings", **kwargs)
    assert response.status_code == 400
    assert workspaces.calls == []


@pytest.mark.parametrize("payload", [None, [], "text", 1, {"data": []}, {"data": None}, {"data": "text"}])
def test_invalid_workspace_payload_is_rejected_before_store_access(workspace_api, payload):
    client, workspaces = workspace_api
    response = client.post("/user/settings", headers=bearer(), json=payload)
    assert response.status_code == 400
    assert workspaces.calls == []


def test_malformed_json_is_rejected_before_store_access(workspace_api):
    client, workspaces = workspace_api
    response = client.post("/user/settings", headers={**bearer(), "Content-Type": "application/json"},
                           content="{broken-json")
    assert response.status_code == 400
    assert workspaces.calls == []


def test_oversize_workspace_does_not_report_saved(workspace_api):
    client, workspaces = workspace_api
    response = client.post("/user/settings", headers=bearer(), json={"data": {"large": "x" * 200_001}})
    assert response.status_code == 413
    assert response.json().get("saved") is not True
    assert workspaces.calls == []


@pytest.mark.parametrize("raw", [
    " " * 240_000 + '{"data":{}}',
    '{"data":{},"ignored_envelope_field":"' + "x" * 240_000 + '"}',
    "{" + "x" * 240_000,
], ids=["whitespace", "envelope-field", "malformed-body"])
def test_oversize_raw_envelope_is_rejected_before_parsing(workspace_api, monkeypatch, raw):
    client, workspaces = workspace_api
    original_loads = hub_app._json.loads
    large_parse_attempts = []

    def observe_loads(value, *args, **kwargs):
        if len(value) > 232_768:
            large_parse_attempts.append(len(value))
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(hub_app._json, "loads", observe_loads)
    response = client.post("/user/settings", headers={**bearer(), "Content-Type": "application/json"},
                           content=raw)
    assert response.status_code == 413
    assert large_parse_attempts == []
    assert workspaces.calls == []


@pytest.mark.parametrize("method", ["get", "post"])
def test_unavailable_workspace_store_returns_503_without_false_success(workspace_api, method):
    from services.customer_workspaces import WorkspaceError

    client, workspaces = workspace_api
    workspaces.failure = WorkspaceError(503, "Workspace service is temporarily unavailable.")
    kwargs = {"headers": bearer()}
    if method == "post":
        kwargs["json"] = {"data": {"theme": "dark"}}
    response = getattr(client, method)("/user/settings", **kwargs)
    assert response.status_code == 503
    assert response.json().get("saved") is not True
    assert workspaces.rows[("access-a", "settings-center")] == {"theme": "dark"}


def test_cookie_workspace_mutation_keeps_same_origin_protection(workspace_api):
    client, workspaces = workspace_api
    client.cookies.set("hub_supabase_access", "access-a")
    blocked = client.post("/user/settings", headers={"Origin": "https://evil.example"},
                          json={"data": {"theme": "light"}})
    assert blocked.status_code == 403
    assert workspaces.calls == []
    saved = client.post("/user/settings", headers={"Origin": "https://testserver"},
                        json={"data": {"theme": "system"}})
    assert saved.status_code == 200
    assert workspaces.calls == [("put", "access-a", "settings-center", {"theme": "system"})]


@pytest.mark.parametrize("method,path", [
    ("delete", "/user/settings?ns=settings-center"),
    ("put", "/user/settings"),
    ("patch", "/user/settings"),
    ("get", "/user/settings/extra"),
    ("get", "/paper/trades"),
    ("get", "/settings"),
    ("post", "/engine/timeframe?timeframe=1h"),
    ("post", "/instances"),
])
def test_workspace_exception_does_not_open_legacy_or_other_methods(workspace_api, method, path):
    client, workspaces = workspace_api
    response = client.request(method, path, headers=bearer())
    assert response.status_code == 403
    assert workspaces.calls == []


def test_customer_workspace_route_receives_no_internal_control_header(workspace_api, monkeypatch):
    client, _workspaces = workspace_api
    route = next(route for route in hub_app.app.routes
                 if getattr(route, "path", None) == "/user/settings" and "GET" in route.methods)
    original = route.dependant.call
    observed = []

    @wraps(original)
    async def inspect_request(*args, **kwargs):
        observed.append(kwargs["request"].headers.get("x-webhook-secret"))
        return await original(*args, **kwargs)

    monkeypatch.setattr(route.dependant, "call", inspect_request)
    response = client.get("/user/settings", headers=bearer())
    assert response.status_code == 200
    assert observed == [None]
    assert "internal-control-canary" not in response.text
