"""Adaptive MTF Trend Pullback Lab API.

Reads come from the lab's private bot manager. The chart, gate and decision
evidence are served by the same payload functions as the Instance Visual Lab
(routers/instance_visual_lab.py), pointed at that manager, so this lab shows
exactly what its bot's own strategy object and engine published.

Every read takes an optional ``source``: the lab's own bot (default) or the
id of a Trading Instance running this strategy, which the lab mirrors view
only from that instance's own manager, ledger and decision store.

The one write is the configuration POST (symbol, mode, risk) of the LAB bot,
behind the same control credential as every other lab configuration
endpoint; it can never reach a Trading Instance. Paper only: nothing here can
reach an exchange order endpoint.
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


_SOURCE = Query(None, max_length=64, description="'lab' (default) or a mirrored Trading Instance id")


def _target(source: Optional[str]):
    """(bot id, manager, decision store) the visual payloads read for this source."""
    lab = _lab()
    try:
        kind, manager, _ledger, bot = lab._view(source)
    except AdaptiveLabError as exc:
        raise HTTPException(404, str(exc)) from exc
    if bot is None:
        raise HTTPException(503, {"code": "NO_BOT", "retryable": True,
                                  "message": "the lab has no bot yet; choose a mode to start one"})
    # A mirrored instance's decisions live in the instances' own decision store.
    return bot.id, manager, (lab.decisions if kind == "lab" else manager.decision_store)


def _read(call):
    try:
        return call()
    except AdaptiveLabError as exc:
        raise HTTPException(404, str(exc)) from exc


class AdaptiveLabConfigBody(BaseModel):
    symbol: Optional[str] = Field(default=None, min_length=3, max_length=20)
    mode: Optional[str] = None
    risk_pct: Optional[float] = None


@router.get("/status")
def status(source: Optional[str] = _SOURCE):
    return _read(lambda: _lab().status(source))


@router.get("/paper")
def paper(source: Optional[str] = _SOURCE):
    return _read(lambda: _lab().paper(source))


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


@router.get("/live-chart")
def live_chart(window: int = Query(400, ge=20, le=1500), source: Optional[str] = _SOURCE):
    """The bot's own Binance feed: closed candles, forming candle, bid/ask/mark."""
    _target(source)  # an unknown source is a 404, not a missing feed
    try:
        return _lab().live_chart(window, source)
    except AdaptiveLabError as exc:
        raise HTTPException(503, {"code": "NO_LIVE_FEED", "retryable": True,
                                  "message": str(exc)}) from exc


@router.get("/journal")
def journal(limit: int = Query(200, ge=1, le=1000), source: Optional[str] = _SOURCE):
    """One append-only row per closed candle the bot judged, newest first."""
    return _read(lambda: _lab().journal_entries(limit, source))


@router.get("/state")
def state(source: Optional[str] = _SOURCE):
    bot_id, manager, _decisions = _target(source)
    return visual.state_payload(bot_id, manager=manager)


@router.get("/features")
def features(source: Optional[str] = _SOURCE):
    bot_id, manager, _decisions = _target(source)
    return visual.features_payload(bot_id, manager=manager)


@router.get("/candles")
def candles(timeframe: Optional[str] = Query(None),
            limit: int = Query(300, ge=20, le=1500), source: Optional[str] = _SOURCE):
    bot_id, manager, _decisions = _target(source)
    return visual.candles_payload(bot_id, timeframe, limit, manager=manager)


@router.get("/timeline")
def timeline(limit: int = Query(100, ge=1, le=500),
             decision: Optional[str] = Query(None, pattern="^(accepted|rejected)$"),
             source: Optional[str] = _SOURCE):
    bot_id, manager, decisions = _target(source)
    return visual.timeline_payload(bot_id, limit, decision, manager=manager, decisions=decisions)
