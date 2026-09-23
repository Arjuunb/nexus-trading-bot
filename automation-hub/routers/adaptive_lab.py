"""Adaptive MTF Trend Pullback Lab API.

Reads come from the lab's private bot manager. The chart, gate and decision
evidence are served by the same payload functions as the Instance Visual Lab
(routers/instance_visual_lab.py), pointed at that manager, so this lab shows
exactly what its bot's own strategy object and engine published.

The one write is the configuration POST (symbol, mode, risk), behind the
same control credential as every other lab configuration endpoint. Paper
only: nothing here can reach an exchange order endpoint.
"""
from __future__ import annotations

import importlib
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from routers import instance_visual_lab as visual
from services.adaptive_lab import AdaptiveLabError

router = APIRouter(prefix="/research/adaptive-lab", tags=["adaptive-lab"])


class _WebhookAPIProxy:
    """Resolve application singletons without an import-order cycle."""

    def __getattr__(self, name):
        return getattr(importlib.import_module("webhook_api"), name)


_wa = _WebhookAPIProxy()


def _lab():
    return _wa.adaptive_lab


def _bot_id() -> str:
    bot = _lab().current()
    if bot is None:
        raise HTTPException(503, {"code": "NO_BOT", "retryable": True,
                                  "message": "the lab has no bot yet; choose a mode to start one"})
    return bot.id


class AdaptiveLabConfigBody(BaseModel):
    symbol: Optional[str] = Field(default=None, min_length=3, max_length=20)
    mode: Optional[str] = None
    risk_pct: Optional[float] = None


@router.get("/status")
def status():
    return _lab().status()


@router.get("/paper")
def paper():
    return _lab().paper()


@router.post("/configuration")
def configure(body: AdaptiveLabConfigBody,
              x_webhook_secret: Optional[str] = Header(default=None)):
    _wa._check_secret(x_webhook_secret)
    try:
        return _lab().configure(symbol=body.symbol, mode=body.mode, risk_pct=body.risk_pct)
    except AdaptiveLabError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:  # the instance manager refusing, e.g. an open position
        raise HTTPException(409, str(exc)) from exc


@router.get("/state")
def state():
    return visual.state_payload(_bot_id(), manager=_lab().manager)


@router.get("/features")
def features():
    return visual.features_payload(_bot_id(), manager=_lab().manager)


@router.get("/candles")
def candles(timeframe: Optional[str] = Query(None),
            limit: int = Query(300, ge=20, le=1500)):
    return visual.candles_payload(_bot_id(), timeframe, limit, manager=_lab().manager)


@router.get("/timeline")
def timeline(limit: int = Query(100, ge=1, le=500),
             decision: Optional[str] = Query(None, pattern="^(accepted|rejected)$")):
    lab = _lab()
    return visual.timeline_payload(_bot_id(), limit, decision,
                                   manager=lab.manager, decisions=lab.decisions)
