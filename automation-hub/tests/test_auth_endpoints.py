"""The auth flows over HTTP.

The load-bearing property here is that a correct password alone stops being a
session once 2FA is on. Everything else in this file exists to make sure the
half-finished sign-in cannot be talked into becoming a whole one.
"""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import app as hub_app  # noqa: E402
from database.store import SqliteStore  # noqa: E402
from services import auth_flows as flows  # noqa: E402
from services import mailer, oauth, totp  # noqa: E402

PW = "owner-password-1"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = SqliteStore(tmp_path / "hub.db")
    s.create_user("arjun@example.com", PW, role="owner")
    monkeypatch.setattr(hub_app, "store", s)
    hub_app._sessions.clear()
    return s


@pytest.fixture()
def client(store):
    return TestClient(hub_app.app, follow_redirects=False)


@pytest.fixture()
def sent(monkeypatch):
    box = []
    monkeypatch.setenv("ALERT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("HUB_PUBLIC_URL", "https://hub.example.com")
    monkeypatch.setattr(mailer, "send",
                        lambda to, s, b: box.append({"to": to, "body": b}) or True)
    return box


def _enable_2fa(store, user="arjun@example.com"):
    setup = flows.begin_totp_setup(store, user)
    flows.enable_totp(store, user, totp.code_now(setup["secret"]))
    return setup["secret"]


def _fresh_code(secret):
    import time
    return totp.code_now(secret, at=time.time() + totp.STEP_S)


# ═════════════════════════════════════════════ 2FA gates the sign-in

def test_without_2fa_a_password_still_signs_in(client):
    r = client.post("/login", data={"username": "arjun@example.com", "password": PW})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert hub_app.COOKIE in r.cookies


def test_with_2fa_a_correct_password_grants_no_session(client, store):
    """The whole point. If this regresses, the second factor is decoration."""
    _enable_2fa(store)
    r = client.post("/login", data={"username": "arjun@example.com", "password": PW})
    assert r.status_code == 303 and r.headers["location"] == "/auth/two-factor"
    assert hub_app.COOKIE not in r.cookies
    assert hub_app.PENDING_2FA_COOKIE in r.cookies


def test_the_pending_ticket_is_not_usable_as_a_session_cookie(client, store):
    """Domain separation, checked end to end: the pending token has the same
    shape as a session token, so a bare HMAC would have let it through."""
    _enable_2fa(store)
    r = client.post("/login", data={"username": "arjun@example.com", "password": PW})
    pending = r.cookies[hub_app.PENDING_2FA_COOKIE].strip('"')

    assert hub_app._verify_session(pending) is None
    client.cookies.clear()
    client.cookies.set(hub_app.COOKIE, pending)
    dashboard = "/app" if hub_app._LANDING_READY else "/"
    denied = client.get(dashboard)
    assert denied.status_code == 303
    assert denied.headers["location"] == "/login"
    assert client.get("/settings").status_code == 401


def test_a_valid_code_completes_the_sign_in(client, store):
    secret = _enable_2fa(store)
    client.post("/login", data={"username": "arjun@example.com", "password": PW})
    r = client.post("/auth/two-factor", data={"code": _fresh_code(secret)})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert hub_app.COOKIE in r.cookies
    dashboard = "/app" if hub_app._LANDING_READY else "/"
    assert client.get(dashboard).status_code == 200


def test_a_wrong_code_does_not_complete_it(client, store):
    _enable_2fa(store)
    client.post("/login", data={"username": "arjun@example.com", "password": PW})
    r = client.post("/auth/two-factor", data={"code": "000000"})
    assert r.status_code == 303 and "/auth/two-factor" in r.headers["location"]
    assert hub_app.COOKIE not in r.cookies


def test_the_second_factor_cannot_be_posted_without_the_password_step(client, store):
    """Otherwise anyone who guesses a live code gets in with no password."""
    secret = _enable_2fa(store)
    r = client.post("/auth/two-factor", data={"code": _fresh_code(secret)})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert hub_app.COOKIE not in r.cookies


def test_the_two_factor_page_without_a_ticket_sends_you_back(client, store):
    _enable_2fa(store)
    assert "Start again" in client.get("/auth/two-factor").text


def test_a_recovery_code_completes_the_sign_in(client, store):
    setup = flows.begin_totp_setup(store, "arjun@example.com")
    out = flows.enable_totp(store, "arjun@example.com", totp.code_now(setup["secret"]))
    client.post("/login", data={"username": "arjun@example.com", "password": PW})
    r = client.post("/auth/two-factor", data={"code": out["recovery_codes"][0]})
    assert r.status_code == 303 and hub_app.COOKIE in r.cookies


# ------------------------------------------------------------ the JSON login

def test_json_login_withholds_the_token_when_2fa_is_on(client, store):
    """A JWT handed out here would make 2FA cosmetic for every API client."""
    _enable_2fa(store)
    r = client.post("/auth/login", data={"username": "arjun@example.com", "password": PW})
    assert r.status_code == 401
    body = r.json()
    assert body["mfa_required"] is True and "token" not in body


def test_json_login_still_returns_a_token_without_2fa(client):
    r = client.post("/auth/login", data={"username": "arjun@example.com", "password": PW})
    assert r.status_code == 200 and r.json()["token"]


# ═════════════════════════════════════════════════ password reset

def test_forgot_password_redirects_identically_for_hit_and_miss(client, sent):
    """A different destination for a real account turns this into a free
    membership check."""
    hit = client.post("/auth/forgot-password", data={"identifier": "arjun@example.com"})
    miss = client.post("/auth/forgot-password", data={"identifier": "nobody@example.com"})
    assert hit.status_code == miss.status_code == 303
    assert hit.headers["location"] == miss.headers["location"]


def test_the_emailed_link_resets_the_password_through_the_form(client, sent):
    client.post("/auth/forgot-password", data={"identifier": "arjun@example.com"})
    token = sent[0]["body"].split("token=", 1)[1].split()[0]

    assert client.get(f"/auth/reset-password?token={token}").status_code == 200
    r = client.post("/auth/reset-password",
                    data={"token": token, "password": "new-password-9",
                          "confirm": "new-password-9"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.post("/login", data={"username": "arjun@example.com",
                                       "password": "new-password-9"}).status_code == 303


def test_the_reset_form_does_not_sign_you_in(client, sent):
    """Whoever opened a forwarded email should still have to know the password
    they just set."""
    client.post("/auth/forgot-password", data={"identifier": "arjun@example.com"})
    token = sent[0]["body"].split("token=", 1)[1].split()[0]
    r = client.post("/auth/reset-password",
                    data={"token": token, "password": "new-password-9",
                          "confirm": "new-password-9"})
    assert hub_app.COOKIE not in r.cookies


def test_reset_without_a_token_explains_itself(client):
    assert "Link missing" in client.get("/auth/reset-password").text


def test_a_bad_reset_token_bounces_back_with_the_reason(client):
    r = client.post("/auth/reset-password",
                    data={"token": "nope", "password": "new-password-9",
                          "confirm": "new-password-9"})
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_the_forgot_page_warns_when_mail_is_not_configured(client, monkeypatch):
    monkeypatch.delenv("ALERT_SMTP_HOST", raising=False)
    monkeypatch.delenv("HUB_PUBLIC_URL", raising=False)
    assert "ALERT_SMTP_HOST" in client.get("/auth/forgot-password").text


# ═════════════════════════════════════════════ email verification

def test_the_verification_link_confirms_the_address(client, store, sent):
    flows.request_email_verification(store, "arjun@example.com")
    token = sent[0]["body"].split("token=", 1)[1].split()[0]
    assert "Email confirmed" in client.get(f"/auth/verify-email?token={token}").text
    assert store.get_user("arjun@example.com").email_verified is True


def test_a_bad_verification_link_says_so(client):
    assert "didn't work" in client.get("/auth/verify-email?token=nope").text


def test_resending_verification_requires_a_session(client):
    assert client.post("/auth/resend-verification").status_code == 401


# ═════════════════════════════════════════════════════════ 2FA management

def test_managing_2fa_requires_a_session(client):
    assert client.get("/auth/2fa/status").status_code == 401
    assert client.post("/auth/2fa/setup").status_code == 401
    assert client.post("/auth/2fa/enable", data={"code": "123456"}).status_code == 401


def test_setup_then_enable_then_disable_over_http(client, store):
    client.post("/login", data={"username": "arjun@example.com", "password": PW})
    secret = client.post("/auth/2fa/setup").json()["secret"]
    assert client.get("/auth/2fa/status").json()["pending_setup"] is True

    out = client.post("/auth/2fa/enable", data={"code": totp.code_now(secret)}).json()
    assert out["ok"] is True and len(out["recovery_codes"]) == totp.RECOVERY_COUNT
    assert client.get("/auth/2fa/status").json()["enabled"] is True

    assert client.post("/auth/2fa/disable", data={"password": "wrong"}).json()["ok"] is False
    assert client.post("/auth/2fa/disable", data={"password": PW}).json()["ok"] is True


# ═════════════════════════════════════════════════════════════ OAuth

@pytest.fixture()
def oauth_env(monkeypatch):
    monkeypatch.setenv("HUB_PUBLIC_URL", "https://hub.example.com")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")


def test_providers_report_honestly_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    body = client.get("/auth/oauth/providers").json()
    assert body["google"]["available"] is False and "GOOGLE_CLIENT_ID" in body["google"]["note"]


def test_starting_an_unconfigured_provider_fails_with_the_reason(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    r = client.get("/auth/oauth/google/start")
    assert r.status_code == 503 and "GOOGLE_CLIENT_ID" in r.json()["detail"]


def test_an_unknown_provider_is_a_404(client):
    assert client.get("/auth/oauth/facebook/start").status_code == 404


def test_starting_redirects_to_the_provider_with_a_signed_state(client, oauth_env):
    r = client.get("/auth/oauth/google/start")
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith(oauth.GOOGLE.authorize_endpoint)
    state = loc.split("state=", 1)[1].split("&")[0]
    from urllib.parse import unquote
    assert oauth.verify_state(unquote(state), hub_app.settings.secret_key) is True


def test_the_callback_refuses_a_forged_state(client, oauth_env, monkeypatch):
    """This is the CSRF guard: without it an attacker can have a victim's
    browser redeem a code of the attacker's choosing."""
    monkeypatch.setattr(oauth, "exchange_code",
                        lambda *a, **k: pytest.fail("must not exchange on bad state"))
    r = client.get("/auth/oauth/google/callback?code=abc&state=forged")
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert hub_app.COOKIE not in r.cookies


def _good_state():
    return oauth.sign_state(hub_app.settings.secret_key)


def test_a_linked_identity_signs_in(client, store, oauth_env, monkeypatch):
    store.link_oauth("google", "sub-1", "arjun@example.com", "arjun@example.com")
    monkeypatch.setattr(oauth, "exchange_code", lambda *a, **k: "tok")
    monkeypatch.setattr(oauth, "fetch_profile", lambda *a, **k: oauth.Profile(
        provider="google", subject="sub-1", email="arjun@example.com",
        email_verified=True, name="Arjun"))
    r = client.get(f"/auth/oauth/google/callback?code=abc&state={_good_state()}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert hub_app.COOKIE in r.cookies


def test_a_verified_provider_email_auto_links_an_existing_account(client, store,
                                                                  oauth_env, monkeypatch):
    monkeypatch.setattr(oauth, "exchange_code", lambda *a, **k: "tok")
    monkeypatch.setattr(oauth, "fetch_profile", lambda *a, **k: oauth.Profile(
        provider="google", subject="sub-9", email="arjun@example.com",
        email_verified=True, name="Arjun"))
    r = client.get(f"/auth/oauth/google/callback?code=abc&state={_good_state()}")
    assert r.status_code == 303 and hub_app.COOKIE in r.cookies
    assert store.find_by_oauth("google", "sub-9").username == "arjun@example.com"


def test_an_unverified_provider_email_does_not_auto_link(client, store,
                                                         oauth_env, monkeypatch):
    """Anyone can put an address on a provider account. Honouring an unverified
    one would let them claim the matching local account."""
    monkeypatch.setattr(oauth, "exchange_code", lambda *a, **k: "tok")
    monkeypatch.setattr(oauth, "fetch_profile", lambda *a, **k: oauth.Profile(
        provider="google", subject="attacker", email="arjun@example.com",
        email_verified=False, name="Not Arjun"))
    r = client.get(f"/auth/oauth/google/callback?code=abc&state={_good_state()}")
    assert r.status_code == 303 and hub_app.COOKIE not in r.cookies
    assert store.find_by_oauth("google", "attacker") is None


def test_a_failed_profile_fetch_does_not_sign_anyone_in(client, oauth_env, monkeypatch):
    monkeypatch.setattr(oauth, "exchange_code", lambda *a, **k: "tok")
    monkeypatch.setattr(oauth, "fetch_profile", lambda *a, **k: None)
    r = client.get(f"/auth/oauth/google/callback?code=abc&state={_good_state()}")
    assert r.status_code == 303 and hub_app.COOKIE not in r.cookies


def test_a_cancelled_consent_screen_is_reported_not_crashed(client, oauth_env):
    r = client.get(f"/auth/oauth/google/callback?error=access_denied&state={_good_state()}")
    assert r.status_code == 303 and "cancelled" in r.headers["location"]


def test_oauth_still_stops_at_the_second_factor(client, store, oauth_env, monkeypatch):
    """OAuth proves the provider account, not the second factor. Skipping it
    would make 2FA bypassable by whoever holds the linked inbox."""
    _enable_2fa(store)
    store.link_oauth("google", "sub-1", "arjun@example.com", "arjun@example.com")
    monkeypatch.setattr(oauth, "exchange_code", lambda *a, **k: "tok")
    monkeypatch.setattr(oauth, "fetch_profile", lambda *a, **k: oauth.Profile(
        provider="google", subject="sub-1", email="arjun@example.com",
        email_verified=True, name="Arjun"))
    r = client.get(f"/auth/oauth/google/callback?code=abc&state={_good_state()}")
    assert r.headers["location"] == "/auth/two-factor"
    assert hub_app.COOKIE not in r.cookies


def test_listing_and_unlinking_require_a_session(client):
    assert client.get("/auth/oauth/links").status_code == 401
    assert client.post("/auth/oauth/google/unlink").status_code == 401


def test_a_signed_in_user_can_see_and_remove_a_link(client, store, oauth_env):
    store.link_oauth("google", "sub-1", "arjun@example.com", "arjun@example.com")
    client.post("/login", data={"username": "arjun@example.com", "password": PW})
    assert len(client.get("/auth/oauth/links").json()["links"]) == 1
    assert client.post("/auth/oauth/google/unlink").json()["links"] == []
