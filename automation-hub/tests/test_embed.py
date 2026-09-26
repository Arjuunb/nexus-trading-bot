"""Iframe embedding (Tradexa integration): env-driven cookie SameSite and a
frame-ancestors policy that only allows the configured embedding origin."""
import pytest


def test_cookie_defaults_to_lax(monkeypatch):
    import app as hub_app
    monkeypatch.delenv("HUB_COOKIE_SAMESITE", raising=False)
    kw = hub_app._cookie_kwargs()
    assert kw["samesite"] == "lax" and "secure" not in kw
    assert kw["httponly"] is True


def test_cookie_none_requires_secure(monkeypatch):
    import app as hub_app
    monkeypatch.setenv("HUB_COOKIE_SAMESITE", "none")
    kw = hub_app._cookie_kwargs()
    assert kw["samesite"] == "none" and kw["secure"] is True
    # junk values fall back to lax, never an invalid attribute
    monkeypatch.setenv("HUB_COOKIE_SAMESITE", "banana")
    assert hub_app._cookie_kwargs()["samesite"] == "lax"


def test_framing_is_same_origin_by_default_and_follows_the_configured_ancestors(monkeypatch):
    """The CSP (services/csp.py) is always sent; its frame-ancestors is 'self'
    unless HUB_FRAME_ANCESTORS names the sites allowed to embed the app."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import app as hub_app
    client = TestClient(hub_app.app)

    monkeypatch.delenv("HUB_FRAME_ANCESTORS", raising=False)
    r = client.get("/health")
    assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"]

    monkeypatch.setenv("HUB_FRAME_ANCESTORS", "'self' https://tradexa.app")
    r2 = client.get("/health")
    assert "frame-ancestors 'self' https://tradexa.app" in r2.headers["Content-Security-Policy"]
