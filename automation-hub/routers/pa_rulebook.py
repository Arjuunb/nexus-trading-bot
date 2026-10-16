"""Nexus PA rulebook v0.1 lab API — read-only decision evidence, no orders.

The visual Price Action Lab runs the frozen ``NativePriceActionEngine`` and
owns its own sessions, journal and paper account. This router deliberately does
not touch any of that. It answers one question against the same candle store:
given the market as it stands, what does the rulebook engine conclude, and on
what measured evidence?

Everything here is a pure function of closed candles. No lab session is
created, no trading state is written and no order is proposed to a broker --
the only store touched is the shared market-data cache the labs already read.
That is the rulebook's own constraint, "The design must not route exchange
orders", and it is also what makes these endpoints safe to poll while the labs
are running.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from bot.types import Bar
from services.pa_rulebook_v01 import (
    CONFIRM_TF,
    CONTEXT_TF,
    FLIP_RETEST_ID,
    RULEBOOK_VERSION,
    SETUP_TF,
    SR_REJECTION_ID,
    CostModel,
    PriceActionRulebookEngine,
    RulebookConfig,
    SetupState,
)

router = APIRouter(prefix="/research/pa-rulebook", tags=["research-pa-rulebook"])

_STRATEGY_CHOICES = {"rejection": (SR_REJECTION_ID,), "flip": (FLIP_RETEST_ID,),
                     "both": (SR_REJECTION_ID, FLIP_RETEST_ID)}


def _bad(exc: Exception, status: int = 400):
    raise HTTPException(status, str(exc)) from exc


def _bars(symbol: str, timeframe: str, limit: int) -> list[Bar]:
    """The lab's own candle store, resolved at call time.

    Imported inside the function rather than at module scope: webhook_api
    includes this router while it is still initialising, so a top-level import
    makes the two modules circular and which one is imported first decides
    whether the app starts.
    """
    import webhook_api as _wa

    return list(_wa.v2_market_data.bars(symbol, timeframe, limit=limit))


def _zone_row(zone) -> dict:
    return {"id": zone.id, "kind": zone.kind, "lower": zone.lower, "upper": zone.upper,
            "centre": zone.centre, "origin": zone.origin, "retired": zone.retired,
            "created_at": zone.created_at.isoformat(), "creation_atr": zone.creation_atr}


def _setup_row(setup) -> Optional[dict]:
    if setup is None:
        return None
    return {
        "id": setup.id, "strategy_id": setup.strategy_id,
        "direction": setup.direction, "state": setup.state.value,
        "zone": _zone_row(setup.zone), "setup_atr15": setup.setup_atr,
        "created_at": setup.created_at.isoformat(),
        "cancel_price": setup.cancel_price,
        "confirm_slots_used": setup.confirm_slots_used,
        "confirm_window_start": (setup.confirm_window_start.isoformat()
                                 if setup.confirm_window_start else None),
        "rejection_open_time": (setup.rejection.timestamp.isoformat()
                                if setup.rejection else None),
        "breakout_open_time": (setup.breakout.timestamp.isoformat()
                               if setup.breakout else None),
        "blocker": setup.blocker.value if setup.blocker else None,
        "evidence": setup.evidence,
    }


@router.get("/manifest")
def manifest():
    """What this engine is, and the attestation that it is still pure.

    The hash is the point. "Pure" is a property of the source, so a claim that
    the engine reads no clock and touches no IO is only worth anything next to
    the bytes it was verified against.
    """
    source = Path(__file__).resolve().parents[1] / "services" / "pa_rulebook_v01.py"
    return {
        "research_id": "NEXUS_PA_RULEBOOK_V01",
        "status": "RESEARCH_ONLY",
        "version": RULEBOOK_VERSION,
        "engine_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "strategies": [SR_REJECTION_ID, FLIP_RETEST_ID],
        "timeframes": {"context": CONTEXT_TF, "setup": SETUP_TF, "confirm": CONFIRM_TF},
        "venue": "Binance USDⓈ-M Futures",
        "signal_inputs": ["open", "high", "low", "close"],
        "volume_used_for_signals": False,
        "pure_engine": True,
        "reads_clock": False,
        "real_order_path": False,
        "live_execution_allowed": False,
        "instance_strategy_id": "pa_rulebook",
        "status_note": ("Research hypothesis, not a proven edge. Every threshold is an "
                        "initial engineering choice. No backtest or forward experiment "
                        "supports it yet."),
        "evidence": ["tests/test_pa_rulebook_v01.py",
                     "tests/test_pa_rulebook_engine.py",
                     "tests/test_pa_rulebook_instance_strategy.py",
                     "tests/test_pa_rulebook_replay.py"],
    }


@router.get("/config")
def config(symbol: str = "BTCUSDT"):
    """Chapter 18's contract, resolved. Every default persisted explicitly."""
    return {"symbol": symbol, "rulebook_version": RULEBOOK_VERSION,
            "config": asdict(RulebookConfig(symbol=symbol)),
            "costs": asdict(CostModel()), "real_execution_allowed": False}


@router.get("/state")
def state(symbol: str = "BTCUSDT",
          strategy: str = Query("both", pattern="^(both|rejection|flip)$"),
          equity: float = Query(10_000.0, gt=0),
          confirm_bars: int = Query(600, ge=50, le=3000)):
    """Run the engine to the latest closed candle and return what it sees.

    The zone registry, the regime with the evidence that produced it, the
    pending setup and the last decision's blocker. A setup that never appears
    and a setup that appears and is rejected are different problems, and the
    blocker is the only thing that tells them apart.
    """
    try:
        rulebook = RulebookConfig(symbol=symbol)
        rulebook.validate()
        context = _bars(symbol, CONTEXT_TF, rulebook.warmup_bars + 100)
        setups = _bars(symbol, SETUP_TF, rulebook.warmup_bars + 100)
        confirms = _bars(symbol, CONFIRM_TF, confirm_bars)
    except (ValueError, RuntimeError) as exc:
        _bad(exc, 503)

    missing = [name for name, rows in ((CONTEXT_TF, context), (SETUP_TF, setups),
                                       (CONFIRM_TF, confirms)) if not rows]
    if missing:
        raise HTTPException(503, {
            "state": "NO_CANDLES", "code": "NO_CANDLES", "retryable": True,
            "message": f"no cached candles for {symbol} on {', '.join(missing)}",
            "real_execution_allowed": False})
    if len(context) < rulebook.warmup_bars:
        return {"symbol": symbol, "state": "WARMING_UP",
                "required_context_bars": rulebook.warmup_bars,
                "available_context_bars": len(context),
                "real_execution_allowed": False}

    engine = PriceActionRulebookEngine(rulebook, CostModel(),
                                       strategies=_STRATEGY_CHOICES[strategy])
    engine.update_context(context)
    engine.on_setup_close(setups)
    decision = engine.on_confirm_close(confirms, equity=equity)

    plan = decision.plan
    return {
        "symbol": symbol, "rulebook_version": RULEBOOK_VERSION,
        "strategies": list(engine.strategies),
        "regime": decision.regime.value,
        "regime_evidence": engine.regime_evidence,
        "timeframes": {"context": CONTEXT_TF, "setup": SETUP_TF, "confirm": CONFIRM_TF},
        "last_closed": {CONTEXT_TF: context[-1].timestamp.isoformat(),
                        SETUP_TF: setups[-1].timestamp.isoformat(),
                        CONFIRM_TF: confirms[-1].timestamp.isoformat()},
        "zones": [_zone_row(zone) for zone in engine.zones if not zone.retired],
        "retired_zones": [_zone_row(zone) for zone in engine.zones if zone.retired],
        "consumed_zone_ids": sorted(engine.consumed_zone_ids),
        "pending_setup": _setup_row(engine.pending),
        "history": [_setup_row(setup) for setup in engine.history[-20:]],
        "decision": {
            "state": decision.state.value if decision.state else None,
            "blocker": decision.blocker.value if decision.blocker else None,
            "evidence": decision.evidence,
            "confirmed": decision.state is SetupState.CONFIRMED,
        },
        "plan": ({"accepted": plan.accepted, "direction": plan.direction,
                  "strategy_id": plan.strategy_id, "entry_bound": plan.entry_bound,
                  "stop": plan.stop, "target": plan.target,
                  "stop_distance": plan.stop_distance,
                  "stop_distance_atr": plan.stop_distance_atr,
                  "net_rr": plan.net_rr, "costs_loss": plan.costs_loss,
                  "costs_win": plan.costs_win, "quantity": plan.quantity,
                  "planned_loss": plan.planned_loss, "zone_id": plan.zone_id,
                  "blocker": plan.blocker.value if plan.blocker else None,
                  "evidence": plan.evidence} if plan else None),
        # Not a hedge: this endpoint computes evidence and nothing else. There
        # is no broker, no session and no writable state behind it.
        "real_execution_allowed": False,
        "paper_execution_allowed": False,
    }
