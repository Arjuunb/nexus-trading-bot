"""Content-Security-Policy and the other browser hardening headers."""
import re

import pytest

from services import csp


def _nonce(header: str) -> str:
    m = re.search(r"script-src 'self' 'nonce-([A-Za-z0-9_-]+)'", header)
    assert m, header
    return m.group(1)


def test_the_policy_allows_only_own_scripts_and_the_nonce():
    p = csp.policy("abc", supabase_url="https://xyz.supabase.co", https=True)
    assert "script-src 'self' 'nonce-abc'" in p
    assert "'unsafe-eval'" not in p
    assert "unsafe-inline" not in p.split("script-src", 1)[1].split(";", 1)[0]
    assert "script-src-attr 'none'" in p and "object-src 'none'" in p and "base-uri 'self'" in p
    assert "https://xyz.supabase.co" in p and "wss://xyz.supabase.co" in p
    assert "frame-ancestors 'self'" in p and "upgrade-insecure-requests" in p


def test_framing_follows_the_configured_ancestors_and_docs_get_their_own_policy():
    assert "frame-ancestors https://tradexa.app" in csp.policy("n", frame_ancestors="https://tradexa.app")
    docs = csp.policy("n", path="/api/v1/docs", https=False)
    assert "https://cdn.jsdelivr.net" in docs and "upgrade-insecure-requests" not in docs


@pytest.fixture()
def client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import app as hub_app
    return TestClient(hub_app.app)


def test_every_response_carries_the_hardening_headers(client):
    r = client.get("/login")
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert "camera=()" in h["Permissions-Policy"] and "payment=()" in h["Permissions-Policy"]
    assert h["Cross-Origin-Opener-Policy"] == "same-origin-allow-popups"
    assert h["X-Permitted-Cross-Domain-Policies"] == "none"
    assert "default-src 'self'" in h["Content-Security-Policy"]


def test_the_sign_in_page_scripts_carry_this_responses_nonce_and_no_inline_handlers(client):
    first, second = client.get("/login"), client.get("/login")
    n1, n2 = _nonce(first.headers["Content-Security-Policy"]), _nonce(second.headers["Content-Security-Policy"])
    assert n1 != n2  # fresh per response
    scripts = re.findall(r"<script([^>]*)>", first.text)
    assert scripts and all(f'nonce="{n1}"' in attrs for attrs in scripts)
    assert not re.search(r"\son(click|submit|change|input|load)=", first.text)
    assert "data-toggle-pw=" in first.text


def test_the_runtime_config_cannot_close_its_script_element(monkeypatch):
    import app as hub_app
    monkeypatch.setattr(hub_app.supabase_auth, "url", "https://x.supabase.co/</script><script>alert(1)</script>",
                        raising=False)
    csp.new_nonce()
    out = hub_app._runtime_config_script()
    assert out.count("</script>") == 1 and "<script>alert" not in out
    assert out.startswith(f'<script nonce="{csp.nonce()}">')


def test_security_txt_names_the_private_reporting_route_and_never_goes_stale(client):
    from datetime import datetime, timedelta, timezone
    r = client.get("/.well-known/security.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    fields = dict(line.split(": ", 1) for line in r.text.strip().splitlines())
    assert fields["Contact"].endswith("/security/advisories/new")
    expires = datetime.fromisoformat(fields["Expires"].replace("Z", "+00:00"))
    assert timedelta(days=30) < expires - datetime.now(timezone.utc) < timedelta(days=365)
    assert fields["Canonical"].endswith("/.well-known/security.txt")
