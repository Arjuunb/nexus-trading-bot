"""Instance Visual Lab API — observability over a running instance, read only.

One page serves every Trading Instance strategy, so this router answers the
same four questions for all of them: which instances exist, what is the
selected one doing right now, which gates stand between it and an order, and
what did it decide on each candle it has recorded.

None of that is recomputed here. The decision state, the blocker and the gate
outcomes all come from evidence the runtime already published -- the engine's
own ``last_blocker``, the strategy's ``decision_report()``, and the persisted
``decisions`` table. services/strategy_visual_registry.py knows only where a
blocker sits in a strategy's gate sequence; it evaluates no condition itself.
That is deliberate: a Lab that re-derived the conditions would eventually
disagree with the strategy, and the operator would have no way to tell which
of the two was lying.

Every route is a GET. This module imports nothing that can place an order,
change an instance's mode, or write to any store, and a test asserts that
structurally so a later "just one POST to arm it" has to change the test.
"""
from __future__ import annotations

import importlib
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from services.strategy_visual_registry import (
    ADAPTERS,
    PIPELINE,
    adapter_for,
    current_stage,
    decision_state,
    explain,
    normalise_blocker,
    resolve_gates,
)

router = APIRouter(prefix="/research/instance-visual", tags=["instance-visual-lab"])


class _WebhookAPIProxy:
    """Resolve application singletons without an import-order cycle."""

    def __getattr__(self, name):
        return getattr(importlib.import_module("webhook_api"), name)


_wa = _WebhookAPIProxy()


def _adapter_public(adapter) -> dict:
    return {
        "strategy_id": adapter.strategy_id,
        "module": adapter.module,
        "features": [feature.value for feature in adapter.features],
        "overlays": list(adapter.overlays),
        "entry_trigger": adapter.entry_trigger,
        "invalidation": adapter.invalidation,
        "stop_model": adapter.stop_model,
        "target_model": adapter.target_model,
        "risk_requirements": adapter.risk_requirements,
        "notes": adapter.notes,
    }


def _strategy_id_of(status: dict) -> str:
    """The instance's strategy, taken from the instance record.

    Not from the engine's label and not from the page: the catalog key is the
    one identity every other component resolves through, and taking it from
    anywhere else is how a UI ends up naming a strategy that is not running.
    """
    return str(status.get("strategy_key") or status.get("strategy") or "")


def _selector_row(status: dict) -> dict:
    market = status.get("market") or {}
    engine = status.get("engine") or {}
    mtf = engine.get("mtf_policy") or {}
    strategy_id = _strategy_id_of(status)
    adapter = adapter_for(strategy_id)
    return {
        "instance_id": status.get("id"),
        "name": status.get("name") or status.get("label") or status.get("id"),
        "strategy_id": strategy_id,
        "strategy_label": status.get("strategy_label") or status.get("strategy"),
        "strategy_version": status.get("strategy_version"),
        "symbol": status.get("symbol"),
        "timeframe": status.get("timeframe"),
        "htf": mtf.get("label") or mtf.get("primary_htf"),
        "venue": status.get("exchange") or "Binance USDⓈ-M",
        "operating_mode": status.get("execution_mode") or status.get("mode"),
        "runtime_state": status.get("state"),
        "market_data_state": market.get("market_data_status"),
        "market_data_mode": market.get("market_data_mode"),
        "last_closed_candle": market.get("last_market_data_timestamp"),
        "position_open": bool(status.get("current_position")),
        # A selectable instance whose strategy has no adapter is listed and
        # marked, never hidden: an operator looking for it would otherwise be
        # told it does not exist.
        "has_visual_adapter": adapter is not None,
    }


@router.get("/strategies")
def strategies():
    """Every strategy the Lab can visualise, and exactly what it will draw."""
    return {
        "strategies": [_adapter_public(adapter)
                       for adapter in sorted(ADAPTERS.values(),
                                             key=lambda a: a.strategy_id)],
        "pipeline": [stage.value for stage in PIPELINE],
        "real_execution_allowed": False,
    }


@router.get("/instances")
def instances():
    """The selector. One row per instance, with enough state to choose one."""
    try:
        rows, _positions, _trades = _wa.instance_manager.snapshot()
    except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed into an empty list
        raise HTTPException(503, {"code": "INSTANCES_UNAVAILABLE", "message": str(exc)}) from exc
    return {"instances": [_selector_row(row) for row in rows],
            "real_execution_allowed": False}


def _status(instance_id: str) -> dict:
    try:
        return _wa.instance_manager.status(instance_id)
    except KeyError as exc:
        raise HTTPException(404, {"code": "NO_SUCH_INSTANCE",
                                  "message": f"no instance {instance_id}"}) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, {"code": "INSTANCE_UNAVAILABLE",
                                  "message": str(exc)}) from exc


@router.get("/state")
def state(instance_id: str = Query(..., min_length=1)):
    """What this instance is doing, and what stands between it and an order."""
    status = _status(instance_id)
    engine = status.get("engine") or {}
    market = status.get("market") or {}
    strategy_id = _strategy_id_of(status)
    adapter = adapter_for(strategy_id)
    if adapter is None:
        raise HTTPException(501, {
            "code": "NO_VISUAL_ADAPTER",
            "message": (f"strategy '{strategy_id}' has no visual adapter; add one to "
                        "services/strategy_visual_registry.py rather than a new page"),
            "strategy_id": strategy_id})

    blocker = normalise_blocker(engine.get("last_blocker") or market.get("last_blocker"))
    running = str(status.get("state") or "").lower() in {"running", "degraded", "rebooting"}
    position_open = bool(status.get("current_position"))
    gates = resolve_gates(adapter, blocker=blocker, position_open=position_open)
    stage = current_stage(gates, position_open=position_open)

    failing = next((g for g in gates if g.state.value == "FAIL"), None)
    waiting = next((g for g in gates if g.state.value == "WAITING"), None)
    return {
        "instance": _selector_row(status),
        "strategy": _adapter_public(adapter),
        "decision_state": decision_state(blocker=blocker, running=running,
                                         position_open=position_open).value,
        "blocker": blocker or None,
        "blocker_explanation": explain(blocker) if blocker else "",
        "blocker_at": engine.get("last_blocker_timestamp"),
        "required_next": (waiting.gate.label if waiting else
                          (failing.gate.label if failing else None)),
        "gates": [gate.public() for gate in gates],
        "pipeline": [stage_.value for stage_ in PIPELINE],
        "current_stage": stage.value,
        "mtf_evidence": (engine.get("mtf_policy") or {}).get("evidence"),
        "position": status.get("current_position"),
        "last_closed_candle": market.get("last_market_data_timestamp"),
        "last_processed_candle": market.get("last_processed_candle_timestamp"),
        "data_source": market.get("data_source"),
        "real_execution_allowed": False,
        "paper_execution_allowed": False,
    }


@router.get("/candles")
def candles(instance_id: str = Query(..., min_length=1),
            limit: int = Query(300, ge=20, le=1500)):
    """The closed candles this instance decides on, or a refusal saying why.

    ``require_real=True`` is the whole point. The bundled sample series and the
    synthetic generator are legitimate for fixtures and must never reach a
    forward-paper decision or a chart that claims to show one -- an operator
    reading manufactured candles to explain a real refusal is worse off than
    one shown an error. If the real series is unavailable this returns 503
    rather than a plausible-looking fallback.
    """
    status = _status(instance_id)
    symbol = str(status.get("symbol") or "")
    timeframe = str(status.get("timeframe") or "")
    if not symbol or not timeframe:
        raise HTTPException(503, {"code": "NO_MARKET", "retryable": False,
                                  "message": "the instance declares no symbol/timeframe"})
    try:
        from data.market_data import get_bars

        rows, source = get_bars(symbol, n=limit, timeframe=timeframe, require_real=True)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(503, {
            "code": "NO_REAL_MARKET_DATA", "retryable": True, "symbol": symbol,
            "timeframe": timeframe, "message": str(exc),
            "note": "The Visual Lab will not substitute sample or synthetic candles."},
        ) from exc
    if not rows:
        raise HTTPException(503, {
            "code": "NO_REAL_MARKET_DATA", "retryable": True, "symbol": symbol,
            "timeframe": timeframe, "message": f"no real closed candles for {symbol} {timeframe}"})
    market = status.get("market") or {}
    return {
        "instance_id": instance_id, "symbol": symbol, "timeframe": timeframe,
        "source": source,
        "candles": [{"t": bar.timestamp.isoformat(), "o": float(bar.open),
                     "h": float(bar.high), "l": float(bar.low),
                     "c": float(bar.close), "v": float(bar.volume)} for bar in rows],
        "market_data_state": market.get("market_data_status"),
        "last_closed_candle": market.get("last_market_data_timestamp"),
        "real_execution_allowed": False,
    }


@router.get("/timeline")
def timeline(instance_id: str = Query(..., min_length=1),
             limit: int = Query(100, ge=1, le=500),
             decision: Optional[str] = Query(None, pattern="^(accepted|rejected)$")):
    """The recorded decisions for this instance, newest first.

    Read straight from the ``decisions`` table the engine already writes, so a
    row here is the row the runtime acted on -- same candle identity, same
    blocker, same final state. The Lab adds only the human sentence.

    Decisions are recorded where a signal exists. Candles on which the strategy
    found nothing are represented by the instance's live blocker rather than a
    row each, which is stated here because an empty timeline otherwise reads as
    "nothing happened" when it means "nothing reached a signal".
    """
    status = _status(instance_id)
    try:
        rows = _wa.decision_store.list(instance_id=instance_id, limit=limit,
                                       decision=decision)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, {"code": "DECISIONS_UNAVAILABLE",
                                  "message": str(exc)}) from exc
    events = []
    for row in rows:
        code = normalise_blocker(row.get("blocker"))
        events.append({
            "id": row.get("id"),
            "timestamp": row.get("ts"),
            "candle_identity": row.get("decision_identity"),
            "symbol": row.get("symbol"),
            "timeframe": row.get("timeframe"),
            "strategy": row.get("strategy"),
            "side": row.get("side"),
            "regime": row.get("regime"),
            "htf_bias": row.get("htf_bias"),
            "decision": row.get("decision"),
            "final_state": row.get("final_state"),
            "gate_stage": row.get("gate_stage"),
            "blocker": code or None,
            "blocker_explanation": explain(code) if code else "",
            "reason": row.get("reason"),
            "passed_rules": row.get("passed_rules"),
            "failed_rules": row.get("failed_rules"),
            "components": row.get("components"),
            "executed": row.get("executed"),
        })
    accepted = next((e for e in events if e["decision"] == "accepted"), None)
    rejected = next((e for e in events if e["decision"] == "rejected"), None)
    filled = next((e for e in events if e["executed"]), None)
    return {
        "instance_id": instance_id,
        "symbol": status.get("symbol"),
        "events": events,
        "focus": {"last_accepted": accepted, "last_rejected": rejected,
                  "last_trade": filled},
        "coverage": ("Decisions are recorded once a signal exists. Candles where the "
                     "strategy found no setup are reported by the instance's live "
                     "blocker, not as a row each."),
        "real_execution_allowed": False,
    }
