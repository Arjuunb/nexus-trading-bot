"""Customer metadata stored only through the customer's Supabase RLS token.

This store has no SQLite cache, anonymous mirror, elevated-key path or delete
operation. A successful write requires a matching database representation.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from services.supabase_auth import SupabaseAuth, SupabaseAuthError


NAMESPACE = "settings-center"
MAX_WORKSPACE_BYTES = 200_000
_UNAVAILABLE = "Workspace service is temporarily unavailable. Please try again."


class WorkspaceError(RuntimeError):
    """A safe HTTP status and user-facing workspace failure."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_namespace(namespace: str) -> None:
    if namespace != NAMESPACE:
        raise WorkspaceError(400, "Unknown workspace namespace.")


def validate_data(data: Any) -> None:
    if not isinstance(data, dict):
        raise WorkspaceError(400, "data must be an object")
    try:
        def json_keys(value: Any) -> None:
            if isinstance(value, dict):
                if not all(isinstance(key, str) for key in value):
                    raise ValueError("JSON object keys must be strings")
                for child in value.values():
                    json_keys(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    json_keys(child)

        json_keys(data)
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise WorkspaceError(400, "data must contain finite JSON values") from None
    if len(encoded) > MAX_WORKSPACE_BYTES:
        raise WorkspaceError(413, "settings blob too large")


class CustomerWorkspaces:
    def __init__(self, auth: SupabaseAuth) -> None:
        self.auth = auth

    def _owner(self, token: str) -> str:
        if not token:
            raise WorkspaceError(401, "Sign in required")
        try:
            principal = self.auth.principal(token)
        except SupabaseAuthError:
            raise WorkspaceError(401, "Your session is invalid or has expired.") from None
        if not principal.email_confirmed:
            raise WorkspaceError(403, "Confirm your email before using your workspace.")
        try:
            return str(UUID(principal.id))
        except (ValueError, TypeError, AttributeError):
            raise WorkspaceError(503, _UNAVAILABLE) from None

    def _request(self, path: str, *, token: str, **kwargs: Any) -> Any:
        try:
            # The publishable apikey identifies the project; the user's JWT is
            # the Authorization credential enforcing auth.uid() row policies.
            # JSON transports may escape valid non-ASCII data. Keep the read
            # bounded while allowing that wire expansion of a 200000-byte blob.
            return self.auth._request(path, token=token, max_response_bytes=1_000_000, **kwargs)
        except Exception:
            # Provider diagnostics can contain paths, identifiers or tokens.
            raise WorkspaceError(503, _UNAVAILABLE) from None

    @staticmethod
    def _row(rows: Any, owner: str, namespace: str, *, missing_ok: bool) -> dict:
        if not isinstance(rows, list) or len(rows) > 1 or (not rows and not missing_ok):
            raise WorkspaceError(503, _UNAVAILABLE)
        if not rows:
            return {}
        row = rows[0]
        if (not isinstance(row, dict) or row.get("user_id") != owner
                or row.get("namespace") != namespace):
            raise WorkspaceError(503, _UNAVAILABLE)
        try:
            validate_data(row.get("data"))
        except WorkspaceError:
            raise WorkspaceError(503, _UNAVAILABLE) from None
        return row["data"]

    def get(self, token: str, namespace: str) -> dict:
        validate_namespace(namespace)
        owner = self._owner(token)
        query = urlencode({"user_id": f"eq.{owner}", "namespace": f"eq.{namespace}",
                           "select": "user_id,namespace,data", "limit": "2"})
        rows = self._request(f"/rest/v1/tradexa_customer_workspaces?{query}", token=token)
        return self._row(rows, owner, namespace, missing_ok=True)

    def put(self, token: str, namespace: str, data: dict) -> None:
        validate_namespace(namespace)
        validate_data(data)
        owner = self._owner(token)
        query = urlencode({"on_conflict": "user_id,namespace", "select": "user_id,namespace,data"})
        rows = self._request(
            f"/rest/v1/tradexa_customer_workspaces?{query}", token=token, method="POST",
            payload={"user_id": owner, "namespace": namespace, "data": data},
            prefer="resolution=merge-duplicates,return=representation",
        )
        persisted = self._row(rows, owner, namespace, missing_ok=False)
        # Python considers True == 1; JSON/database receipts must retain the
        # actual JSON value types as well as every key and value.
        if (json.dumps(persisted, sort_keys=True, ensure_ascii=False, allow_nan=False)
                != json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)):
            raise WorkspaceError(503, _UNAVAILABLE)
