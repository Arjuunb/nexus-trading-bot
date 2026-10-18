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
    policy_version: str = "challenger.v2"
    minimum_episodes: int = 30
    minimum_expectancy_improvement_r: str = "0"
    maximum_drawdown_regression_r: str = "0"
    require_complete_costs: bool = True
    require_positive_oos: bool = True
    require_walk_forward: bool = True
    bootstrap_runs: int = 1000
    confidence_level: str = "0.90"

    def __post_init__(self):
        if (self.policy_version != "challenger.v2"
                or type(self.minimum_episodes) is not int or not 30 <= self.minimum_episodes <= 1_000_000
                or type(self.bootstrap_runs) is not int or not 100 <= self.bootstrap_runs <= 5000
                or any(value is not True for value in (self.require_complete_costs,
                    self.require_positive_oos, self.require_walk_forward))):
            raise ValueError("invalid or weakened challenger policy")
        for value in (self.minimum_expectancy_improvement_r, self.maximum_drawdown_regression_r):
            if _decimal(value) is None or _decimal(value) < 0:
                raise ValueError("invalid challenger policy threshold")
        if _decimal(self.confidence_level) is None or not 0 < _decimal(self.confidence_level) < 1:
            raise ValueError("invalid challenger policy confidence level")

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
                if status not in {"COMPLETE", "VERIFIED"}:
                    reasons.append(f"{label} evaluation coverage is {status}")
                if isinstance(coverage, dict) and coverage.get("missing_candles") not in (None, 0):
                    reasons.append(f"{label} has missing critical candles")
        return {"compatible": not reasons, "reasons": reasons}

    @staticmethod
    def _outcomes(metrics):
        # Called only after Task 1's independent raw financial reconciliation.
        return [Decimal(row["net_pnl"]) / Decimal(row["initial_risk"])
                for row in metrics["financial_samples"]]

    @staticmethod
    def _bootstrap_difference(champion, challenger, *, runs, seed, confidence_level="0.90"):
        if not champion or not challenger:
            return {"status": "UNKNOWN", "reason": "Matched episode outcomes are unavailable", "seed": seed}
        rng = random.Random(seed); differences = []
        for _ in range(max(1, min(int(runs), 5000))):
            left = sum(champion[rng.randrange(len(champion))] for _ in champion) / Decimal(len(champion))
            right = sum(challenger[rng.randrange(len(challenger))] for _ in challenger) / Decimal(len(challenger))
            differences.append(right - left)
        differences.sort(); n = len(differences)
        tail = (1 - Decimal(str(confidence_level))) / 2
        return {"status": "PASS", "seed": seed, "runs": n, "confidence_level": confidence_level,
                "lower_bound_r": str(differences[int(tail * (n - 1))]),
                "upper_bound_r": str(differences[int((1-tail) * (n - 1))]),
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
        if not isinstance(policy_values, dict) or set(policy_values) - allowed_policy:
            raise ValueError("invalid challenger policy fields")
        try:
            policy = ChallengerEvaluationPolicy(**policy_values)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid challenger policy: {exc}") from None
        # Candidate claims and worker payload overrides are not financial
        # authority. Resolve only immutable committed references, scoped before
        # inspecting a report. A worker can supply references, never evidence.
        references = request.get("validation_run_ids") or []
        champion_evidence = self._bound_evidence(champion, references)
        challenger_evidence = self._bound_evidence(challenger, references)
        for candidate, evidence in ((champion, champion_evidence), (challenger, challenger_evidence)):
            if evidence.get("complete"):
                candidate.update(evidence["comparability"])
                candidate["evidence_quality"] = "COMPLETE"
                candidate["validation_run_ids"] = list(evidence["provenance"]["validation_run_ids"].values())
        gate = self.comparability(champion, challenger)
        if (champion_evidence.get("complete") and challenger_evidence.get("complete")
                and champion_evidence["cost_models"] != challenger_evidence["cost_models"]):
            gate["compatible"] = False
            gate["reasons"].append("original financial cost models differ between candidates")
        report = {"contract_version": "strategy_comparison.v1", "champion": champion,
                  "challenger": challenger, "comparability": gate,
                  "advisory_only": True, "orders_allowed": False, "qualification_granted": False,
                  "policy": policy.as_dict(), "calculated_at": datetime.now(timezone.utc).isoformat()}
        checks = []
        if not gate["compatible"]:
            recommendation = "NOT_COMPARABLE"; checks.append(_check(False, name="comparability", reason="; ".join(gate["reasons"])))
        else:
            c_metrics = champion_evidence.get("metrics") or {}
            h_metrics = challenger_evidence.get("metrics") or {}
            c_exp = _decimal(c_metrics.get("expectancy_net_r"))
            h_exp = _decimal(h_metrics.get("expectancy_net_r"))
            c_dd = _decimal(c_metrics.get("max_drawdown_r"))
            h_dd = _decimal(h_metrics.get("max_drawdown_r"))
            diff = h_exp - c_exp if h_exp is not None and c_exp is not None else None
            dd_diff = h_dd - c_dd if h_dd is not None and c_dd is not None else None
            report["evidence_basis"] = "bound_out_of_sample_financial_samples"
            report["evidence"] = {"champion": champion_evidence.get("provenance"),
                                  "challenger": challenger_evidence.get("provenance")}
            report["metrics"] = {"champion_expectancy_r": str(c_exp) if c_exp is not None else None,
                                  "challenger_expectancy_r": str(h_exp) if h_exp is not None else None,
                                  "expectancy_difference_r": str(diff) if diff is not None else None,
                                  "champion_drawdown_r": str(c_dd) if c_dd is not None else None,
                                  "challenger_drawdown_r": str(h_dd) if h_dd is not None else None,
                                  "drawdown_difference_r": str(dd_diff) if dd_diff is not None else None}
            complete = champion_evidence.get("complete") and challenger_evidence.get("complete")
            checks.extend([
                _check(True if complete else None, name="bound_financial_evidence",
                    reason="both candidates require owned, frozen historical, OOS, walk-forward, robustness and cost-stress financial evidence"),
                _check(c_metrics["completed_episode_count"] >= policy.minimum_episodes
                    and h_metrics["completed_episode_count"] >= policy.minimum_episodes if complete else None,
                    name="minimum_episodes", reason=f"both independent holdouts require at least {policy.minimum_episodes} closed episodes"),
                _check(c_exp > 0 and h_exp > 0 if c_exp is not None and h_exp is not None else None,
                    name="positive_oos", reason="both candidates require positive independent OOS expectancy after costs"),
                _check(diff > _decimal(policy.minimum_expectancy_improvement_r) if diff is not None else None,
                    name="expectancy_improvement", reason=f"challenger expectancy improvement must exceed {policy.minimum_expectancy_improvement_r}"),
                _check(dd_diff <= _decimal(policy.maximum_drawdown_regression_r) if dd_diff is not None else None,
                    name="drawdown_tradeoff", reason="both drawdowns must be known and challenger regression within policy"),
            ])
            bootstrap = self._bootstrap_difference(self._outcomes(c_metrics) if complete else [],
                self._outcomes(h_metrics) if complete else [], runs=policy.bootstrap_runs,
                seed=int(request.get("random_seed", 1)), confidence_level=policy.confidence_level)
            report["uncertainty"] = bootstrap
            lower = _decimal(bootstrap.get("lower_bound_r"))
            checks.append(_check(lower > _decimal(policy.minimum_expectancy_improvement_r) if lower is not None else None,
                name="bootstrap_uncertainty", reason="the seeded bootstrap lower bound must support improvement; dependent returns remain a limitation"))
            statuses = {check["status"] for check in checks}
            if "UNKNOWN" in statuses or any(check["name"] == "minimum_episodes" and check["status"] != "PASS" for check in checks):
                recommendation = "INSUFFICIENT_EVIDENCE"
            elif all(check["status"] == "PASS" for check in checks):
                recommendation = "CHALLENGER_PREFERRED"
            elif any(check["name"] == "positive_oos" and check["status"] == "FAIL" for check in checks):
                recommendation = "INCONCLUSIVE"
            elif any(check["name"] == "expectancy_improvement" and check["status"] == "FAIL" for check in checks):
                recommendation = "CHAMPION_RETAINED"
            else:
                recommendation = "INCONCLUSIVE"
        report["checks"] = checks
        reasons = [check["reason"] for check in checks if check["status"] != "PASS"]
        comparison = {"comparison_id": "comparison:" + uuid.uuid4().hex, "idempotency_key": request.get("idempotency_key") or uuid.uuid4().hex,
                      "advisory_only": True, "orders_allowed": False, "qualification_granted": False,
                      "owner_id": owner_id, "champion_candidate_id": champion_id, "challenger_candidate_id": challenger_id,
                      "scope": request.get("scope") or champion.get("scope") or {}, "policy_version": policy.policy_version,
                      "statistical_method": "seeded_bootstrap" if gate["compatible"] else "comparability_gate",
                      "random_seed": request.get("random_seed", 1), "status": "COMPLETED",
                      "recommendation": recommendation, "blocking_reasons": reasons, "report": report,
                      "validation_run_ids": list(dict.fromkeys((request.get("validation_run_ids") or []) + champion.get("validation_run_ids", []) + challenger.get("validation_run_ids", [])))}
        return self.context.record_strategy_comparison(comparison)

    def _bound_evidence(self, candidate, references):
        from services.historical_forward_evidence import build_evidence_chain, _authorized
        from services.strategy_identity import configuration_fingerprint
        scope = {key: (candidate.get("scope") or {}).get(key) for key in
                 ("account_id", "instance_id", "simulation_session_id", "lab_id")}
        scope["owner_id"] = candidate["owner_id"]
        rows = []
        for reference in dict.fromkeys([*(candidate.get("validation_run_ids") or []), *references]):
            row = self.context.get_validation_run(reference)
            if (row and _authorized(row, scope) and row.get("lab_id") == scope["lab_id"]
                    and all(row.get(key) == candidate.get(key) for key in
                        ("strategy_id", "strategy_version", "config_fingerprint"))):
                rows.append(row)
        base = next((row for row in rows if row.get("method") == "historical_backtest"), {})
        report = base.get("report") or {}
        frozen = report.get("identity") if isinstance(report, dict) else None
        try:
            if (not frozen or frozen.get("configuration") != candidate.get("frozen_config")
                    or configuration_fingerprint(candidate.get("frozen_config")) != candidate.get("config_fingerprint")):
                return {"complete": False}
            chain = build_evidence_chain(scope=scope, identity={**candidate, **scope}, frozen=frozen,
                                         records=rows, performance={})
        except (TypeError, ValueError, KeyError, AttributeError):
            return {"complete": False}
        required = ("historical_backtest", "out_of_sample", "walk_forward", "robustness", "cost_stress")
        if any(chain["stages"][method]["binding_status"] != "BOUND"
               or chain["stages"][method]["status"] not in {"COMPLETE", "PASS"} for method in required):
            return {"complete": False}
        reports = chain["reports"]
        dataset = reports["historical_backtest"]["dataset"]
        if (candidate.get("dataset_identity") != dataset["dataset_id"]
                or candidate.get("data_revision") != dataset["content_hash"]
                or candidate.get("timeframe") != dataset["timeframe"]):
            return {"complete": False}
        financial = [reports["historical_backtest"]["metrics"], reports["robustness"]["base"], reports["cost_stress"]["base"]]
        financial.extend(reports["out_of_sample"][key] for key in ("train", "validation", "holdout"))
        financial.extend(window[key] for window in reports["walk_forward"]["windows"] for key in ("development", "evaluation"))
        cost_models = {key: sorted({sample[key] for metrics in financial for sample in metrics["financial_samples"]})
                       for key in ("fees_cost_model", "funding_cost_model", "slippage_cost_model")}
        return {"complete": True, "metrics": reports["out_of_sample"]["holdout"],
            "cost_models": cost_models,
            "provenance": {"validation_run_ids": chain["validation_run_ids"], "dataset": dataset,
                           "cost_models": cost_models,
                           "identity": {key: candidate[key] for key in
                               ("strategy_id", "strategy_version", "config_fingerprint")}},
            "comparability": {"evaluation_coverage": dataset["coverage"], "period_start": dataset["period_start"],
                "period_end": dataset["period_end"]}}

    def comparisons(self, *, owner_id, limit=100):
        return self.context.strategy_comparisons(owner_id=owner_id, limit=limit)

    def degradation(self, candidate_id: str, *, owner_id: str) -> dict:
        candidate = self.candidate(candidate_id, owner_id=owner_id)
        if not candidate:
            raise KeyError(candidate_id)
        # Independent rolling decay is measured only over actual forward-paper
        # episodes by Task 4. Repeated backtests are not new paper observations.
        return {"status": "UNKNOWN", "candidate_id": candidate_id, "advisory": True,
                "reason": "Use exact-cohort /decay monitoring over independent forward-paper episodes"}
