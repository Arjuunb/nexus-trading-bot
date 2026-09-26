"""What an exchange API key is allowed to do, asked of the exchange itself.

Encryption protects a key at rest. Scope decides what a leaked key could do.
A key that can withdraw or move funds out of the trading account turns a leak
into a loss of everything in it, and nothing this platform does needs that
permission -- so such a key is refused, at the moment it is attached, before
it is stored anywhere.

The check asks the venue rather than trusting what the user ticked: for
Binance, ``GET /sapi/v1/account/apiRestrictions`` (signed with the key) reports
the key's real permissions. If the venue cannot be asked -- network down,
region blocked, bad signature -- the key is refused too: a scope that could
not be confirmed is not a safe scope.

Only Binance is supported, because it is the only venue the platform connects
to today. Any other venue is refused with that reason rather than stored
unchecked.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

BINANCE_RESTRICTIONS_URL = "https://api.binance.com/sapi/v1/account/apiRestrictions"

# Permissions that let a key move money out of the trading account. Any one
# of them makes the key unacceptable.
_FUND_MOVEMENT = {
    "enableWithdrawals": "can withdraw funds",
    "enableInternalTransfer": "can transfer funds to other accounts",
    "permitsUniversalTransfer": "can move funds between wallets",
}
_TRADING = ("enableFutures", "enableSpotAndMarginTrading", "enableMargin",
            "enablePortfolioMarginTrading", "enableVanillaOptions")

SUPPORTED_VENUES = ("binance",)


class ScopeCheckUnavailable(RuntimeError):
    """The venue could not be asked what the key may do."""


def _http_get(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- fixed https URL
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def binance_restrictions(api_key: str, api_secret: str, *, timeout: float = 10.0,
                         http_get: Optional[Callable[[str, dict, float], tuple[int, bytes]]] = None,
                         now_ms: Optional[int] = None) -> dict:
    """The raw ``apiRestrictions`` document for this key."""
    query = urllib.parse.urlencode({"timestamp": now_ms or int(time.time() * 1000),
                                    "recvWindow": 10000})
    signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BINANCE_RESTRICTIONS_URL}?{query}&signature={signature}"
    try:
        status, body = (http_get or _http_get)(url, {"X-MBX-APIKEY": api_key}, timeout)
    except Exception as exc:  # noqa: BLE001 -- any transport failure means "could not confirm"
        raise ScopeCheckUnavailable(f"Binance could not be reached: {type(exc).__name__}") from None
    try:
        doc = json.loads(body or b"{}")
    except ValueError:
        doc = {}
    if status != 200:
        detail = doc.get("msg") if isinstance(doc, dict) else None
        raise ScopeCheckUnavailable(f"Binance refused the scope check (HTTP {status}"
                                    + (f": {detail}" if detail else "") + ")")
    if not isinstance(doc, dict) or "enableWithdrawals" not in doc:
        raise ScopeCheckUnavailable("Binance answered without the key's permissions")
    return doc


def evaluate(restrictions: dict) -> dict:
    """Turn the venue's permission flags into a decision.

    ``allowed`` is False when any fund-movement permission is on. IP binding
    is reported as a warning, not a refusal: it is strongly recommended, but
    only the account owner can set it, on the venue's side.
    """
    refusals = [why for flag, why in _FUND_MOVEMENT.items() if restrictions.get(flag)]
    trading = [flag for flag in _TRADING if restrictions.get(flag)]
    warnings = []
    if not restrictions.get("ipRestrict"):
        warnings.append("The key is not restricted to trusted IP addresses on the exchange.")
    return {
        "allowed": not refusals,
        "refusals": refusals,
        "warnings": warnings,
        "read_only": not trading,
        "can_trade": trading,
        "ip_restricted": bool(restrictions.get("ipRestrict")),
        "can_read": bool(restrictions.get("enableReading")),
        "checked_at": int(time.time()),
    }


def check(venue: str, api_key: str, api_secret: str, **kw) -> dict:
    """Ask the venue and decide. Raises ``ScopeCheckUnavailable`` when the
    question cannot be answered."""
    venue = (venue or "").lower()
    if venue not in SUPPORTED_VENUES:
        raise ScopeCheckUnavailable(
            f"Key scope cannot be verified for {venue or 'this venue'} yet; only Binance is supported.")
    return {"venue": venue, **evaluate(binance_restrictions(api_key, api_secret, **kw))}
