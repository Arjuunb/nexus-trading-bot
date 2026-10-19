"""Versioned descriptive Edge Score over bound historical and actual paper evidence.

This is a research policy, not a calibrated probability or an execution gate.
Unknown evidence never becomes a zero or a redistributed component weight.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import re

from services.historical_forward_evidence import build_evidence_chain, number, _count
from services.strategy_evidence import evidence_json


VERSION = "strategy_edge_score.v1"
WEIGHTS = (("expectancy", "15"), ("profit_factor", "10"), ("drawdown", "10"),
           ("trade_count", "10"), ("consistency", "10"), ("out_of_sample", "10"),
           ("forward_performance", "15"), ("regime_robustness", "10"), ("cost_resilience", "10"))
METHODS = ("historical_backtest", "out_of_sample", "walk_forward", "robustness", "cost_stress")
CLASSIFIER_FIELDS = ("classifier_id", "classifier_version", "parameter_hash")
MODEL_FIELDS = ("fees_cost_model", "funding_cost_model", "slippage_cost_model")
SCOPE_FIELDS = ("owner_id", "account_id", "instance_id", "simulation_session_id")
COHORT_FIELDS = (*SCOPE_FIELDS, "strategy_id", "strategy_version", "config_fingerprint", "symbol",
                 "execution_mode", "source_kind", "lab_id")


def _hash(value):
    return hashlib.sha256(evidence_json(value).encode()).hexdigest()


def _text(value):
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class EdgeScorePolicy:
    policy_version: str = "edge_score.v1"
    weights: tuple = WEIGHTS
    minimum_historical_episodes: int = 100
    minimum_forward_episodes: int = 30
    historical_sample_target: int = 300
    forward_sample_target: int = 100
    minimum_walk_forward_windows: int = 3
    minimum_regimes: int = 2
    minimum_regime_episodes: int = 30
    expectancy_target_r: str = "0.5"
    profit_factor_target: str = "2"
    drawdown_half_score_r: str = "10"
    regime_expectancy_target_r: str = "0.25"

    def __post_init__(self):
        if not isinstance(self.policy_version, str) or not re.fullmatch(r"edge_score\.[\w.-]+", self.policy_version):
            raise ValueError("invalid Edge Score policy version")
        try:
            if not isinstance(self.weights, (tuple, list)):
                raise ValueError("weights require named pairs")
            weights = dict(self.weights)
        except (TypeError, ValueError):
            raise ValueError("invalid component weights") from None
        values = [number(v) for v in weights.values()]
        if (len(self.weights) != 9 or set(weights) != dict(WEIGHTS).keys()
                or any(v is None or not 0 < v <= 100 for v in values)
                or sum((Fraction(v) for v in values), Fraction()) != 100):
            raise ValueError("nine positive finite component weights must sum to 100")
        # Seal caller-owned lists and normalize equivalent numeric spellings.
        object.__setattr__(self, "weights", tuple((name, _text(number(weights[name]))) for name, _ in WEIGHTS))
        weights = dict(self.weights)
        fields = self.__dataclass_fields__
        for name, field in fields.items():
            if name in ("policy_version", "weights"):
                continue
            value = getattr(self, name)
            if isinstance(field.default, int):
                if type(value) is not int or not 1 <= value <= 1_000_000:
                    raise ValueError("invalid minimum evidence or sample target")
            elif number(value) is None or number(value) <= 0:
                raise ValueError("normalization targets must be positive finite decimals")
            else:
                object.__setattr__(self, name, _text(number(value)))
        if number(self.profit_factor_target) <= 1:
            raise ValueError("profit factor target must exceed 1")
        if (self.historical_sample_target < self.minimum_historical_episodes
                or self.forward_sample_target < self.minimum_forward_episodes):
            raise ValueError("sample targets cannot be below minimum evidence")
        if self.policy_version == "edge_score.v1" and (
                weights != dict(WEIGHTS) or any(getattr(self, k) != v.default for k, v in fields.items()
                if k not in ("policy_version", "weights"))):
            raise ValueError("changed Edge Score parameters require a new policy version")

    def as_dict(self):
        value = asdict(self)
        value["weights"] = {k: _text(number(v)) for k, v in self.weights}
        value.update(calibration_status="UNCALIBRATED_RESEARCH_POLICY",
            formula_version="clamped_weighted_components.v1",
            bands={"LOW_SCORE": "0 <= score < 40", "MID_SCORE": "40 <= score < 70", "HIGH_SCORE": "70 <= score <= 100"})
        return {**value, "fingerprint": _hash(value)}


def _ids(value):
    return (isinstance(value, list) and bool(value) and all(isinstance(v, str) and v for v in value)
            and len(set(value)) == len(value))


def _same_identity(left, right):
    return isinstance(left, dict) and all(k in left and left[k] == right.get(k) for k in COHORT_FIELDS)


def _complete_group(group, identity, watermark, classifier, cost_model=None):
    if not isinstance(group, dict):
        return False
    metrics, quality = group.get("metrics") or {}, group.get("evidence_quality") or {}
    count, ids = _count(metrics.get("completed_episode_count")), group.get("episode_ids")
    models = group.get("cost_model") or {}
    return (group.get("cohort") == identity and group.get("classifier") == classifier
        and (cost_model is None or models == cost_model)
        and all(isinstance(models.get(k), str) and models[k] not in ("", "UNKNOWN") for k in MODEL_FIELDS)
        and group.get("profitability_verified") is True
        and (group.get("context_quality") or {}).get("status") == "VALID"
        and (group.get("cost_coverage") or {}).get("status") == "COMPLETE"
        and quality.get("status") == "COMPLETE" and quality.get("binding_status") == "MATCHED_SUBGROUP"
        and quality.get("source_watermark") == watermark and metrics.get("financial_evidence_complete") is True
        and count is not None and count > 0 and _count(metrics.get("r_sample_size")) == count
        and _ids(ids) and len(ids) == count and _ids(quality.get("subgroup_episode_ids"))
        and set(ids) == set(quality["subgroup_episode_ids"]))


def calculate_edge_score(identity, qualification, performance, regimes, *, classifier, policy=None):
    """Pure calculation; inputs come from the owner-authorized metadata reader."""
    policy = policy or EdgeScorePolicy()
    with localcontext() as precision:
        precision.prec = 50
        return _calculate(identity, qualification, performance, regimes, classifier, policy)


def _calculate(identity, qualification, performance, regimes, classifier, policy):
    descriptions = {
        "expectancy": "100 * clamp(historical net expectancy / target R, 0, 1)",
        "profit_factor": "100 * clamp((historical monetary profit factor - 1) / (target - 1), 0, 1)",
        "drawdown": "100 * half-score R / (half-score R + worst historical/forward R drawdown)",
        "trade_count": "100 * min(historical count / target, forward count / target, 1)",
        "consistency": "100 * positive walk-forward evaluation windows / all bound evaluation windows",
        "out_of_sample": "100 * clamp(holdout net expectancy / positive training net expectancy, 0, 1)",
        "forward_performance": "100 * clamp(actual paper net expectancy / target R, 0, 1)",
        "regime_robustness": "100 * clamp(worst observed regime net expectancy / target R, 0, 1)",
        "cost_resilience": "100 * clamp(adversely stressed net R / positive base net R, 0, 1)",
    }
    output = {"edge_score_contract_version": VERSION, "view": "edge_score", "identity": dict(identity),
        "policy": policy.as_dict(), "status": "UNSCORED", "score": None, "band": None,
        "advisory_only": True, "orders_allowed": False, "recommendation_eligible": False,
        "statistical_proof_of_profitability": False, "qualification_status": "UNKNOWN",
        "components": [{"name": name, "weight": _text(number(weight)), "status": "UNKNOWN",
                        "normalized_score": None, "weighted_points": None, "inputs": {},
                        "formula": descriptions[name], "reason": "BOUND_EVIDENCE_REQUIRED"} for name, weight in policy.weights],
        "blocking_reasons": [], "evidence": {},
        "limitations": ["Uncalibrated descriptive score; not a probability of profit or proof of independent edge.",
                        "Regime robustness covers observed regimes only; unseen conditions remain unknown.",
                        "Sample counts describe volume and do not establish independent observations.",
                        "Qualification and selection policies remain separate; the score grants no promotion or execution rights."]}
    def abort(reason):
        output["blocking_reasons"].append(reason)
        return output
    if (not all(identity.get(k) for k in (*SCOPE_FIELDS, "strategy_id", "strategy_version", "symbol"))
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("config_fingerprint")))
            or identity.get("execution_mode") != "paper" or identity.get("source_kind") != "forward_paper"
            or not all(isinstance(classifier.get(k), str) and classifier[k] for k in CLASSIFIER_FIELDS)):
        return abort("EXACT_FROZEN_PAPER_IDENTITY_AND_CLASSIFIER_REQUIRED")
    chain = qualification.get("evidence_chain") or {}
    if not _same_identity(qualification.get("identity"), identity) or not _same_identity(chain.get("identity"), identity):
        return abort("QUALIFICATION_EVIDENCE_UNBOUND_OR_INCOMPLETE")
    if qualification.get("qualification_status") in {"QUALIFIED", "NOT_QUALIFIED"}:
        output["qualification_status"] = qualification["qualification_status"]
    if chain.get("status") != "COMPLETE":
        return abort("QUALIFICATION_EVIDENCE_UNBOUND_OR_INCOMPLETE")
    refs = {method: (chain.get("validation_run_ids") or {}).get(method) for method in METHODS}
    rows = qualification.get("validation_history") or []
    if (any((chain.get("stages", {}).get(m) or {}).get("binding_status") != "BOUND" for m in METHODS)
            or any(not isinstance(refs.get(m), str) or not refs[m] for m in METHODS)
            or len(set(refs[m] for m in METHODS)) != len(METHODS)):
        return abort("VALIDATION_LINEAGE_INCOMPLETE")
    selected = [row for row in rows if isinstance(row, dict) and row.get("run_id") in refs.values()]
    if (len(selected) != len(METHODS) or len({row.get("run_id") for row in selected}) != len(METHODS)
            or any(row.get("method") not in METHODS or refs[row["method"]] != row.get("run_id") for row in selected)):
        return abort("VALIDATION_REFERENCES_AMBIGUOUS")
    reports = {row["method"]: row.get("report") or {} for row in selected}
    frozen = reports["historical_backtest"].get("identity")
    rebuilt = build_evidence_chain(scope={k: identity[k] for k in SCOPE_FIELDS}, identity=identity, frozen=frozen,
        records=selected, performance=performance)
    if (rebuilt["status"] != "COMPLETE" or rebuilt["validation_run_ids"] != refs
            or rebuilt["historical"] != chain.get("historical")
            or rebuilt["forward_paper"] != chain.get("forward_paper")):
        return abort("FINANCIAL_EVIDENCE_OR_PROVENANCE_MISMATCH")
    for value, grouping in ((performance, ["symbol"]), (regimes, ["trend_regime"])):
        if (value.get("cache_status") != "READY" or value.get("cohort") != identity
                or value.get("group_by") != grouping or value.get("source_watermark") != performance.get("source_watermark")):
            return abort("CACHE_IDENTITY_FRESHNESS_OR_SOURCE_MISMATCH")
        if any(not _same_identity(g.get("cohort"), identity) for g in value.get("groups", [])):
            return abort("CACHE_GROUP_IDENTITY_MISMATCH")
    parents = performance.get("groups") or []
    parent = parents[0] if len(parents) == 1 else {}
    watermark = performance.get("source_watermark")
    if not _complete_group(parent, identity, watermark, classifier):
        return abort("ACTUAL_PAPER_FINANCIAL_OR_CONTEXT_EVIDENCE_INCOMPLETE")
    historical, paper = rebuilt["historical"]["metrics"], parent["metrics"]
    hc, pc = _count(historical.get("completed_episode_count")), _count(paper.get("completed_episode_count"))
    if hc < policy.minimum_historical_episodes or pc < policy.minimum_forward_episodes:
        output["blocking_reasons"].append("MINIMUM_FINANCIAL_SAMPLE_NOT_MET")
    output["evidence"] = {"historical_run_id": refs["historical_backtest"], "validation_run_ids": dict(refs),
        "paper_cache_run_id": performance.get("run_id"), "regime_cache_run_id": regimes.get("run_id"),
        "source_watermark": watermark, "classifier": dict(classifier), "cost_model": dict(parent["cost_model"]),
        "paper_episode_ids": list(parent["episode_ids"]), "historical_episodes": hc, "forward_episodes": pc,
        "historical_dataset": deepcopy(reports["historical_backtest"]["dataset"]),
        "forward_observation_period": dict(parent.get("observation_period") or {})}
    clamp = lambda n: max(Decimal(0), min(Decimal(1), n))
    def component(name, value, inputs, reason="COMPLETE_BOUND_EVIDENCE"):
        item = next(c for c in output["components"] if c["name"] == name)
        item.update(inputs=inputs, reason=reason)
        if value is not None:
            normalized = (100 * clamp(value)).quantize(Decimal("0.000001"))
            item.update(status="KNOWN", normalized_score=_text(normalized),
                weighted_points=_text((normalized * number(item["weight"]) / 100).quantize(Decimal("0.000001"))))
        else:
            output["blocking_reasons"].append(name.upper() + "_EVIDENCE_INCOMPLETE")
    exp, fexp = number(historical.get("expectancy_net_r")), number(paper.get("expectancy_net_r"))
    component("expectancy", exp / number(policy.expectancy_target_r) if exp is not None else None,
              {"historical_net_expectancy_r": historical.get("expectancy_net_r"), "target_r": policy.expectancy_target_r})
    pf = number(historical.get("net_profit_factor"))
    pnls = [number(row.get("net_pnl")) for row in historical["financial_samples"]]
    no_losses = all(v is not None and v >= 0 for v in pnls) and any(v > 0 for v in pnls)
    all_flat = all(v == 0 for v in pnls)
    pf_score = (pf - 1) / (number(policy.profit_factor_target) - 1) if pf is not None else Decimal(1) if no_losses else Decimal(0) if all_flat else None
    component("profit_factor", pf_score, {"historical_monetary_profit_factor": historical.get("net_profit_factor"),
        "ratio_state": "FINITE" if pf is not None else "NO_LOSSES_OBSERVED" if no_losses else "ALL_BREAKEVEN" if all_flat else "UNKNOWN",
        "target": policy.profit_factor_target})
    dd = [number(historical.get("max_drawdown_r")), number(paper.get("max_drawdown_r"))]
    worst = max(dd) if all(v is not None and v >= 0 for v in dd) else None
    component("drawdown", number(policy.drawdown_half_score_r) / (number(policy.drawdown_half_score_r) + worst) if worst is not None else None,
        {"historical_drawdown_r": historical.get("max_drawdown_r"), "forward_drawdown_r": paper.get("max_drawdown_r"),
         "half_score_r": policy.drawdown_half_score_r})
    component("trade_count", min(Decimal(hc) / policy.historical_sample_target, Decimal(pc) / policy.forward_sample_target),
        {"historical_episodes": hc, "forward_episodes": pc, "historical_target": policy.historical_sample_target, "forward_target": policy.forward_sample_target})
    windows = reports["walk_forward"].get("windows") or []
    exps = [number((w.get("evaluation") or {}).get("expectancy_net_r")) for w in windows]
    enough = len(exps) >= policy.minimum_walk_forward_windows and all(v is not None for v in exps)
    positive = sum(v > 0 for v in exps) if enough else None
    component("consistency", Decimal(positive) / len(exps) if enough else None,
        {"evaluation_windows": len(exps), "positive_windows": positive, "minimum_windows": policy.minimum_walk_forward_windows})
    oos = reports["out_of_sample"]
    train, holdout = number((oos.get("train") or {}).get("expectancy_net_r")), number((oos.get("holdout") or {}).get("expectancy_net_r"))
    retention = holdout / train if train is not None and train > 0 and holdout is not None else Decimal(0) if train is not None and train <= 0 and holdout is not None else None
    component("out_of_sample", retention, {"train_expectancy_r": (oos.get("train") or {}).get("expectancy_net_r"),
        "holdout_expectancy_r": (oos.get("holdout") or {}).get("expectancy_net_r"), "holdout_access_count": 1})
    component("forward_performance", fexp / number(policy.expectancy_target_r) if fexp is not None else None,
        {"actual_paper_expectancy_r": paper.get("expectancy_net_r"), "target_r": policy.expectancy_target_r})
    groups, seen, valid_regimes, regime_values = regimes.get("groups") or [], set(), set(), []
    regime_ok = len(groups) >= policy.minimum_regimes
    for group in groups:
        regime = (group.get("group") or {}).get("trend_regime")
        ids, metrics = group.get("episode_ids") or [], group.get("metrics") or {}
        mean, count = number(metrics.get("expectancy_net_r")), _count(metrics.get("completed_episode_count"))
        valid = (_complete_group(group, identity, watermark, classifier, parent["cost_model"])
            and regime in {"BULL", "BEAR", "STRONG_BULL", "STRONG_BEAR", "RANGE"}
            and regime not in valid_regimes and count is not None and count >= policy.minimum_regime_episodes
            and mean is not None and not seen.intersection(ids))
        regime_ok = regime_ok and valid
        if valid:
            valid_regimes.add(regime); seen.update(ids); regime_values.append(mean)
    regime_ok = regime_ok and seen == set(parent["episode_ids"])
    component("regime_robustness", min(regime_values) / number(policy.regime_expectancy_target_r) if regime_ok else None,
        {"observed_regimes": sorted(valid_regimes), "minimum_regimes": policy.minimum_regimes,
         "minimum_episodes_each": policy.minimum_regime_episodes, "worst_expectancy_r": _text(min(regime_values)) if regime_ok else None,
         "target_r": policy.regime_expectancy_target_r})
    stress = reports["cost_stress"].get("stress") or {}
    base, net, fee, slip, funding = (number(stress.get(k)) for k in ("gross_r", "net_r", "fee", "slippage", "funding"))
    adverse = all(v is not None for v in (base, net, fee, slip, funding)) and fee > 0 and slip > 0 and funding >= 0
    resilience = net / base if adverse and base > 0 else Decimal(0) if adverse and base <= 0 else None
    component("cost_resilience", resilience, {k: stress.get(k) for k in ("gross_r", "net_r", "fee", "slippage", "funding")},
              "ADVERSE_INCREMENTAL_COST_STRESS_REQUIRED" if not adverse else "COMPLETE_BOUND_ADVERSE_STRESS")
    if not output["blocking_reasons"]:
        total = sum((number(c["weighted_points"]) for c in output["components"]), Decimal(0))
        output.update(status="SCORED", score=_text(total), band="LOW_SCORE" if total < 40 else "MID_SCORE" if total < 70 else "HIGH_SCORE")
    output["score_id"] = "edge_score:" + _hash({"identity": identity, "policy": output["policy"],
        "reports": reports, "performance": performance, "regimes": regimes,
        "components": output["components"], "score": output["score"], "status": output["status"],
        "qualification_policy": qualification.get("policy"), "qualification_status": output["qualification_status"]})
    return output
