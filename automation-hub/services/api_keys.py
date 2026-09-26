"""Personal API keys for the public /v1 API.

A key is shown exactly once, when it is created. Only its SHA-256 is stored,
so the database -- or a backup of it -- cannot be used to call the API. A key
carries:

* **scopes** -- ``read`` (every GET) and optionally ``control`` (closing a
  position, queuing a backtest). Control never includes live trading: live
  order routing is locked for every caller.
* **an API version** -- the ``Nexus-Version`` date it was created against.
  Requests without the header get that version, so a later release cannot
  change what an existing integration parses.
* **revocation** -- immediate, and a revoked key can never be restored.

Keys look like ``nxs_<8-char id>_<secret>``; the id part is safe to show in
listings and logs and identifies the key without revealing it.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PREFIX = "nxs_"
SCOPES = ("read", "control")
API_VERSIONS = ("2026-09-24",)
CURRENT_VERSION = API_VERSIONS[-1]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id      TEXT PRIMARY KEY,
    tenant      TEXT NOT NULL,
    name        TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    scopes      TEXT NOT NULL,
    version     TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at  TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _public(row: sqlite3.Row) -> dict:
    return {"id": row["key_id"], "name": row["name"], "scopes": row["scopes"].split(","),
            "version": row["version"], "created_by": row["created_by"],
            "created_at": row["created_at"], "last_used_at": row["last_used_at"],
            "revoked_at": row["revoked_at"], "active": row["revoked_at"] is None,
            "hint": f"{PREFIX}{row['key_id']}_…"}


class ApiKeyStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(self.path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._c.executescript(_SCHEMA)
            self._c.commit()

    def create(self, tenant: str, name: str, scopes, *, created_by: str,
               version: str = CURRENT_VERSION) -> dict:
        """Create a key. The returned ``token`` is the only time it exists in
        clear anywhere."""
        name = (name or "").strip()[:80]
        if not name:
            raise ValueError("A key needs a name, so it can be recognised and revoked later.")
        wanted = sorted({str(s).strip().lower() for s in (scopes or ["read"])})
        unknown = [s for s in wanted if s not in SCOPES]
        if unknown:
            raise ValueError(f"Unknown scope(s): {', '.join(unknown)}. Allowed: {', '.join(SCOPES)}.")
        if "control" in wanted and "read" not in wanted:
            wanted.append("read")
        if version not in API_VERSIONS:
            raise ValueError(f"Unknown API version {version}.")
        key_id = secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        with self._lock:
            self._c.execute("INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                            (key_id, tenant, name, _hash(secret), ",".join(sorted(wanted)), version,
                             str(created_by)[:120], _now()))
            self._c.commit()
            row = self._c.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
        return {**_public(row), "token": f"{PREFIX}{key_id}_{secret}"}

    def authenticate(self, token: str) -> Optional[dict]:
        """The key behind a presented token, or None. Constant-time comparison
        of the secret's hash; revoked keys never authenticate."""
        if not token or not token.startswith(PREFIX):
            return None
        body = token[len(PREFIX):]
        key_id, sep, secret = body.partition("_")
        if not sep or len(key_id) != 8 or not secret:
            return None
        with self._lock:
            row = self._c.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            if not hmac.compare_digest(row["secret_hash"], _hash(secret)):
                return None
            self._c.execute("UPDATE api_keys SET last_used_at=? WHERE key_id=?", (_now(), key_id))
            self._c.commit()
        return {**_public(row), "tenant": row["tenant"]}

    def list(self, tenant: str) -> list[dict]:
        with self._lock:
            rows = self._c.execute("SELECT * FROM api_keys WHERE tenant=? ORDER BY created_at DESC",
                                   (tenant,)).fetchall()
        return [_public(r) for r in rows]

    def revoke(self, tenant: str, key_id: str) -> dict:
        with self._lock:
            row = self._c.execute("SELECT * FROM api_keys WHERE key_id=? AND tenant=?",
                                  (key_id, tenant)).fetchone()
            if row is None:
                raise KeyError(key_id)
            if row["revoked_at"] is None:
                self._c.execute("UPDATE api_keys SET revoked_at=? WHERE key_id=?", (_now(), key_id))
                self._c.commit()
            row = self._c.execute("SELECT * FROM api_keys WHERE key_id=?", (key_id,)).fetchone()
        return _public(row)


_default: Optional[ApiKeyStore] = None
_default_lock = threading.Lock()


def default_store() -> ApiKeyStore:
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                from config import settings
                import os
                _default = ApiKeyStore(os.environ.get(
                    "HUB_API_KEYS_DB", str(Path(settings.audit_path).parent / "api_keys.db")))
    return _default


def set_default_store(store: Optional[ApiKeyStore]) -> None:
    global _default
    _default = store
