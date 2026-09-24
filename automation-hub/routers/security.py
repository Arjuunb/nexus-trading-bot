"""Security endpoints: the audit log and exchange-key custody.

Every route requires the control credential (an operator's session is bridged
to it by the auth wall). None of them ever returns a secret: the audit log is
redacted before it is written, and key custody returns metadata only -- a key
is shown as its last four characters and nothing else, ever.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import webhook_api as _wa
from services import audit_log

router = APIRouter(prefix="/security", tags=["security"])


def _log():
    log = audit_log.default_log()
    if log is None:
        raise HTTPException(503, "The audit log could not be opened; see the server log.")
    return log


@router.get("/audit")
def audit_entries(limit: int = Query(100, ge=1, le=1000), before_seq: Optional[int] = None,
                  actor: str = "", path_prefix: str = "", kind: str = "",
                  x_webhook_secret: Optional[str] = Header(default=None)):
    """Newest first. Page with ``before_seq`` = the smallest seq you have."""
    _wa._check_secret(x_webhook_secret)
    log = _log()
    return {"entries": log.list(limit=limit, before_seq=before_seq, actor=actor,
                                path_prefix=path_prefix, kind=kind),
            "head": log.head()}


@router.get("/audit/verify")
def audit_verify(x_webhook_secret: Optional[str] = Header(default=None)):
    """Recompute the whole chain and report the first entry that fails."""
    _wa._check_secret(x_webhook_secret)
    return _log().verify()


@router.get("/audit/export")
def audit_export(x_webhook_secret: Optional[str] = Header(default=None)):
    """The full chain as JSON Lines, hashes included -- keep a copy elsewhere
    and its last hash pins the head of the log."""
    _wa._check_secret(x_webhook_secret)
    log = _log()
    head = log.head()
    return StreamingResponse(
        log.export_jsonl(), media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="audit-{head["seq"]}.jsonl"',
                 "X-Audit-Head-Hash": head["hash"], "Cache-Control": "no-store"})


# ------------------------------------------------------------ key custody
def _vault():
    from services.key_vault import default_vault
    return default_vault()


def _tenant(request: Request) -> str:
    from services.tenancy import resolve_tenant
    return resolve_tenant(_wa.request_user(request))


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@router.get("/keys")
def keys_list(request: Request, x_webhook_secret: Optional[str] = Header(default=None)):
    """Attached exchange keys: venue, label, a four-character hint, scope and
    status. Never the key or the secret."""
    _wa._check_secret(x_webhook_secret)
    vault = _vault()
    return {"vault": vault.status(), "keys": vault.list(_tenant(request))}


@router.post("/keys")
def keys_attach(request: Request, body: dict = Body(...),
                x_webhook_secret: Optional[str] = Header(default=None)):
    """Attach a key: the venue is asked what it may do first, and a key that
    can withdraw or transfer funds -- or whose permissions cannot be
    confirmed -- is refused before anything is stored. Replaces (rotates) the
    venue's previous key. Body: {venue, api_key, api_secret, label?}."""
    _wa._check_secret(x_webhook_secret)
    from services.key_vault import KeyRefused, VaultNotConfigured
    vault = _vault()
    try:
        meta = vault.attach(_tenant(request), str(body.get("venue", "")),
                            str(body.get("api_key", "")), str(body.get("api_secret", "")),
                            label=str(body.get("label", "")))
    except VaultNotConfigured as exc:
        raise HTTPException(503, str(exc)) from None
    except KeyRefused as exc:
        raise HTTPException(422, {"error": str(exc), "scope": exc.scope}) from None
    audit_log.record_change(action="key.attach", actor=_wa.request_user(request),
                            after=meta, ip=_client_ip(request))
    return {"attached": meta}


@router.post("/keys/{cred_id}/check")
def keys_recheck(cred_id: str, request: Request,
                 x_webhook_secret: Optional[str] = Header(default=None)):
    """Ask the venue again. A key that has since gained a withdrawal or
    transfer permission is revoked immediately."""
    _wa._check_secret(x_webhook_secret)
    from services import key_scope
    from services.key_vault import VaultNotConfigured
    vault = _vault()
    tenant = _tenant(request)
    try:
        before = vault.get(tenant, cred_id)
        meta = vault.recheck(tenant, cred_id)
    except KeyError:
        raise HTTPException(404, "No such key.") from None
    except VaultNotConfigured as exc:
        raise HTTPException(503, str(exc)) from None
    except key_scope.ScopeCheckUnavailable as exc:
        raise HTTPException(502, f"The exchange could not be asked: {exc}") from None
    audit_log.record_change(action="key.recheck", actor=_wa.request_user(request),
                            before=before, after=meta, ip=_client_ip(request))
    return {"key": meta}


@router.delete("/keys/{cred_id}")
def keys_revoke(cred_id: str, request: Request,
                x_webhook_secret: Optional[str] = Header(default=None)):
    """Take a key out of use now. Its metadata stays for the audit trail."""
    _wa._check_secret(x_webhook_secret)
    vault = _vault()
    tenant = _tenant(request)
    try:
        before = vault.get(tenant, cred_id)
        meta = vault.revoke(tenant, cred_id)
    except KeyError:
        raise HTTPException(404, "No such key.") from None
    audit_log.record_change(action="key.revoke", actor=_wa.request_user(request),
                            before=before, after=meta, ip=_client_ip(request))
    return {"key": meta}


# ------------------------------------------------------------------ overview
@router.get("/status")
def security_status(request: Request, x_webhook_secret: Optional[str] = Header(default=None)):
    """One view of what is protecting this deployment right now."""
    _wa._check_secret(x_webhook_secret)
    from services import redaction
    log = audit_log.default_log()
    vault = _vault()
    return {
        "audit": ({"available": True, "head": log.head(), "path_note": "append-only, SHA-256 chained"}
                  if log else {"available": False}),
        "redaction": {"active": True, "live_secrets_guarded": len(redaction.known_secrets())},
        "vault": vault.status(),
        "keys": vault.list(_tenant(request)),
        "live_routing_locked": True,
    }
