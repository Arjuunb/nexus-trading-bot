"""Security checkup: what is protecting this deployment, and what to fix.

Every check reads real state -- the vault, the backups on disk, the audit log,
the keys and webhooks that exist -- and says pass, warn or fail with the one
thing to do about it. Nothing here is a score of how secure the platform "is";
it is a list of the protections that are switched on and the ones that are not.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

PASS, WARN, FAIL = "pass", "warn", "fail"
BACKUP_MAX_AGE = timedelta(hours=36)
STALE_KEY_AGE = timedelta(days=90)


def _check(cid: str, title: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": cid, "title": title, "status": status, "detail": detail, "fix": fix if status != PASS else ""}


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _snapshot_time(name: str) -> Optional[datetime]:
    try:
        return datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def run(*, https: bool, local: bool, vault: dict, default_session_secret: bool, default_control_key: bool,
        two_factor: Optional[bool], backups: dict, audit_available: bool, audit_export: dict,
        exchange_keys: list[dict], api_keys: list[dict], webhooks: list[dict], live_routing_locked: bool,
        now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    checks: list[dict] = []

    # 1. transport
    if https:
        checks.append(_check("https", "Encrypted connection", PASS, "This session reached the platform over HTTPS."))
    else:
        checks.append(_check("https", "Encrypted connection", WARN if local else FAIL,
                             "This session is plain HTTP." + (" (a local address)" if local else ""),
                             "Serve the platform through nginx with a certificate; DEPLOYMENT_VPS.md has the steps."))

    # 2. master key / vault
    if not vault.get("configured"):
        checks.append(_check("master_key", "Master key for secrets", FAIL,
                             vault.get("problem") or "HUB_MASTER_KEY is not set.",
                             "Generate HUB_MASTER_KEY, add it to .env and redeploy. Keep a copy off the server."))
    elif vault.get("tenants_under_other_master_key"):
        checks.append(_check("master_key", "Master key for secrets", FAIL,
                             "Some data keys are sealed under a different master key.",
                             "Restore the original HUB_MASTER_KEY, or finish the rotation with "
                             "python -m services.key_vault rewrap."))
    else:
        checks.append(_check("master_key", "Master key for secrets", PASS,
                             f"Exchange keys, webhook secrets and backups are sealed with AES-256-GCM "
                             f"(master key {vault.get('master_key_id')})."))

    # 3. default credentials
    defaults = [n for n, d in (("session secret (HUB_SECRET)", default_session_secret),
                               ("control key (HUB_CONTROL_KEY)", default_control_key)) if d]
    checks.append(_check("defaults", "No default credentials", FAIL if defaults else PASS,
                         ("Still on the development default: " + ", ".join(defaults) + ".") if defaults
                         else "The session secret and control key are set to deployment-specific values.",
                         "Set them to long random values in .env and redeploy."))

    # 4. two-factor
    if two_factor is None:
        checks.append(_check("two_factor", "Two-factor sign-in", WARN,
                             "Could not tell for this session (signed in with the control key, or through "
                             "an identity provider that manages it).",
                             "Sign in with your account and check Settings → Security → Two-factor."))
    else:
        checks.append(_check("two_factor", "Two-factor sign-in", PASS if two_factor else WARN,
                             "Your account needs a one-time code as well as the password." if two_factor
                             else "Your account signs in with a password alone.",
                             "Turn on two-factor in Settings → Security and store the recovery codes."))

    # 5. backups
    latest = backups.get("latest")
    taken = _snapshot_time(latest.get("snapshot")) if latest else None
    if not latest:
        checks.append(_check("backups", "Recent encrypted backup", WARN, "No backup has been taken yet.",
                             "Take one now from Settings → Security → Backups."))
    elif not latest.get("encrypted"):
        checks.append(_check("backups", "Recent encrypted backup", FAIL, "The latest backup is not encrypted.",
                             "Set HUB_MASTER_KEY; the next backup is sealed automatically."))
    elif taken and now - taken > BACKUP_MAX_AGE:
        hours = int((now - taken).total_seconds() // 3600)
        checks.append(_check("backups", "Recent encrypted backup", WARN, f"The latest backup is {hours} hours old.",
                             "Check that the daily task is running, or take one now."))
    else:
        checks.append(_check("backups", "Recent encrypted backup", PASS,
                             f"Latest backup {latest.get('snapshot')} is encrypted."))
    if backups.get("unencrypted_kept"):
        checks.append(_check("old_backups", "Older backups encrypted", WARN,
                             f"{backups['unencrypted_kept']} older backup(s) on disk are not encrypted.",
                             f"They age out after {backups.get('keep', 7)} days; delete them sooner if the server "
                             "is shared."))

    # 6. audit log
    checks.append(_check("audit", "Tamper-evident audit log", PASS if audit_available else FAIL,
                         "Every state-changing request is recorded in an append-only, SHA-256-chained log."
                         if audit_available else "The audit log is not available.",
                         "Check the data directory is writable and look for audit errors in the app log."))
    if not audit_export.get("configured"):
        checks.append(_check("audit_export", "Audit log copied off the server", WARN,
                             "The audit log exists only on this server.",
                             "Set HUB_AUDIT_EXPORT_URL to an HTTPS log collector you control."))
    elif audit_export.get("last_error"):
        checks.append(_check("audit_export", "Audit log copied off the server", WARN,
                             f"The last export failed: {audit_export['last_error']}",
                             "Check the collector URL and token."))
    else:
        checks.append(_check("audit_export", "Audit log copied off the server", PASS,
                             "New audit entries are shipped to your collector on a schedule."))

    # 7. exchange keys
    active = [k for k in exchange_keys if k.get("status") == "active"]
    unbound = [k for k in active if not (k.get("scope") or {}).get("ip_restricted")]
    if not active:
        checks.append(_check("exchange_keys", "Exchange keys restricted", PASS,
                             "No exchange key is attached; paper trading needs none."))
    elif unbound:
        checks.append(_check("exchange_keys", "Exchange keys restricted", WARN,
                             f"{len(unbound)} key(s) are not restricted to trusted IP addresses at the exchange.",
                             "Restrict each key to your server's address in the exchange's API settings, "
                             "then re-check it."))
    else:
        checks.append(_check("exchange_keys", "Exchange keys restricted", PASS,
                             "Every key is trade-only and IP-restricted at the exchange."))

    # 8. API keys
    stale = []
    for k in api_keys:
        if k.get("revoked_at") or "control" not in (k.get("scopes") or []):
            continue
        last = _parse(k.get("last_used_at")) or _parse(k.get("created_at"))
        if last and now - last > STALE_KEY_AGE:
            stale.append(k)
    checks.append(_check("api_keys", "No idle control keys", WARN if stale else PASS,
                         f"{len(stale)} API key(s) with control scope have not been used for 90 days."
                         if stale else "Every control-scope API key has been used in the last 90 days.",
                         "Revoke keys you no longer use in Settings → Security → API keys."))

    # 9. webhook secrets
    plain = [w for w in webhooks if w.get("active") and not w.get("secret_encrypted")]
    checks.append(_check("webhooks", "Webhook secrets encrypted", WARN if plain else PASS,
                         f"{len(plain)} webhook signing secret(s) are stored in plain text." if plain
                         else "Webhook signing secrets are sealed under the vault."
                         if webhooks else "No webhooks are configured.",
                         "Set HUB_MASTER_KEY and restart; existing secrets are sealed on start."))

    # 10. live routing
    checks.append(_check("live_routing", "Live order routing locked", PASS if live_routing_locked else WARN,
                         "Every strategy trades on a paper account; no order reaches an exchange."
                         if live_routing_locked else "Live order routing is enabled.",
                         "Keep it locked until a strategy has a verified paper record."))

    counts = {s: sum(1 for c in checks if c["status"] == s) for s in (PASS, WARN, FAIL)}
    return {"checked_at": now.isoformat(timespec="seconds"), "counts": counts, "total": len(checks),
            "checks": checks}
