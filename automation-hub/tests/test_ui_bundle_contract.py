"""HTTP contracts for both the legacy renderer and the bundled UI.

Run against clean dashboard and landing builds to exercise the same-origin
production route/asset profile. No frontend build is required for the retained
legacy/standalone profile.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient

import app as hub_app
from database.store import SqliteStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store = SqliteStore(tmp_path / "hub.db")
    store.seed_admin("admin", "admin")
    monkeypatch.setattr(hub_app, "store", store)
    return TestClient(hub_app.app, follow_redirects=False)


def _dashboard_path():
    return "/app" if hub_app._LANDING_READY else "/"


def _runtime_config(body):
    match = re.search(r"window\.__HUB_CONFIG__=(.+?)</script>", body)
    assert match, "the served React document needs same-origin runtime config"
    return json.loads(match.group(1))


def test_front_door_and_dashboard_have_distinct_auth_contracts(client):
    root = client.get("/")
    if hub_app._LANDING_READY:
        assert root.status_code == 200
        assert "text/html" in root.headers["content-type"]
        assert root.headers["cache-control"].startswith("public,")
        assert _runtime_config(root.text)["apiBase"] == ""
        assert hub_app.settings.admin_key not in root.text
        assert hub_app.settings.secret_key not in root.text
    else:
        assert root.status_code == 303
        assert root.headers["location"] == "/login"

    # A control header authorizes APIs; it never substitutes for a UI session.
    for headers in ({}, {"X-Webhook-Secret": hub_app.settings.admin_key}):
        denied = client.get(_dashboard_path(), headers=headers)
        assert denied.status_code == 303
        assert denied.headers["location"] == "/login"
    if hub_app._LANDING_READY:
        denied = client.get("/app/strategies")
        assert denied.status_code == 303
        assert denied.headers["location"] == "/login"
    assert client.get("/settings").status_code == 401


def test_authenticated_dashboard_serves_its_own_assets_without_control_secrets(client):
    login = client.post("/login", data={"username": "admin", "password": "admin"})
    assert login.status_code == 303
    response = client.get(_dashboard_path())
    assert response.status_code == 200
    assert hub_app.settings.admin_key not in response.text
    assert hub_app.settings.secret_key not in response.text
    if not hub_app._WEBUI_READY:
        assert "Running Bots" in response.text
        assert "EventSource" in response.text and "/events/stream" in response.text
        return

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert _runtime_config(response.text)["apiBase"] == ""
    assert '<div id="root"></div>' in response.text
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', response.text)
    styles = re.findall(r'<link[^>]+href="([^"]+\.css)"', response.text)
    assert scripts and styles, "the dashboard boot shell needs executable JS and CSS"
    asset_prefix = "/app/assets/" if hub_app._LANDING_READY else "/assets/"
    for path in scripts + styles:
        assert path.startswith(asset_prefix), "dashboard assets must use its mounted base"
        asset = client.get(path)
        assert asset.status_code == 200
        assert "text/html" not in asset.headers["content-type"]


def test_standalone_react_root_remains_session_gated(client, monkeypatch, tmp_path):
    webui = tmp_path / "webui"
    webui.mkdir()
    (webui / "index.html").write_text(
        '<!doctype html><html><head></head><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(hub_app, "_LANDING_READY", False)
    monkeypatch.setattr(hub_app, "_WEBUI_READY", True)
    monkeypatch.setattr(hub_app, "_WEBUI", webui)
    denied = client.get("/")
    assert denied.status_code == 303 and denied.headers["location"] == "/login"
    client.post("/login", data={"username": "admin", "password": "admin"})
    accepted = client.get("/")
    assert accepted.status_code == 200
    assert '<div id="root"></div>' in accepted.text
    assert _runtime_config(accepted.text)["apiBase"] == ""
    assert hub_app.settings.admin_key not in accepted.text
