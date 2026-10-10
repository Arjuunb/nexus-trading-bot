"""Decision Journal — turns each bot trade into a full, explainable record.

Captures the REAL decision context the bot produces and would otherwise
discard: the brain's actual reads at entry, every pipeline risk-gate result,
the market snapshot the brain computed, the exit reason, and — at close — a
deterministic review + evolution note derived only from what actually
happened. Nothing is invented; a read the strategy did not compute is
recorded as "Not checked".

Pure builders (map_checklist / grade_trade / build_review / build_evolution)
are unit-tested; the ``DecisionJournal`` orchestrator writes to JournalStore.
"""
from __future__ import annotations

from functools import wraps
from typing import Optional

from data.journal_store import EARLY_SIGNAL_MAX, EVIDENCE_MIN, JournalStore


def _atomic_capture(method):
    """Keep one journal observation durable as a complete unit on retries."""
    @wraps(method)
    def capture(self, *args, **kwargs):
        with self.store.transaction():
            return method(self, *args, **kwargs)
    return capture

# friendly names for the pipeline's risk/safety gates (the exact rule keys the
# SignalPipeline emits as Steps). Anything not listed still shows by its key.
_GATE_NAMES = {
    "controls": "Trading controls active", "market_quality": "Market data / microstructure OK",
    "dedup": "Not a duplicate alert", "correlation": "Correlated-exposure safe",
    "event_risk": "No high-impact event blackout", "learning": "No learned block",
    "context": "Cross-asset (BTC trend) OK", "risk_guard": "Drawdown / position-count safe",
    "strategy_health": "Strategy-health entry throttle",
    "trading_day": "Allowed trading day", "session": "Within trading session",
    "daily_loss": "Daily loss limit safe", "weekly_loss": "Weekly loss limit safe",
    "cooldown": "Post-loss cooldown clear", "max_trades": "Max trades/day safe",
    "portfolio_exposure": "Max portfolio exposure safe", "risk": "Position sized",
    "exposure": "Per-trade exposure capped", "execution": "Order executed",
}
# reads the DecisionBrain genuinely does NOT compute — honestly "Not checked"
# (they belong to the Supply/Demand SMC strategy, not the trend brain).
_BRAIN_NOT_CHECKED = ["Supply/demand zone", "Fair-value gap (FVG)",
                      "Break of structure (BOS)", "Change of character (CHoCH)",
                      "Liquidity sweep"]


def _sizing_event_detail(sizing: dict) -> str:
    """Compact, factual timeline text from the server-calculated receipt."""
    if not sizing:
        return "Position-size receipt was not captured for this legacy entry."
    base = sizing.get("base_risk_pct")
    effective = sizing.get("effective_risk_pct")
    units = sizing.get("filled_size", sizing.get("accepted_size"))
    notional = sizing.get("filled_notional", sizing.get("accepted_notional"))
    text = f"risk {base}% → {effective}%"
    if units is not None:
        text += f"; filled {units} units"
    if notional is not None:
        text += f" ({notional} notional)"
    return text


def _quality_event_detail(verdict: dict) -> str:
    """Describe the actual accepted TradeBrain verdict without recomputing it."""
    if not verdict:
        return "Quality gate stood down (disabled or still warming up)."
    score = verdict.get("score")
    regime = verdict.get("regime") or "unknown regime"
    return f"score {score} · {regime} · accepted"


def map_checklist(steps: list, brain_checklist: Optional[list]) -> dict:
    """Unified checklist split into entry-quality reads and risk/safety gates.
    ``steps`` are pipeline Steps (rule/passed/detail dicts); ``brain_checklist``
    is the strategy's own reads. Statuses: Passed / Failed / Neutral / Not checked."""
    entry = list(brain_checklist or [])
    for name in _BRAIN_NOT_CHECKED:
        if not any(c.get("name") == name for c in entry):
            entry.append({"name": name, "status": "Not checked",
                          "detail": "not part of this strategy"})
    risk = []
    for st in steps or []:
        rule = st.get("rule") if isinstance(st, dict) else getattr(st, "rule", "")
        passed = st.get("passed") if isinstance(st, dict) else getattr(st, "passed", True)
        detail = st.get("detail") if isinstance(st, dict) else getattr(st, "detail", "")
        risk.append({"name": _GATE_NAMES.get(rule, rule), "rule": rule,
                     "status": "Passed" if passed else "Failed", "detail": detail})
    return {"entry_reads": entry, "risk_gates": risk}


def grade_trade(*, result: str, actual_rr: float, planned_rr: float,
                followed_strategy: bool, risk_ok: bool) -> str:
    """A–F from the REAL outcome + process quality. Process (followed strategy,
    risk within limits) matters as much as P&L — a lucky win on a broken process
    is not an A; a disciplined loss is not an F."""
    r = actual_rr
    if not risk_ok:
        return "F"                                   # risk was violated — worst
    if result == "win" and r >= max(1.5, 0.6 * planned_rr) and followed_strategy:
        return "A"
    if result == "win" and r > 0 and followed_strategy:
        return "B"
    if r > -1.0 and followed_strategy:
        return "C"                                   # a controlled loss, on process
    if followed_strategy:
        return "D"                                   # full loss but by the book
    return "F"                                       # off-process


def build_review(*, side: str, planned_rr: float, actual_rr: float, result: str,
                 exit_reason: str, quality_score: Optional[float], risk_ok: bool,
                 followed_strategy: bool) -> dict:
    """Deterministic post-trade review from what actually happened."""
    entry_valid = followed_strategy and (quality_score is None or quality_score >= 60)
    exit_valid = exit_reason in ("take-profit", "target", "stop-loss", "stop",
                                 "time", "opposite-signal", "trailing-stop")
    grade = grade_trade(result=result, actual_rr=actual_rr, planned_rr=planned_rr,
                        followed_strategy=followed_strategy, risk_ok=risk_ok)
    mistake = "None — trade followed the plan."
    improve = "Repeat the same disciplined process."
    if not risk_ok:
        mistake = "Risk rules were not respected on this entry."
        improve = "Never bypass the Risk Manager / Safety Center."
    elif result == "loss" and exit_reason in ("stop-loss", "stop"):
        mistake = "None mechanically — the stop did its job; the setup simply failed."
        improve = "Acceptable loss. Only revisit the setup if the pattern keeps failing."
    elif result == "win" and actual_rr < planned_rr * 0.5:
        mistake = "Exited well short of the planned target."
        improve = "Review whether the exit left reward on the table."
    return {
        "entry_valid": entry_valid,
        "risk_valid": risk_ok,
        "exit_valid": exit_valid,
        "followed_strategy": followed_strategy,
        "quality": "good" if grade in ("A", "B") else "acceptable" if grade == "C" else "poor",
        "mistake": mistake,
        "improvement": improve,
        "grade": grade,
    }


def build_evolution(setup_stage: dict) -> dict:
    """Evolution note bounded by the early-signal / evidence staging. Encodes
    the hard rules: no single-trade evolution, never auto-increase risk, never
    bypass the Risk Manager."""
    trades = setup_stage["trades"]
    stage = setup_stage["stage"]
    wr = setup_stage["win_rate"]
    net = setup_stage["net_r"]
    if stage == "early-signal":
        strength = "early signal only"
        repeat = "Keep taking valid setups to gather evidence — do not change anything yet."
        conf = "hold"
    elif stage == "building":
        strength = f"building ({trades}/{EVIDENCE_MIN} trades toward strong evidence)"
        repeat = "Pattern is forming; still below the evidence bar for strategy changes."
        conf = "hold"
    else:  # evidence
        strength = f"strong evidence ({trades} trades)"
        if net > 0 and wr >= 45:
            repeat = "Setup is proven — worth trusting more within existing risk limits."
            conf = "increase (bounded)"
        elif net < 0:
            repeat = "Setup is net-negative on real evidence — consider standing aside."
            conf = "decrease"
        else:
            repeat = "Mixed — no change warranted."
            conf = "hold"
    return {
        "learned": f"{setup_stage['setup_key']}: {setup_stage['note']}",
        "take_similar_again": net >= 0 or stage != "evidence",
        "confidence_direction": conf,
        "rule_weight_hint": ("no change — insufficient evidence" if stage != "evidence"
                             else "the winning reads for this setup may deserve slightly more weight"
                             if net > 0 else "review the reads that led into this setup"),
        "strength": strength,
        "guardrails": ["Risk is never increased automatically.",
                       "The Risk Manager and Safety Center are never bypassed.",
                       f"Insights under {EARLY_SIGNAL_MAX} trades are early signals; "
                       f"{EVIDENCE_MIN}+ trades are needed for stronger strategy changes."],
    }


def build_coach(sections: dict, review: dict, result: str, actual_rr: float,
                planned_rr: float, risk_ok: bool) -> dict:
    """AI Coach note for one completed trade — composed ONLY from the reads the
    bot actually recorded at entry plus the real outcome. Reads like a senior
    trader debriefing a junior: strengths, weaknesses, one lesson, a rating."""
    checklist = sections.get("checklist") or {}
    reads = list(checklist.get("entry_reads") or [])
    ok_reads = [r for r in reads if r.get("ok")]
    bad_reads = [r for r in reads if not r.get("ok")]

    strengths = [f"{r.get('rule', 'read')}: {r.get('detail', 'confirmed')}"
                 for r in ok_reads[:3]]
    if risk_ok:
        strengths.append("Risk was respected — sized within limits, stop honored.")

    weaknesses = [f"{r.get('rule', 'read')}: {r.get('detail', 'not confirmed')}"
                  for r in bad_reads[:3]]
    if result == "win" and planned_rr and actual_rr < planned_rr * 0.5:
        weaknesses.append(f"Banked {actual_rr:.2f}R of a {planned_rr:.1f}R plan — "
                          "the exit left reward on the table.")
    if not weaknesses:
        weaknesses.append("None recorded — the entry reads were clean; "
                          "a losing outcome here is normal variance.")

    lesson = review.get("improvement", "Repeat the disciplined process.")
    grade = review.get("grade", "C")
    rating = grade + ("+" if grade == "A" and actual_rr >= 2.0 else "")
    return {"strengths": strengths, "weaknesses": weaknesses,
            "lesson": lesson, "rating": rating}


class DecisionJournal:
    """Orchestrator wired into the pipeline (entry) and close path (exit)."""

    def __init__(self, store: JournalStore):
        self.store = store

    # ---------------------------------------------------------------- entry
    @_atomic_capture
    def record_entry(self, *, trade_id: str, mode: str, symbol: str, side: str,
                     strategy: str, timeframe: str, entry: float, stop: float,
                     target: Optional[float], size: float, equity: float,
                     confidence: float, brain_score: Optional[float], regime: str,
                     steps: list, payload: dict, position_id: str = "") -> None:
        # The same durable fill can be delivered after a restart or retry.
        if self.store.get(trade_id) is not None:
            return
        risk_dist = abs(entry - stop) if stop else 0.0
        planned_rr = (abs((target or 0) - entry) / risk_dist) if (target and risk_dist) else None
        risk_amount = round(risk_dist * size, 2)
        checklist = map_checklist(steps, payload.get("brain_checklist"))
        snapshot = payload.get("snapshot") or {"note": "Not captured for this entry."}
        reason = payload.get("reason", "")
        risk_step = next((s for s in checklist["risk_gates"] if s["rule"] == "risk"), None)
        sizing = payload.get("journal_sizing") or {}
        engine_guardrails = payload.get("journal_engine") or {}
        quality_gate = payload.get("journal_quality_gate") or {}
        provenance = payload.get("journal_execution") or {}
        identity = payload.get("strategy_identity") or {}
        signal_at = payload.get("original_signal_timestamp") or payload.get("timestamp")
        leg = self.store.episode_for_trade(
            trade_id, **{key: provenance.get(key) for key in (
                "instance_id", "simulation_session_id", "owner_id", "account_id")})
        episode = self.store.get_episode(leg["episode_id"]) if leg else None
        evidence_headers = {
            **{key: provenance.get(key) for key in ("owner_id", "account_id", "lab_id", "source_kind")},
            "signal_id": payload.get("decision_identity"),
            "episode_id": leg["episode_id"] if leg else None,
            "parent_trade_id": leg.get("parent_trade_id") if leg else None,
            "initial_risk_amount_text": episode.get("root_initial_risk") if episode else None,
            "identity_status": provenance.get("identity_status") or identity.get("identity_status"),
            "evidence_schema_version": 1 if leg or provenance.get("strategy_config_hash") else None,
        }
        sections = {
            "entry_decision": {
                "main_reason": reason or "Strategy signal fired.",
                "strategy_setup": f"{strategy} signal on {timeframe}",
                "higher_timeframe_trend": snapshot.get("regime", regime),
                "confidence_score": confidence,
                "final_decision_score": brain_score,
                "quality_gate_score": quality_gate.get("score"),
                "quality_gate_regime": quality_gate.get("regime"),
                "quality_gate_status": ("Passed" if quality_gate.get("allowed")
                                        else "Not evaluated" if not quality_gate
                                        else "Failed"),
                "quality_gate_passed": quality_gate.get("passed") or [],
                "quality_gate_failed": quality_gate.get("failed") or quality_gate.get("blocks") or [],
                "decision_reference": payload.get("journal_decision_id"),
                "decision_reference_status": payload.get("decision_reference_status", "UNVERIFIED"),
                "claimed_decision_reference": payload.get("claimed_decision_reference"),
                "reads": checklist["entry_reads"],
            },
            "checklist": checklist,
            "market_snapshot": {**snapshot, "account_equity": round(equity, 2),
                                "open_trades": payload.get("open_trades")},
            "risk_check": {
                "risk_per_trade": risk_step["detail"] if risk_step else "Not checked",
                "gates": checklist["risk_gates"],
                # Captured after the pipeline calculated and capped the size,
                # then updated once the paper fill returned. It is therefore a
                # receipt of what the bot actually put on, not an estimate.
                "entry_sizing": sizing,
                "engine_guardrails": engine_guardrails,
                "final_risk_decision": "Allowed — all risk gates passed.",
            },
            "provenance": {
                **evidence_headers,
                "instance_id": provenance.get("instance_id"),
                "simulation_session_id": provenance.get("simulation_session_id"),
                "instance_name": provenance.get("instance_name"),
                "strategy_id": provenance.get("strategy_id"),
                "strategy_name": provenance.get("strategy_name") or strategy,
                "strategy_version": provenance.get("strategy_version"),
                "strategy_config_hash": provenance.get("strategy_config_hash"),
                "source_hash": provenance.get("source_hash"),
                "decision_identity": payload.get("decision_identity"),
                "decision_timestamp": payload.get("decision_observed_at"),
                "signal_timestamp": signal_at,
                "entry_timestamp": payload.get("entry_timestamp"),
                "execution_id": payload.get("execution_id"),
                "market_data_mode": provenance.get("market_data_mode") or "unknown",
                "market_data_source": provenance.get("market_data_source") or "unknown",
                "fill_model": provenance.get("fill_model") or "unknown",
                "execution_mode": provenance.get("execution_mode") or mode,
                "exchange": provenance.get("exchange") or "unknown",
                "instrument_type": provenance.get("instrument_type") or "unknown",
                "position_id": position_id or provenance.get("position_id"),
            },
        }
        if payload.get("recovered_execution_only"):
            sections["entry_decision"]["strategy_setup"] = "Original setup unavailable"
            sections["risk_check"]["final_risk_decision"] = "Original preexecution gates unavailable"
        self.store.record_entry({
            **evidence_headers,
            "trade_id": trade_id,
            "mode": provenance.get("execution_mode") or mode,
            "symbol": symbol, "side": side,
            "strategy": strategy, "timeframe": timeframe, "entry": entry, "stop": stop,
            "target": target, "size": size, "risk_amount": risk_amount,
            "planned_rr": planned_rr, "confidence": confidence,
            "brain_score": brain_score, "regime": regime, "sections": sections,
            "instance_id": provenance.get("instance_id"),
            "simulation_session_id": provenance.get("simulation_session_id"),
            "instance_name": provenance.get("instance_name"),
            "strategy_id": provenance.get("strategy_id"),
            "strategy_name": provenance.get("strategy_name") or strategy,
            "strategy_version": provenance.get("strategy_version"),
            "strategy_config_hash": provenance.get("strategy_config_hash"),
            "source_hash": provenance.get("source_hash"),
            "signal_timestamp": signal_at,
            "decision_timestamp": payload.get("decision_observed_at"),
            "entry_timestamp": payload.get("entry_timestamp"),
            "decision_id": payload.get("journal_decision_id"),
            "order_id": payload.get("alert_id"),
            "execution_id": payload.get("execution_id"),
            "execution_mode": provenance.get("execution_mode") or mode,
            "market_data_mode": provenance.get("market_data_mode"),
            "market_data_source": provenance.get("market_data_source"),
            "exchange": provenance.get("exchange"),
            "position_id": position_id or provenance.get("position_id"),
        })
        t = signal_at
        decision_at = payload.get("decision_observed_at")
        order_at = payload.get("order_observed_at") or decision_at
        if not payload.get("recovered_execution_only"):
            self.store.add_event(trade_id, "setup-detected", f"{strategy} setup on {symbol} {timeframe}", t,
                                 event_id=trade_id + ":setup")
            self.store.add_event(trade_id, "quality-gate-passed", _quality_event_detail(quality_gate), decision_at,
                                 event_id=trade_id + ":quality")
            self.store.add_event(trade_id, "risk-check-passed", "All risk gates cleared", order_at,
                                 event_id=trade_id + ":risk")
            self.store.add_event(trade_id, "risk-sized", _sizing_event_detail(sizing), order_at,
                                 event_id=trade_id + ":sized")
        self.store.add_event(trade_id, "trade-opened",
                             f"{side.upper()} {size:.6f} @ {entry} (stop {stop}, target {target})",
                             payload.get("entry_timestamp") or t, event_id=trade_id + ":opened")

    # ---------------------------------------------------------------- exit
    @_atomic_capture
    def record_exit(self, *, trade_id: str, exit_price: float, pnl: float,
                    exit_reason: str, quality_score: Optional[float] = None,
                    risk_ok: bool = True, followed_strategy: bool = True,
                    mfe_r: Optional[float] = None,
                    mae_r: Optional[float] = None,
                    instance_id: str = "", execution_receipt: Optional[dict] = None,
                    exit_timestamp: Optional[str] = None, event_id: str = "") -> Optional[dict]:
        j = self.store.get(trade_id, instance_id=instance_id or None)
        if j is None:
            return None
        if j.get("status") != "open":
            return {"review": j.get("sections", {}).get("review"),
                    "evolution": j.get("sections", {}).get("evolution")}
        entry, stop = j.get("entry"), j.get("stop")
        side = j.get("side")
        planned_rr = j.get("planned_rr") or 0.0
        risk_dist = abs((entry or 0) - (stop or 0)) or 1.0
        move = (exit_price - entry) if side == "long" else (entry - exit_price)
        gross_rr = round(move / risk_dist, 3)
        # PnL is net of modeled commissions while the price move is gross.
        # Grade and stored actual R on the net result when entry risk was
        # captured; otherwise retain the historical gross-price fallback.
        risk_amount = float(j.get("risk_amount") or 0.0)
        actual_rr = round(pnl / risk_amount, 3) if risk_amount > 0 else gross_rr
        result = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
        review = build_review(side=side, planned_rr=planned_rr, actual_rr=actual_rr,
                              result=result, exit_reason=exit_reason,
                              quality_score=quality_score, risk_ok=risk_ok,
                              followed_strategy=followed_strategy)
        setup_key = f"{j.get('strategy')}|{j.get('regime')}|{side}"
        stage = self.store.update_evolution(setup_key, j.get("strategy"),
                                            j.get("regime"), side, actual_rr)
        evolution = build_evolution({**stage, "setup_key": setup_key})
        exit_decision = {"exit_reason": exit_reason, "exit_price": exit_price,
                         "actual_rr": actual_rr, "net_rr": actual_rr,
                         "gross_price_rr": gross_rr,
                         "rr_basis": ("net_pnl / entry_risk" if risk_amount > 0
                                      else "gross price move / stop distance"),
                         "pnl": round(pnl, 2), "result": result,
                         # lifecycle telemetry (in R); honest "not tracked" for
                         # positions adopted without management state
                         "max_profit_r": mfe_r if mfe_r is not None else "not tracked",
                         "max_drawdown_r": mae_r if mae_r is not None else "not tracked"}
        if execution_receipt is not None:
            exit_decision["execution_receipt"] = execution_receipt
        if exit_timestamp is not None:
            exit_decision["exit_timestamp"] = exit_timestamp
        review["coach"] = build_coach(j.get("sections", {}), review, result,
                                      actual_rr, planned_rr, risk_ok)
        self.store.close_trade(trade_id, exit=exit_price, pnl=pnl, actual_rr=actual_rr,
                               result=result, grade=review["grade"],
                               extra_sections={"exit_decision": exit_decision,
                                               "review": review, "evolution": evolution},
                               instance_id=instance_id, closed_at=exit_timestamp)
        self.store.add_event(trade_id, "exit-triggered", f"{exit_reason} @ {exit_price}",
                             exit_timestamp, event_id=trade_id + ":exit")
        self.store.add_event(trade_id, "trade-closed",
                             f"{result} · {actual_rr:+.2f}R · PnL {pnl:+.2f}",
                             exit_timestamp, event_id=trade_id + ":closed")
        self.store.add_event(trade_id, "review-generated", f"Grade {review['grade']}",
                             event_id=trade_id + ":review")
        return {"review": review, "evolution": evolution}
