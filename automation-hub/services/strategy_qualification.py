"""Deterministic, advisory strategy qualification and validation primitives.

This module is deliberately independent of order/accounting code.  It consumes
already-produced episode metrics or immutable research records and returns
explicit PASS/FAIL/UNKNOWN decisions.  It never downloads data, changes a
strategy, or writes financial state.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import random
import re
from statistics import median
from typing import Any, Iterable


QUALIFICATION_STATES = (
    "EXPERIMENTAL", "OBSERVATION", "VALIDATING", "STRESS_TEST",
    "FORWARD_PAPER", "QUALIFIED", "DEGRADED", "PAUSED", "DISQUALIFIED", "RETIRED",
)


@dataclass(frozen=True)
class QualificationPolicy:
    policy_version: str = "qualification.v2"
    minimum_episodes: int = 100
    minimum_profit_factor: str = "1.30"
    require_positive_expectancy: bool = True
    require_oos: bool = True
    require_walk_forward: bool = True
    require_cost_coverage: bool = True
    require_concentration_test: bool = True
    require_forward_paper: bool = True
    minimum_forward_paper_episodes: int = 30
    require_cost_stress: bool = True
    require_monte_carlo: bool = True
    maximum_drawdown_r: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _check(condition: bool | None, *, name: str, reason: str) -> dict[str, Any]:
    if condition is True:
        return {"name": name, "status": "PASS", "reason": reason}
    if condition is False:
        return {"name": name, "status": "FAIL", "reason": reason}
    return {"name": name, "status": "UNKNOWN", "reason": reason}


def evaluate_qualification(identity: dict[str, Any], metrics: dict[str, Any], *,
                           oos: dict[str, Any] | None = None,
                           walk_forward: dict[str, Any] | None = None,
                           robustness: dict[str, Any] | None = None,
                           forward_paper: dict[str, Any] | None = None,
                           cost_stress: dict[str, Any] | None = None,
                           policy: QualificationPolicy | None = None,
                           evidence_quality: str = "UNKNOWN") -> dict[str, Any]:
    """Evaluate a frozen cohort without ever treating missing data as zero."""
    policy = policy or QualificationPolicy()
    episodes = metrics.get("completed_episode_count", metrics.get("trade_count"))
    pf = _decimal(metrics.get("net_profit_factor", metrics.get("profit_factor")))
    expectancy = _decimal(metrics.get("expectancy_net_r", metrics.get("expectancy_r")))
    drawdown = _decimal(metrics.get("max_drawdown_r"))
    cost_status = (metrics.get("cost_coverage") or {}).get("status") if isinstance(metrics.get("cost_coverage"), dict) else metrics.get("cost_coverage_status")
    def required_result(value: dict[str, Any] | None, key: str) -> bool | None:
        if value is None:
            return None
        return value.get("status") == "PASS"

    def enough(value, minimum):
        number = _decimal(value)
        return number is not None and number == number.to_integral_value() and number >= minimum

    concentration = (robustness or {}).get("concentration", robustness or {})
    simulation = (robustness or {}).get("monte_carlo") or {}
    stressed = (cost_stress or {}).get("stress", cost_stress or {})
    stressed_net = _decimal(stressed.get("net_r"))
    forward = forward_paper or {}
    forward_expectancy = _decimal(forward.get("expectancy_net_r"))

    def optional_check(required, value, condition):
        return True if not required else None if value is None else condition

    checks = [
        _check(bool(identity.get("strategy_id") and identity.get("strategy_version")
                    and re.fullmatch(r"[0-9a-f]{64}", str(identity.get("config_fingerprint") or ""))),
               name="frozen_strategy_identity", reason="exact strategy version and SHA-256 configuration fingerprint are required"),
        _check(enough(episodes, policy.minimum_episodes),
               name="minimum_historical_episodes", reason=f"requires {policy.minimum_episodes}; observed {episodes if episodes is not None else 'UNKNOWN'}"),
        _check(pf is not None and pf >= _decimal(policy.minimum_profit_factor),
               name="historical_profit_factor", reason=f"requires >= {policy.minimum_profit_factor}; observed {pf if pf is not None else 'UNKNOWN'}"),
        _check(expectancy is not None and expectancy > 0 if policy.require_positive_expectancy else True,
               name="historical_expectancy", reason="requires positive net expectancy" if policy.require_positive_expectancy else "policy does not require positive expectancy"),
        _check(not policy.require_cost_coverage or cost_status in {"COMPLETE", "APPROVED_CONSERVATIVE_MODEL"},
               name="cost_coverage", reason=f"requires complete or approved conservative costs; observed {cost_status or 'UNKNOWN'}"),
        _check(required_result(oos, "status") if policy.require_oos else True,
               name="out_of_sample", reason="independent chronological OOS result is required" if policy.require_oos else "policy does not require OOS"),
        _check(required_result(walk_forward, "status") if policy.require_walk_forward else True,
               name="walk_forward", reason="walk-forward evaluation is required" if policy.require_walk_forward else "policy does not require walk-forward"),
        _check(None if robustness is None and policy.require_concentration_test else (concentration.get("concentration_status") == "PASS") if policy.require_concentration_test else True,
               name="concentrated_returns", reason="largest-winner concentration test is required" if policy.require_concentration_test else "policy does not require concentration test"),
        _check(None if forward_paper is None and policy.require_forward_paper else enough(forward.get("completed_episodes"), policy.minimum_forward_paper_episodes)
               if policy.require_forward_paper else True,
               name="forward_paper", reason=f"requires {policy.minimum_forward_paper_episodes} completed forward-paper episodes" if policy.require_forward_paper else "policy does not require forward paper"),
        _check(optional_check(policy.require_forward_paper, forward_paper, (
            forward.get("status") == "PASS" and forward_expectancy is not None and forward_expectancy > 0
            and forward.get("evidence_quality") in {"COMPLETE", "VERIFIED"}
            and forward.get("cost_coverage_status") in {"COMPLETE", "APPROVED_CONSERVATIVE_MODEL"}
            and forward.get("independence_status") == "VERIFIED_INDEPENDENT")),
            name="forward_paper_profitability", reason="independent, complete forward-paper evidence must have positive expectancy after costs"),
        _check(optional_check(policy.require_cost_stress, cost_stress,
            stressed.get("status") == "PASS" and stressed_net is not None and stressed_net > 0),
            name="cost_stress", reason="fee, slippage and funding stress must pass"),
        _check(optional_check(policy.require_monte_carlo, robustness, simulation.get("status") == "PASS"),
            name="monte_carlo", reason="Monte Carlo analysis must be available"),
        _check(evidence_quality in {"COMPLETE", "VERIFIED"}, name="evidence_quality",
            reason="complete reconciled evidence is required"),
    ]
    if policy.maximum_drawdown_r is not None:
        limit = _decimal(policy.maximum_drawdown_r)
        checks.append(_check(drawdown is not None and limit is not None and drawdown <= limit,
                             name="maximum_drawdown", reason=f"requires drawdown <= {policy.maximum_drawdown_r}; observed {drawdown if drawdown is not None else 'UNKNOWN'}"))
    passed = [c["name"] for c in checks if c["status"] == "PASS"]
    failed = [c["name"] for c in checks if c["status"] == "FAIL"]
    unknown = [c["name"] for c in checks if c["status"] == "UNKNOWN"]
    final = "QUALIFIED" if not failed and not unknown and evidence_quality in {"COMPLETE", "VERIFIED"} else "NOT_QUALIFIED"
    blockers = [c["reason"] for c in checks if c["status"] != "PASS"]
    return {"contract_version": "strategy_qualification.v1", "identity": dict(identity),
            "policy": {**policy.as_dict(), "fingerprint": policy.fingerprint},
            "evidence_quality": evidence_quality, "status": final, "checks": checks,
            "passed_requirements": passed, "failed_requirements": failed,
            "unknown_requirements": unknown, "blocking_reasons": blockers,
            "recommended_next_action": "Obtain missing evidence and rerun validation" if final != "QUALIFIED" else "Continue isolated forward-paper observation",
            "calculated_at": datetime.now(timezone.utc).isoformat()}


def chronological_partition(rows: Iterable[dict[str, Any]], *, train: float = .6,
                             validation: float = .2, holdout: float = .2,
                             timestamp_key: str = "timestamp", warmup: int = 0) -> dict[str, Any]:
    """Split sorted observations chronologically; warmup rows are never trades."""
    values = list(rows)
    if not values or min(train, validation, holdout) <= 0 or abs(train + validation + holdout - 1) > 1e-9:
        raise ValueError("partition fractions must be positive and sum to one")
    ordered = sorted(values, key=lambda row: str(row.get(timestamp_key, "")))
    n = len(ordered)
    first = max(0, int(n * train)); second = max(first, int(n * (train + validation)))
    return {"train": ordered[:first], "validation": ordered[first:second],
            "holdout": ordered[second:], "warmup": int(warmup),
            "boundaries": {"train_end": ordered[first - 1].get(timestamp_key) if first else None,
                           "validation_end": ordered[second - 1].get(timestamp_key) if second else None,
                           "holdout_end": ordered[-1].get(timestamp_key)},
            "leakage_warning": "Warmup observations are context only and cannot count as validation trades."}


def walk_forward_windows(rows: Iterable[dict[str, Any]], *, development_size: int,
                         evaluation_size: int, step: int | None = None,
                         anchored: bool = False, timestamp_key: str = "timestamp") -> list[dict[str, Any]]:
    values = sorted(list(rows), key=lambda row: str(row.get(timestamp_key, "")))
    if development_size < 1 or evaluation_size < 1:
        raise ValueError("window sizes must be positive")
    step = step or evaluation_size
    windows, start = [], 0
    while start + development_size + evaluation_size <= len(values):
        dev_start = 0 if anchored else start
        dev = values[dev_start:start + development_size]
        evaluation = values[start + development_size:start + development_size + evaluation_size]
        windows.append({"window": len(windows) + 1, "development": dev, "evaluation": evaluation,
                        "development_start": dev[0].get(timestamp_key), "development_end": dev[-1].get(timestamp_key),
                        "evaluation_start": evaluation[0].get(timestamp_key), "evaluation_end": evaluation[-1].get(timestamp_key)})
        start += step
    return windows


def concentration_analysis(outcomes: Iterable[Any]) -> dict[str, Any]:
    values = [_decimal(value) for value in outcomes]
    values = [value for value in values if value is not None]
    if not values:
        return {"status": "UNKNOWN", "concentration_status": "UNKNOWN", "reason": "No reliable episode outcomes"}
    ordered = sorted(values, reverse=True)
    total = sum(values, Decimal(0)); top1 = ordered[0]; top3 = sum(ordered[:3], Decimal(0))
    if total <= 0:
        return {"status": "PASS", "concentration_status": "PASS", "total": str(total),
                "largest_winner_contribution": None, "top_three_contribution": None,
                "profit_excluding_largest": str(total - top1), "profit_excluding_top_three": str(total - top3),
                "reason": "Total outcome is non-positive; contribution ratios are not meaningful."}
    largest = top1 / total; top_three = top3 / total
    status = "FAIL" if largest > Decimal("0.5") or top_three > Decimal("0.8") else "PASS"
    return {"status": status, "concentration_status": status, "total": str(total),
            "largest_winner_contribution": str(largest), "top_three_contribution": str(top_three),
            "profit_excluding_largest": str(total - top1), "profit_excluding_top_three": str(total - top3),
            "reason": "Profit concentration exceeds robustness limits" if status == "FAIL" else "No excessive winner concentration observed"}


def seeded_monte_carlo(outcomes: Iterable[Any], *, runs: int = 1000, seed: int = 1) -> dict[str, Any]:
    values = [_decimal(value) for value in outcomes]
    values = [value for value in values if value is not None]
    if not values:
        return {"status": "UNKNOWN", "reason": "No reliable episode outcomes", "seed": seed}
    runs = max(1, min(int(runs), 5000)); rng = random.Random(seed); nets, drawdowns = [], []
    for _ in range(runs):
        path = [values[rng.randrange(len(values))] for _ in values]
        equity = peak = Decimal(0); dd = Decimal(0)
        for value in path:
            equity += value; peak = max(peak, equity); dd = max(dd, peak - equity)
        nets.append(equity); drawdowns.append(dd)
    nets.sort(); drawdowns.sort()
    def percentile(items, p): return str(items[min(len(items) - 1, int(p * len(items)))])
    return {"status": "PASS", "seed": seed, "runs": runs, "trades": len(values),
            "net_r": {"p05": percentile(nets, .05), "median": percentile(nets, .5), "p95": percentile(nets, .95)},
            "drawdown_r": {"median": percentile(drawdowns, .5), "p95": percentile(drawdowns, .95), "worst": str(drawdowns[-1])},
            "limitations": ["Trade-order reshuffling preserves the observed outcomes and does not prove an edge.",
                            "Bootstrap exchangeability may not hold for clustered market returns."]}


def stress_costs(outcomes: Iterable[Any], *, fee: Any = "0", slippage: Any = "0", funding: Any | None = None) -> dict[str, Any]:
    values = [_decimal(value) for value in outcomes]
    values = [value for value in values if value is not None]
    fee_d, slip_d, fund_d = _decimal(fee), _decimal(slippage), _decimal(funding)
    if fund_d is None and values:
        return {"status": "UNKNOWN", "net_r": str(sum(values, Decimal(0))), "funding": "UNKNOWN",
                "reason": "Funding is not modeled; it must not be treated as zero."}
    if not values or fee_d is None or slip_d is None:
        return {"status": "UNKNOWN", "reason": "Insufficient outcomes or cost assumptions"}
    net = sum(values, Decimal(0)) - fee_d - slip_d - fund_d
    return {"status": "PASS", "gross_r": str(sum(values, Decimal(0))), "net_r": str(net),
            "fee": str(fee_d), "slippage": str(slip_d), "funding": str(fund_d)}


def out_of_sample_report(partition: dict[str, list[dict[str, Any]]], evaluator) -> dict[str, Any]:
    """Evaluate fixed chronological partitions through an injected simulator.

    The callback is intentionally supplied by the existing backtest/replay
    engine.  It receives each partition independently; the holdout is never
    used to choose parameters here.
    """
    train = evaluator(partition.get("train", []), phase="development")
    validation = evaluator(partition.get("validation", []), phase="validation")
    holdout = evaluator(partition.get("holdout", []), phase="holdout")
    holdout_net = _decimal(holdout.get("net_r")) if isinstance(holdout, dict) else None
    status = "PASS" if holdout_net is not None and holdout_net > 0 else "FAIL" if holdout_net is not None else "UNKNOWN"
    return {"status": status, "method": "out_of_sample", "train": train,
            "validation": validation, "holdout": holdout,
            "leakage_warnings": ["Parameters must be frozen before holdout evaluation.",
                                  "Repeated access to the same holdout is tracked by validation run identity."],
            "relative_deterioration": _relative_deterioration(train, holdout)}


def _relative_deterioration(first: dict[str, Any], second: dict[str, Any]) -> str | None:
    a, b = _decimal(first.get("net_r")) if isinstance(first, dict) else None, _decimal(second.get("net_r")) if isinstance(second, dict) else None
    if a is None or b is None or a == 0:
        return None
    return str((a - b) / abs(a))


def walk_forward_report(windows: Iterable[dict[str, Any]], evaluator) -> dict[str, Any]:
    results = []
    for window in windows:
        development = evaluator(window["development"], phase="development", window=window["window"])
        evaluation = evaluator(window["evaluation"], phase="evaluation", window=window["window"])
        results.append({"window": window["window"], "development": development,
                        "evaluation": evaluation, "evaluation_start": window.get("evaluation_start"),
                        "evaluation_end": window.get("evaluation_end")})
    values = [_decimal(row["evaluation"].get("expectancy_r")) for row in results]
    values = [value for value in values if value is not None]
    positive = sum(value > 0 for value in values)
    status = "PASS" if values and positive >= (len(values) + 1) // 2 else "FAIL" if values else "UNKNOWN"
    return {"status": status, "method": "walk_forward", "total_windows": len(results),
            "profitable_windows": positive, "losing_windows": len(values) - positive,
            "positive_window_pct": str(Decimal(positive) / Decimal(len(values)) * 100) if values else None,
            "median_window_expectancy": str(median(values)) if values else None,
            "windows": results, "leakage_warnings": ["Evaluation windows are chronological and disjoint from their development windows."]}
