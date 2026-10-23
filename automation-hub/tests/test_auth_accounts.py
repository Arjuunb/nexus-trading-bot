"""Tradexa accounts: sign up (single owner), sign in/out, session survival,
API protection wall, password change, and the new bot settings."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app as hub_app
    from database.store import SqliteStore
    hub_app.store = SqliteStore(str(tmp_path / "hub.db"))
    hub_app.store.seed_admin("admin", "admin")
    hub_app._sessions.clear()
    return TestClient(hub_app.app, follow_redirects=False)


def _login(c, user="admin", pw="admin"):
    return c.post("/login", data={"username": user, "password": pw})


# ─────────────────────────── the auth wall ───────────────────────────
def test_react_dashboard_requires_login(client):
    import app as hub_app
    # The bundled landing owns the public root; the protected dashboard is /app.
    dashboard = "/app" if hub_app._LANDING_READY else "/"
    r = client.get(dashboard)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_api_is_walled_for_anonymous_but_open_with_secret_or_session(client):
    assert client.get("/settings").status_code == 401          # anonymous: no
    ok = client.get("/settings", headers={"X-Webhook-Secret": "dev-control-key"})
    assert ok.status_code == 200                               # secret header: yes
    _login(client)
    assert client.get("/settings").status_code == 200          # session cookie: yes


def test_auth_flow_pages_are_exempt(client):
    assert client.get("/login").status_code == 200
    assert client.get("/signup").status_code == 200
    assert client.get("/auth/status").status_code == 200


# ─────────────────────────── sign up (single owner) ───────────────────────────
def test_signup_creates_owner_locks_admin_and_then_closes(client):
    assert client.get("/auth/status").json()["signup_open"] is True
    r = client.post("/signup", data={"username": "arjun", "password": "supersecret1",
                                     "confirm": "supersecret1"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    # signed in immediately
    me = client.get("/auth/status").json()
    assert me["authenticated"] is True and me["user"] == "arjun"
    # signup is now closed and the seeded default admin no longer works
    assert client.get("/auth/status").json()["signup_open"] is False
    c2 = TestClient(__import__("app").app, follow_redirects=False)
    assert "error=" in _login(c2, "admin", "admin").headers.get("location", "")
    r2 = c2.post("/signup", data={"username": "intruder", "password": "hackhackhack",
                                  "confirm": "hackhackhack"})
    assert "error=" in r2.headers.get("location", "")


def test_signup_validation(client):
    bad = client.post("/signup", data={"username": "x", "password": "short",
                                       "confirm": "short"})
    assert "error=" in bad.headers["location"]
    mismatch = client.post("/signup", data={"username": "gooduser",
                                            "password": "longenough1",
                                            "confirm": "different111"})
    assert "error=" in mismatch.headers["location"]


# ─────────────────────────── sessions ───────────────────────────
def test_session_survives_restart_and_logout_clears_it(client):
    _login(client)
    assert client.get("/auth/status").json()["authenticated"] is True
    # "restart": in-memory sessions wiped — the SIGNED cookie still works
    import app as hub_app
    hub_app._sessions.clear()
    assert client.get("/auth/status").json()["authenticated"] is True
    client.post("/auth/logout")
    assert client.get("/auth/status").json()["authenticated"] is False


def test_tampered_session_cookie_is_rejected(client):
    client.cookies.set("hub_session", "admin|9999999999|deadbeef")
    assert client.get("/auth/status").json()["authenticated"] is False


# ─────────────────────────── password change ───────────────────────────
def test_change_password(client):
    _login(client)
    bad = client.post("/auth/change-password",
                      json={"current": "wrong", "new": "newpassword1"})
    assert bad.status_code == 400
    ok = client.post("/auth/change-password",
                     json={"current": "admin", "new": "newpassword1"})
    assert ok.status_code == 200
    import app as hub_app
    assert hub_app.store.authenticate("admin", "newpassword1") is not None
    assert hub_app.store.authenticate("admin", "admin") is None


# ─────────────────────────── new bot settings ───────────────────────────
def test_entry_mode_and_report_hour_settings(client):
    _login(client)
    import webhook_api
    r = client.post("/settings", json={"entry_mode": "market", "daily_report_hour": 6},
                    headers={"X-Webhook-Secret": "dev-control-key"})
    assert r.status_code == 200
    assert webhook_api.engine.entry_mode == "market"
    assert webhook_api.daily_tasks.hour == 6
    body = client.get("/settings").json()
    assert body["editable"]["entry_mode"] == "market"
    assert body["editable"]["daily_report_hour"] == 6
    # invalid values rejected; restore defaults
    assert client.post("/settings", json={"entry_mode": "yolo"},
                       headers={"X-Webhook-Secret": "dev-control-key"}).status_code == 400
    client.post("/settings", json={"entry_mode": "limit", "daily_report_hour": 8},
                headers={"X-Webhook-Secret": "dev-control-key"})
