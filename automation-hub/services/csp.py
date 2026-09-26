"""Content-Security-Policy with a fresh nonce per response.

Scripts run only from this origin, plus the few inline scripts the server
itself writes (the runtime config block, the sign-in page helpers, the legacy
live feed). Each of those carries this request's nonce, so an injected
``<script>`` -- which cannot know the nonce -- does not run. There is no
``'unsafe-inline'`` and no ``'unsafe-eval'`` for scripts.

Styles keep ``'unsafe-inline'``: the pre-rendered pages carry style
attributes, and a style cannot run code.

The nonce lives in a context variable set by the security-header middleware
before the request reaches its handler, so any template can read it with
``nonce()`` without the request being threaded through.
"""
from __future__ import annotations

import contextvars
import secrets
from typing import Iterable, Optional
from urllib.parse import urlparse

_NONCE: contextvars.ContextVar[str] = contextvars.ContextVar("csp_nonce", default="")

# FastAPI's interactive docs load Swagger UI / ReDoc from a CDN and bootstrap
# them with an inline script. They are signed-in pages for developers; they get
# a policy of their own rather than weakening every other page.
DOCS_PATH_SUFFIXES = ("/docs", "/redoc", "/docs/oauth2-redirect")

# Identity providers the browser is sent to directly.
_OAUTH_ORIGINS = ("https://accounts.google.com", "https://appleid.apple.com")


def _market_stream_origins() -> list[str]:
    """The browser charts open Binance's websocket directly. The host comes
    from the one constant that defines it, so the policy cannot drift."""
    from services.price_action_stream import BINANCE_USDM_ROOT
    host = urlparse(BINANCE_USDM_ROOT).hostname or ""
    return [f"https://{host}", f"wss://{host}"] if host else []


def new_nonce() -> str:
    value = secrets.token_urlsafe(18)
    _NONCE.set(value)
    return value


def nonce() -> str:
    """This request's nonce ('' outside a request)."""
    return _NONCE.get()


def script(body: str) -> str:
    """An inline script the server wrote itself, marked with this request's nonce."""
    return f'<script nonce="{nonce()}">{body}</script>'


def _origin(url: Optional[str]) -> list[str]:
    """``https://x.supabase.co/...`` -> its https and wss origins."""
    if not url:
        return []
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return []
    host = p.hostname + (f":{p.port}" if p.port else "")
    ws = "wss" if p.scheme == "https" else "ws"
    return [f"{p.scheme}://{host}", f"{ws}://{host}"]


def _join(*parts: Iterable[str]) -> str:
    seen: list[str] = []
    for group in parts:
        for item in group:
            if item and item not in seen:
                seen.append(item)
    return " ".join(seen)


def policy(value: str, *, supabase_url: Optional[str] = None, frame_ancestors: str = "",
           https: bool = True, path: str = "") -> str:
    """The policy for one response. ``value`` is the response's nonce."""
    ancestors = frame_ancestors.strip() or "'self'"
    supabase = _origin(supabase_url)
    if path.endswith(DOCS_PATH_SUFFIXES):
        directives = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
            "font-src 'self' data: https://fonts.gstatic.com",
            "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly",
            "worker-src 'self' blob:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            f"frame-ancestors {ancestors}",
        ]
    else:
        directives = [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{value}'",
            "script-src-attr 'none'",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' data: https://fonts.gstatic.com",
            "img-src 'self' data: blob: https:",
            "connect-src " + _join(["'self'"], _market_stream_origins(), supabase),
            "worker-src 'self' blob:",
            "manifest-src 'self'",
            "media-src 'self'",
            "frame-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action " + _join(["'self'"], [o for o in supabase if o.startswith("http")], _OAUTH_ORIGINS),
            f"frame-ancestors {ancestors}",
        ]
    if https:
        directives.append("upgrade-insecure-requests")
    return "; ".join(directives)


# Browser features nothing on this site uses. Denying them means an injected
# script or a compromised dependency cannot ask for them either.
PERMISSIONS_POLICY = ", ".join(f"{feature}=()" for feature in (
    "accelerometer", "autoplay", "bluetooth", "browsing-topics", "camera", "display-capture",
    "geolocation", "gyroscope", "hid", "magnetometer", "microphone", "midi", "payment",
    "serial", "usb", "xr-spatial-tracking"))
