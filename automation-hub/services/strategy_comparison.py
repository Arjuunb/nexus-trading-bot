"""Advisory champion/challenger comparison over immutable research evidence.

This module consumes candidate metadata and completed validation reports.  It
does not run orders, alter execution settings, or treat missing evidence as a
zero.  A comparison is a versioned research artifact, not an execution
decision.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import random
import uuid

from services.strategy_evidence import evidence_json


RECOMMENDATIONS = {"CHALLENGER_PREFERRED", "CHAMPION_RETAINED", "INCONCLUSIVE",
                   "NOT_COMPARABLE", "INSUFFICIENT_EVIDENCE"}


@dataclass(frozen=True)
class ChallengerEvaluationPolicy:
    policy_version: str = "challenger.v1"
    minimum_episodes: int = 30
    minimum_expectancy_improvement_r: str = "0"
    maximum_drawdown_regression_r: str = "0"
    require_complete_costs: bool = True
    require_positive_oos: bool = True
    require_walk_forward: bool = True
    bootstrap_runs: int = 1000
    confidence_level: str = "0.90"

    def as_dict(self):
        return asdict(self)


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _check(condition, *, name, reason):
    if condition is True:
        status = "PASS"
    elif condition is False:
        status = "FAIL"
    else:
        status = "UNKNOWN"
    return {"name": name, "status": status, "reason": reason}


def _metric(report, *keys):
    if not isinstance(report, dict):
        return None
    candidates = [report, report.get("metrics"), report.get("holdout"), report.get("evaluation"), report.get("base")]
    for item in candidates:
        if isinstance(item, dict):
            for key in keys:
                value = _decimal(item.get(key))
                if value is not None:
                    return value
    return None


def _candidate_identity(payload):
    identity = {key: payload.get(key) for key in (
        "strategy_id", "strategy_version", "config_fingerprint", "frozen_config",
        "dataset_identity", "data_revision", "symbol", "asset_portfolio", "venue",
        "market_type", "timeframe", "execution_mode", "cost_model_version",
        "classifier_version", "software_revision", "policy_version", "scope")}
    return "candidate:" + hashlib.sha256(evidence_json(identity).encode()).hexdigest()


class StrategyComparisonService:
    def __init__(self, context_store):
        self.context = context_store

    def create_candidate(self, request: dict, *, owner_id: str) -> dict:
        candidate = dict(request)
        candidate["owner_id"] = owner_id
        candidate.setdefault("candidate_id", _candidate_identity(candidate))
        candidate.setdefault("validation_run_ids", [])
        candidate.setdefault("evidence_quality", "UNKNOWN")
        if not candidate.get("dataset_identity") or not candidate.get("strategy_id"):
            raise ValueError("strategy_id and dataset_identity are required")
        if not candidate.get("execution_mode"):
            raise ValueError("execution_mode is required")
        return self.context.create_research_candidate(candidate)

    def candidates(self, *, owner_id, limit=100):
        return self.context.research_candidates(owner_id=owner_id, limit=limit)

    def candidate(self, candidate_id, *, owner_id):
        return self.context.get_research_candidate(candidate_id, owner_id=owner_id)

    @staticmethod
    def comparability(champion: dict, challenger: dict) -> dict:
        fields = ("dataset_identity", "data_revision", "period_start", "period_end", "venue",
                  "market_type", "symbol", "timeframe", "execution_mode", "cost_model_version",
                  "funding_coverage", "slippage_model", "method", "oos_status", "classifier_version")
        reasons = []
        for field in fields:
            left = champion.get(field, (champion.get("scope") or {}).get(field))
            right = challenger.get(field, (challenger.get("scope") or {}).get(field))
            if left != right:
                reasons.append(f"{field} mismatch: champion={left or 'UNKNOWN'} challenger={right or 'UNKNOWN'}")
        for candidate, label in ((champion, "champion"), (challenger, "challenger")):
            if candidate.get("evidence_quality") not in {"COMPLETE", "VERIFIED"}:
                reasons.append(f"{label} evidence quality is {candidate.get('evidence_quality', 'UNKNOWN')}")
            if not candidate.get("validation_run_ids"):
                reasons.append(f"{label} has no committed validation run reference")
            coverage = candidate.get("evaluation_coverage")
            if coverage is not None:
                status = coverage.get("status") if isinstance(coverage, dict) else "UNKNOWN"
                if status != "VERIFIED":
                    reasons.append(f"{label} evaluation coverage is {status}")
                if coverage.get("missing_candles") not in (None, 0):
                    reasons.append(f"{label} has missing critical candles")
        return {"compatible": not reasons, "reasons": reasons}

    @staticmethod
    def _outcomes(candidate):
        scope = candidate.get("scope") or {}
        values = scope.get("episode_outcomes") or scope.get("matched_outcomes") or []
        result = [_decimal(value) for value in values]
        return [value for value in result if value is not None]

    @staticmethod
    def _bootstrap_difference(champion, challenger, *, runs, seed):
        if not champion or not challenger:
            return {"status": "UNKNOWN", "reason": "Matched episode outcomes are unavailable", "seed": seed}
        rng = random.Random(seed); differences = []
        for _ in range(max(1, min(int(runs), 5000))):
            left = sum(champion[rng.randrange(len(champion))] for _ in champion) / Decimal(len(champion))
            right = sum(challenger[rng.randrange(len(challenger))] for _ in challenger) / Decimal(len(challenger))
            differences.append(right - left)
        differences.sort(); n = len(differences)
        return {"status": "PASS", "seed": seed, "runs": n,
                "difference_r": {"p05": str(differences[int(.05 * (n - 1))]),
                                 "median": str(differences[n // 2]),
                                 "p95": str(differences[int(.95 * (n - 1))])},
                "limitations": ["Bootstrap intervals are uncertain for clustered or dependent returns."]}

    def compare(self, request: dict, *, owner_id: str) -> dict:
        champion_id = request.get("champion_candidate_id")
        challenger_id = request.get("challenger_candidate_id")
        if not champion_id or not challenger_id or champion_id == challenger_id:
            raise ValueError("distinct champion and challenger candidates are required")
        champion = self.candidate(champion_id, owner_id=owner_id)
        challenger = self.candidate(challenger_id, owner_id=owner_id)
        if not champion or not challenger:
            raise KeyError("candidate not found")
        policy_values = request.get("policy") or {}
        allowed_policy = set(ChallengerEvaluationPolicy.__dataclass_fields__)
        try:
            policy = ChallengerEvaluationPolicy(**{key: value for key, value in policy_values.items() if key in allowed_policy})
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid challenger policy: {exc}") from None
        # A completed worker result is the authoritative evidence for this
        # experiment even when the candidate was registered before execution.
        overrides = request.get("validation_overrides") or {}
        for candidate_id, label in ((champion_id, "champion"), (challenger_id, "challenger")):
            evidence = overrides.get(candidate_id)
            if not isinstance(evidence, dict):
                continue
            dataset = evidence.get("dataset") or {}
            coverage = dataset.get("coverage") or evidence.get("coverage")
            target = champion if label == "champion" else challenger
            target = {**target}
            for key, source_key in (("dataset_identity", "dataset_id"), ("data_revision", "content_hash"),
                                    ("period_start", "period_start"), ("period_end", "period_end")):
                if dataset.get(source_key) is not None:
                    target[key] = dataset[source_key]
            target["evaluation_coverage"] = coverage or {"status": "UNKNOWN"}
            if label == "champion": champion = target
            else: challenger = target
        if overrides:
            if (overrides.get(champion_id) or {}).get("status") == "COMPLETE":
                champion = {**champion, "evidence_quality": "COMPLETE"}
            if (overrides.get(challenger_id) or {}).get("status") == "COMPLETE":
                challenger = {**challenger, "evidence_quality": "COMPLETE"}
        gate = self.comparability(champion, challenger)
        report = {"contract_version": "strategy_comparison.v1", "champion": champion,
                  "challenger": challenger, "comparability": gate,
                  "policy": policy.as_dict(), "calculated_at": datetime.now(timezone.utc).isoformat()}
        checks = []
        if not gate["compatible"]:
            recommendation = "NOT_COMPARABLE"; checks.append(_check(False, name="comparability", reason="; ".join(gate["reasons"])))
        else:
            champion_run = overrides.get(champion_id) or self._latest_report(champion)
            challenger_run = overrides.get(challenger_id) or self._latest_report(challenger)
            c_exp = _metric(champion_run, "expectancy_r", "net_expectancy_r")
            h_exp = _metric(challenger_run, "expectancy_r", "net_expectancy_r")
            c_dd = _metric(champion_run, "max_drawdown_r", "drawdown_r")
            h_dd = _metric(challenger_run, "max_drawdown_r", "drawdown_r")
            diff = h_exp - c_exp if h_exp is not None and c_exp is not None else None
            dd_diff = h_dd - c_dd if h_dd is not None and c_dd is not None else None
            report["metrics"] = {"champion_expectancy_r": str(c_exp) if c_exp is not None else None,
                                  "challenger_expectancy_r": str(h_exp) if h_exp is not None else None,
                                  "expectancy_difference_r": str(diff) if diff is not None else None,
                                  "champion_drawdown_r": str(c_dd) if c_dd is not None else None,
                                  "challenger_drawdown_r": str(h_dd) if h_dd is not None else None,
                                  "drawdown_difference_r": str(dd_diff) if dd_diff is not None else None}
            checks.extend([
                _check(champion.get("evidence_quality") in {"COMPLETE", "VERIFIED"} and challenger.get("evidence_quality") in {"COMPLETE", "VERIFIED"}, name="evidence_quality", reason="both candidates require complete or verified evidence"),
                _check(diff is not None and diff > _decimal(policy.minimum_expectancy_improvement_r), name="expectancy_improvement", reason=f"challenger expectancy improvement must exceed {policy.minimum_expectancy_improvement_r}"),
                _check(dd_diff is None or dd_diff <= _decimal(policy.maximum_drawdown_regression_r), name="drawdown_tradeoff", reason="challenger drawdown regression exceeds policy"),
                _check((champion.get("scope") or {}).get("funding_coverage") in {"COMPLETE", "APPROVED_CONSERVATIVE_MODEL"} and (challenger.get("scope") or {}).get("funding_coverage") in {"COMPLETE", "APPROVED_CONSERVATIVE_MODEL"} if policy.require_complete_costs else True, name="cost_coverage", reason="both candidates require complete or approved funding coverage"),
            ])
            bootstrap = self._bootstrap_difference(self._outcomes(champion), self._outcomes(challenger), runs=policy.bootstrap_runs, seed=int(request.get("random_seed", 1)))
            report["uncertainty"] = bootstrap
            statuses = {check["status"] for check in checks}
            if "UNKNOWN" in statuses:
                recommendation = "INSUFFICIENT_EVIDENCE"
            elif all(check["status"] == "PASS" for check in checks):
                recommendation = "CHALLENGER_PREFERRED"
            elif any(check["name"] == "expectancy_improvement" and check["status"] == "FAIL" for check in checks):
                recommendation = "CHAMPION_RETAINED"
            else:
                recommendation = "INCONCLUSIVE"
        report["checks"] = checks
        reasons = [check["reason"] for check in checks if check["status"] != "PASS"]
        comparison = {"comparison_id": "comparison:" + uuid.uuid4().hex, "idempotency_key": request.get("idempotency_key") or uuid.uuid4().hex,
                      "owner_id": owner_id, "champion_candidate_id": champion_id, "challenger_candidate_id": challenger_id,
                      "scope": request.get("scope") or champion.get("scope") or {}, "policy_version": policy.policy_version,
                      "statistical_method": "seeded_bootstrap" if gate["compatible"] else "comparability_gate",
                      "random_seed": request.get("random_seed", 1), "status": "COMPLETED",
                      "recommendation": recommendation, "blocking_reasons": reasons, "report": report,
                      "validation_run_ids": list(dict.fromkeys((request.get("validation_run_ids") or []) + champion.get("validation_run_ids", []) + challenger.get("validation_run_ids", [])))}
        return self.context.record_strategy_comparison(comparison)

    def _latest_report(self, candidate):
        for run_id in reversed(candidate.get("validation_run_ids") or []):
            report = self.context.get_validation_run(run_id)
            if report and report.get("status") == "COMPLETE":
                return report.get("report") or report
        return {}

    def comparisons(self, *, owner_id, limit=100):
        return self.context.strategy_comparisons(owner_id=owner_id, limit=limit)

    def degradation(self, candidate_id: str, *, owner_id: str) -> dict:
        candidate = self.candidate(candidate_id, owner_id=owner_id)
        if not candidate:
            raise KeyError(candidate_id)
        reports = [self.context.get_validation_run(run_id) for run_id in candidate.get("validation_run_ids", [])]
        reports = [item for item in reports if item and item.get("status") == "COMPLETE"]
        if len(reports) < 2:
            return {"status": "UNKNOWN", "reason": "Insufficient independent recent evidence", "candidate_id": candidate_id}
        values = [_metric(item.get("report") or item, "expectancy_r", "net_expectancy_r") for item in reports]
        values = [value for value in values if value is not None]
        if len(values) < 2:
            return {"status": "UNKNOWN", "reason": "Recent expectancy is unavailable", "candidate_id": candidate_id}
        delta = values[-1] - values[0]
        status = "DEGRADED" if delta < 0 and values[-1] < 0 else "WATCH" if delta < 0 else "STABLE"
        return {"status": status, "candidate_id": candidate_id, "expectancy_delta_r": str(delta),
                "evidence_count": len(values), "advisory": True}
