"""Secret redaction at the point data leaves the process.

Redaction used to be the job of each endpoint: whoever wrote a handler had to
remember not to return a key, and whoever wrote a log line had to remember not
to print one. That fails by omission -- the one endpoint nobody thought about
is the one that leaks. This module is applied where the data crosses the
boundary instead:

* ``RedactionMiddleware`` rewrites every JSON response body, whatever route
  produced it and whether it returned a dict or its own ``JSONResponse``;
* ``redact`` / ``scrub_text`` are applied to ledger log lines and audit
  entries before they are stored.

Two independent rules, so either one alone still catches a leak:

1. **By name.** A string stored under a secret-looking key (``password``,
   ``api_secret``, ``apiKey``, ``x-webhook-secret``, ``access_token`` ...)
   is replaced with ``[redacted]``. Booleans, numbers and nulls under those
   names pass through, so ``"api_key_set": true`` style status fields keep
   working, and ``"api_key": null`` still says "not configured".
2. **By value.** The process's own live secrets -- every environment
   variable whose name marks it as a key, secret, token or password -- are
   replaced wherever they appear, under any key, inside any string. So is
   anything shaped like a bearer token, an Anthropic key or a
   ``secret=...`` query parameter.

Endpoints whose whole purpose is to hand a credential to its owner (the login
token, the 2FA enrolment secret) are exempt from rule 1 only. Rule 2 applies
everywhere: no response ever needs to contain the control key.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable

REDACTED = "[redacted]"

# Key names, after camelCase -> snake_case and "-" -> "_". Anchored on the
# END of the name so status fields such as "api_key_set" or "secret_count"
# and counters such as "input_tokens" are not caught.
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:password|passwd|passphrase|secret|api_?key|api_?secret|"
    r"private_?key|access_?token|refresh_?token|auth_?token|session_?token|"
    r"id_?token|token|authorization|cookie|set_cookie|credentials?|"
    r"client_?secret|webhook_?secret|admin_?key|control_?key|secret_?key|"
    r"master_?key|service_?role_?key)$")

_CAMEL = re.compile(r"(?<=[a-z0-9])([A-Z])")

# Environment variables holding live secrets. Public-by-design values (the
# Supabase anon key is shipped to every browser) are excluded so scrubbing
# them cannot break the sign-in page.
_SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|PASS|WEBHOOK_URL|DSN)$")
_PUBLIC_ENV = re.compile(r"(ANON|PUBLIC|PUBLISHABLE)")
_MIN_SECRET_LEN = 12  # shorter values would match ordinary words

# Credential shapes that are secrets wherever they turn up.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), REDACTED),
    (re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/\-]{16,}=*", re.I), r"\1" + REDACTED),
    (re.compile(r"(?i)\b((?:api[_-]?key|api[_-]?secret|secret|password|passwd|token|signature)=)"
                r"[^\s&\"']{6,}"), r"\1" + REDACTED),
)

# Cheap byte-level triggers: a JSON body containing none of these, and none of
# the live secret values, is returned untouched without being parsed.
_NAME_TRIGGERS = (b"password", b"passwd", b"passphrase", b"secret", b"api_key", b"apikey",
                  b"token", b"authorization", b"cookie", b"credential", b"private_key",
                  b"privatekey", b"admin_key", b"control_key", b"master_key", b"sk-ant-",
                  b"bearer ")

_known: tuple[str, ...] | None = None


def normalise_name(name: str) -> str:
    return _CAMEL.sub(r"_\1", str(name)).lower().replace("-", "_").replace(" ", "_")


def is_secret_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_SECRET_NAME.search(normalise_name(name)))


def known_secrets() -> tuple[str, ...]:
    """The live secret values of this process, longest first (so a secret that
    contains another is replaced whole)."""
    global _known
    if _known is None:
        values = {v for k, v in os.environ.items()
                  if _SECRET_ENV.search(k.upper()) and not _PUBLIC_ENV.search(k.upper())
                  and isinstance(v, str) and len(v.strip()) >= _MIN_SECRET_LEN}
        try:  # the configured credentials, including their dev defaults
            from config import settings
            for v in (settings.admin_key, settings.webhook_secret, settings.secret_key):
                if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN:
                    values.add(v)
        except Exception:  # noqa: BLE001 -- redaction must work without config
            pass
        _known = tuple(sorted((v.strip() for v in values), key=len, reverse=True))
    return _known


def refresh_known_secrets(extra: Iterable[str] = ()) -> None:
    """Re-read the environment (tests, or after a key is rotated in place)."""
    global _known
    _known = None
    if extra:
        _known = tuple(sorted(set(known_secrets()) | {e for e in extra if len(e) >= _MIN_SECRET_LEN},
                              key=len, reverse=True))


def scrub_text(text: str) -> str:
    """Remove live secret values and credential-shaped substrings."""
    if not isinstance(text, str) or not text:
        return text
    for secret in known_secrets():
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact(value: Any, *, by_name: bool = True, _depth: int = 0) -> Any:
    """A copy of ``value`` with secrets removed (see the module docstring)."""
    if _depth > 64:  # pathological nesting -- refuse rather than recurse forever
        return REDACTED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if by_name and is_secret_name(k) and isinstance(v, str) and v:
                out[k] = REDACTED
            else:
                out[k] = redact(v, by_name=by_name, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, by_name=by_name, _depth=_depth + 1) for v in value]
    return value


def might_contain_secret(body: bytes) -> bool:
    lowered = body.lower()
    if any(t in lowered for t in _NAME_TRIGGERS):
        return True
    return any(s.encode("utf-8") in body for s in known_secrets())


def redact_json_bytes(body: bytes, *, by_name: bool = True) -> bytes:
    """Redact a serialised JSON document. Returns the SAME object when nothing
    needed changing, so callers can skip rewriting headers."""
    if not body or not might_contain_secret(body):
        return body
    try:
        doc = json.loads(body)
    except ValueError:
        # Not valid JSON despite the content type: still never let a live
        # secret through.
        text = body.decode("utf-8", "replace")
        cleaned = scrub_text(text)
        return body if cleaned == text else cleaned.encode("utf-8")
    cleaned_doc = redact(doc, by_name=by_name)
    if cleaned_doc == doc:
        return body
    return json.dumps(cleaned_doc, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


class RedactionMiddleware:
    """ASGI middleware: redact every ``application/json`` response body.

    ``credential_paths`` are the endpoints that exist to return a credential to
    its owner; they skip name-based redaction (value-based still applies).
    Non-JSON responses -- HTML, files, event streams -- pass straight through.
    """

    def __init__(self, app, *, credential_paths: Iterable[str] = ()):
        self.app = app
        self.credential_paths = frozenset(credential_paths)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        by_name = path not in self.credential_paths
        state: dict[str, Any] = {"start": None, "passthrough": False, "chunks": []}

        async def _send(message):
            kind = message.get("type")
            if kind == "http.response.start":
                ctype = b""
                for k, v in message.get("headers", []):
                    if k.lower() == b"content-type":
                        ctype = v.lower()
                        break
                if not ctype.startswith(b"application/json"):
                    state["passthrough"] = True
                    await send(message)
                    return
                state["start"] = message
                return
            if kind == "http.response.body" and not state["passthrough"] and state["start"] is not None:
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(state["chunks"])
                try:
                    new_body = redact_json_bytes(body, by_name=by_name)
                except Exception:  # noqa: BLE001 -- a redaction fault must not leak the original
                    new_body = json.dumps({"error": "Response withheld: it could not be checked for secrets."}).encode()
                start = state["start"]
                if new_body is not body:
                    headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
                    headers.append((b"content-length", str(len(new_body)).encode()))
                    start = {**start, "headers": headers}
                await send(start)
                await send({"type": "http.response.body", "body": new_body})
                return
            await send(message)

        await self.app(scope, receive, _send)
