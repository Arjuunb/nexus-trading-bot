"""Server-side verification and account administration for Supabase Auth.

Passwords, reset tokens, OAuth exchanges, and refresh tokens are intentionally
handled by Supabase GoTrue.  This module only verifies a presented access token
against GoTrue, reads the least-privileged application profile, and performs
the two operations that must remain server-only: account deletion and audit
logging.  The Supabase service-role key is never returned to a browser.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class SupabaseAuthError(RuntimeError):
    """A safe, user-facing Supabase authentication failure."""


@dataclass(frozen=True)
class Principal:
    id: str
    email: str
    email_confirmed: bool
    full_name: str
    role: str
    avatar_url: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class SupabaseAuth:
    """Small HTTP client so backend token validation needs no auth SDK magic."""

    def __init__(self, *, url: str | None = None, anon_key: str | None = None,
                 service_role_key: str | None = None) -> None:
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.anon_key = anon_key or os.environ.get("SUPABASE_ANON_KEY", "")
        self.service_role_key = service_role_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        self._cache: dict[str, tuple[float, Principal]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.url and self.anon_key)

    def _request(self, path: str, *, token: str, method: str = "GET",
                 payload: dict[str, Any] | None = None, service: bool = False) -> Any:
        if not self.configured:
            raise SupabaseAuthError("Supabase Auth is not configured on this server.")
        key = self.service_role_key if service else self.anon_key
        if service and not key:
            raise SupabaseAuthError("Server account administration is not configured.")
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url + path, data=body, method=method,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key if service else token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as response:  # noqa: S310 -- URL is configured by operator
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            # Do not relay provider internals or tokens to a client.
            if exc.code in (401, 403):
                raise SupabaseAuthError("Your session is invalid or has expired. Please sign in again.") from exc
            if exc.code == 404:
                raise SupabaseAuthError("Required Supabase database migrations have not been applied.") from exc
            raise SupabaseAuthError("Authentication service is temporarily unavailable.") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SupabaseAuthError("Authentication service is temporarily unavailable.") from exc

    def _profile(self, token: str, user_id: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(user_id, safe="")
        rows = self._request(
            f"/rest/v1/tradexa_profiles?id=eq.{quoted}&select=id,full_name,avatar_url,role,timezone,preferences",
            token=token,
        )
        if not isinstance(rows, list) or len(rows) != 1:
            raise SupabaseAuthError("Your TradeLogX profile is not ready. Please try again shortly.")
        return rows[0]

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        """Exchange a refresh token for a new access token.

        A Supabase access token is short-lived -- an hour by default, and
        projects may set it far lower. The session cookie held one from sign-in
        and was never renewed, so the cookie outlived the credential inside it
        and users were returned to the sign-in page mid-session while their
        refresh token was still perfectly valid.

        Returns the provider payload; ``access_token`` and ``refresh_token``
        are both rotated, so the caller must store whatever comes back.
        """
        if not refresh_token:
            raise SupabaseAuthError("A refresh token is required.")
        data = self._request(
            "/auth/v1/token?grant_type=refresh_token",
            token=self.anon_key, method="POST",
            payload={"refresh_token": refresh_token},
        )
        if not isinstance(data, dict) or not data.get("access_token"):
            raise SupabaseAuthError("Your session is invalid or has expired. Please sign in again.")
        return data

    def principal(self, access_token: str) -> Principal:
        if not access_token:
            raise SupabaseAuthError("A session token is required.")
        digest = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
        cached = self._cache.get(digest)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]
        user = self._request("/auth/v1/user", token=access_token)
        if not isinstance(user, dict) or not user.get("id") or not user.get("email"):
            raise SupabaseAuthError("Your session is invalid or has expired. Please sign in again.")
        profile = self._profile(access_token, str(user["id"]))
        metadata = user.get("user_metadata") or {}
        principal = Principal(
            id=str(user["id"]), email=str(user["email"]),
            email_confirmed=bool(user.get("email_confirmed_at")),
            full_name=str(profile.get("full_name") or metadata.get("full_name") or ""),
            avatar_url=profile.get("avatar_url"), role=str(profile.get("role") or "user"),
        )
        # A short cache reduces dashboard polling overhead without turning a
        # revoked/expired Supabase session into a long-lived local credential.
        self._cache[digest] = (now + 30, principal)
        return principal

    def update_profile(self, access_token: str, user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {k: v for k, v in patch.items() if k in {"full_name", "avatar_url", "timezone", "preferences"}}
        if not allowed:
            raise SupabaseAuthError("No supported profile fields were supplied.")
        quoted = urllib.parse.quote(user_id, safe="")
        out = self._request(f"/rest/v1/tradexa_profiles?id=eq.{quoted}", token=access_token,
                            method="PATCH", payload=allowed)
        return out[0] if isinstance(out, list) and out else allowed

    def touch_last_login(self, access_token: str, user_id: str) -> None:
        """Record a successful customer login through the user's RLS policy."""
        quoted = urllib.parse.quote(user_id, safe="")
        self._request(
            f"/rest/v1/tradexa_profiles?id=eq.{quoted}", token=access_token, method="PATCH",
            payload={"last_login": datetime.now(timezone.utc).isoformat()},
        )

    def delete_account(self, user_id: str) -> None:
        quoted = urllib.parse.quote(user_id, safe="")
        self._request(f"/auth/v1/admin/users/{quoted}", token="", method="DELETE", service=True)

    def admin_profiles(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return a bounded admin-only profile list (never password/session data)."""
        rows = self._request(
            f"/rest/v1/tradexa_profiles?select=id,full_name,role,created_at,last_login,timezone"
            f"&order=created_at.desc&limit={max(1, min(int(limit), 100))}",
            token="", service=True,
        )
        return rows if isinstance(rows, list) else []

    def audit(self, *, actor_id: str, event: str, target_id: str | None = None,
              metadata: dict[str, Any] | None = None) -> None:
        """Best-effort audit write. Auth must not become unavailable if logging is."""
        if not self.service_role_key:
            return
        try:
            self._request("/rest/v1/tradexa_audit_log", token="", method="POST", service=True,
                          payload={"actor_id": actor_id, "event": event, "target_id": target_id,
                                   "metadata": metadata or {}})
        except SupabaseAuthError:
            return
