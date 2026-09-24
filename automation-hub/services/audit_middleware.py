"""ASGI middleware feeding the security audit log (services/audit_log.py).

It sits outside every other middleware, so it records requests that were
refused (a 401 from the auth wall, a 403 from the role check, a 429 from the
rate limiter) as well as the ones that succeeded -- "who tried to stop this
instance" matters as much as "who stopped it".

Only state-changing methods are recorded. Reads are not: they change nothing,
and logging every dashboard poll would bury the entries that matter.

The request body is captured as it streams past (it is never consumed ahead
of the application) and stored in redacted form: JSON and form bodies are
parsed and secret-named fields removed; anything else is summarised by size
and type. The audit write happens after the response has been sent, in a
worker thread, and a failure to write is reported on stderr -- it can never
turn a successful request into an error.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable, Optional
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool

from services.audit_log import AuditLog
from services.redaction import redact

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MAX_CAPTURE = 64 * 1024


def summarise_body(body: bytes, content_type: str, truncated: bool) -> Any:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not body:
        return None
    if truncated:
        return f"[{len(body)}+ bytes of {ctype or 'unknown type'}, too large to record]"
    if ctype == "application/json":
        try:
            return redact(json.loads(body))
        except ValueError:
            return "[unparseable JSON body]"
    if ctype == "application/x-www-form-urlencoded":
        form = {k: (v[0] if len(v) == 1 else v)
                for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
        return redact(form)
    if ctype.startswith("multipart/"):
        return f"[multipart body, {len(body)} bytes, not recorded]"
    return f"[{len(body)} bytes of {ctype or 'unknown type'}]"


def _preread_eligible(headers: dict) -> bool:
    ctype = headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        length = int(headers.get("content-length", "-1"))
    except ValueError:
        return False
    return 0 < length <= _MAX_CAPTURE and ctype in (
        "application/json", "application/x-www-form-urlencoded")


async def _read_body(receive) -> tuple[bytes, Optional[dict]]:
    """The whole body, or what arrived plus the disconnect that cut it off."""
    chunks = []
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            return b"".join(chunks), message
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(chunks), None


class AuditMiddleware:
    """``identify(scope, client_headers) -> (actor, auth)`` names the caller.

    ``client_headers`` are the headers exactly as the client sent them: inner
    middleware may add credentials to the scope (the auth wall bridges the
    control key for signed-in operators), and the audit must record how the
    caller really authenticated, not what the app added afterwards.
    """

    def __init__(self, app, *, log_factory: Callable[[], Optional[AuditLog]],
                 identify: Callable[[dict, dict], tuple[str, str]],
                 sign_in_paths: frozenset[str] | tuple[str, ...] = ()):
        self.app = app
        self.log_factory = log_factory
        self.identify = identify
        # Where a caller is anonymous by definition; the entry names the account
        # they tried to sign in as, and the status says whether it worked.
        self.sign_in_paths = frozenset(sign_in_paths)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method", "GET").upper() not in MUTATING:
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        client_headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                          for k, v in scope.get("headers", [])}
        captured = bytearray()
        truncated = False
        status = {"code": 500}

        # A small JSON or form body is read before the app runs and replayed to
        # it unchanged. Waiting for the app to read it would miss every request
        # the auth wall refuses -- those never read their body -- and a refused
        # attempt is exactly what an audit needs to show. Anything larger, or of
        # another type (uploads), streams through and is captured on the way.
        if _preread_eligible(client_headers):
            body, disconnect = await _read_body(receive)
            captured.extend(body)
            pending = [disconnect or {"type": "http.request", "body": body, "more_body": False}]

            async def app_receive():
                # A client that hung up mid-body is passed on as the disconnect
                # it was, never as a shorter request.
                return pending.pop() if pending else await receive()
        else:
            async def app_receive():
                nonlocal truncated
                message = await receive()
                if message.get("type") == "http.request":
                    chunk = message.get("body", b"")
                    if len(captured) + len(chunk) <= _MAX_CAPTURE:
                        captured.extend(chunk)
                    else:
                        truncated = True
                return message

        async def _send(message):
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, app_receive, _send)
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            await self._record(scope, client_headers, bytes(captured), truncated,
                               status["code"], duration_ms)

    async def _record(self, scope, client_headers, body, truncated, status, duration_ms):
        try:
            log = self.log_factory()
            if log is None:
                return
            try:
                # May verify a token with the auth provider: keep it off the event loop.
                actor, auth = await run_in_threadpool(self.identify, scope, client_headers)
            except Exception:  # noqa: BLE001 -- an unidentifiable caller is still recorded
                actor, auth = "unknown", "none"
            summary = summarise_body(body, client_headers.get("content-type", ""), truncated)
            if actor == "anonymous" and scope.get("path") in self.sign_in_paths and isinstance(summary, dict):
                attempted = summary.get("username") or summary.get("email")
                if isinstance(attempted, str) and attempted:
                    actor, auth = attempted[:200], "password"
            client = scope.get("client") or ("", 0)
            route = scope.get("route")
            route_path = getattr(route, "path", "") or scope.get("path", "")
            method = scope.get("method", "").upper()
            await run_in_threadpool(
                log.append,
                kind="request", actor=actor, auth=auth, ip=client[0] or "",
                user_agent=client_headers.get("user-agent", ""),
                method=method, path=scope.get("path", ""),
                query=scope.get("query_string", b"").decode("latin-1"),
                status=status, duration_ms=duration_ms,
                action=f"{method} {route_path}",
                detail={"body": summary},
            )
        except Exception as exc:  # noqa: BLE001 -- never fail the request over the audit
            print(f"[audit] entry not written: {type(exc).__name__}: {exc}"[:400],
                  file=sys.stderr, flush=True)
