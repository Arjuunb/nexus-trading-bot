"""Token-scoped workspace persistence checks without provider or database I/O."""
from copy import deepcopy
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from services.supabase_auth import Principal, SupabaseAuth, SupabaseAuthError


OWNER = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class FakeAuth:
    def __init__(self):
        self.verified = []
        self.calls = []
        self.confirmed = True
        self.response = []
        self.failure = None

    def principal(self, token):
        self.verified.append(token)
        if token != "customer-access-token":
            raise SupabaseAuthError("token-canary")
        return Principal(id=OWNER, email="person@example.invalid", email_confirmed=self.confirmed,
                         full_name="Person", role="user")

    def _request(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if self.failure:
            raise self.failure
        return deepcopy(self.response)


@pytest.fixture
def workspace_store():
    from services.customer_workspaces import CustomerWorkspaces, WorkspaceError

    auth = FakeAuth()
    return CustomerWorkspaces(auth), auth, WorkspaceError


def row(data=None, **overrides):
    return {"user_id": OWNER, "namespace": "settings-center",
            "data": data if data is not None else {"theme": "dark"}, **overrides}


def test_workspace_get_uses_verified_owner_and_end_user_jwt(workspace_store):
    store, auth, _error = workspace_store
    auth.response = [row()]
    assert store.get("customer-access-token", "settings-center") == {"theme": "dark"}
    assert auth.verified == ["customer-access-token"]
    path, kwargs = auth.calls[0]
    assert urlsplit(path).path == "/rest/v1/tradexa_customer_workspaces"
    query = parse_qs(urlsplit(path).query)
    assert query["user_id"] == [f"eq.{OWNER}"]
    assert query["namespace"] == ["eq.settings-center"]
    assert set(query["select"][0].split(",")) == {"user_id", "namespace", "data"}
    assert query["limit"] == ["2"]
    assert kwargs["token"] == "customer-access-token"
    assert kwargs.get("service", False) is False


def test_missing_workspace_is_empty_without_a_write(workspace_store):
    store, auth, _error = workspace_store
    assert store.get("customer-access-token", "settings-center") == {}
    assert len(auth.calls) == 1
    assert auth.calls[0][1].get("method", "GET") == "GET"


def test_put_verifies_returned_representation_before_success(workspace_store):
    store, auth, _error = workspace_store
    data = {"appearance": {"theme": "dark"}, "preferences": {"sound": False}}
    auth.response = [row(data)]
    store.put("customer-access-token", "settings-center", data)
    path, kwargs = auth.calls[0]
    assert urlsplit(path).path == "/rest/v1/tradexa_customer_workspaces"
    assert parse_qs(urlsplit(path).query)["on_conflict"] == ["user_id,namespace"]
    assert kwargs["payload"] == row(data)
    assert kwargs["method"] == "POST"
    assert kwargs["token"] == "customer-access-token"
    assert kwargs["prefer"] == "resolution=merge-duplicates,return=representation"
    assert kwargs.get("service", False) is False


@pytest.mark.parametrize("method", ["get", "put"])
@pytest.mark.parametrize("namespace", ["dashboard", "preferences", "profile", "account", "", "../settings-center"])
def test_store_rejects_every_other_namespace(workspace_store, method, namespace):
    store, auth, error = workspace_store
    args = ("customer-access-token", namespace) + (({},) if method == "put" else ())
    with pytest.raises(error) as failure:
        getattr(store, method)(*args)
    assert failure.value.status_code == 400
    assert auth.calls == []


@pytest.mark.parametrize("value", [[], "string", None, {"number": float("nan")},
                                   {"number": float("inf")}, {"nested": {"number": -float("inf")}},
                                   {"unsupported": {1, 2}}])
def test_store_rejects_non_object_or_non_finite_json(workspace_store, value):
    store, auth, error = workspace_store
    with pytest.raises(error) as failure:
        store.put("customer-access-token", "settings-center", value)
    assert failure.value.status_code == 400
    assert auth.calls == []


def test_workspace_limit_counts_serialized_utf8_bytes(workspace_store):
    store, auth, error = workspace_store
    # Fewer than 200000 Python characters, more than 200000 UTF-8 bytes.
    with pytest.raises(error) as failure:
        store.put("customer-access-token", "settings-center", {"text": "界" * 70_000})
    assert failure.value.status_code == 413
    assert auth.calls == []


@pytest.mark.parametrize("method", ["get", "put"])
def test_unconfirmed_owner_is_denied_before_database_request(workspace_store, method):
    store, auth, error = workspace_store
    auth.confirmed = False
    args = ("customer-access-token", "settings-center") + (({},) if method == "put" else ())
    with pytest.raises(error) as failure:
        getattr(store, method)(*args)
    assert failure.value.status_code == 403
    assert auth.calls == []


@pytest.mark.parametrize("token", ["", "wrong-token"])
def test_unverified_access_token_never_reaches_workspace_database(workspace_store, token):
    store, auth, error = workspace_store
    with pytest.raises(error) as failure:
        store.get(token, "settings-center")
    assert failure.value.status_code == 401
    assert "token-canary" not in str(failure.value)
    assert auth.calls == []


@pytest.mark.parametrize("response", [None, {}, [row(), row()], [row(user_id=OTHER)],
                                     [row(namespace="profile")], [row(data=[])],
                                     [row(data={"number": float("nan")})],
                                     [row(data={"large": "x" * 200_001})]])
def test_malformed_or_cross_owner_read_is_safe_failure(workspace_store, response):
    store, auth, error = workspace_store
    auth.response = response
    with pytest.raises(error) as failure:
        store.get("customer-access-token", "settings-center")
    assert failure.value.status_code == 503
    assert OWNER not in str(failure.value)
    assert OTHER not in str(failure.value)


@pytest.mark.parametrize("response", [None, [], [row(), row()], [row(user_id=OTHER)],
                                     [row(namespace="profile")], [row(data={"theme": "light"})]])
def test_missing_mismatched_or_duplicate_write_receipt_is_not_success(workspace_store, response):
    store, auth, error = workspace_store
    auth.response = response
    with pytest.raises(error) as failure:
        store.put("customer-access-token", "settings-center", {"theme": "dark"})
    assert failure.value.status_code == 503


def test_boolean_and_numeric_receipts_are_not_interchangeable(workspace_store):
    store, auth, error = workspace_store
    auth.response = [row({"sound": 1})]
    with pytest.raises(error) as failure:
        store.put("customer-access-token", "settings-center", {"sound": True})
    assert failure.value.status_code == 503


@pytest.mark.parametrize("method", ["get", "put"])
def test_provider_failure_is_503_with_no_private_error_detail(workspace_store, method):
    store, auth, error = workspace_store
    auth.failure = SupabaseAuthError("provider-canary: customer-access-token /rest/v1/private")
    args = ("customer-access-token", "settings-center") + (({},) if method == "put" else ())
    with pytest.raises(error) as failure:
        getattr(store, method)(*args)
    assert failure.value.status_code == 503
    assert "canary" not in str(failure.value)
    assert "customer-access-token" not in str(failure.value)


def test_workspace_transport_sends_publishable_key_user_bearer_and_prefer(monkeypatch):
    auth = SupabaseAuth(url="https://example.supabase.co", anon_key="publishable-key",
                        service_role_key="service-key-canary")
    seen = []

    def fake_open(request, timeout):
        seen.append(request)
        assert timeout == 8
        return io.BytesIO(json.dumps([row()]).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    result = auth._request("/rest/v1/tradexa_customer_workspaces", token="customer-access-token",
                           method="POST", payload=row(), max_response_bytes=400_000,
                           prefer="resolution=merge-duplicates,return=representation")
    assert result == [row()]
    request = seen[0]
    assert request.get_header("Apikey") == "publishable-key"
    assert request.get_header("Authorization") == "Bearer customer-access-token"
    assert request.get_header("Prefer") == "resolution=merge-duplicates,return=representation"
    assert "service-key-canary" not in str(request.headers)


def test_bounded_transport_rejects_an_oversize_response(monkeypatch):
    auth = SupabaseAuth(url="https://example.supabase.co", anon_key="publishable-key")
    reads = []

    class BoundedResponse(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: BoundedResponse(b"x" * 101))
    with pytest.raises(SupabaseAuthError):
        auth._request("/rest/v1/tradexa_customer_workspaces", token="customer-access-token",
                      max_response_bytes=100)
    assert reads == [101]
