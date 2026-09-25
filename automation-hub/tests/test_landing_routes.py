"""The public marketing site's routes are served by this app, and stay in sync.

The site is a BrowserRouter SPA bundled into the Docker image and served at
"/". Every page is a real URL: it is in the sitemap, it is what a search result
links to, and it is what a refresh reloads. That only works if this app returns
HTML for each one.

It did not. Eighteen of the nineteen pages returned the API's 404 JSON, and
"/docs" was shadowed by FastAPI's own Swagger UI. Client-side navigation hid
it completely — the site worked perfectly right up until someone arrived from
outside it.

The failure mode that follows is drift: a page gets added to the route table in
TypeScript and nobody remembers this Python tuple. So the first test reads the
route table itself and compares.
"""
import re
from pathlib import Path

import app as app_module

# tradexa-landing/src/site/routes.ts, from automation-hub/tests/
_ROUTES_TS = (
    Path(__file__).resolve().parents[2] / "tradexa-landing" / "src" / "site" / "routes.ts"
)


def _paths_declared_in_typescript() -> set[str]:
    """Every `path: "/…"` in the site's route table."""
    source = _ROUTES_TS.read_text(encoding="utf-8")
    return set(re.findall(r'^\s*path:\s*"(/[^"]*)"', source, flags=re.MULTILINE))


def test_route_table_is_readable():
    # A silent zero-match regex would make the drift test below pass forever.
    assert _ROUTES_TS.exists(), f"route table not found at {_ROUTES_TS}"
    assert len(_paths_declared_in_typescript()) >= 19


def test_python_page_list_matches_the_site_route_table():
    declared = _paths_declared_in_typescript()
    served = {"/" + p for p in app_module._LANDING_PAGES}
    missing = declared - served
    extra = served - declared
    assert not missing, (
        f"pages exist in the site but this app will 404 them: {sorted(missing)} — "
        "add them to _LANDING_PAGES in app.py"
    )
    assert not extra, (
        f"this app serves pages the site no longer has: {sorted(extra)} — "
        "remove them from _LANDING_PAGES in app.py"
    )


def test_pages_are_matched_exactly_not_as_prefixes():
    """"/api" is a page. The "/api/v1/*" subtree behind it must stay gated.

    `_AUTH_EXEMPT` is prefix-matched, so putting "/api" there would have made
    every authenticated API endpoint public. The page paths are a separate,
    exact-match set precisely to avoid that.
    """
    assert "/api" in app_module._LANDING_PAGE_PATHS
    assert not any(
        "/api/v1/orders".startswith(p) for p in app_module._AUTH_EXEMPT
    ), "an _AUTH_EXEMPT prefix now unlocks the versioned API subtree"


def test_swagger_moved_off_the_documentation_page_path():
    """FastAPI's docs must not occupy "/docs", which is a public page."""
    assert app_module.app.docs_url == "/api/v1/docs"
    assert app_module.app.redoc_url == "/api/v1/redoc"
    builtin = {getattr(r, "path", "") for r in app_module.app.routes}
    assert "/docs" not in builtin or app_module._LANDING_READY


def test_landing_pages_registered_when_the_build_is_bundled(monkeypatch, tmp_path):
    """With the landing build present, each page path is a real GET route.

    The suite runs without the bundled build (_LANDING_READY is False), so this
    asserts the registration loop covers the same list rather than re-importing
    the module against a fake build tree.
    """
    if not app_module._LANDING_READY:
        # The registration loop iterates _LANDING_PAGES; the drift test above
        # pins that list to the site. Assert the loop's inputs are sane.
        assert app_module._LANDING_PAGES, "no pages would be registered"
        assert all(not p.startswith("/") for p in app_module._LANDING_PAGES)
        return
    served = {getattr(r, "path", "") for r in app_module.app.routes}
    for page in app_module._LANDING_PAGES:
        assert f"/{page}" in served, f"/{page} is not served"


def test_supabase_email_links_land_on_the_spa_pages_that_can_finish_them():
    """Supabase sends the reset and verification emails, and its links carry the
    session in the URL fragment, which only the SPA reads. Served by the
    server-rendered pages (which use the local users table), a reset link showed
    "Link missing" and the password could never be changed."""
    supabase = app_module._landing_auth_pages("supabase")
    legacy = app_module._landing_auth_pages("legacy")
    for page in ("forgot-password", "reset-password", "verify-email"):
        assert page in supabase
        assert page not in legacy          # legacy accounts live in this app's table
    for page in ("login", "register", "session-expired"):
        assert page in supabase and page in legacy
    assert "two-factor" not in supabase and "two-factor" not in legacy
