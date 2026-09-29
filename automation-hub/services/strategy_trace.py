"""Decision traces for Trading Instance strategies (Guardian Phase 2, PRD §8).

Every closed candle an instance evaluates ends in one outcome: no setup, a
setup that a gate refused, or an order. This module turns that outcome into
the ordered trace the PRD asks for::

    Candle 1 at a level         PASS
    Candle 2 rejection          PASS
    Candle 3 confirmation       PASS
    EMA 9 / EMA 33 trend        FAIL   EMA_TREND_NOT_ALIGNED
    Decision Brain              NOT REACHED
    Risk                        NOT REACHED
    Final: NO_SETUP

It decides nothing. It locates what the engine already decided:

* the strategy's own blocker code, in the strategy's declared gate sequence
  (``services/strategy_visual_registry.py`` -- the sequence the Instance
  Visual Lab shows, so the two cannot disagree);
* the pipeline stage that refused a signal, and the Decision Brain verdict
  when it was the Brain;
* the order outcome.

Everything before the failing gate passed, the failing gate failed, and
everything after it was never reached. A code the sequence does not know is
reported as unattributed rather than guessed at.

The trace reaches Guardian through ``services.guardian.emit``: queued or
dropped, never waited on, never raised. With Guardian not running nothing is
built at all.
"""
from __future__ import annotations

from typing import Optional

from services.strategy_visual_registry import (
    EXECUTION_GATES, MARKET_DATA_GATES, RISK_GATES, Gate, Stage, adapter_for,
    normalise_blocker,
)

PASS, FAIL, HELD, BYPASSED = "PASS", "FAIL", "HELD", "BYPASSED"
NOT_REACHED, NOT_APPLICABLE, UNATTRIBUTED = "NOT_REACHED", "NOT_APPLICABLE", "UNATTRIBUTED"

#: The two engine gates between a strategy's signal and the risk pipeline.
#: They belong to the engine (services/auto_engine.py::_on_signal), not to any
#: strategy, which is why the registry's per-strategy sequences omit them.
QUALITY_GATE = Gate("decision_brain", Stage.STRATEGY_ACCEPT, "Decision Brain quality gate",
                    frozenset({"BRAIN"}),
                    "No hard block, and the quality score is at or above the instance minimum.")
CONTEXT_GATE = Gate("cross_asset_context", Stage.STRATEGY_ACCEPT, "Cross-asset context gate",
                    frozenset({"CONTEXT"}),
                    "The market-context filter (for example BTC's own trend) does not oppose it.")
POST_SIGNAL: tuple[Gate, ...] = (QUALITY_GATE, CONTEXT_GATE) + RISK_GATES + EXECUTION_GATES

#: The Decision Brain's own words when the native higher-timeframe context is
#: missing (strategies/brain.py). Compared for equality, never searched for.
BRAIN_HTF_BLOCK = "native primary HTF context unavailable"
_HTF_MISSING = frozenset({"HTF_NOT_READY", "HTF_UNAVAILABLE", "MISSING_HTF_CANDLE"})
_HTF_STALE = frozenset({"STALE_HTF_CANDLE", "STALE_HTF"})

#: engine outcome kind -> the trace's final state
_FINAL = {"opened": "ENTERED", "pending": "ORDER_PENDING", "queued": "APPROVAL_REQUIRED",
          "signal": "SIGNAL_ONLY", "rejected": "REJECTED", "closed": "EXITED_ON_SIGNAL",
          "hold": "POSITION_ALREADY_ALIGNED", "error": "ERROR", "noop": "NO_ACTION"}
#: final state -> Guardian event type
EVENT_FOR_FINAL = {"ENTERED": "setup_detected", "ORDER_PENDING": "setup_detected",
                   "APPROVAL_REQUIRED": "setup_detected", "SIGNAL_ONLY": "setup_detected",
                   "REJECTED": "setup_rejected"}


def _row(gate: Gate, kind: str, state: str, code: str = "", detail: str = "") -> dict:
    return {"id": gate.id, "label": gate.label, "stage": gate.stage.value, "kind": kind,
            "state": state, "code": code or None, "detail": detail or gate.detail or None}


def _locate(gates: tuple[Gate, ...], code: str) -> Optional[int]:
    if not code:
        return None
    return next((i for i, gate in enumerate(gates) if code in gate.blockers), None)


def _positional(gates: tuple[Gate, ...], kinds: dict[str, str], failing: Optional[int],
                code: str, detail: str) -> list[dict]:
    rows = []
    for i, gate in enumerate(gates):
        kind = kinds.get(gate.id, "gate")
        if failing is None:
            rows.append(_row(gate, kind, UNATTRIBUTED))
        elif i < failing:
            rows.append(_row(gate, kind, PASS))
        elif i == failing:
            rows.append(_row(gate, kind, FAIL, code, detail))
        else:
            rows.append(_row(gate, kind, NOT_REACHED))
    return rows


def _quality(verdict, min_score: Optional[float]) -> Optional[dict]:
    if verdict is None:
        return None
    return {"score": getattr(verdict, "score", None), "min_score": min_score,
            "grade": getattr(verdict, "grade", None),
            "allowed": bool(getattr(verdict, "allowed", False)),
            "hard_blocks": list(getattr(verdict, "blocks", None) or []),
            "passed": list(getattr(verdict, "passed", None) or []),
            "weak": list(getattr(verdict, "failed", None) or [])}


def _brain_blocking(quality: Optional[dict], reason: str) -> list[dict]:
    if not quality:
        return [{"id": QUALITY_GATE.id, "code": "BRAIN", "detail": reason}]
    items = [{"id": QUALITY_GATE.id, "code": "HARD_BLOCK", "detail": block}
             for block in quality["hard_blocks"]]
    score, floor = quality.get("score"), quality.get("min_score")
    if score is not None and floor is not None and score < floor:
        items.append({"id": QUALITY_GATE.id, "code": "SCORE_BELOW_MINIMUM",
                      "detail": f"quality score {score} below the minimum {floor}"})
    return items or [{"id": QUALITY_GATE.id, "code": "BRAIN", "detail": reason}]


def instance_trace(*, strategy_id: str, blocker: Optional[str], outcome: Optional[dict],
                   signal_side: Optional[str], strategy_decision: Optional[dict] = None,
                   position_managed: bool = False, min_quality_score: Optional[float] = None,
                   htf_verified: bool = True) -> dict:
    """The ordered condition trace for one closed candle of one instance."""
    adapter = adapter_for(strategy_id)
    setup = adapter.setup_gates if adapter else ()
    kinds = {g.id: "market_data" for g in MARKET_DATA_GATES} | {g.id: "strategy" for g in setup}
    outcome = outcome or {}
    kind = str(outcome.get("kind") or "")
    code = normalise_blocker(blocker)
    report = strategy_decision or outcome.get("strategy_decision") or {}
    verdict = outcome.get("verdict")
    quality = _quality(verdict, min_quality_score)
    pre = MARKET_DATA_GATES + setup
    reason = str(outcome.get("reason") or report.get("reason") or "")
    blocking: list[dict] = []

    if signal_side is None:
        post = [_row(g, "gate", NOT_REACHED) for g in POST_SIGNAL]
        if position_managed:
            final = "POSITION_MANAGED"
            rows = ([_row(g, "market_data", PASS) for g in MARKET_DATA_GATES]
                    + [_row(g, "strategy", NOT_APPLICABLE) for g in setup])
            post = [_row(g, "gate", NOT_APPLICABLE) for g in POST_SIGNAL]
        else:
            final = "NO_SETUP"
            failing = _locate(pre, code)
            if failing is None and code:
                # A real code whose place in this strategy is not registered.
                rows = ([_row(g, "market_data", PASS) for g in MARKET_DATA_GATES]
                        + [_row(g, "strategy", UNATTRIBUTED) for g in setup])
            else:
                rows = _positional(pre, kinds, failing, code, reason)
            if code:
                blocking = [{"id": rows[failing]["id"] if failing is not None else None,
                             "code": code, "detail": reason}]
        rows += post
    else:
        rows = [_row(g, kinds[g.id], PASS) for g in pre]
        final = _FINAL.get(kind, "NO_ACTION")
        if kind == "rejected":
            stage = str(outcome.get("stage") or "").lower()
            if stage == "brain":
                failing = 0
            elif stage == "context":
                failing = 1
            else:
                from services.signal_pipeline import gate_blocker
                failing = _locate(POST_SIGNAL, normalise_blocker(gate_blocker(stage, "")))
                if failing is None:
                    failing = _locate(POST_SIGNAL, normalise_blocker(outcome.get("blocker")))
            fail_code = ("BRAIN" if failing == 0 else "CONTEXT" if failing == 1 else
                         normalise_blocker(outcome.get("blocker")) or stage.upper())
            rows += _positional(POST_SIGNAL, {}, failing, fail_code, reason)
            blocking = (_brain_blocking(quality, reason) if failing == 0 else
                        [{"id": POST_SIGNAL[failing].id if failing is not None else None,
                          "code": fail_code, "detail": reason}])
        elif kind in ("opened", "pending"):
            rows += [_row(g, "gate", HELD if kind == "pending" and g.id == "broker_accepts" else PASS,
                          "ORDER_PENDING" if kind == "pending" and g.id == "broker_accepts" else "")
                     for g in POST_SIGNAL]
        elif kind in ("queued", "signal"):
            # Operating mode is checked after the Brain and context gates and
            # before the risk pipeline (services/auto_engine.py::_on_signal).
            held = "APPROVAL_REQUIRED" if kind == "queued" else "SIGNALS_ONLY"
            for g in POST_SIGNAL:
                if g in (QUALITY_GATE, CONTEXT_GATE):
                    rows.append(_row(g, "gate", PASS))
                elif g.id == "execution_mode":
                    rows.append(_row(g, "gate", HELD, held, "operating mode, not a rule failure"))
                else:
                    rows.append(_row(g, "gate", NOT_REACHED))
        elif kind == "hold":
            rows += [_row(g, "gate", HELD, "POSITION_ALREADY_ALIGNED") if g.id == "exposure_limits"
                     else _row(g, "gate", NOT_APPLICABLE) for g in POST_SIGNAL]
        elif kind == "closed":
            rows += [_row(g, "gate", NOT_APPLICABLE, detail="an exit on an opposite signal "
                          "does not pass through the entry gates") for g in POST_SIGNAL]
        elif kind == "error":
            rows += [_row(g, "gate", FAIL, "PIPELINE_ERROR", reason) if g.id == "broker_accepts"
                     else _row(g, "gate", UNATTRIBUTED) for g in POST_SIGNAL]
            blocking = [{"id": "broker_accepts", "code": "PIPELINE_ERROR", "detail": reason}]
        else:
            rows += [_row(g, "gate", UNATTRIBUTED) for g in POST_SIGNAL]

    # The engine verifies the higher timeframe only on live data
    # (services/auto_engine.py::_refresh_multi_timeframe_context); a replay
    # never checked it, so it did not pass it either.
    if not htf_verified:
        for row in rows:
            if row["id"] in ("htf_available", "htf_fresh") and row["state"] == PASS:
                row["state"] = NOT_APPLICABLE
                row["detail"] = "not verified: replay data, the higher timeframe is fetched live only"
    # A quality gate the owner switched off is not a pass either: say what the
    # Brain would have done (services/quality_gate.py).
    if verdict is not None and quality and kind != "rejected":
        floor = quality.get("min_score")
        would_refuse = (not quality["allowed"]
                        or (floor is not None and quality.get("score") is not None
                            and quality["score"] < floor))
        if would_refuse:
            for row in rows:
                if row["id"] == QUALITY_GATE.id and row["state"] == PASS:
                    row["state"] = BYPASSED
                    row["detail"] = str((outcome.get("decision") or {}).get("reason")
                                        or "quality gate off for this instance")
    # A quality gate that never ran is not a pass.
    if verdict is None:
        for row in rows:
            if row["id"] == QUALITY_GATE.id and row["state"] == PASS:
                row["state"] = NOT_APPLICABLE
                row["detail"] = str((outcome.get("decision") or {}).get("reason")
                                    or "the quality gate did not evaluate this signal")

    stage_reached = next((r["stage"] for r in rows if r["state"] in (FAIL, HELD)), None) or next(
        (r["stage"] for r in rows if r["state"] in (NOT_REACHED, UNATTRIBUTED)), None) or "EXECUTION"
    return {
        "v": 1,
        "strategy_registered": adapter is not None,
        "direction": signal_side or report.get("direction"),
        "final": final,
        "blocker_code": code or None,
        "reason": reason or None,
        "stage_reached": stage_reached,
        "conditions": rows,
        "blocking": blocking,
        "quality": quality,
    }


def htf_problem(trace: dict) -> Optional[tuple[str, str]]:
    """(event_type, detail) when the trace shows the higher timeframe missing
    or stale, from structured codes only."""
    for row in trace.get("conditions") or []:
        if row.get("state") != FAIL:
            continue
        if row.get("code") in _HTF_MISSING:
            return "missing_htf_candle", f"{row['label']}: {row['code']}"
        if row.get("code") in _HTF_STALE:
            return "stale_htf_candle", f"{row['label']}: {row['code']}"
    quality = trace.get("quality") or {}
    if BRAIN_HTF_BLOCK in (quality.get("hard_blocks") or []):
        return "missing_htf_candle", f"Decision Brain: {BRAIN_HTF_BLOCK}"
    return None


def _source_component(engine) -> str:
    instance_id = getattr(engine, "instance_id", None)
    return f"instance:{instance_id}" if instance_id else "engine:main"


def publish_instance_trace(engine, *, symbol: str, candle_time: str, blocker: Optional[str],
                           outcome: Optional[dict], signal_side: Optional[str],
                           strategy_decision: Optional[dict], position_managed: bool,
                           decision_identity: str = "") -> bool:
    """Build and queue one candle's trace. Never raises, never waits.

    Only live engines publish: a replay runs history as fast as it can and
    is a simulation, not the platform's behaviour (``guardian_trace`` on the
    engine overrides this, for tests)."""
    try:
        from services import guardian
        if guardian.installed() is None:
            return False
        if not getattr(engine, "guardian_trace", bool(getattr(engine, "live", False))):
            return False
        strategy_id = str(getattr(engine, "strategy_key", "") or "")
        trace = instance_trace(strategy_id=strategy_id, blocker=blocker, outcome=outcome,
                               signal_side=signal_side, strategy_decision=strategy_decision,
                               position_managed=position_managed,
                               min_quality_score=getattr(engine, "min_quality_score", None),
                               htf_verified=bool(getattr(engine, "live", False)))
        trace["candle_time"] = candle_time
        trace["data"] = "live" if getattr(engine, "live", False) else "replay"
        common = dict(source_service="trading_instances", source_component=_source_component(engine),
                      instance_id=getattr(engine, "instance_id", None) or None,
                      lab_id=getattr(engine, "guardian_lab_id", None),
                      strategy_id=strategy_id or getattr(engine, "strategy_label", None),
                      strategy_version=getattr(engine, "strategy_version", None) or None,
                      symbol=symbol, timeframe=getattr(engine, "timeframe", None),
                      correlation_id=decision_identity or None)
        final = trace["final"]
        sent = guardian.emit_deferred(EVENT_FOR_FINAL.get(final, "evaluation_completed"),
                             severity="WARNING" if final == "ERROR" else "INFO",
                             decision=final, reason=trace["reason"] or trace["blocker_code"],
                             evidence=trace,
                             metadata={"blocker_code": trace["blocker_code"],
                                       "trace_source": "instance_engine",
                                       "source_ref": decision_identity or candle_time},
                             **common)
        _publish_htf_transition(engine, symbol, trace, common)
        return sent
    except Exception:  # noqa: BLE001 -- observability must never reach the bar loop
        return False


def _publish_htf_transition(engine, symbol: str, trace: dict, common: dict) -> None:
    """Report the higher timeframe going missing or stale once, and its
    return once, rather than once per candle."""
    from services import guardian
    seen = engine.__dict__.setdefault("_guardian_htf_state", {})
    problem = htf_problem(trace)
    before = seen.get(symbol)
    now = problem[0] if problem else None
    if now == before:
        return
    seen[symbol] = now
    if problem:
        guardian.emit(problem[0], severity="WARNING", reason=problem[1],
                      state_before=before, state_after=problem[0], evidence={
                          "candle_time": trace.get("candle_time"), "final": trace["final"]},
                      **{k: v for k, v in common.items() if k != "correlation_id"})
    elif before is not None:
        guardian.emit("htf_candle_recovered", severity="INFO",
                      reason="the higher timeframe is available and current again",
                      state_before=before, state_after="available",
                      **{k: v for k, v in common.items() if k != "correlation_id"})


def publish_htf_context_failure(engine, *, symbol: str, timeframe: str, code: str,
                                detail: str) -> None:
    """A mandatory higher-timeframe series could not be loaded or was stale
    (services/auto_engine.py raises right after this). Never raises."""
    try:
        from services import guardian
        if guardian.installed() is None:
            return
        event = "stale_htf_candle" if code in _HTF_STALE else "missing_htf_candle"
        seen = engine.__dict__.setdefault("_guardian_htf_state", {})
        if seen.get(symbol) == event:
            return
        before, seen[symbol] = seen.get(symbol), event
        guardian.emit(event, source_service="trading_instances",
                      source_component=_source_component(engine), severity="WARNING",
                      instance_id=getattr(engine, "instance_id", None) or None,
                      lab_id=getattr(engine, "guardian_lab_id", None),
                      strategy_id=str(getattr(engine, "strategy_key", "") or "") or None,
                      strategy_version=getattr(engine, "strategy_version", None) or None,
                      symbol=symbol, timeframe=getattr(engine, "timeframe", None),
                      state_before=before, state_after=event, reason=detail,
                      evidence={"htf_timeframe": timeframe, "code": code},
                      metadata={"htf_timeframe": timeframe})
    except Exception:  # noqa: BLE001
        return


__all__ = ["instance_trace", "htf_problem", "publish_instance_trace",
           "publish_htf_context_failure", "QUALITY_GATE", "CONTEXT_GATE", "POST_SIGNAL",
           "EVENT_FOR_FINAL"]
