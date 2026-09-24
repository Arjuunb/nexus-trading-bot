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


@router.post("/audit/export/run")
def audit_export_run(x_webhook_secret: Optional[str] = Header(default=None)):
    """Push every entry not yet exported to HUB_AUDIT_EXPORT_URL now."""
    _wa._check_secret(x_webhook_secret)
    result = _wa.audit_exporter.export_once()
    if not _wa.audit_exporter.configured:
        raise HTTPException(409, result["error"])
    return {**result, "export": _wa.audit_exporter.status()}


# ---------------------------------------------------------------- backups
@router.post("/backups/{snapshot}/verify")
def backup_verify(snapshot: str, x_webhook_secret: Optional[str] = Header(default=None)):
    """Decrypt a snapshot into a scratch directory and open every database in
    it -- proof that the backup can actually be restored."""
    _wa._check_secret(x_webhook_secret)
    import config as _cfg
    from services.backup import restore_check
    if not snapshot.replace("T", "").replace("Z", "").isdigit():
        raise HTTPException(400, "Unknown snapshot name.")
    return restore_check(str(_cfg.DATA_DIR), snapshot)


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


# ---------------------------------------------------------------- API keys
@router.get("/api-keys")
def api_keys_list(request: Request, x_webhook_secret: Optional[str] = Header(default=None)):
    """Personal API keys for the /v1 API: names, scopes, last use. Never the key."""
    _wa._check_secret(x_webhook_secret)
    from services.api_keys import API_VERSIONS, SCOPES, default_store
    return {"keys": default_store().list(_tenant(request)), "scopes": list(SCOPES),
            "versions": list(API_VERSIONS)}


@router.post("/api-keys")
def api_keys_create(request: Request, body: dict = Body(...),
                    x_webhook_secret: Optional[str] = Header(default=None)):
    """Create a key. The response's ``token`` is the only time the key is ever
    shown; only its hash is kept. Body: {name, scopes: ["read"] | ["read","control"]}."""
    _wa._check_secret(x_webhook_secret)
    from services.api_keys import default_store
    try:
        created = default_store().create(_tenant(request), str(body.get("name", "")),
                                         body.get("scopes") or ["read"],
                                         created_by=_wa.request_user(request))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    audit_log.record_change(action="api_key.create", actor=_wa.request_user(request),
                            after={k: v for k, v in created.items() if k != "token"},
                            ip=_client_ip(request))
    return created


@router.delete("/api-keys/{key_id}")
def api_keys_revoke(key_id: str, request: Request,
                    x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    from services.api_keys import default_store
    try:
        revoked = default_store().revoke(_tenant(request), key_id)
    except KeyError:
        raise HTTPException(404, "No such API key.") from None
    audit_log.record_change(action="api_key.revoke", actor=_wa.request_user(request),
                            after=revoked, ip=_client_ip(request))
    return {"key": revoked}


# ---------------------------------------------------------------- webhooks
@router.get("/webhooks")
def webhooks_list(request: Request, x_webhook_secret: Optional[str] = Header(default=None)):
    """Outbound webhook subscriptions. Never the signing secret."""
    _wa._check_secret(x_webhook_secret)
    from services.outbound_webhooks import EVENT_TYPES
    return {"webhooks": _wa.outbound_webhooks.list(_tenant(request)), "event_types": list(EVENT_TYPES)}


@router.post("/webhooks")
def webhooks_create(request: Request, body: dict = Body(...),
                    x_webhook_secret: Optional[str] = Header(default=None)):
    """Subscribe an HTTPS endpoint. The response's ``secret`` (for verifying
    Nexus-Signature) is the only time it is shown. Body: {url, events?, description?}."""
    _wa._check_secret(x_webhook_secret)
    try:
        created = _wa.outbound_webhooks.subscribe(_tenant(request), str(body.get("url", "")),
                                                  body.get("events"), description=str(body.get("description", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    audit_log.record_change(action="webhook.create", actor=_wa.request_user(request),
                            after={k: v for k, v in created.items() if k != "secret"}, ip=_client_ip(request))
    return created


@router.delete("/webhooks/{sub_id}")
def webhooks_disable(sub_id: str, request: Request, x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    try:
        disabled = _wa.outbound_webhooks.disable(_tenant(request), sub_id)
    except KeyError:
        raise HTTPException(404, "No such webhook.") from None
    audit_log.record_change(action="webhook.disable", actor=_wa.request_user(request),
                            after=disabled, ip=_client_ip(request))
    return {"webhook": disabled}


@router.get("/webhooks/{sub_id}/deliveries")
def webhooks_deliveries(sub_id: str, request: Request, limit: int = 50,
                        x_webhook_secret: Optional[str] = Header(default=None)):
    """Every delivery attempt for one subscription, newest first."""
    _wa._check_secret(x_webhook_secret)
    try:
        return {"deliveries": _wa.outbound_webhooks.deliveries(_tenant(request), sub_id, limit)}
    except KeyError:
        raise HTTPException(404, "No such webhook.") from None


@router.post("/webhooks/{sub_id}/test")
def webhooks_test(sub_id: str, request: Request, x_webhook_secret: Optional[str] = Header(default=None)):
    """Queue a webhook.test event to this endpoint and attempt it now."""
    _wa._check_secret(x_webhook_secret)
    try:
        _wa.outbound_webhooks.send_test(_tenant(request), sub_id)
    except KeyError:
        raise HTTPException(404, "No such webhook.") from None
    _wa.outbound_webhooks.deliver_due()
    return {"deliveries": _wa.outbound_webhooks.deliveries(_tenant(request), sub_id, 1)}


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
        "audit_export": _wa.audit_exporter.status(),
        "backups": _backup_status(),
        "live_routing_locked": True,
    }


def _backup_status() -> dict:
    import config as _cfg
    from services.backup import status
    try:
        return status(str(_cfg.DATA_DIR))
    except Exception as exc:  # noqa: BLE001 -- a listing problem must not hide the rest
        return {"encrypting": False, "count": 0, "problem": f"Backups could not be listed ({type(exc).__name__})."}
