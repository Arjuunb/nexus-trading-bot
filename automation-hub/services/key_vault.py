"""Exchange-key custody: envelope encryption, scope-checked on the way in.

A trading key is the one thing a user hands over, so its lifecycle is exact:

* **Checked before it is kept.** ``attach`` asks the venue what the key may
  do (services/key_scope.py) and refuses a key that can withdraw or move
  funds -- or whose scope cannot be confirmed -- before anything is written.
* **Encrypted with envelope encryption.** Each tenant has its own random
  256-bit data key (DEK). Credentials are sealed with AES-256-GCM under that
  DEK, and the DEK itself is stored only wrapped (AES-256-GCM) under the
  master key, which comes from the environment (``HUB_MASTER_KEY``) and is
  never written to disk by this module. Decrypting one tenant's secrets says
  nothing about another's; the database file alone decrypts nothing.
* **Bound to their row.** Every ciphertext carries the tenant, venue, record
  id and field name as authenticated data, so a ciphertext copied into
  another row -- or another tenant -- fails to decrypt instead of being used.
* **Never returned.** Nothing here returns a secret except ``load_active``,
  which the execution path calls in memory. Listings show a four-character
  hint and the scope, nothing more.
* **Rotated without a gap.** Attaching a new key for a venue retires the
  previous one in the same transaction; revoking removes a key from use
  immediately. Retired rows keep their metadata (not their usability) for the
  audit trail.

With no master key configured the vault refuses to store anything: failing
closed beats writing a key that could only be protected by a default.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services import key_scope

MASTER_KEY_ENV = "HUB_MASTER_KEY"
GENERATE_HINT = ('python -c "import base64,secrets;'
                 'print(base64.b64encode(secrets.token_bytes(32)).decode())"')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant_keys (
    tenant      TEXT PRIMARY KEY,
    wrapped_dek BLOB NOT NULL,
    nonce       BLOB NOT NULL,
    kek_id      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
    id               TEXT PRIMARY KEY,
    tenant           TEXT NOT NULL,
    venue            TEXT NOT NULL,
    label            TEXT NOT NULL,
    key_hint         TEXT NOT NULL,
    api_key_ct       BLOB NOT NULL,
    api_key_nonce    BLOB NOT NULL,
    api_secret_ct    BLOB NOT NULL,
    api_secret_nonce BLOB NOT NULL,
    scope_json       TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    retired_at       TEXT
);
CREATE INDEX IF NOT EXISTS credentials_tenant ON credentials(tenant, venue, status);
"""


class VaultNotConfigured(RuntimeError):
    """No usable master key: the vault will not store or read keys."""


class KeyRefused(ValueError):
    """The key failed the scope check, or its scope could not be confirmed."""

    def __init__(self, message: str, scope: Optional[dict] = None):
        super().__init__(message)
        self.scope = scope


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_master_key(value: Optional[str]) -> Optional[bytes]:
    """32 bytes from base64 (standard or URL-safe) or 64 hex characters."""
    if not value:
        return None
    value = value.strip()
    for decode in (lambda v: bytes.fromhex(v) if len(v) == 64 else b"",
                   base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decode(value)
        except (ValueError, binascii.Error):
            continue
        if len(raw) == 32:
            return raw
    raise VaultNotConfigured(f"{MASTER_KEY_ENV} must be 32 random bytes, base64-encoded. "
                             f"Generate one with: {GENERATE_HINT}")


def kek_fingerprint(kek: bytes) -> str:
    return hashlib.sha256(b"tradelogx-kek-id|" + kek).hexdigest()[:16]


def _aad(*parts: str) -> bytes:
    return "|".join(parts).encode("utf-8")


def _public(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "venue": row["venue"], "label": row["label"],
            "key_hint": row["key_hint"], "scope": json.loads(row["scope_json"] or "{}"),
            "status": row["status"], "created_at": row["created_at"],
            "retired_at": row["retired_at"]}


class KeyVault:
    def __init__(self, path: str | Path, master_key: Optional[bytes] = None):
        self.path = str(path)
        self._kek = master_key
        self._error = ""
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._c.executescript(_SCHEMA)

    @classmethod
    def from_env(cls, path: str | Path) -> "KeyVault":
        vault = cls(path)
        try:
            vault._kek = parse_master_key(os.environ.get(MASTER_KEY_ENV))
        except VaultNotConfigured as exc:
            vault._error = str(exc)
        return vault

    # ------------------------------------------------------------ status
    @property
    def configured(self) -> bool:
        return self._kek is not None

    def status(self) -> dict:
        with self._lock:
            active = self._c.execute(
                "SELECT COUNT(*) FROM credentials WHERE status='active'").fetchone()[0]
            mismatched = self._c.execute(
                "SELECT COUNT(*) FROM tenant_keys WHERE kek_id != ?",
                (kek_fingerprint(self._kek) if self._kek else "",)).fetchone()[0] if self._kek else 0
        return {
            "configured": self.configured,
            "encryption": "AES-256-GCM envelope (per-tenant data keys)",
            "master_key_id": kek_fingerprint(self._kek) if self._kek else None,
            "active_keys": active,
            "tenants_under_other_master_key": mismatched,
            "problem": self._error or ("" if self.configured else
                                       f"{MASTER_KEY_ENV} is not set; exchange keys cannot be stored. "
                                       f"Generate one with: {GENERATE_HINT}"),
            "scope_check_venues": list(key_scope.SUPPORTED_VENUES),
        }

    def _require_kek(self) -> bytes:
        if self._kek is None:
            raise VaultNotConfigured(self.status()["problem"])
        return self._kek

    # ------------------------------------------------------- data keys
    def _tenant_dek(self, tenant: str, *, create: bool) -> Optional[bytes]:
        kek = self._require_kek()
        row = self._c.execute("SELECT * FROM tenant_keys WHERE tenant=?", (tenant,)).fetchone()
        if row is None:
            if not create:
                return None
            dek = AESGCM.generate_key(bit_length=256)
            nonce = os.urandom(12)
            wrapped = AESGCM(kek).encrypt(nonce, dek, _aad("dek", tenant))
            self._c.execute("INSERT INTO tenant_keys VALUES (?,?,?,?,?)",
                            (tenant, wrapped, nonce, kek_fingerprint(kek), _now()))
            return dek
        if row["kek_id"] != kek_fingerprint(kek):
            raise VaultNotConfigured("This tenant's keys were sealed under a different master key; "
                                     "restore the original HUB_MASTER_KEY or re-wrap the vault.")
        try:
            return AESGCM(kek).decrypt(row["nonce"], row["wrapped_dek"], _aad("dek", tenant))
        except InvalidTag:
            raise VaultNotConfigured("The tenant data key failed authentication; the vault "
                                     "file or master key is not the one it was sealed with.") from None

    # ----------------------------------------------------------- write
    def attach(self, tenant: str, venue: str, api_key: str, api_secret: str, *, label: str = "",
               checker: Optional[Callable[[str, str, str], dict]] = None) -> dict:
        """Scope-check, then encrypt and store a key as the tenant's active key
        for ``venue`` (retiring any previous one). Returns metadata only."""
        self._require_kek()
        venue = (venue or "").strip().lower()
        api_key, api_secret = (api_key or "").strip(), (api_secret or "").strip()
        if not venue or len(api_key) < 8 or len(api_secret) < 8:
            raise KeyRefused("A venue, an API key and an API secret are required.")
        try:
            scope = (checker or key_scope.check)(venue, api_key, api_secret)
        except key_scope.ScopeCheckUnavailable as exc:
            raise KeyRefused(f"Key not stored: its permissions could not be confirmed. {exc}") from None
        if not scope.get("allowed"):
            raise KeyRefused("Key not stored: it " + "; it ".join(scope.get("refusals") or
                             ["has a permission this platform does not accept"])
                             + ". Create a key without withdrawal or transfer permission.", scope)
        cred_id = uuid.uuid4().hex
        with self._lock:
            self._c.execute("BEGIN IMMEDIATE")
            try:
                dek = self._tenant_dek(tenant, create=True)
                aead = AESGCM(dek)
                k_nonce, s_nonce = os.urandom(12), os.urandom(12)
                k_ct = aead.encrypt(k_nonce, api_key.encode(), _aad(tenant, venue, cred_id, "api_key"))
                s_ct = aead.encrypt(s_nonce, api_secret.encode(), _aad(tenant, venue, cred_id, "api_secret"))
                now = _now()
                self._c.execute(
                    "UPDATE credentials SET status='rotated', retired_at=? "
                    "WHERE tenant=? AND venue=? AND status='active'", (now, tenant, venue))
                self._c.execute(
                    "INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (cred_id, tenant, venue, (label or venue.title())[:80], "…" + api_key[-4:],
                     k_ct, k_nonce, s_ct, s_nonce, json.dumps(scope, sort_keys=True), "active", now))
                self._c.execute("COMMIT")
            except BaseException:
                self._c.execute("ROLLBACK")
                raise
            return _public(self._c.execute("SELECT * FROM credentials WHERE id=?", (cred_id,)).fetchone())

    def revoke(self, tenant: str, cred_id: str) -> dict:
        with self._lock:
            row = self._c.execute("SELECT * FROM credentials WHERE id=? AND tenant=?",
                                  (cred_id, tenant)).fetchone()
            if row is None:
                raise KeyError(cred_id)
            if row["status"] == "active":
                self._c.execute("UPDATE credentials SET status='revoked', retired_at=? WHERE id=?",
                                (_now(), cred_id))
            return _public(self._c.execute("SELECT * FROM credentials WHERE id=?", (cred_id,)).fetchone())

    def recheck(self, tenant: str, cred_id: str, *,
                checker: Optional[Callable[[str, str, str], dict]] = None) -> dict:
        """Ask the venue again (permissions can be changed on the venue side
        after attaching). A key that has gained a fund-movement permission is
        revoked on the spot."""
        api_key, api_secret, venue = self._decrypt(tenant, cred_id)
        scope = (checker or key_scope.check)(venue, api_key, api_secret)
        with self._lock:
            self._c.execute("UPDATE credentials SET scope_json=? WHERE id=?",
                            (json.dumps(scope, sort_keys=True), cred_id))
        if not scope.get("allowed"):
            self.revoke(tenant, cred_id)
        return self.get(tenant, cred_id)

    # ------------------------------------------------------------ read
    def list(self, tenant: str) -> list[dict]:
        with self._lock:
            rows = self._c.execute("SELECT * FROM credentials WHERE tenant=? ORDER BY created_at DESC",
                                   (tenant,)).fetchall()
        return [_public(r) for r in rows]

    def get(self, tenant: str, cred_id: str) -> dict:
        with self._lock:
            row = self._c.execute("SELECT * FROM credentials WHERE id=? AND tenant=?",
                                  (cred_id, tenant)).fetchone()
        if row is None:
            raise KeyError(cred_id)
        return _public(row)

    def _decrypt(self, tenant: str, cred_id: str) -> tuple[str, str, str]:
        with self._lock:
            row = self._c.execute("SELECT * FROM credentials WHERE id=? AND tenant=?",
                                  (cred_id, tenant)).fetchone()
            if row is None:
                raise KeyError(cred_id)
            dek = self._tenant_dek(tenant, create=False)
        if dek is None:
            raise VaultNotConfigured("No data key for this tenant.")
        aead, venue = AESGCM(dek), row["venue"]
        try:
            api_key = aead.decrypt(row["api_key_nonce"], row["api_key_ct"],
                                   _aad(tenant, venue, cred_id, "api_key")).decode()
            api_secret = aead.decrypt(row["api_secret_nonce"], row["api_secret_ct"],
                                      _aad(tenant, venue, cred_id, "api_secret")).decode()
        except InvalidTag:
            raise VaultNotConfigured("A stored credential failed authentication and was not used.") from None
        return api_key, api_secret, venue

    def load_active(self, tenant: str, venue: str) -> Optional[tuple[str, str]]:
        """The active key for a venue, decrypted in memory for the caller to use
        and drop. ``None`` when the tenant has no active key there."""
        if not self.configured:
            return None
        with self._lock:
            row = self._c.execute(
                "SELECT id FROM credentials WHERE tenant=? AND venue=? AND status='active' "
                "ORDER BY created_at DESC LIMIT 1", (tenant, (venue or "").lower())).fetchone()
        if row is None:
            return None
        api_key, api_secret, _ = self._decrypt(tenant, row["id"])
        return api_key, api_secret

    # ------------------------------------------------- master key rotation
    def rewrap(self, new_master_key: bytes) -> int:
        """Re-wrap every tenant data key under a new master key. Credentials are
        untouched (they are sealed under the data keys). Returns how many data
        keys were re-wrapped; afterwards the vault uses the new master key."""
        old = self._require_kek()
        if len(new_master_key) != 32:
            raise ValueError("The new master key must be 32 bytes.")
        count = 0
        with self._lock:
            self._c.execute("BEGIN IMMEDIATE")
            try:
                for row in self._c.execute("SELECT * FROM tenant_keys").fetchall():
                    dek = AESGCM(old).decrypt(row["nonce"], row["wrapped_dek"], _aad("dek", row["tenant"]))
                    nonce = os.urandom(12)
                    wrapped = AESGCM(new_master_key).encrypt(nonce, dek, _aad("dek", row["tenant"]))
                    self._c.execute("UPDATE tenant_keys SET wrapped_dek=?, nonce=?, kek_id=? WHERE tenant=?",
                                    (wrapped, nonce, kek_fingerprint(new_master_key), row["tenant"]))
                    count += 1
                self._c.execute("COMMIT")
            except BaseException:
                self._c.execute("ROLLBACK")
                raise
        self._kek = new_master_key
        return count


# ------------------------------------------------------------ process default
_default: Optional[KeyVault] = None
_default_lock = threading.Lock()


def default_vault() -> KeyVault:
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                from config import settings
                _default = KeyVault.from_env(settings.key_vault_path)
    return _default


def set_default_vault(vault: Optional[KeyVault]) -> None:
    global _default
    _default = vault


def new_master_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _main(argv: list[str]) -> int:
    """``python -m services.key_vault new-key``  print a fresh master key.
    ``python -m services.key_vault rewrap``     re-wrap every data key under
    the key in HUB_MASTER_KEY_NEW, then print what to set HUB_MASTER_KEY to."""
    command = argv[1] if len(argv) > 1 else ""
    if command == "new-key":
        print(new_master_key())
        return 0
    if command == "rewrap":
        new = parse_master_key(os.environ.get("HUB_MASTER_KEY_NEW"))
        if new is None:
            print("Set HUB_MASTER_KEY_NEW to the new key (python -m services.key_vault new-key).")
            return 2
        vault = default_vault()
        count = vault.rewrap(new)
        print(f"Re-wrapped {count} tenant data key(s). Now set HUB_MASTER_KEY to the value of "
              f"HUB_MASTER_KEY_NEW and restart the app.")
        return 0
    print(_main.__doc__)
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
