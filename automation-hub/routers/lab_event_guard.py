"""News blackout switches for the research labs (services/lab_event_guard.py).

Reads need a signed-in session like every other lab read; the switch also
needs the control credential, like every lab configuration write.
"""
from __future__ import annotations

import importlib
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from services.lab_event_guard import LABS, lab_key

router = APIRouter(prefix="/research/event-guard", tags=["research"])


class _WebhookAPIProxy:
    """Resolve application singletons without an import-order cycle."""

    def __getattr__(self, name):
        return getattr(importlib.import_module("webhook_api"), name)


_wa = _WebhookAPIProxy()


class LabEventGuardUpdate(BaseModel):
    enabled: bool


def _guard():
    guard = getattr(importlib.import_module("webhook_api"), "lab_event_guard", None)
    if guard is None:
        raise HTTPException(503, "News blackout is not available on this server")
    return guard


def _state(lab: str) -> dict:
    return {"lab": lab, "label": LABS[lab], **_guard().state(lab_key(lab))}


@router.get("")
def lab_event_guards():
    """Every lab's switch, and what the economic calendar says right now."""
    return {"labs": [_state(lab) for lab in LABS]}


@router.get("/{lab}")
def lab_event_guard(lab: str):
    if lab not in LABS:
        raise HTTPException(404, "Unknown lab")
    return _state(lab)


@router.patch("/{lab}")
def update_lab_event_guard(lab: str, body: LabEventGuardUpdate,
                           x_webhook_secret: Optional[str] = Header(default=None)):
    """Switch one lab's news blackout on or off. Applies to its next entry;
    open positions keep their stops and targets, manual orders are not
    affected."""
    _wa._check_secret(x_webhook_secret)
    if lab not in LABS:
        raise HTTPException(404, "Unknown lab")
    _guard().set(lab_key(lab), body.enabled, by="control")
    try:
        _wa.ledger.log(level="info", stage="event_guard",
                       message=f"{LABS[lab]} news blackout {'on' if body.enabled else 'off'}")
    except Exception:  # noqa: BLE001 -- the switch is saved; the log line is a courtesy
        pass
    return _state(lab)
