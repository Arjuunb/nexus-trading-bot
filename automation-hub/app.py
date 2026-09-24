"""Automation Hub — FastAPI application (Phase 1).

Run locally:
    pip install -e ".[hub]"           # from the repo root (fastapi + uvicorn)
    cd automation-hub && uvicorn app:app --reload

Flow: Login -> Dashboard -> Create Bot -> Choose Strategy -> Select Exchange ->
Set Risk Rules -> Paper Trade -> Review Results -> (Deploy Live*) -> Monitor.
*Live execution is Phase 5; Phase 1 runs everything in paper mode.

Phase 1 is single-process and in-memory: one operator (configured credentials),
an in-memory BotManager, and paper runs over historical/synthetic data. The
package layout (bots/ strategies/ exchanges/ execution/ risk/ data/ ...) is the
production shape later phases fill in.
"""
from __future__ import annotations

import hmac
import queue
import secrets
import sys
from pathlib import Path

# Make the sibling packages (bots, dashboard, strategies, ...) importable
# whether launched via uvicorn from this dir or imported by the test suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from typing import Optional  # noqa: E402
from fastapi import FastAPI, Form, HTTPException, Request  # noqa: E402
from fastapi.responses import (  # noqa: E402
    HTMLResponse, RedirectResponse, StreamingResponse,
)

from config import settings, validate_credential_separation  # noqa: E402
from bots.manager import BotManager  # noqa: E402
from bots.registry import EXCHANGES, STRATEGIES, exchange_label, strategy_label  # noqa: E402
from dashboard import widgets as w  # noqa: E402
from dashboard.overview import render_overview  # noqa: E402
from database.models import BotConfig, BotMode, RiskRules  # noqa: E402
from database.store import SqliteStore  # noqa: E402
from core_engine.observer import CoreV2ShadowObserver  # noqa: E402
from core_engine.persistence import ShadowDecisionStore  # noqa: E402
from routers.core_v2 import create_router as create_core_v2_router  # noqa: E402
from services.supabase_auth import Principal, SupabaseAuth, SupabaseAuthError  # noqa: E402

# The API version prefix, declared before the app so the OpenAPI docs can live
# under it (see below). The router mount further down reuses this constant.
API_VERSION = "v1"

# Swagger and ReDoc move under /api/v1 rather than sitting at the root.
#
# FastAPI serves them at "/docs" and "/redoc" by default, and "/docs" is now a
# page of the public marketing site — the documentation index. The built-in
# routes are registered at construction, so they win every match and the public
# page was simply unreachable in the bundled deployment. Moving them is also
# what docs/API_SPEC.md already prescribes for the versioned API surface.
app = FastAPI(
    title=settings.app_name,
    docs_url=f"/api/{API_VERSION}/docs",
    redoc_url=f"/api/{API_VERSION}/redoc",
)
# SQLite keeps bot configuration and is retained for the emergency-only legacy
# mode. Customer identity is Supabase Auth; do not seed an env-password user in
# that mode because it would silently create a second login authority.
store = SqliteStore(settings.db_path)
# Durable settings mirror: when SUPABASE_URL + SUPABASE_KEY are set, per-user
# settings persist to Supabase so they survive an ephemeral-disk restart (no
# more "defaults on every login") without needing a paid disk. No-op otherwise.
try:
    from data.settings_store import make_settings_mirror, SETTINGS_MIRROR_STATUS  # noqa: E402
    store.settings_mirror = make_settings_mirror()
    if store.settings_mirror is not None:
        print("[settings] durable Supabase mirror CONNECTED — settings survive redeploys.", flush=True)
    elif SETTINGS_MIRROR_STATUS["configured"]:
        print(f"[settings] Supabase configured but NOT working: {SETTINGS_MIRROR_STATUS['error']} "
              f"— settings will still reset on redeploy until this is fixed.", flush=True)
    else:
        print("[settings] no Supabase (SUPABASE_URL/KEY unset) — settings are local only "
              "and reset on an ephemeral-disk redeploy.", flush=True)
except Exception:  # noqa: BLE001 — never let settings persistence break boot
    pass
if settings.auth_mode == "legacy":
    store.seed_admin(settings.username, settings.password)
elif settings.auth_mode != "supabase":
    raise RuntimeError("HUB_AUTH_MODE must be 'supabase' or 'legacy'")
supabase_auth = SupabaseAuth()
manager = BotManager(store=store)
# V2 records are kept in a distinct table in the durable decisions database.
# They are shadow observations, not legacy decisions and never execution input.
core_v2_store = ShadowDecisionStore(settings.decisions_db)

# M-7: fail closed on insecure defaults in production. The deployment operator
# strong HUB_SECRET, so a default here on a cloud host means misconfiguration —
# and a default session-signing key makes cookie forgery trivial. A default
# password is warned about loudly but not hard-blocked (so you're never locked
# out of your own deploy).
import os as _sec_os  # noqa: E402
_ON_CLOUD = bool(_sec_os.environ.get("RENDER") or _sec_os.environ.get("DYNO")
                 or _sec_os.environ.get("HUB_ENV", "").lower() == "production")
_UNDER_TEST = "PYTEST_CURRENT_TEST" in _sec_os.environ or bool(_sec_os.environ.get("HUB_DEV"))
validate_credential_separation(production=_ON_CLOUD and not _UNDER_TEST)
if _ON_CLOUD and not _UNDER_TEST:
    if settings.auth_mode != "supabase" and not settings.emergency_admin_enabled:
        raise RuntimeError(
            "REFUSING TO BOOT: production customer authentication requires "
            "HUB_AUTH_MODE=supabase. Set HUB_EMERGENCY_ADMIN_ENABLED=1 only "
            "for a time-limited break-glass recovery session.")
    if settings.auth_mode == "supabase" and not supabase_auth.configured:
        raise RuntimeError(
            "REFUSING TO BOOT: SUPABASE_URL and SUPABASE_ANON_KEY are required "
            "when HUB_AUTH_MODE=supabase.")
    if settings.secret_key == "dev-insecure-secret":
        raise RuntimeError(
            "REFUSING TO BOOT: HUB_SECRET is unset (default session-signing key). "
            "Set HUB_SECRET to a strong random value before exposing this service "
            "— otherwise session "
            "cookies are forgeable.")
    import sys as _sec_sys
    if settings.password == "admin":
        print("\n" + "=" * 68 + "\n  SECURITY WARNING: HUB_PASSWORD is the default 'admin'.\n"
              "  Set a strong HUB_PASSWORD before real use.\n" + "=" * 68 + "\n",
              file=_sec_sys.stderr, flush=True)
print("[auth] webhook, control and exchange credentials are independently scoped.", flush=True)

# Kyros Phase 1: TradingView webhook -> paper-execution -> ledger API.
# `webhook_api` also owns the process-wide paper account / ledger / control
# switch singletons; the dashboard pages below read them via the module so they
# always reflect live webhook activity.
import webhook_api  # noqa: E402
from webhook_api import router as webhook_router  # noqa: E402
from services.factory_reset import FactoryResetService  # noqa: E402
webhook_api.core_v2_store = core_v2_store
webhook_api.factory_reset_service = FactoryResetService(webhook_api, store, manager)
_core_v2_mode = _sec_os.environ.get("HUB_CORE_V2_MODE", "off").lower()
if _core_v2_mode == "shadow":
    webhook_api.engine.core_v2_observer = CoreV2ShadowObserver(core_v2_store)
elif _core_v2_mode not in ("off", ""):
    raise RuntimeError("HUB_CORE_V2_MODE must be 'off' or 'shadow'; V2 execution is unavailable")
app.include_router(webhook_router)
# DSP Sprint 1c: expose the SAME JSON API under a versioned /api/v1 namespace,
# with the legacy root paths preserved (aliased) so nothing breaks. New clients
# target /api/v1; existing callers keep working unchanged. The auth/user
# endpoints defined directly on `app` stay at root for now (they move into a
# router in a later slice), so /api/v1 covers the router-based API surface.
app.include_router(webhook_router, prefix="/api/" + API_VERSION)
app.include_router(create_core_v2_router(core_v2_store))


@app.get("/api/" + API_VERSION)
@app.get("/api/version")
def api_info():
    """Version handshake for the versioned API namespace."""
    return {"ok": True, "name": settings.app_name, "api_version": API_VERSION,
            "endpoints_base": "/api/" + API_VERSION, "legacy_base": "/"}

# The React dashboard runs on its own dev origin (Vite) and calls this API, so
# allow cross-origin during local development.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
# CORS origins are configurable: set HUB_CORS_ORIGINS to a comma-separated allow
# list (e.g. "https://www.trade-logx.com,https://x.onrender.com") in production.
# Unset defaults to "*" for local dev, but on a cloud host that is a loud warning
# (§10/§18 of the SAD) — credentials are off so the session cookie is never sent
# cross-origin, but tightening origins is still correct hygiene.
_cors_env = _sec_os.environ.get("HUB_CORS_ORIGINS", "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    _cors_origins = ["*"]
    if _ON_CLOUD and not _UNDER_TEST:
        print("[cors] WARNING: HUB_CORS_ORIGINS unset — allowing all origins. "
              "Set it to your real origins in production.", flush=True)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins, allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)

# Iframe embedding (e.g. inside the Tradexa app): set HUB_FRAME_ANCESTORS to the
# embedding origin(s) — e.g. "https://tradexa.app" or "'self' https://tradexa.app"
# — and browsers will allow ONLY those sites to frame the dashboard (clickjacking
# stays blocked everywhere else). Unset = header not sent (default behavior).
import os as _mw_os  # noqa: E402


@app.middleware("http")
async def _security_headers(request, call_next):
    """Baseline security response headers (DSP Sprint 11). Deliberately does NOT
    set X-Frame-Options — framing is governed by the configurable CSP
    frame-ancestors below so the dashboard can still be embedded in the Tradexa
    app when HUB_FRAME_ANCESTORS is set."""
    resp = await call_next(request)
    # always-safe hardening
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # HSTS only on HTTPS (honours Render's X-Forwarded-Proto) — never on plain
    # HTTP, where browsers ignore it and it only risks dev confusion.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    # configurable framing policy (unchanged): opt-in embedding allow-list
    ancestors = _mw_os.environ.get("HUB_FRAME_ANCESTORS", "").strip()
    if ancestors:
        resp.headers["Content-Security-Policy"] = f"frame-ancestors {ancestors}"
    return resp

# API protection: every data/control endpoint requires a signed-in session
# (cookie) or the webhook secret (header). Exempt: the auth flow itself, the
# TradingView webhook (it authenticates with the secret in its own handler),
# static assets, and "/" (which redirects anonymous visitors to /login).
_AUTH_EXEMPT = ("/login", "/signup", "/auth/", "/webhook", "/assets",
                "/favicon", "/openapi.json", "/health", "/version",
                "/api/v1/docs", "/api/v1/redoc",   # Swagger/ReDoc moved off "/docs"; same audience as before
                "/nexus-mark", "/apple-touch", "/icon-", "/maskable-", "/mstile-",
                "/og-image", "/logo-mark", "/site.webmanifest", "/robots.txt",
                "/sitemap.xml",
                "/status/public")  # the public status feed: it must answer people who cannot sign in

# The public marketing site's pages.
#
# Every one is a real URL in the sitemap, and the SPA uses BrowserRouter, so a
# hard load, a refresh or a crawler visit has to get HTML back from this app.
# Before these existed, eighteen of the nineteen returned this API's 404 JSON:
# client-side navigation worked, so the breakage was invisible to anyone
# already on the site and total for anyone arriving from a link or a search
# result — which is the entire audience the routes were split out for.
#
# Matched EXACTLY, never as prefixes. `_AUTH_EXEMPT` above is a prefix list,
# and exempting "/api" that way would have unlocked the whole "/api/v1/*"
# subtree that must stay session-gated.
_LANDING_PAGES = (
    "features", "engine", "live-trade", "selectivity", "how-it-works", "security",
    "performance", "dashboard", "docs", "api", "sdks", "open-source", "github",
    "support", "community", "status", "privacy", "terms", "risk-disclosure",
)
_LANDING_PAGE_PATHS = frozenset("/" + p for p in _LANDING_PAGES)


from services.ratelimit import limiter as _rl  # noqa: E402
# Per-IP sliding-window caps on the endpoints worth brute-forcing. Configurable;
# generous enough not to bite real users. Skipped under pytest.
_RL_AUTH = (int(_sec_os.environ.get("HUB_RL_AUTH_MAX", "12")), 300.0)      # login/signup: 12 / 5 min
_RL_WEBHOOK = (int(_sec_os.environ.get("HUB_RL_WEBHOOK_MAX", "120")), 60.0)  # webhook: 120 / min


def _client_ip(request: Request) -> str:
    """Real client IP behind Render's proxy (first X-Forwarded-For hop)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def _require_auth(request: Request, call_next):
    path = request.url.path
    factory_reset_service = getattr(webhook_api, "factory_reset_service", None)
    if (factory_reset_service is not None and factory_reset_service.in_progress
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and not path.rstrip("/").endswith("/system/factory-reset")):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"error": "Factory Reset is in progress; state-changing requests are blocked."},
            status_code=503,
        )
    # Same-origin protection for state-changing requests authenticated only by
    # the HttpOnly Supabase bridge cookie. A browser supplies Origin for fetch/
    # form requests; an absent Origin is retained for non-browser API clients.
    if (settings.auth_mode == "supabase" and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.cookies.get(SUPABASE_COOKIE) and not request.headers.get("authorization")):
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            if urlparse(origin).netloc != request.headers.get("host", ""):
                from fastapi.responses import JSONResponse
                return JSONResponse({"error": "Cross-site request rejected."}, status_code=403)
    # Trading Instance mutations are execution-control operations, not ordinary
    # authenticated account writes. Viewers can inspect state but never start,
    # pause, stop, restart, configure, create or delete a worker.
    if (path == "/instances" or path.startswith("/instances/")) \
            and request.method in {"POST", "PUT", "PATCH", "DELETE"} \
            and _user(request) and not _has_role(request, "operator"):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"error": "Operator role required for Trading Instance control."},
            status_code=403,
        )
    # --- rate limiting (brute-force protection) — before auth so failed attempts
    # count. The under-test check is evaluated per-REQUEST (PYTEST_CURRENT_TEST is
    # set during a test's execution, not necessarily at import), so the limiter is
    # inert in the suite but live in production.
    if request.method == "POST" and not _sec_os.environ.get("PYTEST_CURRENT_TEST"):
        rule = None
        if path in ("/login", "/signup"):
            rule, tag = _RL_AUTH, "auth"
        elif path.startswith("/webhook"):
            rule, tag = _RL_WEBHOOK, "webhook"
        if rule is not None:
            limit, window = rule
            key = f"{_client_ip(request)}:{tag}"
            if not _rl.allow(key, limit, window):
                from fastapi.responses import JSONResponse
                ra = _rl.retry_after(key, window)
                return JSONResponse(
                    {"error": "Too many attempts. Please wait and try again."},
                    status_code=429, headers={"Retry-After": str(ra)})
    exempt = _AUTH_EXEMPT
    # With the landing bundled, its public SPA routes bypass the API auth wall.
    # "/app" is exempt here but self-gates in its own handler (it carries the
    # control secret, so anonymous visitors are redirected to /login there).
    if _LANDING_READY:
        # H-2: exempt the SPA sub-routes ("/settings/profile", …) but NOT the
        # bare "/settings" API endpoint, which returns live strategy/risk config
        # and must stay session-gated. The landing serves only "/settings/{path}".
        exempt = exempt + ("/settings/", "/app")
    hdr = request.headers.get("x-webhook-secret")
    # The retained SQLite engine is deployment-wide legacy state. While the
    # per-user execution stores are migrated to the RLS-backed Supabase tables,
    # never let a regular SaaS user read or control that owner's account. Their
    # verified account/profile routes remain available under /auth/ and the
    # protected settings SPA under /settings/.
    principal = None
    if settings.auth_mode == "supabase":
        principal = _supabase_principal(request)
        legacy_engine_path = not (path.startswith("/auth/") or path.startswith("/settings/")
                                  or path == "/app" or path.startswith("/app/"))
        if principal and not principal.is_admin and legacy_engine_path:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "This deployment-wide engine is restricted to administrators while its legacy data is migrated."}, status_code=403)
    if (request.method == "OPTIONS"          # CORS preflight — CORSMiddleware answers it
            or path == "/" or path in ("/api/version", "/api/" + API_VERSION)  # public version handshake (NOT the /api/v1/* API subtree)
            # public marketing pages, matched exactly — see _LANDING_PAGE_PATHS
            or (_LANDING_READY and path in _LANDING_PAGE_PATHS)
            or any(path.startswith(p) for p in exempt)
            or _user(request)
            or hdr == settings.admin_key):
        # A browser session is an authenticated operator.  Supply the internal
        # control credential only to downstream endpoint handlers; it is never
        # rendered into React configuration or sent over the network by clients.
        # Legacy handlers consume the control credential as a header. Bridge it
        # internally for a legacy owner or a verified Supabase administrator;
        # it is never exposed to JavaScript or transmitted by the browser.
        # A regular Supabase customer never receives this bridge, preserving the
        # fail-closed boundary around the deployment-wide legacy engine.
        is_control_operator = _has_role(request, "operator")
        if is_control_operator and not hdr:
            request.scope["headers"] = list(request.scope["headers"]) + [
                (b"x-webhook-secret", settings.admin_key.encode("utf-8"))]
        return await call_next(request)
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "Sign in required"}, status_code=401)


# Secret redaction for every JSON response (services/redaction.py). Added after
# the middleware above, so it wraps them and sees every body the app produces --
# a route returning its own JSONResponse is covered as well as one returning a
# dict. The two paths below exist to hand a credential to its owner, so they
# keep name-based fields; live secret VALUES are still removed from them.
from services.redaction import RedactionMiddleware  # noqa: E402
app.add_middleware(RedactionMiddleware, credential_paths=("/auth/login", "/auth/2fa/setup"))


def _audit_identify(scope: dict, client_headers: dict) -> tuple[str, str]:
    """Who made a state-changing request, and how they proved it -- judged
    on the headers the client actually sent (see services/audit_middleware)."""
    path = scope.get("path", "")
    if path.startswith("/webhook"):
        return "tradingview-webhook", "webhook"  # the status says whether its secret was accepted
    try:
        user = _user(Request(scope))
    except Exception:  # noqa: BLE001 -- an unreadable session is simply no session
        user = None
    if user:
        return str(user), "session"
    presented = client_headers.get("x-webhook-secret", "")
    if presented and hmac.compare_digest(presented, settings.admin_key):
        return "control-key", "control_key"
    return "anonymous", "none"


# The security audit log (services/audit_log.py). Outermost of all, so refused
# requests (401/403/429) are recorded alongside the ones that succeeded.
from services import audit_log as _audit_log  # noqa: E402
from services.audit_middleware import AuditMiddleware  # noqa: E402
app.add_middleware(AuditMiddleware, log_factory=_audit_log.default_log, identify=_audit_identify,
                   sign_in_paths=("/login", "/auth/login"))

# Single-origin UI: when the React build is present (copied into ./webui by the
# Docker image), serve it from this backend so Render shows the SAME dashboard as
# Vercel. Assets are mounted; index.html is served at "/" with runtime config
# injected (apiBase="" same-origin + the webhook secret). Without a build (tests/
# local), the legacy server-rendered dashboard is served instead.
import json as _json  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
_WEBUI = Path(__file__).resolve().parent / "webui"
_WEBUI_READY = (_WEBUI / "index.html").exists()

# Optional standalone landing / auth / settings SPA (tradexa-landing), bundled by
# the Docker image into ./landing. When present it becomes the PUBLIC front door
# at "/", and the session-gated dashboard moves under "/app". When absent (tests
# / local without the landing build) the dashboard keeps "/" exactly as before.
_LANDING = Path(__file__).resolve().parent / "landing"
_LANDING_READY = (_LANDING / "index.html").exists()

if _WEBUI_READY and (_WEBUI / "assets").exists():
    # dashboard assets move to /app/assets when the landing owns /assets
    _DASH_ASSETS = "/app/assets" if _LANDING_READY else "/assets"
    app.mount(_DASH_ASSETS, StaticFiles(directory=str(_WEBUI / "assets")), name="assets")
if _LANDING_READY and (_LANDING / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_LANDING / "assets")), name="landing-assets")

# Root-level brand assets (favicons, PWA icons, OG image, manifest). Vite copies
# these from each app's public/ into its dist root, so they live beside index.html
# rather than under /assets. Serve them from whichever built app has them, plus a
# repo fallback so the backend-hosted pages (auth, legacy) always find the mark.
from fastapi.responses import FileResponse  # noqa: E402
_BRAND_FILES = ("nexus-mark.svg", "nexus-mark-small.svg", "favicon-16.png", "favicon-32.png", "favicon-48.png",
                "apple-touch-icon.png", "icon-192.png", "icon-512.png", "maskable-512.png",
                "mstile-150.png", "og-image.png", "logo-mark-512.png", "site.webmanifest",
                # Crawler files. They live beside index.html for the same reason
                # the icons do, and a sitemap that 404s is worse than none —
                # it advertises twenty URLs and then refuses to confirm any.
                "robots.txt", "sitemap.xml")
_BRAND_FALLBACK = Path(__file__).resolve().parent / "static" / "brand"


def _brand_asset(name: str):
    for base in (_LANDING, _WEBUI, _BRAND_FALLBACK):
        p = base / name
        if p.exists():
            return FileResponse(str(p))
    raise HTTPException(status_code=404, detail="not found")


for _bf in _BRAND_FILES:
    app.add_api_route(f"/{_bf}", (lambda n: lambda: _brand_asset(n))(_bf),
                      methods=["GET"], include_in_schema=False)


def _serve_react() -> HTMLResponse:
    html = (_WEBUI / "index.html").read_text(encoding="utf-8")
    cfg = ('<script>window.__HUB_CONFIG__='
           + _json.dumps({"apiBase": "", "authMode": settings.auth_mode,
                          "supabaseUrl": supabase_auth.url or None,
                          "supabaseAnonKey": supabase_auth.anon_key or None,
                          "oauthProviders": [p for p in ("google", "apple")
                                            if _sec_os.environ.get(f"HUB_AUTH_{p.upper()}_ENABLED", "").lower() in ("1", "true", "yes")]})
           + '</script>')
    return HTMLResponse(
        html.replace("<head>", "<head>" + cfg, 1),
        headers={
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


from functools import lru_cache as _lru_cache  # noqa: E402


@_lru_cache(maxsize=64)
def _landing_document(path: str) -> str:
    """Return the route-specific, pre-rendered public document when present.

    The client remains a BrowserRouter SPA, but a crawler (or a browser before
    JavaScript starts) must receive the page it requested rather than twenty
    copies of the home-page boot shell. The build writes immutable documents
    under ``landing/seo``; auth/settings routes deliberately retain the generic
    shell because they are private application surfaces.
    """
    clean = path.rstrip("/") or "/"
    if clean == "/":
        candidate = _LANDING / "seo" / "index.html"
    elif clean in _LANDING_PAGE_PATHS:
        candidate = _LANDING / "seo" / f"{clean[1:]}.html"
    else:
        candidate = _LANDING / "index.html"
    if not candidate.exists():
        candidate = _LANDING / "index.html"
    return candidate.read_text(encoding="utf-8")


def _serve_landing(request: Optional[Request] = None) -> HTMLResponse:
    """The landing/settings SPA is public and never carries the control secret —
    EXCEPT for a signed-in operator, whose Settings pages (e.g. the live
    strategy switcher) drive the engine and need the same runtime config the
    dashboard gets. Anonymous visitors always receive the bare page."""
    path = request.url.path if request is not None else "/"
    html = _landing_document(path)
    # URL and anon key are deliberately public Supabase browser values. Runtime
    # injection avoids baking deployment-specific keys into a Docker image; the
    # service-role key is never present here.
    cfg = ('<script>window.__HUB_CONFIG__='
           + _json.dumps({"apiBase": "", "authMode": settings.auth_mode,
                          "supabaseUrl": supabase_auth.url or None,
                          "supabaseAnonKey": supabase_auth.anon_key or None,
                          "oauthProviders": [p for p in ("google", "apple")
                                            if _sec_os.environ.get(f"HUB_AUTH_{p.upper()}_ENABLED", "").lower() in ("1", "true", "yes")]})
           + '</script>')
    html = html.replace("<head>", "<head>" + cfg, 1)
    clean = path.rstrip("/") or "/"
    public_document = clean == "/" or clean in _LANDING_PAGE_PATHS
    headers = {
        "Cache-Control": (
            "public, max-age=300, stale-while-revalidate=86400"
            if public_document else "no-store"
        ),
    }
    if not public_document:
        headers["X-Robots-Tag"] = "noindex, nofollow"
    return HTMLResponse(html, headers=headers)


# Single-origin routing (only when the landing build is bundled): the public
# landing SPA owns "/", "/auth/*" and "/settings/*"; the dashboard lives at "/app"
# and stays session-gated. BrowserRouter paths need a real HTML response per route.
if _LANDING_READY:
    # Only the SPA shells that merely forward to the backend. The functional
    # flows — forgot/reset password, verify-email, two-factor — are served by
    # this app, because they need the users table and the session cookie. The
    # landing's versions of those pages were Supabase stubs against a second,
    # empty identity store; routing to them would authenticate nobody.
    _LANDING_AUTH = ("login", "register", "session-expired")

    def _landing_page(request: Request) -> HTMLResponse:
        return _serve_landing(request)

    def _landing_sub(request: Request, path: str = "") -> HTMLResponse:  # noqa: ARG001 — path is the SPA route
        return _serve_landing(request)

    for _p in _LANDING_AUTH:
        app.add_api_route(f"/auth/{_p}", _landing_page, response_class=HTMLResponse, methods=["GET"])
    app.add_api_route("/settings/{path:path}", _landing_sub, response_class=HTMLResponse, methods=["GET"])
    app.add_api_route("/admin", _landing_page, response_class=HTMLResponse, methods=["GET"])

    # Every public marketing page. Registered individually rather than behind a
    # catch-all: a catch-all would answer HTML to a mistyped API path, turning
    # a clear 404 into a parse error at the client, and would shadow nothing
    # today but everything added later.
    for _p in _LANDING_PAGES:
        app.add_api_route(f"/{_p}", _landing_page, response_class=HTMLResponse, methods=["GET"])

    @app.get("/app", response_class=HTMLResponse)
    @app.get("/app/{path:path}", response_class=HTMLResponse)
    def _dashboard_app(request: Request, path: str = ""):  # noqa: ARG001
        u = _require(request)
        if isinstance(u, RedirectResponse):
            return u
        p = _supabase_principal(request)
        if settings.auth_mode == "supabase" and p is not None and not p.is_admin:
            return RedirectResponse("/settings/account", status_code=303)
        return _serve_react() if _WEBUI_READY else HTMLResponse(render_overview(manager, user=u))



@app.on_event("startup")
def _start_auto_engine() -> None:
    """Start the autonomous strategy engine when the server boots (real signals
    -> paper execution -> ledger). Disabled under pytest and via HUB_AUTO_ENGINE=0."""
    import os
    # Status monitoring starts first and unconditionally: the early return
    # below (primary ledger unreachable) is exactly the case it must report.
    if "PYTEST_CURRENT_TEST" not in os.environ and webhook_api.status_monitor.start():
        print(f"[startup] status monitor started "
              f"(interval={webhook_api.status_monitor.interval_s:.0f}s)", flush=True)
    backend = type(webhook_api.ledger).__name__
    print(f"[startup] ledger backend = {backend} "
          f"(Supabase active: {backend == 'SupabaseLedger'})", flush=True)
    from data.ledger import SUPABASE_STATUS
    if SUPABASE_STATUS.get("configured") and not SUPABASE_STATUS.get("connected"):
        # Staying closed here is correct: a configured primary that cannot
        # answer must not be worked around. What was wrong is how quietly it
        # happened. No instance worker is restored and the autonomous engine
        # never starts, yet instances still read as desired-running, so the
        # whole platform presents as healthy and idle — the hardest state to
        # diagnose. Record it where an operator actually looks instead of
        # leaving one line in the container log.
        reason = str(SUPABASE_STATUS.get("error") or "primary ledger did not answer")
        detail = ("No trading instance worker was restored and the autonomous "
                  "engine was not started. Instances remain desired-running but "
                  f"nothing is executing. Cause: {reason}")
        print(f"[startup] all primary-ledger execution remains disabled: {reason}",
              flush=True)
        for record in (
            lambda: webhook_api.ledger.log(
                level="error", stage="startup",
                message=f"execution disabled: {detail}"),
            lambda: webhook_api.ledger.add_alert(
                severity="critical", category="system",
                title="Execution disabled — primary ledger unavailable",
                detail=detail),
        ):
            try:
                record()
            except Exception:  # noqa: BLE001 — reporting must not break boot
                pass
        return
    # Instance workers own their own strategy state, execution ledger scope and
    # desired lifecycle. Restore them first; when at least one is intentionally
    # active, do not also start the legacy multi-pair worker (which would create
    # an un-attributed, mixed stream alongside the new instance platform).
    restored_instances = []
    if "PYTEST_CURRENT_TEST" not in os.environ and webhook_api.instance_manager.store.available:
        try:
            restored_instances = webhook_api.instance_manager.restore_desired_instances()
        except Exception as exc:  # noqa: BLE001
            # Boot-time restoration is best effort. A provider or database blip
            # during this exact second must not be the reason nothing is
            # supervised for the rest of the process lifetime -- that is the
            # shape of the outage this whole component exists to end.
            print(f"[startup] instance restoration failed, supervisor will retry: "
                  f"{type(exc).__name__}: {exc}", flush=True)
        # Restoration is one shot; supervision is continuous. Start it even
        # when nothing was restored -- an instance whose feed was unreachable
        # at this exact moment is still desired, and the supervisor is what
        # retries it without anyone opening the dashboard.
        if webhook_api.instance_supervisor.start():
            print("[startup] instance supervisor started "
                  f"(interval={webhook_api.instance_supervisor.interval_s}s)", flush=True)
    # The Adaptive MTF lab's private bot: restore its saved mode, and on its
    # very first start arm the XRPUSDT bot so the lab has something to show.
    # HUB_ADAPTIVE_LAB_AUTOSTART=0 leaves it idle until a mode is chosen.
    if ("PYTEST_CURRENT_TEST" not in os.environ
            and os.environ.get("HUB_ADAPTIVE_LAB_AUTOSTART", "1").strip().lower()
            not in ("0", "false", "no", "off")):
        try:
            webhook_api.adaptive_lab.restore()
            webhook_api.adaptive_lab.ensure_started()
        except Exception as exc:  # noqa: BLE001 — the supervisor retries it
            print(f"[startup] adaptive lab restoration failed, supervisor will retry: "
                  f"{type(exc).__name__}: {exc}", flush=True)
        if webhook_api.adaptive_lab_supervisor.start():
            print("[startup] adaptive lab supervisor started", flush=True)
    if restored_instances:
        print(f"[startup] restored {len(restored_instances)} trading instance worker(s); "
              "legacy autonomous engine remains stopped", flush=True)
    elif (settings.auto_engine and webhook_api.engine.autostart_enabled
            and "PYTEST_CURRENT_TEST" not in os.environ):
        webhook_api.engine.start()
        print(f"[startup] autonomous engine started — symbols={list(settings.auto_symbols)} "
              f"timeframe={settings.auto_timeframe} interval={settings.auto_interval}s", flush=True)
    else:
        print("[startup] autonomous engine NOT started "
              f"(auto_engine={settings.auto_engine}, desired_running="
              f"{webhook_api.engine.autostart_enabled})", flush=True)


@app.on_event("shutdown")
def _shutdown_all_runtimes() -> None:
    """One process-wide shutdown boundary for every execution authority."""
    errors = []

    def run(name, operation):
        try:
            result = operation()
            if isinstance(result, dict) and result.get("errors"):
                errors.extend(f"{name}: {item}" for item in result["errors"])
        except Exception as exc:  # shutdown continues so no sibling is leaked
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    # Close all entry gates before waiting on any worker. Protective closes are
    # still classified as exposure-reducing and bypass these controls.
    webhook_api.controls.stop_all()
    # Stop supervising before quiescing workers, or the supervisor would treat
    # an intentionally stopping worker as a fault and start a replacement.
    run("instance_supervisor", webhook_api.instance_supervisor.stop)
    run("status_monitor", webhook_api.status_monitor.stop)
    run("adaptive_lab_supervisor", webhook_api.adaptive_lab_supervisor.stop)
    run("trading_instances", webhook_api.instance_manager.shutdown)
    run("adaptive_lab", webhook_api.adaptive_lab.shutdown)
    run("autonomous_engine_checkpoint", webhook_api.engine.flush_runtime_state)
    run("autonomous_engine", lambda: webhook_api.engine.stop("Process shutdown"))
    run("legacy_bot_runners", manager.emergency_stop_all)
    run("research_observer", webhook_api.research_observer.stop)
    run("price_action_lab", webhook_api.price_action_runtime.stop)
    run("smc_lab", webhook_api.smc_runtime.stop)
    if errors:
        print("[shutdown] completed with degraded acknowledgements: " + " | ".join(errors),
              flush=True)

# Phase 8: process-wide event hub for the live (SSE) dashboard.
from dashboard.stream import HubEventHub, sse_format  # noqa: E402
hub_events = HubEventHub()

# token -> username (legacy in-memory sessions; kept for test fixtures)
_sessions: dict[str, str] = {}
COOKIE = "hub_session"
SUPABASE_COOKIE = "hub_supabase_access"
#: The refresh half of the Supabase session. The access cookie holds a
#: short-lived JWT; without somewhere to keep its refresh token the server had
#: no way to renew it, so the 30-day access cookie simply carried a dead
#: credential and every request after expiry read as signed out.
SUPABASE_REFRESH_COOKIE = "hub_supabase_refresh"
SESSION_DAYS = 7


def _cookie_kwargs() -> dict:
    """Session-cookie attributes. Default SameSite=Lax (same-origin dashboard).
    Set HUB_COOKIE_SAMESITE=none to allow the dashboard to be embedded in an
    iframe on another site (e.g. the Tradexa app) — SameSite=None requires
    Secure, so it only works over HTTPS (which Render provides)."""
    import os
    samesite = os.environ.get("HUB_COOKIE_SAMESITE", "lax").lower()
    if samesite not in ("lax", "none", "strict"):
        samesite = "lax"
    kw = {"httponly": True, "samesite": samesite,
          "max_age": SESSION_DAYS * 86400}
    # HTTPS is mandatory in production. Cookies may be non-secure only for a
    # local HTTP development server; never allow that production downgrade.
    if samesite == "none" or _ON_CLOUD:
        kw["secure"] = True
    return kw


# --------------------------------------------------------------- auth helpers
def _sign_session(username: str) -> str:
    """Stateless signed session token (survives server restarts — important on
    hosts that spin down). The HMAC itself lives in services.session_auth so the
    API routers verify the same token with the same code, not a copy of it."""
    from services.session_auth import sign
    return sign(username, settings.secret_key, ttl_days=SESSION_DAYS)


def _verify_session(token: str):
    """Authentic + unexpired + still a real account. session_auth answers the
    first two; the user store answers the third."""
    from services.session_auth import verify
    username = verify(token, settings.secret_key)
    return username if username and store.get_user(username) else None


# --------------------------------------------------------------- JWT (Sprint 1)
# A stateless JWT access token issued ALONGSIDE the signed session cookie. Same
# server-only secret (HUB_SECRET), same 7-day life for parity with the cookie
# (short-lived access + refresh rotation arrives with the sessions table). The
# cookie path is unchanged; a request may present either credential.
def issue_access(username: str) -> str:
    from services import jwt_tokens
    return jwt_tokens.encode({"sub": username, "typ": "access"},
                             settings.secret_key, ttl_seconds=SESSION_DAYS * 86400)


def verify_access(token: str):
    from services import jwt_tokens
    body = jwt_tokens.decode(token, settings.secret_key)
    if not body or body.get("typ") != "access":
        return None
    username = body.get("sub")
    return username if username and store.get_user(username) else None


def _legacy_bearer(request: Request):
    """Username from an `Authorization: Bearer <jwt>` header, if present + valid."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return verify_access(auth[7:].strip())
    return None


def _user(request: Request):
    if settings.auth_mode == "supabase":
        p = _supabase_principal(request)
        return p.id if p else None
    token = request.cookies.get(COOKIE, "")
    # cookie first (the browser dashboard), then a Bearer JWT (API clients),
    # then the legacy in-memory map kept only for test fixtures.
    return _verify_session(token) or _legacy_bearer(request) or _sessions.get(token)


def _supabase_token(request: Request) -> str:
    """Read an HttpOnly browser bridge cookie or an API Bearer token."""
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return request.cookies.get(SUPABASE_COOKIE, "")


def _supabase_principal(request: Request) -> Principal | None:
    """Verify the token at Supabase and cache it only for this request."""
    if settings.auth_mode != "supabase":
        return None
    cached = getattr(request.state, "supabase_principal", None)
    if cached is not None:
        return cached
    token = _supabase_token(request)
    if not token:
        return None
    try:
        principal = supabase_auth.principal(token)
    except SupabaseAuthError:
        return None
    request.state.supabase_principal = principal
    return principal


def _require(request: Request):
    """Return username or a RedirectResponse to /login."""
    u = _user(request)
    return u if u else RedirectResponse("/login", status_code=303)


def _tenant(request: Request) -> str:
    """The data-tenant for this request (Phase C seam). Single-owner: always the
    owner. Multi-user (HUB_MULTI_USER): the signed-in user. Stores default to the
    owner, so this only diverges once multi-user is switched on."""
    if settings.auth_mode == "supabase":
        # Supabase Auth UUID is the immutable ownership key.
        return _user(request) or ""
    from services.tenancy import resolve_tenant
    return resolve_tenant(_user(request))


def _request_role(request: Request):
    """The RBAC role for this request. A signed-in user's stored role; or
    'owner' when the caller presents the admin/control secret (automation acts
    with full authority); else None (anonymous)."""
    if settings.auth_mode == "supabase":
        p = _supabase_principal(request)
        return "admin" if p and p.is_admin else ("user" if p else None)
    u = _user(request)
    if u:
        rec = store.get_user(u)
        if rec is not None:
            return rec.role
    # A caller presenting the dedicated control key acts with full authority.
    # Webhook and exchange keys are never accepted here.
    hdr = request.headers.get("x-webhook-secret")
    if hdr and hdr == settings.admin_key:
        return "owner"
    return None


def _has_role(request: Request, minimum: str) -> bool:
    """True if this request's role is at least ``minimum`` (rbac hierarchy)."""
    from services import rbac
    return rbac.role_at_least(_request_role(request), minimum)


def _require_role(request: Request, minimum: str):
    """Raise 403 unless the request's role is at least ``minimum``. For JSON
    endpoints; HTML pages branch on ``_has_role`` instead."""
    if not _has_role(request, minimum):
        raise HTTPException(status_code=403, detail="Insufficient role")


def _signup_open() -> bool:
    """Signup creates the single OWNER account. It stays open only while the
    seeded default admin is the sole user — after that this hub has an owner."""
    return settings.auth_mode == "legacy" and all(u.username == settings.username for u in store.list_users())


# ----------------------------------------------------------------------- login
# TradeLogX Nexus brand mark — gold intelligence core, blue data links (matches
# the dashboard Logo component so both apps carry ONE brand).
_BRAND_MARK = ('<svg width="32" height="32" viewBox="0 0 96 96" fill="none" '
               'xmlns="http://www.w3.org/2000/svg" aria-label="TradeLogX Nexus">'
               '<defs>'
               '<linearGradient id="nxsA" x1="24" y1="0" x2="72" y2="0" gradientUnits="userSpaceOnUse">'
               '<stop offset="0" stop-color="#E9EEF3"/><stop offset=".46" stop-color="#AEB7C2"/>'
               '<stop offset=".54" stop-color="#E7C766"/><stop offset="1" stop-color="#C6961F"/></linearGradient>'
               '<linearGradient id="nxrA" x1="14" y1="14" x2="82" y2="82" gradientUnits="userSpaceOnUse">'
               '<stop stop-color="#E7C766"/><stop offset=".5" stop-color="#8A929C"/><stop offset="1" stop-color="#C6961F"/></linearGradient>'
               '</defs>'
               '<circle cx="48" cy="48" r="41" stroke="url(#nxrA)" stroke-width="2.6" opacity="0.85"/>'
               '<path d="M31 70 V32 M31 32 L65 70 M65 70 V26" stroke="url(#nxsA)" stroke-width="11" stroke-linecap="butt" stroke-linejoin="miter"/>'
               '<path d="M31 14 L40 30 H22 Z" fill="#E9EEF3"/>'
               '<path d="M65 82 L56 66 H74 Z" fill="#C6961F"/></svg>')
_BRAND_HEAD = (f'<div class="brand">{_BRAND_MARK}'
               '<span class="wordmark">TradeLogX <b>Nexus</b></span></div>')

# ---- inline icons (match the landing's lucide set) -------------------------
_IC_USER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>'
_IC_LOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>'
_IC_EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>'
_IC_TREND = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 7 13.5 15.5 8.5 10.5 2 17"/><path d="M16 7h6v6"/></svg>'
_IC_SHIELD = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/></svg>'
_IC_ZAP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z"/></svg>'

# demo equity line for the showcase (hand-drawn shape, clearly labelled demo)
_SC_CURVE = [10, 26, 20, 38, 33, 52, 48, 66, 62, 80]
_SC_PATH = " ".join(
    f'{"M" if i == 0 else "L"}{(i/(len(_SC_CURVE)-1))*260:.1f},{80-(v/100)*80:.1f}'
    for i, v in enumerate(_SC_CURVE))
_SHOWCASE = f'''<aside class="showcase">
  <div class="sc-top">
    <div class="eyebrow">TradeLogX Nexus</div>
    <h2 class="sc-title">Automated Trading.<br><span class="grad">Human Intelligence.</span></h2>
    <p class="sc-sub">Analyze markets, execute strategies, and manage risk — with complete transparency over every decision Nexus makes.</p>
  </div>
  <div class="sc-card">
    <div class="sc-card-h"><span>EQUITY · DEMO</span><span class="emer">+18.4%</span></div>
    <svg viewBox="0 0 260 80" preserveAspectRatio="none" class="sc-svg"><path d="{_SC_PATH}" pathLength="1" fill="none" stroke="#4FD98E" stroke-width="2" stroke-linecap="round" class="sc-line"/></svg>
  </div>
  <div class="sc-stats">
    <div class="sc-chip"><span class="sc-ic">{_IC_TREND}</span><div><b>Fully automated</b><span>Strategies executed</span></div></div>
    <div class="sc-chip"><span class="sc-ic">{_IC_SHIELD}</span><div><b>Encrypted · No withdrawals</b><span>Keys</span></div></div>
    <div class="sc-chip"><span class="sc-ic">{_IC_ZAP}</span><div><b>Sub-100ms routing</b><span>Execution</span></div></div>
  </div>
</aside>'''

# Full premium auth stylesheet — mirrors the landing's design tokens exactly
# (page-depth radial, warm grid, gold/emerald blooms, glass card, gold-sheen
# button, icon inputs). Self-contained; the legacy dashboard CSS is NOT loaded.
_AUTH_CSS = '''*{box-sizing:border-box}
:root{--gold:#C9A24B;--gold-soft:#E7CE86;--ink:#08080A;--line:rgba(255,255,255,.08)}
html,body{margin:0;min-height:100%}
body{background:#08080A;color:#fff;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
 -webkit-font-smoothing:antialiased;position:relative;overflow-x:hidden}
/* background layers (fixed) */
body::before,body::after{content:"";position:fixed;inset:0;z-index:-2;pointer-events:none}
body::before{background:radial-gradient(120% 85% at 50% 0%,#0D0C0A 0%,#08080A 48%,#050506 100%)}
.bg{position:fixed;inset:0;z-index:-1;pointer-events:none;overflow:hidden}
.bg .grid{position:absolute;inset:0;
 background-image:linear-gradient(to right,rgba(226,214,182,.045) 1px,transparent 1px),linear-gradient(to bottom,rgba(226,214,182,.045) 1px,transparent 1px);
 background-size:28px 28px;-webkit-mask-image:linear-gradient(to bottom,#000 0%,#000 55%,transparent 100%);
 mask-image:linear-gradient(to bottom,#000 0%,#000 55%,transparent 100%);opacity:.6;animation:gridpan 8s linear infinite}
.bg .grid2{position:absolute;inset:0;background-image:linear-gradient(to right,rgba(226,214,182,.045) 1px,transparent 1px),linear-gradient(to bottom,rgba(226,214,182,.045) 1px,transparent 1px);background-size:140px 140px;opacity:.35;-webkit-mask-image:linear-gradient(to bottom,#000 0%,#000 55%,transparent 100%);mask-image:linear-gradient(to bottom,#000 0%,#000 55%,transparent 100%)}
.bg .bloom{position:absolute;border-radius:50%;filter:blur(130px)}
.bg .bloom-gold{top:-12rem;left:50%;width:46rem;height:34rem;transform:translateX(-50%);background:rgba(201,162,75,.05);animation:bloom 18s ease-in-out infinite}
.bg .bloom-emer{bottom:-14rem;right:-12rem;width:40rem;height:30rem;background:rgba(47,191,113,.05);filter:blur(150px)}
.bg .vig{position:absolute;inset:0;background:radial-gradient(ellipse at center,transparent 58%,rgba(0,0,0,.6))}
@keyframes gridpan{from{background-position:0 0}to{background-position:28px 28px}}
@keyframes bloom{0%,100%{opacity:.75;transform:translateX(-50%) scale(1)}50%{opacity:1;transform:translateX(-50%) scale(1.06)}}
/* layout */
.auth{display:grid;grid-template-columns:1.05fr .95fr;min-height:100vh}
.topbar{position:absolute;top:0;right:0;padding:22px 26px;z-index:5}
.topbar a{color:rgba(255,255,255,.5);text-decoration:none;font-size:13.5px;display:inline-flex;align-items:center;gap:6px;transition:color .2s}
.topbar a:hover{color:#fff}
/* showcase (left) */
.showcase{position:relative;display:flex;flex-direction:column;justify-content:space-between;padding:56px;overflow:hidden;background:#0C0C0F;border-right:1px solid var(--line)}
.eyebrow{font-size:11px;font-weight:600;letter-spacing:.24em;text-transform:uppercase;color:var(--gold)}
.sc-title{font-size:40px;line-height:1.05;font-weight:800;letter-spacing:-.02em;margin:18px 0 0;max-width:11ch}
.sc-title .grad{background:linear-gradient(120deg,#E7D89A,#C8A94B 55%,#A98E3A);-webkit-background-clip:text;background-clip:text;color:transparent}
.sc-sub{margin:16px 0 0;max-width:34ch;font-size:14px;line-height:1.6;color:rgba(255,255,255,.55)}
.sc-card{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:16px;padding:18px;backdrop-filter:blur(8px);box-shadow:0 20px 50px -20px rgba(0,0,0,.6)}
.sc-card-h{display:flex;justify-content:space-between;font-size:11px;letter-spacing:.08em;color:rgba(255,255,255,.4);margin-bottom:10px}
.sc-card-h .emer{color:#4FD98E;letter-spacing:0}
.sc-svg{width:100%;height:74px;display:block}
.sc-line{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.6s ease-in-out .4s forwards}
@keyframes draw{to{stroke-dashoffset:0}}
.sc-stats{display:flex;flex-direction:column;gap:12px}
.sc-chip{display:flex;align-items:center;gap:12px;background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:12px;padding:12px 14px;opacity:0;transform:translateX(-16px);animation:slidein .5s ease forwards}
.sc-chip:nth-child(1){animation-delay:.35s}.sc-chip:nth-child(2){animation-delay:.47s}.sc-chip:nth-child(3){animation-delay:.59s}
.sc-ic{display:flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:9px;background:rgba(201,162,75,.1);color:var(--gold);flex:none}
.sc-ic svg{width:16px;height:16px}
.sc-chip b{display:block;font-size:13.5px;font-weight:600}
.sc-chip span{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:rgba(255,255,255,.4)}
@keyframes slidein{to{opacity:1;transform:translateX(0)}}
/* form (right) */
.formcol{display:flex;align-items:center;justify-content:center;padding:40px 24px}
.card{width:100%;max-width:400px;opacity:0;transform:translateY(20px) scale(.98);animation:rise .55s cubic-bezier(.22,1,.36,1) forwards}
@keyframes rise{to{opacity:1;transform:none}}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:26px}
.brand svg{width:34px;height:34px}
.wordmark{font-size:19px;font-weight:700;letter-spacing:-.01em;color:#eef1f5}
.wordmark b{color:var(--gold)}
.card h1{font-size:28px;font-weight:800;letter-spacing:-.02em;margin:0}
.card .sub{margin:8px 0 26px;font-size:14px;color:rgba(255,255,255,.5)}
.fld{display:block;margin-bottom:16px}
.fld .lbl{display:flex;justify-content:space-between;align-items:center;font-size:12.5px;font-weight:500;color:rgba(255,255,255,.75);margin-bottom:7px}
.fld .lbl a{color:var(--gold);opacity:.85;text-decoration:none;font-size:12px}
.fld .lbl a:hover{opacity:1}
.inp{display:flex;align-items:center;gap:10px;background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:12px;padding:0 12px;height:48px;transition:border-color .2s,box-shadow .2s,background .2s}
.inp:hover{background:rgba(255,255,255,.05)}
.inp:focus-within{border-color:var(--gold);background:rgba(201,162,75,.05);box-shadow:0 0 0 3px rgba(201,162,75,.18)}
.inp .ico{display:flex;color:rgba(255,255,255,.4);flex:none}
.inp .ico svg{width:17px;height:17px}
.inp input{flex:1;min-width:0;background:none;border:0;outline:0;color:#fff;font-size:14.5px;font-family:inherit;height:100%}
.inp input::placeholder{color:rgba(255,255,255,.3)}
.inp .eye{background:none;border:0;cursor:pointer;color:rgba(255,255,255,.4);display:flex;padding:4px;border-radius:6px}
.inp .eye:hover{color:#fff}
.inp .eye svg{width:17px;height:17px}
.btn-gold{position:relative;overflow:hidden;width:100%;height:50px;margin-top:6px;border:0;border-radius:12px;cursor:pointer;
 background:linear-gradient(135deg,#E7D89A 0%,#C8A94B 45%,#A98E3A 100%);color:#08080A;font-family:inherit;font-size:15px;font-weight:700;letter-spacing:.01em;
 box-shadow:0 10px 40px -12px rgba(200,169,75,.45);transition:filter .2s,transform .12s,box-shadow .2s;display:flex;align-items:center;justify-content:center;gap:8px}
.btn-gold:hover{filter:brightness(1.06);box-shadow:0 14px 46px -12px rgba(200,169,75,.6)}
.btn-gold:active{transform:translateY(1px) scale(.995);filter:brightness(.96)}
.btn-gold .sheen{position:absolute;inset:0;transform:translateX(-100%);background:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent);transition:transform .7s}
.btn-gold:hover .sheen{transform:translateX(100%)}
.btn-gold.loading{pointer-events:none;opacity:.85}
.btn-gold .spin{width:17px;height:17px;border:2px solid rgba(8,8,10,.35);border-top-color:#08080A;border-radius:50%;animation:spin .7s linear infinite;display:none}
.btn-gold.loading .spin{display:block}.btn-gold.loading .txt{opacity:.85}
@keyframes spin{to{transform:rotate(360deg)}}
.divider{display:flex;align-items:center;gap:12px;margin:22px 0;color:rgba(255,255,255,.35);font-size:12px}
.divider::before,.divider::after{content:"";height:1px;flex:1;background:var(--line)}
.foot{margin-top:26px;text-align:center;font-size:13.5px;color:rgba(255,255,255,.5)}
.foot a{color:var(--gold);font-weight:600;text-decoration:none}.foot a:hover{color:var(--gold-soft)}
.err{margin-top:14px;padding:11px 13px;border-radius:11px;font-size:13px;color:#F07E7A;background:rgba(229,96,91,.1);border:1px solid rgba(229,96,91,.3);animation:shake .4s}
@keyframes shake{0%,100%{transform:translateX(0)}20%,60%{transform:translateX(-6px)}40%,80%{transform:translateX(6px)}}
.note{margin-top:18px;padding:11px 13px;border-radius:11px;font-size:12px;color:rgba(255,255,255,.6);background:rgba(201,162,75,.06);border:1px solid rgba(201,162,75,.2)}
@media (max-width:900px){.auth{grid-template-columns:1fr}.showcase{display:none}.formcol{padding:64px 20px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}'''

_AUTH_HEAD_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">'
    '<link rel="icon" type="image/svg+xml" href="/nexus-mark-small.svg">'
    '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">'
    '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">')

_AUTH_JS = ('<script>function tpw(b){var i=document.getElementById("pw");if(i){i.type=i.type==="password"?"text":"password";}}'
            'function tpw2(b){var i=document.getElementById("pw2");if(i){i.type=i.type==="password"?"text":"password";}}'
            'function subm(f){var b=f.querySelector(".btn-gold");if(b)b.classList.add("loading");return true;}</script>')


def _auth_page(title: str, body: str) -> str:
    """Premium auth shell — the landing's background + a glass card, self-contained."""
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#08080A">
<title>{w.esc(title)} · TradeLogX Nexus</title>{_AUTH_HEAD_LINKS}
<style>{_AUTH_CSS}</style></head>
<body>
<div class="bg"><div class="grid"></div><div class="grid2"></div>
<div class="bloom bloom-gold"></div><div class="bloom bloom-emer"></div><div class="vig"></div></div>
<div class="topbar"><a href="/">← Back to site</a></div>
<div class="auth">{_SHOWCASE}<div class="formcol"><div class="card">{body}</div></div></div>
{_AUTH_JS}
</body></html>'''


def _pw_field(name: str, label: str, fid: str, toggle: str, autocomplete: str, hint: str = "") -> str:
    return (f'<label class="fld"><span class="lbl">{label}{hint}</span>'
            f'<div class="inp"><span class="ico">{_IC_LOCK}</span>'
            f'<input id="{fid}" name="{name}" type="password" autocomplete="{autocomplete}" placeholder="••••••••">'
            f'<button type="button" class="eye" onclick="{toggle}(this)" aria-label="Show password">{_IC_EYE}</button></div></label>')


from services import auth_flows as _af  # noqa: E402
from services import mailer as _mailer  # noqa: E402
from services import oauth as _oauth  # noqa: E402


def _q(text: str) -> str:
    """Percent-encode a message for a redirect query string. '+' for spaces is
    not enough — an apostrophe or an '&' in an error message would otherwise
    truncate it or split it into a second parameter."""
    from urllib.parse import quote_plus
    return quote_plus(text or "")


def _storage_notice() -> str:
    """Warn on the sign-in page itself when accounts do not survive a redeploy.

    This is where someone whose account was erased actually lands, and without
    it the only evidence is a boot log they cannot see. Silence here reads as
    "you typed it wrong".
    """
    try:
        if webhook_api.storage_assessment()["local_durable"]:
            return ""
    except Exception:  # noqa: BLE001 — never break the login page over a notice
        return ""
    return ('<div class="err">Accounts are stored on ephemeral disk and are '
            'erased on every redeploy. Set HUB_DATA_DIR to a mounted persistent '
            'disk to keep them, or sign in with HUB_USERNAME / HUB_PASSWORD, '
            'which are re-seeded from the environment on each boot.</div>')


@app.get("/login", response_class=HTMLResponse)
def login_form(error: str = "") -> str:
    if settings.auth_mode == "supabase":
        # The landing SPA owns every customer-facing auth flow. Keeping this
        # route only as a compatibility redirect prevents env credentials from
        # becoming a second customer login path.
        return RedirectResponse("/auth/login", status_code=303)
    err = f'<div class="err">{w.esc(error)}</div>' if error else ""
    err += _storage_notice()
    signup = ('<p class="foot">New here? <a href="/signup">Create your account</a></p>'
              if _signup_open() else "")
    return _auth_page("Sign in", f'''{_BRAND_HEAD}
<h1>Welcome back</h1>
<p class="sub">Sign in to your TradeLogX Nexus workspace.</p>
<form method="post" action="/login" onsubmit="return subm(this)" novalidate>
<label class="fld"><span class="lbl">Username or email</span>
<div class="inp"><span class="ico">{_IC_USER}</span><input name="username" autocomplete="username" placeholder="you@email.com or a username" autofocus></div></label>
{_pw_field("password", "Password", "pw", "tpw", "current-password")}
<button class="btn-gold" type="submit"><span class="sheen"></span><span class="spin"></span><span class="txt">Sign in</span></button>
</form>
{err}{signup}''')


def _login_failure_message(username: str) -> str:
    """Why the sign-in failed, said in a way the person can act on.

    "Invalid credentials" is the right answer for a wrong password and the
    wrong answer for a missing account: it sends someone to re-check a password
    that was never the problem. This hub already discloses whether an owner
    exists (the signup link appears exactly when one does not), so naming a
    missing account leaks nothing new — and on ephemeral storage it is the
    difference between "retype it" and "your account was wiped by a redeploy".
    """
    if store.auth_failure_reason(username) == "bad-password":
        return "Invalid credentials"
    msg = "No account with that username or email exists on this hub"
    try:
        # webhook_api owns the one storage assessment (it knows the real
        # Supabase connection state); re-deriving it here would drift.
        if not webhook_api.storage_assessment()["local_durable"]:
            msg += (". Storage is ephemeral, so accounts are erased on every "
                    "redeploy — set HUB_DATA_DIR to a persistent disk to keep them")
    except Exception:  # noqa: BLE001 — a diagnosis must never break sign-in
        pass
    return msg


PENDING_2FA_COOKIE = "hub_2fa"
PENDING_2FA_TTL_S = 300          # 5 minutes to fetch a code off a phone


def _pending_2fa_token(username: str) -> str:
    """A ticket saying "this password was correct, the second factor is not
    done yet". Signed under a purpose-scoped key so it can NEVER be presented
    as a session cookie — otherwise the second factor would be skippable."""
    from services.session_auth import sign_scoped
    return sign_scoped(username, settings.secret_key, purpose="2fa-pending",
                       ttl_s=PENDING_2FA_TTL_S)


def _pending_2fa_user(request: Request) -> Optional[str]:
    from services.session_auth import verify_scoped
    name = verify_scoped(request.cookies.get(PENDING_2FA_COOKIE, ""),
                         settings.secret_key, purpose="2fa-pending")
    return name if name and store.get_user(name) else None


def _grant_session(user, destination: str = "/"):
    resp = RedirectResponse(destination, status_code=303)
    # Sign the STORED username, not the typed one — otherwise a case-variant
    # sign-in mints a session under a name whose per-user settings namespace
    # is empty.
    resp.set_cookie(COOKIE, _sign_session(user.username), **_cookie_kwargs())
    resp.delete_cookie(PENDING_2FA_COOKIE)
    return resp


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    if settings.auth_mode == "supabase":
        raise HTTPException(status_code=410, detail="Use the Supabase sign-in page at /auth/login.")
    # verify against hashed credentials; signed cookie survives restarts
    user = store.authenticate(username, password)
    if user is not None:
        if user.totp_enabled:
            # A correct password is now only half the sign-in. Hand out the
            # pending ticket, not a session.
            resp = RedirectResponse("/auth/two-factor", status_code=303)
            kw = dict(_cookie_kwargs())
            kw["max_age"] = PENDING_2FA_TTL_S
            resp.set_cookie(PENDING_2FA_COOKIE, _pending_2fa_token(user.username), **kw)
            return resp
        return _grant_session(user)
    err = _login_failure_message(username).replace(" ", "+")
    return RedirectResponse(f"/login?error={err}", status_code=303)


@app.get("/signup", response_class=HTMLResponse)
def signup_form(error: str = "") -> str:
    if settings.auth_mode == "supabase":
        return RedirectResponse("/auth/register", status_code=303)
    if not _signup_open():
        return _auth_page("Sign up", f'''{_BRAND_HEAD}
<h1>Already set up</h1>
<p class="sub">This hub already has an owner account.</p>
<p class="foot"><a href="/login">Sign in instead →</a></p>''')
    err = f'<div class="err">{w.esc(error)}</div>' if error else ""
    return _auth_page("Create account", f'''{_BRAND_HEAD}
<h1>Create your account</h1>
<p class="sub">Set up the owner account for your TradeLogX Nexus workspace.</p>
<form method="post" action="/signup" onsubmit="return subm(this)" novalidate>
<label class="fld"><span class="lbl">Username or email</span>
<div class="inp"><span class="ico">{_IC_USER}</span><input name="username" autocomplete="username" placeholder="you@email.com or a username" autofocus></div></label>
{_pw_field("password", "Password", "pw", "tpw", "new-password", hint="<span style='font-weight:400;color:rgba(255,255,255,.4)'>8+ characters</span>")}
{_pw_field("confirm", "Confirm password", "pw2", "tpw2", "new-password")}
<button class="btn-gold" type="submit"><span class="sheen"></span><span class="spin"></span><span class="txt">Create account</span></button>
</form>
{err}<p class="foot">Already have an account? <a href="/login">Sign in</a></p>''')


def _create_owner(username: str, password: str, confirm: str):
    """Shared signup rules. Returns (username, None) or (None, error)."""
    import re as _re
    if not _signup_open():
        return None, "This hub already has an owner account"
    username = (username or "").strip()
    # accept a plain username OR an email address (people naturally type their
    # email here — the field label offers both), letters/digits and . _ - + @
    if not (3 <= len(username) <= 64) or not _re.fullmatch(r"[A-Za-z0-9._+@-]+", username):
        return None, "Enter a username or email (3–64 chars: letters, digits, . _ - + @)"
    if len(password or "") < 8:
        return None, "Password must be at least 8 characters"
    if password != confirm:
        return None, "Passwords do not match"
    if store.get_user(username):
        return None, "That username is taken"
    store.create_user(username, password, role="owner")
    # lock the seeded default admin if it still carries the default password —
    # the owner account is now the only way in
    if username != settings.username and store.authenticate(settings.username, settings.password):
        store.set_password(settings.username, secrets.token_urlsafe(24))
    return username, None


@app.post("/signup")
def signup(username: str = Form(...), password: str = Form(...), confirm: str = Form(...)):
    if settings.auth_mode == "supabase":
        raise HTTPException(status_code=410, detail="Use the Supabase registration page at /auth/register.")
    user, err = _create_owner(username, password, confirm)
    if err:
        return RedirectResponse(f"/signup?error={err.replace(' ', '+')}", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, _sign_session(user), **_cookie_kwargs())
    return resp


@app.post("/logout")
def logout(request: Request):
    _sessions.pop(request.cookies.get(COOKIE, ""), None)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ------------------------------------------------------------- auth JSON API
@app.post("/auth/login")
def auth_login(username: str = Form(...), password: str = Form(...)):
    """JSON login for API clients: returns a JWT access token AND sets the
    session cookie, so callers can use `Authorization: Bearer` while browsers
    keep the cookie. Same credential check as the form /login."""
    if settings.auth_mode == "supabase":
        raise HTTPException(status_code=410, detail="Use Supabase Auth; this endpoint accepts no customer passwords.")
    from fastapi.responses import JSONResponse
    user = store.authenticate(username, password)
    if user is None:
        raise HTTPException(status_code=401, detail=_login_failure_message(username))
    if user.totp_enabled:
        # No token yet — the caller must post the second factor to
        # /auth/two-factor with this ticket. Issuing the JWT here would make
        # 2FA cosmetic for every API client.
        resp = JSONResponse({"ok": False, "mfa_required": True,
                             "user": user.username,
                             "message": "Enter the code from your authenticator app."},
                            status_code=401)
        kw = dict(_cookie_kwargs())
        kw["max_age"] = PENDING_2FA_TTL_S
        resp.set_cookie(PENDING_2FA_COOKIE, _pending_2fa_token(user.username), **kw)
        return resp
    token = issue_access(user.username)
    resp = JSONResponse({"ok": True, "user": user.username, "token": token,
                         "token_type": "bearer", "expires_in": SESSION_DAYS * 86400})
    resp.set_cookie(COOKIE, _sign_session(user.username), **_cookie_kwargs())
    return resp


@app.get("/auth/status")
def auth_status(request: Request):
    """For the React app: who am I, and is first-time signup still open?"""
    if settings.auth_mode == "supabase":
        p = _supabase_principal(request)
        return {"authenticated": bool(p), "user": p.id if p else None,
                "email": p.email if p else None, "role": p.role if p else None,
                "email_confirmed": bool(p and p.email_confirmed), "signup_open": True,
                "provider": "supabase"}
    u = _user(request)
    return {"authenticated": bool(u), "user": u, "signup_open": _signup_open()}


@app.post("/auth/logout")
def auth_logout(request: Request):
    _sessions.pop(request.cookies.get(COOKIE, ""), None)
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(SUPABASE_COOKIE)
    resp.delete_cookie(SUPABASE_REFRESH_COOKIE)
    resp.delete_cookie(PENDING_2FA_COOKIE)
    return resp


# ---------------------------------------------------------- Supabase bridge
# Supabase owns credentials, verification, OAuth, reset links and refresh
# tokens.  The SPA exchanges only its short-lived access token for this
# HttpOnly same-origin cookie so the independently-built dashboard can use the
# verified backend session without ever seeing a server credential.
@app.post("/auth/supabase/session")
async def supabase_session(request: Request):
    if settings.auth_mode != "supabase":
        raise HTTPException(status_code=404, detail="Supabase Auth is not enabled.")
    token = _supabase_token(request)
    try:
        principal = supabase_auth.principal(token)
    except SupabaseAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not principal.email_confirmed:
        raise HTTPException(status_code=403, detail="Verify your email before opening the dashboard.")
    try:
        body = await request.json()
    except ValueError:
        body = {}
    remember = bool(body.get("remember", True)) if isinstance(body, dict) else True
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True, "user": principal.id, "role": principal.role})
    cookie = dict(_cookie_kwargs())
    if remember:
        cookie["max_age"] = 30 * 86400
    else:
        cookie.pop("max_age", None)  # browser-session cookie
    resp.set_cookie(SUPABASE_COOKIE, token, **cookie)
    # Keep the refresh half when the SPA sends it, so an expired access token
    # can be renewed server-side instead of bouncing the user to sign-in.
    refresh_token = body.get("refresh_token") if isinstance(body, dict) else None
    if refresh_token:
        resp.set_cookie(SUPABASE_REFRESH_COOKIE, str(refresh_token), **cookie)
    try:
        supabase_auth.touch_last_login(token, principal.id)
    except SupabaseAuthError:
        # A successful authentication must not be rejected because optional
        # telemetry is temporarily unavailable.
        pass
    supabase_auth.audit(actor_id=principal.id, event="session.created",
                        metadata={"remember": remember})
    return resp


@app.post("/auth/supabase/refresh")
def supabase_refresh(request: Request):
    """Renew the access cookie from the stored refresh token.

    Without this the access cookie carried a JWT that expired on Supabase's
    schedule -- an hour by default, less if the project is configured that way
    -- while the cookie itself lived for thirty days. Every request after that
    expiry read as signed out even though the session was still valid, which is
    what made the dashboard demand a fresh sign-in every few minutes.
    """
    if settings.auth_mode != "supabase":
        raise HTTPException(status_code=404, detail="Supabase Auth is not enabled.")
    stored = request.cookies.get(SUPABASE_REFRESH_COOKIE, "")
    if not stored:
        raise HTTPException(status_code=401, detail="Sign in required.")
    try:
        data = supabase_auth.refresh(stored)
        principal = supabase_auth.principal(str(data["access_token"]))
    except SupabaseAuthError as exc:
        # A refresh token that no longer works is a dead session, not a
        # transient error. Clear both halves so the client stops retrying with
        # a credential that cannot recover.
        from fastapi.responses import JSONResponse
        dead = JSONResponse({"ok": False, "detail": str(exc)}, status_code=401)
        dead.delete_cookie(SUPABASE_COOKIE)
        dead.delete_cookie(SUPABASE_REFRESH_COOKIE)
        return dead
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True, "user": principal.id, "role": principal.role})
    cookie = dict(_cookie_kwargs())
    cookie["max_age"] = 30 * 86400
    resp.set_cookie(SUPABASE_COOKIE, str(data["access_token"]), **cookie)
    # Supabase rotates the refresh token on every exchange; storing the old one
    # again would invalidate the session at the next renewal.
    if data.get("refresh_token"):
        resp.set_cookie(SUPABASE_REFRESH_COOKIE, str(data["refresh_token"]), **cookie)
    return resp


@app.get("/auth/me")
def auth_me(request: Request):
    if settings.auth_mode != "supabase":
        raise HTTPException(status_code=404, detail="Supabase Auth is not enabled.")
    p = _supabase_principal(request)
    if p is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return {"id": p.id, "email": p.email, "full_name": p.full_name,
            "avatar_url": p.avatar_url, "role": p.role, "email_confirmed": p.email_confirmed}


@app.patch("/auth/me")
async def update_auth_me(request: Request):
    if settings.auth_mode != "supabase":
        raise HTTPException(status_code=404, detail="Supabase Auth is not enabled.")
    p = _supabase_principal(request)
    if p is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="A JSON profile payload is required.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="A JSON object is required.")
    try:
        profile = supabase_auth.update_profile(_supabase_token(request), p.id, body)
    except SupabaseAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    supabase_auth.audit(actor_id=p.id, event="profile.updated", metadata={"fields": sorted(body)})
    return {"ok": True, "profile": profile}


@app.delete("/auth/me")
def delete_auth_me(request: Request):
    if settings.auth_mode != "supabase":
        raise HTTPException(status_code=404, detail="Supabase Auth is not enabled.")
    p = _supabase_principal(request)
    if p is None:
        raise HTTPException(status_code=401, detail="Sign in required.")
    try:
        supabase_auth.audit(actor_id=p.id, event="account.deleted", target_id=p.id)
        supabase_auth.delete_account(p.id)
    except SupabaseAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SUPABASE_COOKIE)
    resp.delete_cookie(SUPABASE_REFRESH_COOKIE)
    return resp


@app.get("/admin/api/status")
def admin_status(request: Request):
    if settings.auth_mode != "supabase":
        raise HTTPException(status_code=404, detail="Supabase Auth is not enabled.")
    p = _supabase_principal(request)
    if p is None or not p.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required.")
    try:
        users = supabase_auth.admin_profiles()
    except SupabaseAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    supabase_auth.audit(actor_id=p.id, event="admin.status.viewed")
    return {"ok": True, "users": users, "engine": webhook_api.engine.status(),
            "health": {"status": "ok", "auth": "supabase"}}


# ══════════════════════════════════════════════════ real auth flows
# Password reset, email verification, TOTP two-factor and OAuth sign-in, all
# against the SAME users table every other request is authorised against. The
# logic lives in services/auth_flows.py and services/oauth.py; these handlers
# are the HTTP skin over it.

def _msg_block(result: dict) -> str:
    """Render a flow result. A `delivery` block that is unavailable is an
    OPERATOR problem (no SMTP host, failed send) and is always shown — the
    user-facing sentence stays deliberately vague, but nobody should be left
    waiting for an email the server never attempted."""
    out = ""
    if result.get("error"):
        out += f'<div class="err">{w.esc(result["error"])}</div>'
    elif result.get("message"):
        out += f'<div class="ok">{w.esc(result["message"])}</div>'
    d = result.get("delivery") or {}
    if d and not d.get("available") and d.get("note"):
        out += f'<div class="err">{w.esc(d["note"])}</div>'
    return out


# ─────────────────────────────────────────────── forgot / reset password
@app.get("/auth/forgot-password", response_class=HTMLResponse)
def forgot_password_form(sent: str = "") -> str:
    note = ('<div class="ok">' + w.esc(_af.GENERIC_RESET_REPLY) + "</div>") if sent else ""
    delivery = _mailer.available()
    warn = ("" if delivery["available"]
            else f'<div class="err">{w.esc(delivery["note"])}</div>')
    return _auth_page("Reset password", f'''{_BRAND_HEAD}
<h1>Forgot your password?</h1>
<p class="sub">We'll email you a link to set a new one.</p>
<form method="post" action="/auth/forgot-password" onsubmit="return subm(this)" novalidate>
<label class="fld"><span class="lbl">Username or email</span>
<div class="inp"><span class="ico">{_IC_USER}</span><input name="identifier" autocomplete="username" placeholder="you@email.com or a username" autofocus></div></label>
<button class="btn-gold" type="submit"><span class="sheen"></span><span class="spin"></span><span class="txt">Send reset link</span></button>
</form>
{note}{warn}<p class="foot">Remembered it? <a href="/login">Sign in</a></p>''')


@app.post("/auth/forgot-password")
def forgot_password(identifier: str = Form(...)):
    result = _af.request_password_reset(store, identifier)
    # Always the same redirect, hit or miss: a different destination for a real
    # account would turn this form into a membership check.
    d = result.get("delivery") or {}
    if d and not d.get("available"):
        return RedirectResponse(
            "/auth/forgot-password?sent=1&error=" + _q(d.get("note", "")),
            status_code=303)
    return RedirectResponse("/auth/forgot-password?sent=1", status_code=303)


@app.get("/auth/reset-password", response_class=HTMLResponse)
def reset_password_form(token: str = "", error: str = "") -> str:
    if not token:
        return _auth_page("Reset password", f'''{_BRAND_HEAD}
<h1>Link missing</h1>
<p class="sub">Open the link from your email, or request a new one.</p>
<p class="foot"><a href="/auth/forgot-password">Send a new reset link</a></p>''')
    err = f'<div class="err">{w.esc(error)}</div>' if error else ""
    return _auth_page("Reset password", f'''{_BRAND_HEAD}
<h1>Set a new password</h1>
<p class="sub">This link works once and expires an hour after it was sent.</p>
<form method="post" action="/auth/reset-password" onsubmit="return subm(this)" novalidate>
<input type="hidden" name="token" value="{w.esc(token)}">
{_pw_field("password", "New password", "np", "tnp", "new-password")}
{_pw_field("confirm", "Confirm password", "nc", "tnc", "new-password")}
<button class="btn-gold" type="submit"><span class="sheen"></span><span class="spin"></span><span class="txt">Update password</span></button>
</form>
{err}''')


@app.post("/auth/reset-password")
def reset_password(token: str = Form(...), password: str = Form(...),
                   confirm: str = Form("")):
    result = _af.perform_password_reset(store, token, password, confirm or None)
    if not result["ok"]:
        return RedirectResponse(
            f"/auth/reset-password?token={_q(token)}&error={_q(result['error'])}",
            status_code=303)
    # Deliberately NOT signed in here. Someone who reached this page from a
    # forwarded email should have to prove they know the new password.
    return RedirectResponse("/login?error=" + _q(result["message"]), status_code=303)


# ────────────────────────────────────────────────────── email verification
@app.get("/auth/verify-email", response_class=HTMLResponse)
def verify_email_page(request: Request, token: str = "") -> str:
    if token:
        result = _af.confirm_email(store, token)
        body = _msg_block(result)
        head = "Email confirmed" if result["ok"] else "That link didn't work"
        return _auth_page("Verify email", f'''{_BRAND_HEAD}
<h1>{head}</h1>{body}
<p class="foot"><a href="/login">Continue to sign in</a></p>''')
    u = _user(request)
    if not u:
        return _auth_page("Verify email", f'''{_BRAND_HEAD}
<h1>Confirm your email</h1>
<p class="sub">Open the link we emailed you.</p>
<p class="foot"><a href="/login">Sign in</a></p>''')
    return _auth_page("Verify email", f'''{_BRAND_HEAD}
<h1>Confirm your email</h1>
<p class="sub">Signed in as {w.esc(u)}. Send yourself a fresh confirmation link.</p>
<form method="post" action="/auth/resend-verification" onsubmit="return subm(this)">
<button class="btn-gold" type="submit"><span class="sheen"></span><span class="spin"></span><span class="txt">Send confirmation link</span></button>
</form>''')


@app.post("/auth/resend-verification")
def resend_verification(request: Request):
    u = _user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Sign in first")
    return _af.request_email_verification(store, u)


# ────────────────────────────────────────────────────────────── two-factor
@app.get("/auth/two-factor", response_class=HTMLResponse)
def two_factor_page(request: Request, error: str = "") -> str:
    pending = _pending_2fa_user(request)
    if not pending:
        return _auth_page("Two-factor", f'''{_BRAND_HEAD}
<h1>Start again</h1>
<p class="sub">That sign-in expired. Enter your password again to get a fresh code prompt.</p>
<p class="foot"><a href="/login">Back to sign in</a></p>''')
    err = f'<div class="err">{w.esc(error)}</div>' if error else ""
    return _auth_page("Two-factor", f'''{_BRAND_HEAD}
<h1>Two-factor</h1>
<p class="sub">Enter the 6-digit code from your authenticator app, or a recovery code.</p>
<form method="post" action="/auth/two-factor" onsubmit="return subm(this)" novalidate>
<label class="fld"><span class="lbl">Code</span>
<div class="inp"><span class="ico">{_IC_LOCK}</span><input name="code" inputmode="text" autocomplete="one-time-code" placeholder="123456" autofocus></div></label>
<button class="btn-gold" type="submit"><span class="sheen"></span><span class="spin"></span><span class="txt">Verify</span></button>
</form>
{err}<p class="foot">Lost your phone? Use one of your recovery codes above.</p>''')


@app.post("/auth/two-factor")
def two_factor_submit(request: Request, code: str = Form(...)):
    pending = _pending_2fa_user(request)
    if not pending:
        return RedirectResponse("/login?error=" + _q("That sign-in expired. Try again."),
                                status_code=303)
    result = _af.verify_second_factor(store, pending, code)
    if not result["ok"]:
        return RedirectResponse("/auth/two-factor?error=" + _q(result["error"]),
                                status_code=303)
    return _grant_session(store.get_user(pending))


@app.get("/auth/2fa/status")
def two_factor_status(request: Request):
    u = _user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Sign in first")
    return _af.totp_status(store, u)


@app.post("/auth/2fa/setup")
def two_factor_setup(request: Request):
    u = _user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Sign in first")
    return _af.begin_totp_setup(store, u, issuer=settings.app_name)


@app.post("/auth/2fa/enable")
def two_factor_enable(request: Request, code: str = Form(...)):
    u = _user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Sign in first")
    return _af.enable_totp(store, u, code)


@app.post("/auth/2fa/disable")
def two_factor_disable(request: Request, password: str = Form(...)):
    u = _user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Sign in first")
    return _af.disable_totp(store, u, password)


# ──────────────────────────────────────────────────────────────── OAuth
@app.get("/auth/oauth/providers")
def oauth_providers():
    """What can actually complete a sign-in. The UI disables the rest WITH the
    reason, rather than showing a button that dies on click."""
    return _oauth.available()


@app.get("/auth/oauth/{provider}/start")
def oauth_start(provider: str):
    p = _oauth.get_provider(provider)
    if p is None:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if not _oauth.is_configured(p) or not _mailer.public_url():
        raise HTTPException(status_code=503,
                            detail=_oauth.available()[p.key]["note"])
    state = _oauth.sign_state(settings.secret_key)
    return RedirectResponse(_oauth.authorize_url(p, state), status_code=303)


@app.get("/auth/oauth/{provider}/callback")
def oauth_callback(provider: str, code: str = "", state: str = "", error: str = ""):
    p = _oauth.get_provider(provider)
    if p is None:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if error:
        return RedirectResponse("/login?error=" + _q(f"{p.label} sign-in was cancelled."),
                                status_code=303)
    # The state check comes before anything else touches the code: it is what
    # stops an attacker from having a victim's browser redeem a code they chose.
    if not _oauth.verify_state(state, settings.secret_key):
        return RedirectResponse(
            "/login?error=" + _q("That sign-in link expired or was tampered with. Try again."),
            status_code=303)
    if not code:
        return RedirectResponse("/login?error=" + _q(f"{p.label} returned no code."),
                                status_code=303)

    token = _oauth.exchange_code(p, code)
    profile = _oauth.fetch_profile(p, token) if token else None
    if profile is None:
        return RedirectResponse(
            "/login?error=" + _q(f"Could not read your {p.label} account. Try again."),
            status_code=303)

    user = store.find_by_oauth(p.key, profile.subject)
    if user is None:
        user = _link_or_create_oauth_user(p, profile)
        if user is None:
            return RedirectResponse(
                "/login?error=" + _q(
                    f"Your {p.label} account isn't linked to a hub account. Sign in "
                    "with your password first, then link it from Settings."),
                status_code=303)

    if user.totp_enabled:
        # OAuth proves the provider account, not the second factor. Skipping it
        # here would make 2FA bypassable by anyone holding the linked inbox.
        resp = RedirectResponse("/auth/two-factor", status_code=303)
        kw = dict(_cookie_kwargs())
        kw["max_age"] = PENDING_2FA_TTL_S
        resp.set_cookie(PENDING_2FA_COOKIE, _pending_2fa_token(user.username), **kw)
        return resp
    return _grant_session(user)


def _link_or_create_oauth_user(p, profile):
    """Resolve a first-time federated sign-in to a local account.

    Auto-linking by email is only safe when the PROVIDER says the address is
    verified — an unverified one is just a string the user typed, and honouring
    it would let anyone who sets that address inherit the account.

    Creating a brand-new account is allowed only when signup is still open, so
    OAuth cannot become a side door around the single-owner rule.
    """
    if profile.email and profile.email_verified:
        existing = store.find_by_email(profile.email)
        if existing is not None:
            store.link_oauth(p.key, profile.subject, existing.username, profile.email)
            return store.get_user(existing.username)

    if not _signup_open() or not profile.email or not profile.email_verified:
        return None
    username = profile.email
    if store.get_user(username) is None:
        # No password: this account signs in through the provider. A reset link
        # is how they would add one later.
        store.create_user(username, secrets.token_urlsafe(32), role="owner")
        store.set_email(username, profile.email, verified=True)
    store.link_oauth(p.key, profile.subject, username, profile.email)
    return store.get_user(username)


@app.get("/auth/oauth/links")
def oauth_links(request: Request):
    u = _user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Sign in first")
    return {"links": store.list_oauth_links(u), "providers": _oauth.available()}


@app.post("/auth/oauth/{provider}/unlink")
def oauth_unlink(request: Request, provider: str):
    u = _user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Sign in first")
    store.unlink_oauth(provider, u)
    return {"ok": True, "links": store.list_oauth_links(u)}


# ------------------------------------------------- persistent user workspace
# Per-user settings blobs (namespace -> JSON), session-authenticated and
# strictly isolated by username. The backend DB is the source of truth; the
# frontends use localStorage only as a fast-boot cache. Nothing here is ever
# reset implicitly — DELETE is wired only to the user's explicit Reset actions.
_SETTINGS_NAMESPACES = ("settings-center", "dashboard", "preferences", "profile")


@app.get("/user/settings")
async def user_settings_get(request: Request, ns: str = "settings-center"):
    from fastapi.responses import JSONResponse
    u = _user(request)
    if not u:
        return JSONResponse({"error": "Not signed in"}, status_code=401)
    if ns not in _SETTINGS_NAMESPACES:
        return JSONResponse({"error": f"Unknown namespace {ns!r}"}, status_code=400)
    return {"ns": ns, "data": store.get_user_settings(u, ns)}


@app.post("/user/settings")
async def user_settings_set(request: Request):
    from fastapi.responses import JSONResponse
    u = _user(request)
    if not u:
        return JSONResponse({"error": "Not signed in"}, status_code=401)
    body = await request.json()
    ns = str(body.get("ns", "settings-center"))
    data = body.get("data")
    if ns not in _SETTINGS_NAMESPACES:
        return JSONResponse({"error": f"Unknown namespace {ns!r}"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "data must be an object"}, status_code=400)
    if len(str(data)) > 200_000:
        return JSONResponse({"error": "settings blob too large"}, status_code=413)
    store.set_user_settings(u, ns, data)
    return {"saved": True, "ns": ns}


@app.delete("/user/settings")
async def user_settings_reset(request: Request, ns: str = ""):
    """Explicit reset only (Reset Settings / Reset Dashboard / Factory Reset)."""
    from fastapi.responses import JSONResponse
    u = _user(request)
    if not u:
        return JSONResponse({"error": "Not signed in"}, status_code=401)
    if ns and ns not in _SETTINGS_NAMESPACES:
        return JSONResponse({"error": f"Unknown namespace {ns!r}"}, status_code=400)
    store.delete_user_settings(u, ns or None)
    return {"reset": True, "ns": ns or "all"}


@app.post("/auth/change-password")
async def auth_change_password(request: Request):
    u = _user(request)
    from fastapi.responses import JSONResponse
    if not u:
        return JSONResponse({"error": "Not signed in"}, status_code=401)
    body = await request.json()
    current, new = str(body.get("current", "")), str(body.get("new", ""))
    if store.authenticate(u, current) is None:
        return JSONResponse({"error": "Current password is wrong"}, status_code=400)
    if len(new) < 8:
        return JSONResponse({"error": "New password must be at least 8 characters"}, status_code=400)
    if hasattr(store, "set_password"):
        store.set_password(u, new)
        return {"ok": True, "note": "Password changed."}
    return JSONResponse({"error": "Password change not supported by this store"}, status_code=500)


# ------------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    # With the landing bundled, "/" is the PUBLIC marketing front door (it carries
    # no control secret). The dashboard lives at "/app" and stays sign-in gated.
    if _LANDING_READY:
        return _serve_landing(request)
    # ALL dashboards require sign-in — the served page carries the control
    # secret, so an anonymous visitor must never receive it.
    u = _require(request)
    if isinstance(u, RedirectResponse):
        return u
    if _WEBUI_READY:
        return _serve_react()
    return HTMLResponse(render_overview(manager, user=u))


# ------------------------------------------------------------------------ bots
@app.get("/bots", response_class=HTMLResponse)
def bots_page(request: Request):
    u = _require(request)
    if isinstance(u, RedirectResponse):
        return u
    bots = manager.list()
    if bots:
        rows = "".join(
            f"<tr><td><b>{w.esc(b.config.name)}</b></td>"
            f"<td>{w.esc(strategy_label(b.config.strategy))}</td>"
            f"<td>{w.esc(exchange_label(b.config.exchange))}</td>"
            f"<td>{w.esc(b.config.symbol)}</td>"
            f"<td>{w.esc(b.config.mode.value)}</td>"
            f"<td>{w.state_badge(b.runtime.state.value)}</td>"
            f'<td class="rowbtns">'
            f'<a class="btn btn-ghost" href="/bots/{b.id}/backtest">Backtest</a>'
            f'<a class="btn btn-ghost" href="/bots/{b.id}/edit">Edit</a></td></tr>'
            for b in bots
        )
        table = (f'<div class="card"><table><thead><tr><th>Name</th><th>Strategy</th>'
                 f'<th>Exchange</th><th>Symbol</th><th>Mode</th><th>State</th>'
                 f'<th></th></tr></thead>'
                 f'<tbody>{rows}</tbody></table></div>')
    else:
        table = '<div class="card"><div class="empty">No bots yet.</div></div>'
    new_btn = '<a class="btn" href="/bots/new">+ Create Bot</a>'
    body = w.topbar("Bots", new_btn) + table
    return HTMLResponse(w.page(title="Bots", active="bots", body=body,
                               app_name=settings.app_name, user=u))


@app.get("/bots/new", response_class=HTMLResponse)
def new_bot_form(request: Request):
    u = _require(request)
    if isinstance(u, RedirectResponse):
        return u
    strat_opts = "".join(
        f'<option value="{k}"{"" if ready else " disabled"}>{w.esc(label)}'
        f'{"" if ready else " (coming soon)"}</option>'
        for k, (_c, label, ready) in STRATEGIES.items()
    )
    exch_opts = "".join(
        f'<option value="{k}"{"" if ready else " disabled"}>{w.esc(label)}'
        f'{"" if ready else " (coming soon)"}</option>'
        for k, (label, _cls, ready) in EXCHANGES.items()
    )
    form = f'''<div class="card"><form method="post" action="/bots"><div class="formgrid">
<div><label>Bot name</label><input name="name" value="My Bot" required></div>
<div><label>Symbol</label><input name="symbol" value="{w.esc(settings.default_symbol)}"></div>
<div><label>Strategy</label><select name="strategy">{strat_opts}</select></div>
<div><label>Exchange</label><select name="exchange">{exch_opts}</select></div>
<div><label>Timeframe</label><select name="timeframe">
<option>5m</option><option>15m</option><option selected>1h</option></select></div>
<div><label>Mode</label><select name="mode">
<option value="paper" selected>Paper</option><option value="live">Live (Phase 5)</option></select></div>
<div><label>Risk per trade (%)</label><input name="risk_per_trade" type="number" step="0.1" value="1.0"></div>
<div><label>Max daily loss (%)</label><input name="max_daily_loss" type="number" step="0.1" value="3.0"></div>
</div><div style="margin-top:16px"><button class="btn" type="submit">Create Bot</button>
<a class="btn btn-ghost" href="/bots" style="margin-left:8px">Cancel</a></div></form></div>'''
    body = w.topbar("Create Bot") + form
    return HTMLResponse(w.page(title="Create Bot", active="bots", body=body,
                               app_name=settings.app_name, user=u))


@app.post("/bots")
def create_bot(
    request: Request,
    name: str = Form(...),
    strategy: str = Form("ema"),
    exchange: str = Form("binance"),
    symbol: str = Form("BTCUSDT"),
    timeframe: str = Form("1h"),
    mode: str = Form("paper"),
    risk_per_trade: float = Form(1.0),
    max_daily_loss: float = Form(3.0),
):
    if not _user(request):
        return RedirectResponse("/login", status_code=303)
    rules = RiskRules(
        risk_per_trade_pct=max(risk_per_trade, 0.01) / 100.0,
        max_daily_loss_pct=max(max_daily_loss, 0.1) / 100.0,
        max_open_positions=settings.max_open_positions,
    )
    cfg = BotConfig(
        name=name, strategy=strategy, exchange=exchange, symbol=symbol,
        timeframe=timeframe, mode=BotMode(mode), risk=rules,
        starting_cash=settings.starting_cash,
    )
    manager.create(cfg)
    return RedirectResponse("/bots", status_code=303)


# ----------------------------------------------------------- edit (Phase 9)
@app.get("/bots/{bot_id}/edit", response_class=HTMLResponse)
def edit_bot_form(bot_id: str, request: Request):
    u = _require(request)
    if isinstance(u, RedirectResponse):
        return u
    bot = manager.get(bot_id)
    if bot is None:
        return RedirectResponse("/bots", status_code=303)
    c, r = bot.config, bot.config.risk
    strat_opts = "".join(
        f'<option value="{k}"{" selected" if k == c.strategy else ""}'
        f'{"" if ready else " disabled"}>{w.esc(label)}</option>'
        for k, (_cls, label, ready) in STRATEGIES.items()
    )
    tf_opts = "".join(
        f'<option{" selected" if tf == c.timeframe else ""}>{tf}</option>'
        for tf in ("5m", "15m", "1h"))
    form = f'''<div class="card"><form method="post" action="/bots/{bot_id}/edit">
<div class="formgrid">
<div><label>Bot name</label><input name="name" value="{w.esc(c.name)}" required></div>
<div><label>Symbol</label><input name="symbol" value="{w.esc(c.symbol)}"></div>
<div><label>Strategy</label><select name="strategy">{strat_opts}</select></div>
<div><label>Timeframe</label><select name="timeframe">{tf_opts}</select></div>
<div><label>Risk per trade (%)</label><input name="risk_per_trade" type="number" step="0.1" value="{r.risk_per_trade_pct*100:.2f}"></div>
<div><label>Max daily loss (%)</label><input name="max_daily_loss" type="number" step="0.1" value="{r.max_daily_loss_pct*100:.2f}"></div>
<div><label>Max drawdown (%)</label><input name="max_drawdown" type="number" step="0.1" value="{r.max_drawdown_pct*100:.2f}"></div>
<div><label>Max consecutive losses</label><input name="max_consecutive_losses" type="number" value="{r.max_consecutive_losses}"></div>
</div><div style="margin-top:16px"><button class="btn" type="submit">Save Changes</button>
<a class="btn btn-ghost" href="/bots" style="margin-left:8px">Cancel</a></div></form></div>'''
    return HTMLResponse(w.page(title="Edit Bot", active="bots",
                               body=w.topbar(f"Edit · {w.esc(c.name)}") + form,
                               app_name=settings.app_name, user=u))


@app.post("/bots/{bot_id}/edit")
def edit_bot(
    bot_id: str,
    request: Request,
    name: str = Form(...),
    strategy: str = Form("ema"),
    symbol: str = Form("BTCUSDT"),
    timeframe: str = Form("1h"),
    risk_per_trade: float = Form(1.0),
    max_daily_loss: float = Form(3.0),
    max_drawdown: float = Form(20.0),
    max_consecutive_losses: int = Form(4),
):
    if not _user(request):
        return RedirectResponse("/login", status_code=303)
    rules = RiskRules(
        risk_per_trade_pct=max(risk_per_trade, 0.01) / 100.0,
        max_daily_loss_pct=max(max_daily_loss, 0.1) / 100.0,
        max_open_positions=settings.max_open_positions,
        max_drawdown_pct=max(max_drawdown, 0.1) / 100.0,
        max_consecutive_losses=max(int(max_consecutive_losses), 0),
    )
    try:
        manager.update(bot_id, name=name, strategy=strategy, symbol=symbol,
                       timeframe=timeframe, risk=rules)
    except Exception as e:  # noqa: BLE001 — M-8: surface, don't swallow
        webhook_api.ledger.log(level="error", stage="bots",
                               message=f"edit_bot {bot_id} failed: {type(e).__name__}: {e}")
        from urllib.parse import quote
        return RedirectResponse(f"/bots?error={quote(f'Could not save bot: {e}')}", status_code=303)
    return RedirectResponse("/bots", status_code=303)


# ------------------------------------------------------- backtest (Phase 9)
@app.get("/bots/{bot_id}/backtest", response_class=HTMLResponse)
def backtest_bot(bot_id: str, request: Request):
    u = _require(request)
    if isinstance(u, RedirectResponse):
        return u
    bot = manager.get(bot_id)
    if bot is None:
        return RedirectResponse("/bots", status_code=303)
    from dashboard.analytics import render_result
    res = manager.backtest(bot_id)
    head = (f'<div class="card"><h2>Backtest — {w.esc(bot.config.name)}</h2>'
            f'<div class="dim">{w.esc(bot.config.strategy.upper())} · '
            f'{w.esc(bot.config.symbol)} · {w.esc(bot.config.timeframe)} · '
            f'{w.esc(res.source)} data</div></div>')
    body = (w.topbar(f"Backtest · {w.esc(bot.config.name)}",
                     '<a class="btn btn-ghost" href="/bots">← Bots</a>')
            + head + render_result(bot.config.name, res.metrics, res.trades, res.equity_curve))
    return HTMLResponse(w.page(title="Backtest", active="bots", body=body,
                               app_name=settings.app_name, user=u))


def _bot_action(request: Request, action):
    if not _user(request):
        return RedirectResponse("/login", status_code=303)
    if not _has_role(request, "operator"):
        return RedirectResponse("/?error=Operator+role+required", status_code=303)
    try:
        action()
    except Exception as e:  # noqa: BLE001 — M-8: surface, don't swallow
        webhook_api.ledger.log(level="error", stage="bots",
                               message=f"bot action failed: {type(e).__name__}: {e}")
        from urllib.parse import quote
        return RedirectResponse(f"/?error={quote(f'Action failed: {e}')}", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/bots/{bot_id}/start")
def start_bot(bot_id: str, request: Request):
    return _bot_action(request, lambda: manager.start(bot_id))


@app.post("/bots/{bot_id}/go-live")
def go_live_bot(bot_id: str, request: Request):
    # Phase 2: stream bars through the live engine (replay-driven demo).
    # Phase 8: forward the runner's events to the hub for the live dashboard.
    if not _user(request):
        return RedirectResponse("/login", status_code=303)
    if not _has_role(request, "operator"):
        return RedirectResponse("/?error=Operator+role+required", status_code=303)
    bot = manager.get(bot_id)
    if bot is None:
        return RedirectResponse("/", status_code=303)
    name = bot.config.name

    def sink(event: dict, _bid: str = bot_id, _bn: str = name) -> None:
        hub_events.publish({**event, "bot_id": _bid, "bot_name": _bn})

    try:
        # Subscribe the sink before the worker starts (no missed events).
        manager.start_live(bot_id, event_sink=sink)
        hub_events.publish({"type": "lifecycle", "bot_id": bot_id,
                            "bot_name": name, "message": "went live"})
    except Exception as e:  # noqa: BLE001 — M-8: surface, don't swallow
        webhook_api.ledger.log(level="error", stage="bots",
                               message=f"go_live {bot_id} failed: {type(e).__name__}: {e}")
        from urllib.parse import quote
        return RedirectResponse(f"/?error={quote(f'Could not go live: {e}')}", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/bots/{bot_id}/pause")
def pause_bot(bot_id: str, request: Request):
    return _bot_action(request, lambda: manager.pause(bot_id))


@app.post("/bots/{bot_id}/stop")
def stop_bot(bot_id: str, request: Request):
    return _bot_action(request, lambda: manager.stop(bot_id))


@app.post("/emergency-stop")
def emergency_stop(request: Request):
    return _bot_action(request, lambda: manager.emergency_stop_all())


# ------------------------------------------------------- secondary nav pages
def _simple_page(request: Request, title: str, active: str, body_inner: str):
    u = _require(request)
    if isinstance(u, RedirectResponse):
        return u
    body = w.topbar(title) + body_inner
    return HTMLResponse(w.page(title=title, active=active, body=body,
                               app_name=settings.app_name, user=u))


@app.get("/strategies", response_class=HTMLResponse)
def strategies_page(request: Request):
    rows = "".join(
        f"<tr><td><b>{w.esc(label)}</b></td><td>{w.esc(k)}</td>"
        f"<td>{w.state_badge('Running' if ready else 'Created')}</td></tr>"
        for k, (_c, label, ready) in STRATEGIES.items()
    )
    inner = (f'<div class="card"><h2>Available Strategies</h2><table><thead><tr>'
             f'<th>Strategy</th><th>Key</th><th>Status</th></tr></thead>'
             f'<tbody>{rows}</tbody></table></div>')
    return _simple_page(request, "Strategies", "strategies", inner)


@app.get("/paper-trading", response_class=HTMLResponse)
def paper_page(request: Request):
    """Live paper account driven by the TradingView webhook -> paper engine.

    KPIs + emergency controls + open positions + closed-trade history, all read
    from the Kyros ledger (the source of truth)."""
    paper, controls = webhook_api.paper, webhook_api.controls
    cur = settings.currency
    realized = paper.realized_pnl()
    positions = paper.positions()
    history = paper.history()

    kpis = ('<div class="kpis">'
            + w.kpi("Balance", f"{cur}{paper.balance():,.2f}")
            + w.kpi("Realized P&L", f"{cur}{realized:,.2f}",
                    "pos" if realized >= 0 else "neg")
            + w.kpi("Open Positions", str(len(positions)))
            + w.kpi("Trading State", w.state_badge(controls.state))
            + '</div>')

    # Emergency controls (paper mode) — session-gated POSTs operate the same
    # control switch the webhook pipeline consults before every entry.
    controls_card = (
        '<div class="card"><h2>Emergency Controls</h2>'
        '<div class="rowbtns" style="justify-content:flex-start;gap:8px">'
        '<form class="inline" method="post" action="/paper-trading/pause">'
        '<button class="btn btn-warn" type="submit">⏸ Pause All</button></form>'
        '<form class="inline" method="post" action="/paper-trading/stop">'
        '<button class="btn btn-danger" type="submit">■ Stop All</button></form>'
        '<form class="inline" method="post" action="/paper-trading/resume">'
        '<button class="btn" type="submit">▶ Resume Trading</button></form>'
        '</div>'
        f'<p class="dim" style="margin-top:10px">Current state: '
        f'<b>{w.esc(controls.state)}</b>. Pause/Stop block new webhook entries; '
        'open positions still close on exit signals. Paper mode only.</p></div>')

    if positions:
        prows = "".join(
            f"<tr><td><b>{w.esc(p['symbol'])}</b></td>"
            f"<td>{w.esc(str(p['side']).title())}</td>"
            f"<td>{p['size']:.6f}</td><td>{cur}{p['entry']:,.2f}</td>"
            f"<td>{(cur + format(p['stop'], ',.2f')) if p.get('stop') else '—'}</td></tr>"
            for p in positions
        )
        pos_card = (f'<div class="card"><h2>Open Positions</h2><table><thead><tr>'
                    f'<th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th>'
                    f'<th>Stop</th></tr></thead><tbody>{prows}</tbody></table></div>')
    else:
        pos_card = ('<div class="card"><h2>Open Positions</h2>'
                    '<div class="empty">No open positions. Send a TradingView '
                    'webhook to <code>/webhook/tradingview</code> to open one.</div></div>')

    if history:
        hrows = "".join(
            f"<tr><td><b>{w.esc(t['symbol'])}</b></td>"
            f"<td>{w.esc(str(t['side']).title())}</td>"
            f"<td>{cur}{t['entry']:,.2f}</td>"
            f"<td>{cur}{(t.get('exit') or 0):,.2f}</td>"
            f"<td class='{'pos' if (t.get('pnl') or 0)>=0 else 'neg'}'>"
            f"{cur}{(t.get('pnl') or 0):,.2f}</td>"
            f"<td>{(t.get('rr') if t.get('rr') is not None else 0):.2f}R</td></tr>"
            for t in history
        )
        hist_card = (f'<div class="card"><h2>Trade History</h2><table><thead><tr>'
                     f'<th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th>'
                     f'<th>P&L</th><th>R:R</th></tr></thead>'
                     f'<tbody>{hrows}</tbody></table></div>')
    else:
        hist_card = ('<div class="card"><h2>Trade History</h2>'
                     '<div class="empty">No closed trades yet.</div></div>')

    return _simple_page(request, "Paper Trading", "paper",
                        kpis + controls_card + pos_card + hist_card)


# --------------------------------------------- emergency controls (UI, session-gated)
def _control_action(request: Request, action, level: str, message: str):
    if not _user(request):
        return RedirectResponse("/login", status_code=303)
    action()
    webhook_api.ledger.log(level=level, stage="controls", message=message)
    return RedirectResponse("/paper-trading", status_code=303)


@app.post("/paper-trading/pause")
def ui_pause_all(request: Request):
    return _control_action(request, webhook_api.controls.pause_all,
                           "warning", "PAUSE ALL — entries blocked (dashboard)")


@app.post("/paper-trading/stop")
def ui_stop_all(request: Request):
    return _control_action(request, webhook_api.controls.stop_all,
                           "warning", "STOP ALL — trading halted (dashboard)")


@app.post("/paper-trading/resume")
def ui_resume(request: Request):
    return _control_action(request, webhook_api.controls.resume,
                           "info", "RESUME — trading active (dashboard)")


@app.get("/risk-center", response_class=HTMLResponse)
def risk_page(request: Request):
    bots = manager.list()
    cur = settings.currency
    daily_loss = -sum(min(0.0, b.runtime.pnl_today) for b in bots)
    limit = settings.max_daily_loss_pct * settings.starting_cash
    worst_dd = min((b.runtime.metrics.get("max_dd", 0.0) for b in bots), default=0.0)
    summary = manager.summary()

    halted = [b for b in bots if b.runtime.halt_reason]
    if halted:
        rows = "".join(
            f"<tr><td><b>{w.esc(b.config.name)}</b></td>"
            f"<td class='neg'>⛔ {w.esc(b.runtime.halt_reason)}</td></tr>"
            for b in halted
        )
        halts = (f'<div class="card"><h2>Tripped Circuit Breakers</h2>'
                 f'<table><tbody>{rows}</tbody></table></div>')
    else:
        halts = ('<div class="card"><h2>Tripped Circuit Breakers</h2>'
                 '<div class="empty">None — all bots within risk limits.</div></div>')

    inner = (
        '<div class="card"><h2>Risk Center</h2>'
        f'<div>Daily loss: <b>{cur}{daily_loss:,.0f}</b> / {cur}{limit:,.0f}</div>'
        f'<div style="margin-top:8px">Worst bot drawdown: <b class="neg">{worst_dd*100:.2f}%</b></div>'
        f'<div style="margin-top:8px">Active bots: <b>{summary["running"] + summary["paper"]}</b></div>'
        '</div>'
        '<div class="card"><h2>Live Circuit Breakers</h2>'
        '<table><thead><tr><th>Breaker</th><th>Action</th></tr></thead><tbody>'
        f'<tr><td>Daily loss &gt; {settings.max_daily_loss_pct*100:.0f}% of equity</td>'
        '<td>Halt bot + alert</td></tr>'
        '<tr><td>Max drawdown breach</td><td>Halt bot + alert</td></tr>'
        '<tr><td>Consecutive-loss streak</td><td>Halt bot + alert</td></tr>'
        '<tr><td>Emergency stop (manual)</td><td>Halt all bots immediately</td></tr>'
        '</tbody></table>'
        '<p class="dim" style="margin-top:10px">Enforced live by risk/guards.py after every '
        'bar; the engine also applies the daily-loss kill switch + post-loss cooldown during runs.</p></div>'
        + halts
    )
    return _simple_page(request, "Risk Center", "risk", inner)


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, bot: str = ""):
    from dashboard.analytics import render_analytics
    return _simple_page(request, "Analytics", "analytics",
                        render_analytics(manager, bot_id=bot or None))


def _level_badge(level: str) -> str:
    cls = {"error": "b-err", "warning": "b-pause", "info": "b-paper"}.get(
        str(level).lower(), "b-new")
    return f'<span class="badge {cls}">{w.esc(str(level).upper())}</span>'


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    # Decision log from the ledger: every pipeline stage (controls/dedup/risk/
    # sizing/execution) records why a webhook signal executed or was rejected.
    decisions = webhook_api.ledger.get_logs(200)
    if decisions:
        drows = "".join(
            f"<tr><td class='dim'>{w.esc((d.get('ts') or '')[:19].replace('T',' '))}</td>"
            f"<td>{_level_badge(d.get('level'))}</td>"
            f"<td>{w.esc(d.get('stage') or '')}</td>"
            f"<td>{w.esc(d.get('symbol') or '')}</td>"
            f"<td>{w.esc(d.get('message') or '')}</td></tr>"
            for d in decisions
        )
        decision_card = (f'<div class="card"><h2>Decision Log</h2><table><thead><tr>'
                         f'<th>Time</th><th>Level</th><th>Stage</th><th>Symbol</th>'
                         f'<th>Message</th></tr></thead><tbody>{drows}</tbody></table></div>')
    else:
        decision_card = ('<div class="card"><h2>Decision Log</h2>'
                         '<div class="empty">No webhook decisions yet. Signals sent to '
                         '<code>/webhook/tradingview</code> appear here with their outcome.</div></div>')

    # Engine runtime events (backtest/live runner) — kept as a secondary feed.
    lines = []
    for b in manager.list():
        for ev in b.runtime.events[-40:]:
            lines.append(f"[{b.config.name}] {ev.get('type')} "
                         f"{ev.get('symbol','')} {ev.get('reason','')}".strip())
    engine_card = ('<div class="card"><h2>Engine Events</h2>'
                   + ('<pre style="white-space:pre-wrap;color:#9fb0c0">'
                      + w.esc("\n".join(lines[-200:])) + '</pre>'
                      if lines else '<div class="empty">No engine events yet.</div>')
                   + '</div>')
    return _simple_page(request, "Logs", "logs", decision_card + engine_card)


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request):
    alerts = webhook_api.ledger.get_alerts(100)
    if alerts:
        rows = "".join(
            f"<tr><td class='dim'>{w.esc((a.get('ts') or '')[:19].replace('T',' '))}</td>"
            f"<td>{_level_badge(a.get('severity'))}</td>"
            f"<td>{w.esc(a.get('category') or '')}</td>"
            f"<td><b>{w.esc(a.get('title') or '')}</b></td>"
            f"<td class='dim'>{w.esc(a.get('detail') or '')}</td></tr>"
            for a in alerts
        )
        inner = (f'<div class="card"><h2>Alerts</h2><table><thead><tr><th>Time</th>'
                 f'<th>Severity</th><th>Category</th><th>Title</th><th>Detail</th>'
                 f'</tr></thead><tbody>{rows}</tbody></table></div>')
    else:
        inner = ('<div class="card"><h2>Alerts</h2>'
                 '<div class="empty">No alerts yet. Trade executions, rejections and '
                 'risk events raise alerts here.</div></div>')
    return _simple_page(request, "Alerts", "alerts", inner)


@app.get("/live-trading", response_class=HTMLResponse)
def live_page(request: Request):
    from database.models import BotState
    bots = manager.list()
    cur = settings.currency
    live = [b for b in bots if b.runtime.state in (BotState.RUNNING, BotState.PAPER)]
    total_pnl = sum(b.runtime.pnl_today for b in bots)

    if bots:
        rows = "".join(
            f"<tr><td><b>{w.esc(b.config.name)}</b></td>"
            f"<td>{w.esc(exchange_label(b.config.exchange))}</td>"
            f"<td>{w.esc(b.config.symbol)}</td>"
            f"<td>{w.state_badge(b.runtime.state.value)}</td>"
            f"<td>{b.runtime.metrics.get('num_trades',0)}</td>"
            f"<td class='{'pos' if b.runtime.pnl_today>=0 else 'neg'}'>"
            f"{cur}{b.runtime.pnl_today:,.2f}</td></tr>"
            for b in bots
        )
        table = (f'<div class="card"><h2>Supervised Bots</h2><table><thead><tr>'
                 f'<th>Bot</th><th>Exchange</th><th>Symbol</th><th>State</th>'
                 f'<th>Trades</th><th>P&L today</th></tr></thead>'
                 f'<tbody>{rows}</tbody></table>'
                 '<form class="inline" method="post" action="/emergency-stop" style="margin-top:10px">'
                 '<button class="btn btn-danger" type="submit">■ Stop All Bots</button></form></div>')
    else:
        table = ('<div class="card"><div class="empty">No bots. Create one, then '
                 '“Go Live”.</div></div>')

    kpis = ('<div class="kpis">'
            + w.kpi("Live / Active", str(len(live)))
            + w.kpi("Total bots", str(len(bots)))
            + w.kpi("Aggregate P&L today", f"{cur}{total_pnl:,.2f}",
                    "pos" if total_pnl >= 0 else "neg")
            + w.kpi("Order routing", "dry-run (paper)")
            + '</div>')

    note = ('<div class="card"><h2>Real Order Routing</h2>'
            '<p class="dim">Set venue API keys (env / .env) and the runner mirrors each '
            'engine order to the exchange via execution/live_bridge.py as a bracket order; '
            'AlertDispatcher fires Telegram/Discord/email on fills, halts and completion. '
            'Defaults to dry-run until keys are supplied.</p></div>')
    return _simple_page(request, "Live Trading", "live", kpis + table + note)


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    tg = "configured" if settings.telegram_token else "not configured"
    inner = (f'<div class="card"><h2>Notifications</h2>'
             f'<div>Telegram: <b>{tg}</b></div>'
             '<p class="dim" style="margin-top:10px">Telegram / Email / Discord channels are '
             'wired through the notifications/ package (Phase 5).</p></div>')
    return _simple_page(request, "Notifications", "notifications", inner)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    # The bundled landing owns the full Settings Center at /settings/*.
    if _LANDING_READY:
        return _serve_landing(request)
    u = _user(request)
    me = store.get_user(u) if u else None
    role = me.role if me else "operator"
    inner = (f'<div class="card"><h2>Settings</h2>'
             f'<div>Signed in as: <b>{w.esc(u or "")}</b> '
             f'<span class="dim">({w.esc(role)})</span></div>'
             f'<div>Default exchange: <b>{w.esc(settings.default_exchange)}</b></div>'
             f'<div>Currency: <b>{w.esc(settings.currency)}</b></div>'
             f'<div>Starting cash: <b>{settings.currency}{settings.starting_cash:,.0f}</b></div>'
             '<p class="dim" style="margin-top:10px">Passwords are hashed (PBKDF2). '
             'Manage accounts under <a class="pos" href="/users">Users</a>.</p></div>')
    return _simple_page(request, "Settings", "settings", inner)


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, error: str = ""):
    u = _require(request)
    if isinstance(u, RedirectResponse):
        return u
    me = store.get_user(u)
    rows = "".join(
        f"<tr><td><b>{w.esc(x.username)}</b></td>"
        f"<td>{w.state_badge('Running' if x.role == 'admin' else 'Created')}</td>"
        f"<td>{w.esc(x.role)}</td>"
        f"<td class='dim'>{w.esc(x.created_at.strftime('%Y-%m-%d'))}</td></tr>"
        for x in store.list_users()
    )
    table = (f'<div class="card"><h2>Users</h2><table><thead><tr><th>Username</th>'
             f'<th></th><th>Role</th><th>Created</th></tr></thead>'
             f'<tbody>{rows}</tbody></table></div>')
    if _has_role(request, "admin"):
        err = f'<div class="err">{w.esc(error)}</div>' if error else ""
        # only the owner may mint admins (privilege-escalation guard); everyone
        # with the Add-User form can create viewers and operators.
        admin_opt = '<option value="admin">admin</option>' if _has_role(request, "owner") else ""
        form = ('<div class="card"><h2>Add User</h2>'
                '<form method="post" action="/users"><div class="formgrid">'
                '<div><label>Username</label><input name="username" required></div>'
                '<div><label>Password</label><input name="password" type="password" required></div>'
                '<div><label>Role</label><select name="role">'
                '<option value="viewer">viewer</option>'
                '<option value="operator" selected>operator</option>'
                f'{admin_opt}</select></div>'
                '</div><div style="margin-top:12px">'
                '<button class="btn" type="submit">Create User</button></div>'
                f'{err}</form></div>')
    else:
        form = '<div class="card"><div class="dim">Only admins can add users.</div></div>'
    return _simple_page(request, "Users", "settings", table + form)


@app.post("/users")
def create_user(request: Request, username: str = Form(...),
                password: str = Form(...), role: str = Form("operator")):
    u = _user(request)
    if not u:
        return RedirectResponse("/login", status_code=303)
    if not _has_role(request, "admin"):   # rbac: owner + admin may manage users
        return RedirectResponse("/users?error=Admin+only", status_code=303)
    # normalise the requested role to a creatable one (owner is never mintable
    # via the form — there is exactly one owner, seeded at signup).
    role = (role or "operator").strip().lower()
    if role not in ("viewer", "operator", "admin"):
        role = "operator"
    # privilege-escalation guard: only the OWNER may mint admins, so an admin
    # can never create a peer that could in turn lock the owner out.
    if role == "admin" and not _has_role(request, "owner"):
        return RedirectResponse("/users?error=Only+the+owner+can+create+admins",
                                status_code=303)
    if store.get_user(username) is not None:
        return RedirectResponse("/users?error=User+already+exists", status_code=303)
    store.create_user(username, password, role=role)
    return RedirectResponse("/users", status_code=303)


# --------------------------------------------------- live event stream (P8)
@app.get("/events/state")
def events_state(request: Request):
    if not _user(request):
        return RedirectResponse("/login", status_code=303)
    return {"events": hub_events.replay()}


@app.get("/events/stream")
def events_stream(request: Request):
    if not _user(request):
        return RedirectResponse("/login", status_code=303)
    q = hub_events.subscribe()

    def gen():
        try:
            for ev in hub_events.replay():
                yield sse_format(ev)
            while True:
                try:
                    yield sse_format(q.get(timeout=15))
                except queue.Empty:
                    yield ": ping\n\n"      # heartbeat keeps proxies open
        finally:
            hub_events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _deploy_info() -> dict:
    """What version is actually deployed — from the env vars Render/host inject
    at build time, so you can confirm which commit is live without the dashboard.
    Empty strings when running locally (no CI env)."""
    import os
    commit = os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GIT_COMMIT", "")
    return {
        "commit": commit,
        "commit_short": commit[:7],
        "branch": os.environ.get("RENDER_GIT_BRANCH") or os.environ.get("GIT_BRANCH", ""),
        "service": os.environ.get("RENDER_SERVICE_NAME", ""),
        "deployed_at": os.environ.get("RENDER_DEPLOY_ID") or os.environ.get("DEPLOYED_AT", ""),
    }


def _persistence_info() -> dict:
    """Whether durable storage actually works — the honest answer to 'why do my
    settings reset?'. Both must be connected for settings + account to survive a
    free-tier redeploy."""
    try:
        from data.settings_store import SETTINGS_MIRROR_STATUS
        from data import ledger as _led
        return {
            "settings_supabase": {"configured": SETTINGS_MIRROR_STATUS["configured"],
                                  "connected": SETTINGS_MIRROR_STATUS["connected"],
                                  "error": SETTINGS_MIRROR_STATUS["error"]},
            "ledger_supabase": {"configured": _led.SUPABASE_STATUS.get("configured"),
                                "connected": _led.SUPABASE_STATUS.get("connected"),
                                "error": _led.SUPABASE_STATUS.get("error")},
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


@app.get("/health")
def health():
    from services.tenancy import multi_user_enabled
    return {"status": "ok", "app": settings.app_name, **_deploy_info(),
            "persistence": _persistence_info(),
            "tenancy": {"multi_user": multi_user_enabled(), "mode": "multi" if multi_user_enabled() else "single-owner"}}


@app.get("/version")
def version():
    """The deployed build's commit/branch — match commit_short to `git log`."""
    return {"app": settings.app_name, **_deploy_info()}
