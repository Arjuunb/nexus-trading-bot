"""The owner's controls over Guardian's research and reasoning (PRD §38, §43).

The only POST routes Guardian has. Each needs the control credential, and
none of them touches a strategy, a risk limit, an order, a position or the
paper/live mode:

* an owner action changes a hypothesis's standing in Guardian's own research
  store, logged append-only. APPROVE_FOR_DEVELOPMENT means "approved for a
  person to develop"; production changes only when a person implements,
  tests and deploys it;
* a question to the reasoning layer sends Guardian's secret-free evidence pack
  to the model and records the answer; the answer is advice, never executed.

There is deliberately no "optimize production" or "apply" endpoint.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

import webhook_api as _wa

router = APIRouter()


class OwnerActionBody(BaseModel):
    action: str = Field(..., max_length=40)
    note: str = Field("", max_length=2000)


class AskBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)


@router.post("/guardian/research/{hypothesis_id}/action")
def guardian_owner_action(hypothesis_id: int, body: OwnerActionBody,
                          x_webhook_secret: Optional[str] = Header(default=None)) -> dict:
    _wa._check_secret(x_webhook_secret)
    try:
        return _wa.guardian.research.owner_action(hypothesis_id, body.action, note=body.note)
    except KeyError:
        raise HTTPException(404, "no such hypothesis") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/guardian/reasoning/ask")
def guardian_ask(body: AskBody, x_webhook_secret: Optional[str] = Header(default=None)) -> dict:
    _wa._check_secret(x_webhook_secret)
    import services.guardian_reasoning as reasoning
    try:
        return reasoning.ask(_wa.guardian, body.question, research=_wa.guardian.research)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
