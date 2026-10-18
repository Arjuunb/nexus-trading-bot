"""Controlled, research-only strategy hypothesis agent.

This module is deliberately deterministic: it proposes structured plans from
persisted evidence, validates allowlisted configuration changes, and delegates
approved execution to the existing matched-experiment/research-worker stack.
It has no trading or financial-store dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import uuid

from services.strategy_identity import canonical_configuration_json, configuration_fingerprint
from services.strategy_comparison import StrategyComparisonService
from services.matched_experiments import MatchedExperimentService


CAPABILITIES = frozenset({"READ_EVIDENCE", "PROPOSE_HYPOTHESIS", "PROPOSE_CONFIGURATION",
                          "CREATE_DRAFT_EXPERIMENT", "REQUEST_APPROVAL", "SUBMIT_APPROVED_RESEARCH",
                          "REVIEW_RESULTS", "GENERATE_REPORT"})
FORBIDDEN_CAPABILITIES = frozenset({"PLACE_ORDER", "MODIFY_LIVE_STRATEGY", "CHANGE_LEVERAGE",
                                    "CHANGE_PAPER_ACCOUNT", "PROMOTE_CHAMPION", "EXECUTE_CODE"})


@dataclass(frozen=True)
class ResearchAgentPolicy:
    policy_version: str = "research-agent.v1"
    allowed_strategies: tuple[str, ...] = ("adaptive_trend_pullback",)
    allowed_parameters: tuple[str, ...] = ("min_score", "rr", "trend_filter", "volume_filter",
                                           "regime_filter", "session_filter")
    max_hypotheses_per_campaign: int = 5
    max_variations_per_hypothesis: int = 3
    max_challengers: int = 2
    max_simultaneous_jobs: int = 2
    max_bars: int = 10000
    max_walk_forward_windows: int = 12
    max_monte_carlo_iterations: int = 5000
    max_runtime_seconds: int = 3600
    allowed_market_types: tuple[str, ...] = ("futures", "spot")
    allowed_methodologies: tuple[str, ...] = ("OUT_OF_SAMPLE_VALIDATION", "ANCHORED_WALK_FORWARD", "ROLLING_WALK_FORWARD")
    holdout_requires_untouched: bool = True

    def as_dict(self):
        return asdict(self)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _plan_fingerprint(plan: dict) -> str:
    return hashlib.sha256(canonical_configuration_json(plan).encode()).hexdigest()


class ResearchAgentService:
    def __init__(self, context_store):
        self.context = context_store
        self.comparisons = StrategyComparisonService(context_store)
        self.matched = MatchedExperimentService(context_store)

    @staticmethod
    def policy() -> ResearchAgentPolicy:
        return ResearchAgentPolicy()

    @staticmethod
    def capabilities() -> list[str]:
        return sorted(CAPABILITIES)

    @staticmethod
    def lookback_spec(strategy_id: str, config: dict) -> dict | None:
        """Hook for reviewed strategy dependency specifications.

        No universal lookback is guessed here; callers must supply a reviewed
        specification until each strategy's dependencies are audited.
        """
        return None

    def _event(self, owner_id, event_type, *, actor_id="research-agent", hypothesis_id=None, payload=None):
        return self.context.record_agent_event({"event_id": "agent-event:" + uuid.uuid4().hex,
            "owner_id": owner_id, "hypothesis_id": hypothesis_id, "event_type": event_type,
            "actor_id": actor_id, "payload": payload or {}})

    def validate_variation(self, *, strategy_id: str, baseline: dict, proposed: dict) -> dict:
        policy = self.policy(); errors = []
        if strategy_id not in policy.allowed_strategies:
            errors.append("strategy is not allowlisted for research-agent proposals")
        if not isinstance(proposed, dict):
            errors.append("proposed configuration must be an object")
            proposed = {}
        changed = sorted(key for key in set(baseline) | set(proposed) if baseline.get(key) != proposed.get(key))
        disallowed = [key for key in changed if key not in policy.allowed_parameters]
        if disallowed:
            errors.append("fixed or unallowlisted parameters: " + ", ".join(disallowed))
        if not changed:
            errors.append("proposed configuration must differ from baseline")
        if len(changed) > policy.max_variations_per_hypothesis:
            errors.append("variation exceeds the research parameter budget")
        if "rr" in proposed and (not isinstance(proposed["rr"], (int, float)) or not 0.1 <= float(proposed["rr"]) <= 10):
            errors.append("rr must be between 0.1 and 10")
        if "min_score" in proposed and (not isinstance(proposed["min_score"], int) or not 0 <= proposed["min_score"] <= 100):
            errors.append("min_score must be an integer from 0 to 100")
        return {"ok": not errors, "errors": errors, "changed_parameters": changed,
                "baseline_fingerprint": configuration_fingerprint(baseline),
                "proposed_fingerprint": configuration_fingerprint(proposed)}

    def generate_hypothesis(self, request: dict, *, owner_id: str) -> dict:
        candidate_id = request.get("candidate_id")
        candidate = self.comparisons.candidate(candidate_id, owner_id=owner_id) if candidate_id else None
        if not candidate:
            raise KeyError("research candidate not found")
        evidence = candidate.get("validation_run_ids") or []
        if not evidence:
            raise ValueError("INSUFFICIENT_EVIDENCE: hypothesis requires committed validation references")
        missing = [run_id for run_id in evidence if not (self.context.get_validation_run(run_id) or {}).get("status") == "COMPLETED"]
        if missing:
            raise ValueError("INSUFFICIENT_EVIDENCE: validation references are not committed completed runs: " + ", ".join(missing))
        campaign_id = request.get("campaign_id")
        if campaign_id:
            existing = [item for item in self.context.agent_hypotheses(owner_id=owner_id, limit=1000)
                        if item.get("campaign_id") == campaign_id]
            if len(existing) >= self.policy().max_hypotheses_per_campaign:
                raise ValueError("research campaign hypothesis budget exceeded")
        baseline = dict(candidate.get("frozen_config") or {})
        proposed = dict(request.get("proposed_config") or baseline)
        validation = self.validate_variation(strategy_id=candidate["strategy_id"], baseline=baseline, proposed=proposed)
        if not validation["ok"]: raise ValueError("; ".join(validation["errors"]))
        question = str(request.get("question") or "Does the proposed research-only configuration improve robustness under the recorded conditions?").strip()
        plan = {"baseline_config": baseline, "proposed_config": proposed,
                "baseline_candidate_id": candidate_id,
                "dataset_scope": request.get("dataset_scope") or candidate.get("scope") or {},
                "methodology": request.get("methodology") or {"method": "OUT_OF_SAMPLE_VALIDATION"},
                "cost_assumptions": request.get("cost_assumptions") or {},
                "lookback_verified": bool(request.get("lookback_verified", False)),
                "lookback_spec": request.get("lookback_spec") or self.lookback_spec(candidate["strategy_id"], baseline),
                "policy_version": self.policy().policy_version}
        hypothesis = {"hypothesis_id": request.get("hypothesis_id") or "hypothesis:" + uuid.uuid4().hex,
                      "campaign_id": request.get("campaign_id"), "owner_id": owner_id,
                      "strategy_id": candidate["strategy_id"], "strategy_version": candidate.get("strategy_version"),
                      "baseline_fingerprint": validation["baseline_fingerprint"], "proposed_fingerprint": validation["proposed_fingerprint"],
                      "question": question, "expected_effect": request.get("expected_effect"),
                      "evidence_refs": evidence, "uncertainties": request.get("uncertainties") or ["Historical evidence does not guarantee future performance."],
                      "methodology": plan["methodology"], "dataset_scope": plan["dataset_scope"],
                      "cost_assumptions": plan["cost_assumptions"], "agent_version": "deterministic-agent.v1",
                      "status": "DRAFT", "plan": plan, "created_at": _now()}
        result = self.context.create_agent_hypothesis(hypothesis)
        self._event(owner_id, "HYPOTHESIS_CREATED", hypothesis_id=result["hypothesis_id"], payload={"evidence_refs": evidence})
        return result

    def validate_plan(self, hypothesis_id: str, *, owner_id: str) -> dict:
        hypothesis = self.context.get_agent_hypothesis(hypothesis_id, owner_id=owner_id)
        if not hypothesis: raise KeyError(hypothesis_id)
        plan = hypothesis["plan"]; errors = []
        variation = self.validate_variation(strategy_id=hypothesis["strategy_id"], baseline=plan.get("baseline_config", {}), proposed=plan.get("proposed_config", {}))
        errors.extend(variation["errors"])
        scope = plan.get("dataset_scope") or {}
        for field in ("dataset_identity", "data_revision", "period_start", "period_end"):
            if not scope.get(field): errors.append(f"missing dataset scope field: {field}")
        if not plan.get("lookback_verified") or not plan.get("lookback_spec"):
            errors.append("LOOKBACK_UNVERIFIED: reviewed lookback specification is required")
        costs = plan.get("cost_assumptions") or {}
        if costs.get("funding") in (None, "", "UNKNOWN"): errors.append("funding cost coverage is unknown")
        method = (plan.get("methodology") or {}).get("method", "")
        if method not in self.policy().allowed_methodologies:
            errors.append("research methodology is not allowlisted")
        market_type = scope.get("market_type")
        if market_type and market_type not in self.policy().allowed_market_types:
            errors.append("market type is not allowlisted")
        if scope.get("holdout_status") == "CONTAMINATED" and self.policy().holdout_requires_untouched:
            errors.append("holdout is contaminated and cannot support independent research")
        if int(scope.get("bars", 0) or 0) > self.policy().max_bars:
            errors.append("dataset exceeds the research bar budget")
        if int((plan.get("methodology") or {}).get("walk_forward_windows", 0) or 0) > self.policy().max_walk_forward_windows:
            errors.append("walk-forward window budget exceeded")
        result = {"valid": not errors, "errors": errors, "plan_fingerprint": _plan_fingerprint(plan), "policy": self.policy().as_dict()}
        self._event(owner_id, "PLAN_VALIDATED", hypothesis_id=hypothesis_id, payload=result)
        return result

    def approve(self, hypothesis_id: str, *, owner_id: str, reviewer_id: str, decision="APPROVED", max_jobs=2) -> dict:
        validation = self.validate_plan(hypothesis_id, owner_id=owner_id)
        if decision == "APPROVED" and not validation["valid"]: raise ValueError("plan is not eligible for approval: " + "; ".join(validation["errors"]))
        if decision not in {"APPROVED", "REJECTED"}: raise ValueError("invalid approval decision")
        approval = self.context.create_agent_approval({"approval_id": "approval:" + uuid.uuid4().hex,
            "hypothesis_id": hypothesis_id, "owner_id": owner_id, "plan_fingerprint": validation["plan_fingerprint"],
            "reviewer_id": reviewer_id, "decision": decision, "max_jobs": min(int(max_jobs), self.policy().max_simultaneous_jobs),
            "resource_budget": {"max_runtime_seconds": self.policy().max_runtime_seconds}, "approved_at": _now()})
        self.context.update_agent_hypothesis(hypothesis_id, owner_id=owner_id, status="APPROVED" if decision == "APPROVED" else "REJECTED")
        self._event(owner_id, "APPROVAL_RECORDED", actor_id=reviewer_id, hypothesis_id=hypothesis_id, payload={"approval_id": approval["approval_id"], "decision": decision})
        return approval

    def submit_approved(self, hypothesis_id: str, *, owner_id: str) -> dict:
        hypothesis = self.context.get_agent_hypothesis(hypothesis_id, owner_id=owner_id)
        if not hypothesis: raise KeyError(hypothesis_id)
        validation = self.validate_plan(hypothesis_id, owner_id=owner_id)
        approval = self.context.agent_approval(hypothesis_id, owner_id=owner_id, plan_fingerprint=validation["plan_fingerprint"])
        if not approval or approval.get("decision") != "APPROVED": raise PermissionError("explicit approval for the current frozen plan is required")
        plan = hypothesis["plan"]; candidate = self.comparisons.create_candidate({
            "strategy_id": hypothesis["strategy_id"], "strategy_version": hypothesis.get("strategy_version"),
            "config_fingerprint": hypothesis["proposed_fingerprint"], "frozen_config": plan["proposed_config"],
            "dataset_identity": plan["dataset_scope"]["dataset_identity"], "data_revision": plan["dataset_scope"]["data_revision"],
            "symbol": plan["dataset_scope"].get("symbol"), "venue": plan["dataset_scope"].get("venue"),
            "market_type": plan["dataset_scope"].get("market_type"), "timeframe": plan["dataset_scope"].get("timeframe"),
            "execution_mode": "backtest", "cost_model_version": plan.get("cost_assumptions", {}).get("version"),
            "scope": plan["dataset_scope"], "evidence_quality": "UNKNOWN"}, owner_id=owner_id)
        experiment = self.matched.create_experiment({"idempotency_key": f"agent:{hypothesis_id}:{approval['plan_fingerprint']}",
            "champion_candidate_id": hypothesis["plan"].get("baseline_candidate_id") or hypothesis.get("baseline_candidate_id"),
            "challenger_candidate_ids": [candidate["candidate_id"]], "method": plan["methodology"].get("method", "OUT_OF_SAMPLE_VALIDATION"),
            "scope": plan["dataset_scope"], "policy_version": plan.get("policy_version"), "comparison_policy_version": "challenger.v2",
            "cost_model_version": plan.get("cost_assumptions", {}).get("version"), "warmup": plan.get("warmup", 0)}, owner_id=owner_id)
        self.context.update_agent_hypothesis(hypothesis_id, owner_id=owner_id, status="SUBMITTED")
        self._event(owner_id, "RESEARCH_SUBMITTED", hypothesis_id=hypothesis_id, payload={"experiment_id": experiment["experiment_id"], "candidate_id": candidate["candidate_id"]})
        return experiment

    def review(self, hypothesis_id: str, *, owner_id: str) -> dict:
        hypothesis = self.context.get_agent_hypothesis(hypothesis_id, owner_id=owner_id)
        if not hypothesis: raise KeyError(hypothesis_id)
        events = self.context.agent_events(owner_id=owner_id, hypothesis_id=hypothesis_id)
        submitted = next((event for event in events if event["event_type"] == "RESEARCH_SUBMITTED"), None)
        if not submitted: return {"outcome": "INSUFFICIENT_EVIDENCE", "reason": "No submitted research experiment", "hypothesis": hypothesis}
        experiment = self.matched.experiment(submitted["payload"].get("experiment_id"), owner_id=owner_id)
        if not experiment or experiment.get("status") != "COMPLETED":
            return {"outcome": "INCONCLUSIVE", "reason": "Committed completed research is unavailable", "hypothesis": hypothesis, "experiment": experiment}
        mapping = {"CHALLENGER_PREFERRED": "SUPPORTED", "CHAMPION_RETAINED": "NOT_SUPPORTED"}
        outcome = mapping.get(experiment.get("recommendation"), "INCONCLUSIVE")
        return {"outcome": outcome, "hypothesis": hypothesis, "experiment": experiment,
                "limitations": ["Supported means supported under recorded research conditions, not future profitability."]}

    def forbidden_action(self, action: str):
        if action in FORBIDDEN_CAPABILITIES or action not in CAPABILITIES:
            raise PermissionError(f"agent capability is not permitted: {action}")
