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


def _status(instance_id: str, manager=None) -> dict:
    manager = _wa.instance_manager if manager is None else manager
    try:
        return manager.status(instance_id)
    except KeyError as exc:
        raise HTTPException(404, {"code": "NO_SUCH_INSTANCE",
                                  "message": f"no instance {instance_id}"}) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, {"code": "INSTANCE_UNAVAILABLE",
                                  "message": str(exc)}) from exc


def state_payload(instance_id: str, *, manager=None) -> dict:
    """What this instance is doing, and what stands between it and an order."""
    status = _status(instance_id, manager)
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
        "feed": _feed_panel(status),
        "mtf_evidence": (engine.get("mtf_policy") or {}).get("evidence"),
        "position": status.get("current_position"),
        "last_closed_candle": market.get("last_market_data_timestamp"),
        "last_processed_candle": market.get("last_processed_candle_timestamp"),
        "data_source": market.get("data_source"),
        "real_execution_allowed": False,
        "paper_execution_allowed": False,
    }


_TIMEFRAME_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
                      "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}


def _feed_panel(status: dict) -> dict:
    """The live market facts, taken from the instance's own status contract.

    services/instance_status.py already decides what "fresh" means for this
    instance across four independent axes. Recomputing any of it here would
    give the Lab a second opinion about staleness, and the operator no way to
    tell which one the bot acted on.
    """
    feed = dict(status.get("feed") or {})
    subscription = dict(status.get("subscription") or {})
    bid, ask = feed.get("bid"), feed.get("ask")
    spread = None
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
        spread = round(float(ask) - float(bid), 10)

    # Candle countdown from the last CLOSED candle, never from wall-clock
    # guesswork: the bar the strategy will decide on next is the one after the
    # last one it closed.
    seconds_to_close = None
    period = _TIMEFRAME_SECONDS.get(str(status.get("timeframe") or ""))
    last_closed = feed.get("last_closed_candle_timestamp")
    if period and last_closed:
        from datetime import datetime, timezone
        try:
            opened = datetime.fromisoformat(str(last_closed).replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - opened).total_seconds()
            seconds_to_close = max(0, int(period - (elapsed % period)))
        except (TypeError, ValueError):
            seconds_to_close = None

    return {
        "exchange": feed.get("exchange"), "market_type": feed.get("market_type"),
        "symbol": feed.get("symbol"), "timeframe": feed.get("execution_timeframe"),
        "htf_primary": feed.get("htf_primary_timeframe"),
        "htf_secondary": feed.get("htf_secondary_timeframe"),
        "last_price": feed.get("last_trade_price"),
        "bid": bid, "ask": ask, "mark_price": feed.get("mark_price"), "spread": spread,
        "last_closed_candle": last_closed,
        "last_processed_candle": feed.get("last_processed_candle_timestamp"),
        "last_quote_timestamp": feed.get("last_quote_timestamp"),
        "last_websocket_message": feed.get("last_websocket_message_timestamp"),
        "data_age_seconds": feed.get("data_age_seconds"),
        "quote_age_seconds": feed.get("quote_age_seconds"),
        "seconds_to_candle_close": seconds_to_close,
        "candle_period_seconds": period,
        "data_source": feed.get("data_source"),
        "warmup_bars": feed.get("warmup_bars"),
        "warmup_required": feed.get("warmup_required"),
        "transport_state": subscription.get("transport_state"),
        "subscription_state": subscription.get("state"),
        "reliable": subscription.get("reliable"),
        "health_reason": subscription.get("health_reason"),
        "failing_dependency": subscription.get("failing_dependency"),
        "market_status": status.get("market_status"),
        "current_blocker": status.get("current_blocker"),
    }


def _live_strategy(instance_id: str, symbol: str, manager=None):
    """The very strategy object the run loop is driving, or None.

    Reached through the engine's published reference rather than rebuilt: a
    freshly constructed strategy would hold no zones, no pivots and no
    structure, and would quietly show an empty chart for a busy market.
    """
    manager = _wa.instance_manager if manager is None else manager
    runtime = getattr(manager, "_runtime", {}).get(instance_id)
    if not runtime:
        return None
    engine = runtime[0]
    live = getattr(engine, "_live_strategies", None) or {}
    return live.get(symbol) or live.get(str(symbol).upper())


def features_payload(instance_id: str, *, manager=None) -> dict:
    """Chart overlays read from the running strategy's own state.

    Refuses rather than returns an empty set when the runtime does not expose
    them: "this strategy sees nothing" and "we cannot see what it sees" look
    identical on a chart and mean opposite things.
    """
    from services import strategy_visual_features as features_module

    status = _status(instance_id, manager)
    strategy_id = _strategy_id_of(status)
    adapter = adapter_for(strategy_id)
    if adapter is None:
        raise HTTPException(501, {"code": "NO_VISUAL_ADAPTER", "strategy_id": strategy_id,
                                  "message": f"no visual adapter for '{strategy_id}'"})
    strategy = _live_strategy(instance_id, str(status.get("symbol") or ""), manager)
    if strategy is None:
        raise HTTPException(503, {
            "code": "STRATEGY_NOT_RUNNING", "retryable": True,
            "strategy_id": strategy_id,
            "message": ("the instance is not running a strategy worker, so it has "
                        "no live feature state to show")})
    try:
        overlays = features_module.extract(strategy_id, strategy)
    except features_module.FeatureUnavailable as exc:
        raise HTTPException(501, {
            "code": "FEATURES_NOT_EXPOSED", "strategy_id": strategy_id,
            "message": str(exc),
            "note": "The Visual Lab draws runtime evidence only; it will not invent it."},
        ) from exc

    declared = {feature.value for feature in adapter.features}
    drawn = [overlay for overlay in overlays if overlay.feature in declared]
    # A feature the adapter never declared must not reach the chart even if an
    # engine happens to publish it: the declaration is what the test suite
    # checks against the implementation.
    withheld = sorted({o.feature for o in overlays} - declared)
    return {
        "instance_id": instance_id, "strategy_id": strategy_id,
        "symbol": status.get("symbol"), "timeframe": status.get("timeframe"),
        "strategy_version": status.get("strategy_version"),
        "declared_features": sorted(declared),
        "overlays": [overlay.public() for overlay in drawn],
        "withheld_features": withheld,
        "real_execution_allowed": False,
    }


def _strategy_series(strategy, timeframe: str) -> list:
    """The candles this strategy is actually holding for ``timeframe``.

    A multi-timeframe strategy keeps every frame it was given in ``_context``;
    every strategy keeps its decision stream in ``bars``. Both are the exact
    objects the last decision saw, which is what makes them the right series
    to draw the overlays on -- overlays come from this same strategy, so a
    separately fetched provider series can disagree with them by a candle and
    put a zone in the wrong place.
    """
    if strategy is None or not timeframe:
        return []
    context = getattr(strategy, "_context", None)
    if isinstance(context, dict):
        rows = context.get(timeframe) or context.get(str(timeframe).lower())
        if rows:
            return list(rows)
    decision_tf = str(getattr(strategy, "decision_timeframe", "") or "")
    if decision_tf and decision_tf != timeframe:
        return []
    return list(getattr(strategy, "bars", None) or [])


def _strategy_timeframes(strategy, fallback: str) -> list[str]:
    """Which frames this strategy actually holds, in chart order."""
    frames: set[str] = set()
    context = getattr(strategy, "_context", None)
    if isinstance(context, dict):
        frames |= {str(key) for key, rows in context.items() if rows}
    if getattr(strategy, "bars", None):
        decision_tf = str(getattr(strategy, "decision_timeframe", "") or fallback)
        if decision_tf:
            frames.add(decision_tf)
    order = list(_TIMEFRAME_SECONDS)
    return sorted(frames, key=lambda tf: order.index(tf) if tf in order else len(order))


def _candle_rows(rows) -> list[dict]:
    return [{"t": bar.timestamp.isoformat(), "o": float(bar.open),
             "h": float(bar.high), "l": float(bar.low),
             "c": float(bar.close), "v": float(bar.volume)} for bar in rows]


def _judge(symbol: str, timeframe: str, rows) -> object:
    """The platform's one verdict on how current a series is.

    services/market_data_freshness.py is the single authority every consumer
    on the platform already answers to, so the Lab asks it rather than forming
    a second opinion about the same candle. Ages are measured from the close,
    and a series is fresh only until the next candle of that timeframe is due.
    """
    from services.live_candle_source import judge

    return judge(symbol, timeframe, rows)


def _venue_key(status: dict) -> str:
    """The live-visual venue this instance trades, defaulting to Binance USD-M."""
    from services.native_smc_live_visual import LIVE_VENUES

    market = status.get("market") or {}
    for candidate in (market.get("data_source"), status.get("exchange"),
                      status.get("venue")):
        key = str(candidate or "").strip().lower().replace(" ", "_")
        if key in LIVE_VENUES:
            return key
    return "binance_usdm"


def candles_payload(instance_id: str, timeframe: Optional[str] = None,
                    limit: int = 300, *, manager=None) -> dict:
    """The closed candles this instance decides on, freshest real source first.

    Three real sources, tried in order, each judged by the one freshness
    authority the whole platform answers to:

      1. the running strategy's own series -- the same object the overlays are
         read from, so zones, pivots and EMAs land on the candle that produced
         them. Used only while that series is FRESH; a worker that has fallen
         behind must not decide what the chart shows.
      2. a direct venue read, exactly the fetch and validation the SMC labs
         use. Fresh by construction, and on the same exchange candle grid, so
         overlays still align by timestamp.
      3. the local real-candle cache, which is only as current as the last
         /data/sync and is therefore the last thing tried, never the first.

    ``require_real=True`` on that last one is the whole point. The bundled
    sample series and the synthetic generator are legitimate for fixtures and
    must never reach a forward-paper decision or a chart that claims to show
    one -- an operator reading manufactured candles to explain a real refusal
    is worse off than one shown an error. If no real series is available this
    returns 503 rather than a plausible-looking fallback.

    Every response carries the verdict, the age and the tolerance that produced
    it, so nothing downstream has to assume the data is current -- or can
    quietly claim it is.
    """
    status = _status(instance_id, manager)
    symbol = str(status.get("symbol") or "")
    instance_tf = str(status.get("timeframe") or "")
    requested = str(timeframe or instance_tf or "")
    if not symbol or not requested:
        raise HTTPException(503, {"code": "NO_MARKET", "retryable": False,
                                  "message": "the instance declares no symbol/timeframe"})
    if requested not in _TIMEFRAME_SECONDS:
        raise HTTPException(400, {"code": "UNKNOWN_TIMEFRAME", "timeframe": requested,
                                  "message": f"unsupported timeframe '{requested}'"})

    strategy = _live_strategy(instance_id, symbol, manager)
    market = status.get("market") or {}
    venue = _venue_key(status)
    envelope = {
        "instance_id": instance_id, "symbol": symbol, "timeframe": requested,
        "instance_timeframe": instance_tf,
        "strategy_timeframes": _strategy_timeframes(strategy, instance_tf),
        "venue": venue,
        "market_data_state": market.get("market_data_status"),
        "last_closed_candle": market.get("last_market_data_timestamp"),
        "real_execution_allowed": False,
    }

    attempts: list[dict] = []

    # 1. The strategy's own series, while it is current.
    own = _strategy_series(strategy, requested)
    if own:
        verdict = _judge(symbol, requested, own)
        attempts.append({"source": "instance strategy state",
                         "freshness": verdict.to_dict()})
        if verdict.fresh:
            return {**envelope, "candles": _candle_rows(own[-limit:]),
                    "source": f"instance strategy state · {requested}",
                    "aligned_with_overlays": True,
                    "freshness": verdict.to_dict(), "attempts": attempts}

    # 2. A direct venue read -- the same path the SMC labs use.
    from services.live_candle_source import LiveCandlesUnavailable, live_series

    try:
        live = live_series(symbol, requested, venue, limit=limit)
    except LiveCandlesUnavailable as exc:
        attempts.append({"source": f"venue {venue}", "error": str(exc)})
    else:
        verdict = _judge(symbol, requested, live)
        attempts.append({"source": f"venue {venue}", "freshness": verdict.to_dict()})
        return {**envelope, "candles": _candle_rows(live),
                "source": f"venue {venue} · live closed candles",
                # Same exchange grid, so an overlay still lands on its own
                # candle; this series can simply run ahead of a lagging worker.
                "aligned_with_overlays": True,
                "strategy_series_behind": bool(own) and not _judge(
                    symbol, requested, own).fresh,
                "freshness": verdict.to_dict(), "attempts": attempts}

    # 3. The local cache of real candles, last and clearly labelled.
    try:
        from data.market_data import get_bars

        rows, source = get_bars(symbol, n=limit, timeframe=requested, require_real=True)
    except (ValueError, RuntimeError) as exc:
        attempts.append({"source": "local real-candle cache", "error": str(exc)})
        raise HTTPException(503, {
            **envelope, "code": "NO_REAL_MARKET_DATA", "retryable": True,
            "message": str(exc), "attempts": attempts,
            "note": "The Visual Lab will not substitute sample or synthetic candles."},
        ) from exc
    if not rows:
        # get_bars answers an empty series with the reason in its source string
        # ("unavailable (real data required -- run /data/sync)"). Dropping that
        # leaves the operator with a refusal and no next step.
        attempts.append({"source": "local real-candle cache", "error": source})
        raise HTTPException(503, {
            **envelope, "code": "NO_REAL_MARKET_DATA", "retryable": True,
            "source": source, "attempts": attempts,
            "message": f"no real closed candles for {symbol} {requested}: {source}",
            "note": "The Visual Lab will not substitute sample or synthetic candles."})
    verdict = _judge(symbol, requested, rows)
    attempts.append({"source": "local real-candle cache",
                     "freshness": verdict.to_dict()})
    return {**envelope, "candles": _candle_rows(rows),
            "source": f"local real-candle cache · {source}",
            "aligned_with_overlays": False,
            "freshness": verdict.to_dict(), "attempts": attempts}


def timeline_payload(instance_id: str, limit: int = 100, decision: Optional[str] = None,
                     *, manager=None, decisions=None) -> dict:
    """The recorded decisions for this instance, newest first.

    Read straight from the ``decisions`` table the engine already writes, so a
    row here is the row the runtime acted on -- same candle identity, same
    blocker, same final state. The Lab adds only the human sentence.

    Decisions are recorded where a signal exists. Candles on which the strategy
    found nothing are represented by the instance's live blocker rather than a
    row each, which is stated here because an empty timeline otherwise reads as
    "nothing happened" when it means "nothing reached a signal".
    """
    status = _status(instance_id, manager)
    store = _wa.decision_store if decisions is None else decisions
    try:
        rows = store.list(instance_id=instance_id, limit=limit, decision=decision)
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


# --------------------------------------------------------------- the routes
# Thin GET wrappers over the payload functions above. The functions take the
# manager (and decision store) to read from so another read-only lab -- the
# Adaptive MTF lab, whose private bot manager has its own database -- can
# serve the same evidence without a second copy of this logic.

@router.get("/state")
def state(instance_id: str = Query(..., min_length=1)):
    """What this instance is doing, and what stands between it and an order."""
    return state_payload(instance_id)


@router.get("/features")
def features(instance_id: str = Query(..., min_length=1)):
    """Chart overlays read from the running strategy's own state."""
    return features_payload(instance_id)


@router.get("/candles")
def candles(instance_id: str = Query(..., min_length=1),
            timeframe: Optional[str] = Query(None),
            limit: int = Query(300, ge=20, le=1500)):
    """The closed candles this instance decides on, freshest real source first."""
    return candles_payload(instance_id, timeframe, limit)


@router.get("/timeline")
def timeline(instance_id: str = Query(..., min_length=1),
             limit: int = Query(100, ge=1, le=500),
             decision: Optional[str] = Query(None, pattern="^(accepted|rejected)$")):
    """The recorded decisions for this instance, newest first."""
    return timeline_payload(instance_id, limit, decision)
