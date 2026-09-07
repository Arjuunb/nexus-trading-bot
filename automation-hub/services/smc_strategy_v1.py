"""Source-informed SMC strategy contract built on frozen native objects.

This module is deliberately a decision layer.  It never recalculates market
structure and it cannot place a live order.  The first executable research
model is the liquidity-sweep reversal; continuation and displacement models
remain explicitly parked until their distinct causal rules are implemented.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal

from services.native_smc import PivotPoint, SMCMarketStructureEngine
from services.smc_strategy_ladder import evaluate_candidate


STRATEGY_ID = "SMC_SOURCE_V1"
STRATEGY_VERSION = "SMC_SOURCE_V1.0.0-paper-draft"
EXECUTION_ALLOWED = False
PAPER_ONLY = True
DEFAULT_RISK_PERCENT = 0.5
MAX_RISK_PERCENT = 1.0
MIN_RUNNER_R = 3.0


@dataclass(frozen=True)
class EntryModel:
    id: str
    label: str
    status: Literal["ACTIVE", "PARKED"]
    narrative: str
    ordered_rules: tuple[str, ...]
    native_candidate_ids: tuple[str, ...]
    entry: str
    invalidation: str
    targets: str


ENTRY_MODELS = (
    EntryModel(
        "SMC_M1_SWEEP_REVERSAL", "Liquidity sweep reversal", "ACTIVE",
        "Trade a lower-timeframe reversal only after higher-timeframe context and a liquidity raid.",
        (
            "confirmed closed-bar HTF bias and premium/discount location",
            "native external or inducement liquidity sweep",
            "same-direction CHoCH/BOS after the sweep",
            "causal order block preferred; causal FVG is the fallback",
            "exact POI retest and same-direction rejection",
        ),
        ("SMC_S5_ORDER_BLOCK_RETEST", "SMC_S4_FVG_RETEST"),
        "confirming retest-bar close",
        "beyond the signal extreme plus the frozen 1.5 ATR buffer",
        "50% at the first structural objective; 50% runner at 3R or farther external liquidity",
    ),
    EntryModel(
        "SMC_M2_BOS_CONTINUATION", "BOS continuation", "PARKED",
        "Continuation after a body-close BOS and retest of the unmitigated causal OB/FVG.",
        ("HTF trend", "body-close BOS", "causal unmitigated POI", "retest", "continuation rejection"),
        (), "not enabled", "not enabled", "not enabled",
    ),
    EntryModel(
        "SMC_M3_DISPLACEMENT_FVG", "Displacement FVG", "PARKED",
        "Displacement creates a fresh FVG; entry is at its boundary or midpoint after context validation.",
        ("HTF context", "displacement", "fresh FVG", "boundary or 50% retest", "rejection"),
        (), "not enabled", "not enabled", "not enabled",
    ),
)


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def manifest() -> dict:
    payload = {
        "strategy_id": STRATEGY_ID,
        "version": STRATEGY_VERSION,
        "status": "PAPER_DRAFT",
        "paper_only": PAPER_ONLY,
        "execution_allowed": EXECUTION_ALLOWED,
        "native_object_authority": "services/native_smc.py",
        "structure_policy": {
            "htf_bos": "FULL_BODY_CLOSE_REQUIRED",
            "wick_through_level": "LIQUIDITY_SWEEP_NOT_BOS",
            "ltf_wick_choch_variant": "DISABLED_IN_V1",
        },
        "risk": {
            "default_risk_percent": DEFAULT_RISK_PERCENT,
            "max_risk_percent": MAX_RISK_PERCENT,
            "minimum_runner_r": MIN_RUNNER_R,
            "scale_out": [{"fraction": 0.5, "target": "FIRST_STRUCTURAL_OBJECTIVE"},
                          {"fraction": 0.5, "target": "MAX_3R_OR_EXTERNAL_LIQUIDITY"}],
        },
        "entry_models": [asdict(row) for row in ENTRY_MODELS],
        "source_notes": [
            "The supplied material is treated as research input, never as executable instructions.",
            "Claims about institutional intent are not assumed; only observable closed-bar objects are used.",
            "A parked model cannot emit a proposal.",
        ],
    }
    return {**payload, "configuration_hash": _hash(payload)}


def strategy_models() -> dict:
    """Public model registry; parked rows are visible but cannot propose."""
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "paper_only": True,
        "real_execution_allowed": False,
        "models": [asdict(row) for row in ENTRY_MODELS],
    }


def _external_targets(engine: SMCMarketStructureEngine, direction: str, entry: float) -> list[tuple[float, str]]:
    kind = "high" if direction == "bullish" else "low"
    candidates = [row for row in engine.pivots.values() if isinstance(row, PivotPoint) and row.kind == kind]
    if direction == "bullish":
        candidates = sorted((row for row in candidates if row.price > entry), key=lambda row: row.price)
    else:
        candidates = sorted((row for row in candidates if row.price < entry), key=lambda row: row.price, reverse=True)
    return [(float(row.price), row.id) for row in candidates]


def _trade_plan(engine: SMCMarketStructureEngine, proposal: dict) -> dict:
    entry, stop = float(proposal["entry"]), float(proposal["stop"])
    direction = proposal["direction"]
    risk = abs(entry - stop)
    externals = _external_targets(engine, direction, entry)
    first_r = 2.0
    first_target_object_id = None
    first_external = next(((price, object_id) for price, object_id in externals
                           if ((price - entry) / risk if direction == "bullish" else (entry - price) / risk) >= 1.0), None)
    if first_external:
        external_r = ((first_external[0] - entry) / risk if direction == "bullish"
                      else (entry - first_external[0]) / risk)
        # When the nearest structural objective is already at the runner
        # threshold or beyond, preserve a distinct 2R scale-out and use that
        # external as the runner.  Two equal targets are invalid for the paper
        # broker's split-exit contract.
        if external_r < MIN_RUNNER_R:
            first_r = external_r
            first_target_object_id = first_external[1]
    target_1 = entry + risk * first_r if direction == "bullish" else entry - risk * first_r
    runner_r = MIN_RUNNER_R
    runner_external = next(((price, object_id) for price, object_id in externals
                            if ((price - entry) / risk if direction == "bullish" else (entry - price) / risk) > first_r), None)
    if runner_external:
        runner_r = max(runner_r, ((runner_external[0] - entry) / risk if direction == "bullish"
                                  else (entry - runner_external[0]) / risk))
    else:
        runner_r = max(runner_r, first_r)
    target_2 = entry + risk * runner_r if direction == "bullish" else entry - risk * runner_r
    return {
        "entry": entry, "stop": stop, "risk_distance": risk,
        "target_1": target_1, "target_1_r": first_r, "target_1_fraction": 0.5,
        "target_2": target_2, "target_2_r": runner_r, "target_2_fraction": 0.5,
        "target_1_object_id": first_target_object_id,
        "target_2_object_id": runner_external[1] if runner_external else None,
        "risk_percent": DEFAULT_RISK_PERCENT,
        "paper_only": True, "execution_allowed": False,
    }


def _snapshot_at(engine: SMCMarketStructureEngine, candle_at: datetime | None):
    if not engine.snapshots:
        return None
    if candle_at is None:
        return engine.snapshots[max(engine.snapshots)]
    eligible = [timestamp for timestamp in engine.snapshots if timestamp <= candle_at]
    return engine.snapshots[max(eligible)] if eligible else None


def _context_conditions(snapshot, direction: str | None,
                        mtf_evidence: dict | None = None) -> tuple[list[dict], list[str]]:
    primary = ((mtf_evidence or {}).get("primary") or {})
    named_bias = primary.get("htf_bias")
    # Historical/frozen unit callers may not own the live hub. Preserve their
    # native SMC snapshot behavior; the paper runtime always supplies the
    # shared policy evidence and never takes this fallback.
    bias = (1 if named_bias == "BULLISH" else -1 if named_bias == "BEARISH"
            else snapshot.htf_bias if snapshot and mtf_evidence is None else 0)
    display_bias = named_bias or ({1: "BULLISH", -1: "BEARISH"}.get(bias, "NEUTRAL"))
    area = snapshot.dealing_range.area if snapshot else "unknown"
    bias_ok = direction is not None and ((direction == "bullish" and bias > 0) or (direction == "bearish" and bias < 0))
    area_ok = direction is not None and ((direction == "bullish" and area == "discount") or
                                         (direction == "bearish" and area == "premium"))
    rows = [
        {"key": "htf_context", "label": "Completed HTF direction", "status": "PASS" if bias_ok else "MISSING",
         "detail": (f"native {primary.get('htf_timeframe', 'HTF')} bias is "
                    f"{display_bias}"),
         "object_id": primary.get("htf_candle_id") or (snapshot.id if snapshot else None)},
        {"key": "location", "label": "Premium / discount location", "status": "PASS" if area_ok else "MISSING",
         "detail": f"native dealing-range area is {area}", "object_id": snapshot.id if snapshot else None},
    ]
    missing = [row["label"] for row in rows if row["status"] != "PASS"]
    return rows, missing


def evaluate(engine: SMCMarketStructureEngine, model_id: str = "SMC_M1_SWEEP_REVERSAL",
             *, candle_at=None, mtf_evidence: dict | None = None) -> dict:
    model = next((row for row in ENTRY_MODELS if row.id == model_id), None)
    if model is None:
        raise ValueError("unknown SMC source strategy model")
    evaluated_at = datetime.now(timezone.utc).isoformat()
    selected_at = candle_at.isoformat() if isinstance(candle_at, datetime) else None
    base = {"strategy_id": STRATEGY_ID, "version": STRATEGY_VERSION,
            "model": asdict(model), "paper_only": True, "execution_allowed": False,
            "real_execution_allowed": False, "evaluated_at": evaluated_at,
            "mtf_evidence": dict(mtf_evidence or {}),
            "data_identity": {"symbol": engine.config.symbol, "timeframe": engine.config.timeframe,
                              "selected_candle": selected_at}}
    if not engine.bars:
        return {**base, "state": "WATCHING", "next_required_event": "Await native closed-bar context",
                "selected_candidate_id": None, "candidate_evaluations": [], "proposal": None,
                "ordered_condition_results": [], "missing_conditions": ["native closed-bar context"],
                "native_object_ids": [], "proposal_id": None, "setup_id": None, "trade_plan": None}
    if model.status != "ACTIVE":
        return {**base, "state": "PARKED", "next_required_event": "Implement and verify the distinct model rules",
                "selected_candidate_id": None, "candidate_evaluations": [], "proposal": None,
                "ordered_condition_results": [], "missing_conditions": ["model implementation and verification"],
                "native_object_ids": [], "proposal_id": None, "setup_id": None, "trade_plan": None}

    evaluations = [evaluate_candidate(engine, candidate_id, candle_at=candle_at)
                   for candidate_id in model.native_candidate_ids]
    selected = next((row for row in evaluations if row.selected_trace and row.selected_trace.proposal), None)
    public = [asdict(row) for row in evaluations]
    selected_trace = selected.selected_trace if selected and selected.selected_trace else None
    proposal = asdict(selected_trace.proposal) if selected_trace else None
    trace = selected_trace or next((row.selected_trace for row in evaluations if row.selected_trace), None)
    direction = proposal.get("direction") if proposal else (trace.direction if trace else None)
    context, context_missing = _context_conditions(
        _snapshot_at(engine, candle_at), direction, mtf_evidence)
    trace_conditions = [asdict(row) for row in trace.conditions] if trace else []
    ordered_conditions = [*context, *trace_conditions]
    trace_missing = list(trace.missing_conditions) if trace else []
    missing_conditions = [*context_missing, *trace_missing]
    native_object_ids = list(trace.supporting_object_ids) if trace else []
    if proposal and not context_missing:
        return {**base, "state": "ENTRY_READY", "next_required_event": "Paper risk approval",
                "selected_candidate_id": selected.strategy_id, "candidate_evaluations": public,
                "ordered_condition_results": ordered_conditions, "missing_conditions": [],
                "native_object_ids": native_object_ids, "proposal_id": proposal["id"],
                "setup_id": proposal["setup_id"], "proposal": proposal, "trade_plan": _trade_plan(engine, proposal)}
    next_event = context_missing[0] if context_missing else (
        evaluations[0].next_required_event if evaluations else "Await native closed-bar context")
    return {**base, "state": "WATCHING", "next_required_event": next_event,
            "selected_candidate_id": None, "candidate_evaluations": public,
            "ordered_condition_results": ordered_conditions, "missing_conditions": missing_conditions,
            "native_object_ids": native_object_ids, "proposal_id": None,
            "setup_id": trace.setup_id if trace else None, "proposal": None, "trade_plan": None}
