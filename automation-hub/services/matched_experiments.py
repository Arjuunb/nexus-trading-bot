"""Durable matched-experiment orchestration over Sprint 4.6 research jobs."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

from services.research_jobs import ResearchJobService
from services.strategy_comparison import StrategyComparisonService, ChallengerEvaluationPolicy


EXPERIMENT_METHODS = {"HISTORICAL_BACKTEST", "OUT_OF_SAMPLE_VALIDATION",
                      "ANCHORED_WALK_FORWARD", "ROLLING_WALK_FORWARD",
                      "COST_STRESS", "ROBUSTNESS_EVALUATION"}
TERMINAL_JOBS = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}


def _now():
    return datetime.now(timezone.utc).isoformat()


class MatchedExperimentService:
    MAX_CHALLENGERS = 8

    def __init__(self, context_store):
        self.context = context_store
        self.jobs = ResearchJobService(context_store)
        self.comparisons = StrategyComparisonService(context_store)

    def create_campaign(self, request: dict, *, owner_id: str) -> dict:
        candidates = request.get("candidate_ids") or []
        if len(candidates) > self.MAX_CHALLENGERS + 1:
            raise ValueError("research campaign candidate limit exceeded")
        return self.context.create_research_campaign({
            "campaign_id": request.get("campaign_id") or "campaign:" + uuid.uuid4().hex,
            "idempotency_key": request.get("idempotency_key") or uuid.uuid4().hex,
            "owner_id": owner_id, "strategy_family": request.get("strategy_family"),
            "selection_criteria": request.get("selection_criteria") or {},
            "policy_versions": request.get("policy_versions") or {}, "candidate_count": len(candidates),
            "status": "DRAFT", "created_at": _now()})

    def campaign(self, campaign_id, *, owner_id):
        return self.context.get_research_campaign(campaign_id, owner_id=owner_id)

    def campaigns(self, *, owner_id, limit=100):
        return self.context.research_campaigns(owner_id=owner_id, limit=limit)

    @staticmethod
    def _structural_reasons(champion, candidate):
        fields = ("dataset_identity", "data_revision", "period_start", "period_end", "venue", "market_type",
                  "symbol", "timeframe", "execution_mode", "cost_model_version", "funding_coverage",
                  "slippage_model", "method", "oos_status", "classifier_version")
        reasons = []
        for field in fields:
            left = champion.get(field, (champion.get("scope") or {}).get(field))
            right = candidate.get(field, (candidate.get("scope") or {}).get(field))
            if left != right:
                reasons.append(f"{field} mismatch: champion={left or 'UNKNOWN'} challenger={right or 'UNKNOWN'}")
        return reasons

    def create_experiment(self, request: dict, *, owner_id: str) -> dict:
        champion_id = request.get("champion_candidate_id")
        challengers = list(dict.fromkeys(request.get("challenger_candidate_ids") or []))
        if not champion_id or not challengers:
            raise ValueError("a champion and at least one challenger are required")
        if len(challengers) > self.MAX_CHALLENGERS:
            raise ValueError(f"at most {self.MAX_CHALLENGERS} challengers are allowed")
        if champion_id in challengers:
            raise ValueError("champion cannot also be a challenger")
        champion = self.comparisons.candidate(champion_id, owner_id=owner_id)
        candidates = [self.comparisons.candidate(candidate_id, owner_id=owner_id) for candidate_id in challengers]
        if not champion or any(candidate is None for candidate in candidates):
            raise KeyError("candidate not found")
        reasons = [reason for candidate in candidates for reason in self._structural_reasons(champion, candidate)]
        method = request.get("method", "OUT_OF_SAMPLE_VALIDATION")
        if method not in EXPERIMENT_METHODS:
            raise ValueError("unsupported matched research method")
        comparison_policy = request.get("comparison_policy_version") or "challenger.v2"
        ChallengerEvaluationPolicy(policy_version=comparison_policy)
        status = "BLOCKED" if reasons else "DRAFT"
        scope = {**(champion.get("scope") or {}), **(request.get("scope") or {})}
        for key in ("period_start", "period_end", "symbol", "timeframe", "venue", "market_type",
                    "execution_mode", "cost_model_version", "funding_coverage", "slippage_model",
                    "method", "oos_status", "classifier_version"):
            if scope.get(key) is None and champion.get(key) is not None:
                scope[key] = champion.get(key)
        scope.setdefault("dataset_identity", champion.get("dataset_identity")); scope.setdefault("data_revision", champion.get("data_revision"))
        spec = {key: value for key, value in request.items() if key not in {"idempotency_key", "champion_candidate_id", "challenger_candidate_ids", "scope"}}
        spec.update({"method": method, "cost_model_version": request.get("cost_model_version") or champion.get("cost_model_version"),
                     "policy_version": request.get("policy_version"), "comparison_policy_version": comparison_policy,
                     "classifier_version": request.get("classifier_version"), "software_revision": request.get("software_revision"),
                     "random_seed": request.get("random_seed", 1)})
        experiment = {"experiment_id": "experiment:" + uuid.uuid4().hex,
                      "idempotency_key": request.get("idempotency_key") or uuid.uuid4().hex, "owner_id": owner_id,
                      "campaign_id": request.get("campaign_id"), "champion_candidate_id": champion_id,
                      "challenger_candidate_ids": challengers, "scope": scope, "frozen_spec": spec,
                      "status": status, "recommendation": "NOT_COMPARABLE" if reasons else "INSUFFICIENT_EVIDENCE",
                      "warnings": reasons, "holdout_status": "UNKNOWN", "created_at": _now()}
        return self.context.create_matched_experiment(experiment, [champion, *candidates])

    def experiment(self, experiment_id, *, owner_id):
        return self.context.get_matched_experiment(experiment_id, owner_id=owner_id)

    def experiments(self, *, owner_id, limit=100):
        return self.context.matched_experiments(owner_id=owner_id, limit=limit)

    def _job_request(self, experiment, candidate):
        spec = experiment["frozen_spec"]; scope = experiment["scope"]
        request = {"idempotency_key": f"{experiment['experiment_id']}:{candidate['candidate_id']}",
                   "job_type": spec["method"], "strategy_id": candidate["strategy_id"],
                   "strategy_version": candidate.get("strategy_version"), "config_fingerprint": candidate.get("config_fingerprint"),
                   "frozen_config": candidate.get("frozen_config") or {}, "symbol": candidate.get("symbol") or scope.get("symbol") or "",
                   "timeframe": candidate.get("timeframe") or scope.get("timeframe") or "5m", "venue": candidate.get("venue") or scope.get("venue") or "unknown",
                   "market_type": candidate.get("market_type") or scope.get("market_type") or "unknown", "bars": int(spec.get("bars", 4000)),
                   "train_fraction": spec.get("train_fraction", .6), "validation_fraction": spec.get("validation_fraction", .2),
                   "holdout_fraction": spec.get("holdout_fraction", .2), "development_size": spec.get("development_size"),
                   "evaluation_size": spec.get("evaluation_size"), "warmup": spec.get("warmup", 0), "fee": spec.get("fee", "0"),
                   "slippage": spec.get("slippage", "0"), "funding": spec.get("funding"), "runs": spec.get("runs", 1000),
                   "seed": spec.get("random_seed", 1), "policy_version": spec.get("policy_version"),
                   "cost_model_version": spec.get("cost_model_version"), "classifier_version": spec.get("classifier_version"),
                   "software_revision": spec.get("software_revision"), "dataset_identity": scope.get("dataset_identity"),
                   "data_revision": scope.get("data_revision"), "period_start": scope.get("period_start"),
                   "period_end": scope.get("period_end"), "experiment_id": experiment["experiment_id"]}
        return request

    def submit(self, experiment_id: str, *, owner_id: str) -> dict:
        experiment = self.experiment(experiment_id, owner_id=owner_id)
        if not experiment: raise KeyError(experiment_id)
        if experiment["status"] == "BLOCKED": return experiment
        if experiment["status"] in {"COMPLETED", "CANCELLED", "FAILED"}: return experiment
        if experiment["status"] == "DRAFT":
            self.context.update_matched_experiment(experiment_id, owner_id=owner_id, to_status="VALIDATING", reason="validated_for_submission")
            experiment = self.experiment(experiment_id, owner_id=owner_id)
        jobs = dict(experiment.get("candidate_job_ids") or {})
        first_submission = not bool(jobs)
        ids = [experiment["champion_candidate_id"], *experiment["challenger_candidate_ids"]]
        for candidate_id in ids:
            if candidate_id in jobs: continue
            candidate = self.comparisons.candidate(candidate_id, owner_id=owner_id)
            job = self.jobs.create(self._job_request(experiment, candidate), owner_id=owner_id,
                                   scope={"owner_id": owner_id})
            jobs[candidate_id] = job["job_id"]
            self.context.update_experiment_candidate(experiment_id, candidate_id, job_id=job["job_id"], status=job["status"])
        self.context.update_matched_experiment(experiment_id, owner_id=owner_id, to_status="QUEUED", reason="candidate_jobs_submitted", candidate_job_ids=jobs)
        experiment = self.experiment(experiment_id, owner_id=owner_id)
        # Results are not exposed until the holdout has been recorded as seen.
        scope = experiment["scope"]
        if first_submission and scope.get("dataset_identity") and scope.get("holdout_end"):
            for candidate_id in ids:
                self.context.record_holdout_exposure({"exposure_id": f"exposure:{experiment_id}:{candidate_id}", "owner_id": owner_id,
                    "campaign_id": experiment.get("campaign_id"), "experiment_id": experiment_id, "dataset_identity": scope["dataset_identity"],
                    "period_start": scope.get("holdout_start"), "period_end": scope.get("holdout_end"), "candidate_id": candidate_id,
                    "exposure_kind": "MATCHED_RESEARCH", "selection_influence": "PENDING"})
        return experiment

    def reconcile(self, experiment_id: str, *, owner_id: str) -> dict:
        experiment = self.experiment(experiment_id, owner_id=owner_id)
        if not experiment: raise KeyError(experiment_id)
        if experiment["status"] in {"DRAFT", "BLOCKED", "CANCELLED", "FAILED", "COMPLETED"}: return experiment
        statuses = []; overrides = {}; refs = {}; ids = [experiment["champion_candidate_id"], *experiment["challenger_candidate_ids"]]
        for candidate_id in ids:
            job_id = (experiment.get("candidate_job_ids") or {}).get(candidate_id)
            job = self.jobs.get(job_id, owner_id=owner_id) if job_id else None
            status = job.get("status") if job else "PENDING"; statuses.append(status)
            if job and status == "COMPLETED" and job.get("result"):
                refs[candidate_id] = job.get("result_reference") or f"validation:{job_id}"; overrides[candidate_id] = job["result"]
                self.context.update_experiment_candidate(experiment_id, candidate_id, status="COMPLETED", validation_run_id=refs[candidate_id])
            elif job and status in TERMINAL_JOBS:
                self.context.update_experiment_candidate(experiment_id, candidate_id, status=status, error_message=job.get("error_message"))
        if all(status == "COMPLETED" for status in statuses) and len(overrides) == len(ids):
            comparisons = []
            for challenger_id in experiment["challenger_candidate_ids"]:
                request = {"idempotency_key": f"{experiment_id}:comparison:{challenger_id}", "champion_candidate_id": experiment["champion_candidate_id"],
                           "challenger_candidate_id": challenger_id, "random_seed": experiment["frozen_spec"].get("random_seed", 1),
                           "scope": experiment["scope"], "validation_run_ids": list(refs.values()),
                           "policy": {"policy_version": experiment["frozen_spec"].get("comparison_policy_version") or "challenger.v2"}}
                comparisons.append(self.comparisons.compare(request, owner_id=owner_id))
            recommendations = {item["recommendation"] for item in comparisons}
            recommendation = comparisons[0]["recommendation"] if len(recommendations) == 1 else "INCONCLUSIVE"
            comparison = {"recommendation": recommendation, "report": {"comparisons": [item["report"] for item in comparisons]},
                          "blocking_reasons": [reason for item in comparisons for reason in item.get("blocking_reasons", [])]}
            result = self.context.update_matched_experiment(experiment_id, owner_id=owner_id, to_status="COMPLETED", reason="all_candidate_results_committed",
                validation_run_ids=refs, recommendation=comparison["recommendation"], report=comparison["report"], warnings=comparison.get("blocking_reasons", []), holdout_status="REUSED" if any(item.get("holdout_status") == "REUSED" for item in self.context.overlapping_holdout_exposures(owner_id=owner_id, dataset_identity=experiment["scope"].get("dataset_identity", ""), period_start=experiment["scope"].get("holdout_start", experiment["scope"].get("period_start")), period_end=experiment["scope"].get("holdout_end", experiment["scope"].get("period_end")))) else experiment.get("holdout_status", "UNKNOWN"), completed_at=_now())
            return result
        if any(status in {"FAILED", "CANCELLED", "INTERRUPTED"} for status in statuses):
            return self.context.update_matched_experiment(experiment_id, owner_id=owner_id, to_status="PARTIALLY_COMPLETED", reason="candidate_job_incomplete")
        if any(status in {"CLAIMED", "RUNNING"} for status in statuses):
            return self.context.update_matched_experiment(experiment_id, owner_id=owner_id, to_status="RUNNING", reason="candidate_jobs_running")
        return experiment

    def cancel(self, experiment_id: str, *, owner_id: str) -> dict:
        experiment = self.experiment(experiment_id, owner_id=owner_id)
        if not experiment: raise KeyError(experiment_id)
        for job_id in (experiment.get("candidate_job_ids") or {}).values():
            self.jobs.cancel(job_id, owner_id=owner_id)
        if experiment["status"] not in {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}:
            return self.context.update_matched_experiment(experiment_id, owner_id=owner_id, to_status="CANCEL_REQUESTED", reason="cancel_requested")
        return experiment
