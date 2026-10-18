"""Advisory regime selection from frozen qualification and cached paper evidence.

No execution capability is imported here. Rankings describe observed outcomes;
they do not estimate statistical superiority or grant promotion rights.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import re

from services.strategy_evidence import evidence_json
from services.strategy_intelligence_metrics import _timestamp


SELECTION_GROUPING = ("symbol", "session", "trend_regime", "volatility_regime", "entry_timeframe")
CLASSIFIER_FIELDS = ("classifier_id", "classifier_version", "parameter_hash")
SCOPE_FIELDS = ("owner_id", "instance_id", "account_id", "simulation_session_id")
MODEL_FIELDS = ("fees_cost_model", "funding_cost_model", "slippage_cost_model")


@dataclass(frozen=True)
class SelectionPolicy:
    policy_version: str = "regime_selection.v1"
    minimum_context_episodes: int = 30
    minimum_context_profit_factor: str = "1.30"
    maximum_candidates: int = 100
    ranking: tuple = ("net_expectancy_desc", "drawdown_asc", "episode_count_desc")


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _ids(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(v, str) and v for v in value) and len(set(value)) == len(value))


def recommend_strategies(service, scope, query):
    """Bounded, read-only recommendation for a single explicit market context."""
    policy = SelectionPolicy()
    if any(not scope.get(key) for key in SCOPE_FIELDS):
        raise ValueError("complete owner/account/instance/session scope is required")
    query = dict(query)
    if any(not isinstance(query.get(key), str) or not query[key].strip()
           or query[key].upper() == "UNKNOWN" for key in SELECTION_GROUPING):
        raise ValueError("explicit known asset, session, regime and timeframe are required")
    if not all(query.get(key) for key in CLASSIFIER_FIELDS):
        if any(query.get(key) for key in CLASSIFIER_FIELDS):
            raise ValueError("classifier identity must be complete")
        from services.market_context_classifier import classifier_definition
        definition = classifier_definition()
        query.update({key: definition[key] for key in CLASSIFIER_FIELDS})
    if not re.fullmatch(r"[0-9a-f]{64}", str(query["parameter_hash"])):
        raise ValueError("classifier parameter_hash must be a SHA-256 fingerprint")
    context = {key: query[key] for key in (*SELECTION_GROUPING, *CLASSIFIER_FIELDS)}
    output = {"selection_contract_version": "strategy_selection.v1", "view": "selection",
              "policy": asdict(policy), "context": context, "advisory_only": True,
              "orders_allowed": False, "recommendation": None, "candidates": [],
              "status": "INSUFFICIENT_EVIDENCE", "cache_status": "READY",
              "limitations": ["Observed net expectancy is descriptive, not proof of statistical superiority.",
                              "Recommendations never change strategy, risk, champion or execution settings."]}
    raw = service.context.cached_cohorts(filters={**scope, "symbol": query["symbol"],
        "execution_mode": "paper", "source_kind": "forward_paper", "lab_id": query.get("lab_id")},
        limit=policy.maximum_candidates + 1)
    if len(raw) > policy.maximum_candidates:
        return {**output, "status": "BOUND_EXCEEDED", "blocking_reasons": ["CANDIDATE_QUERY_BOUND_EXCEEDED"]}
    seen, ranked = set(), []
    for cohort in sorted(raw, key=evidence_json):
        if (any(cohort.get(key) != scope[key] for key in SCOPE_FIELDS)
                or cohort.get("symbol") != query["symbol"] or cohort.get("execution_mode") != "paper"
                or cohort.get("source_kind") != "forward_paper" or cohort.get("lab_id") != query.get("lab_id")
                or not cohort.get("strategy_id") or not cohort.get("strategy_version")
                or not re.fullmatch(r"[0-9a-f]{64}", str(cohort.get("config_fingerprint")))):
            continue
        key = evidence_json(cohort)
        if key in seen:
            continue
        seen.add(key)
        qualification = service.read("qualification", scope, cohort)
        qualification_bound = all(qualification.get("identity", {}).get(k) == v for k, v in cohort.items())
        chain = (qualification.get("evidence_chain") or {}) if qualification_bound else {}
        forward = chain.get("forward_paper") or {}
        performance = service.read("performance", scope, {**cohort, "group_by": list(SELECTION_GROUPING)})
        cache_bound = (performance.get("cohort") == cohort
                       and performance.get("group_by") == list(SELECTION_GROUPING))
        matches = [g for g in performance.get("groups", [])
                   if cache_bound and g.get("cohort") == cohort
                   and g.get("group") == {key: query[key] for key in SELECTION_GROUPING}
                   and g.get("classifier") == {key: query[key] for key in CLASSIFIER_FIELDS}]
        group = matches[0] if len(matches) == 1 else {}
        metrics = group.get("metrics") or {}
        quality = group.get("evidence_quality") or {}
        count = _number(metrics.get("completed_episode_count"))
        expectancy = _number(metrics.get("expectancy_net_r"))
        drawdown = _number(metrics.get("max_drawdown_r"))
        pf = _number(metrics.get("net_profit_factor"))
        ids = group.get("episode_ids")
        period = group.get("observation_period") or {}
        start, end = _timestamp(period.get("first_opened_at")), _timestamp(period.get("last_closed_at"))
        watermark = performance.get("source_watermark") if cache_bound else None
        checks = {
            "STRATEGY_NOT_QUALIFIED": qualification.get("qualification_status") == "QUALIFIED"
                and chain.get("status") == "COMPLETE",
            "QUALIFICATION_IDENTITY_MISMATCH": qualification_bound,
            "CONTEXT_CACHE_NOT_READY": performance.get("cache_status") == "READY",
            "CONTEXT_CACHE_IDENTITY_MISMATCH": cache_bound,
            "EXACT_CONTEXT_UNAVAILABLE_OR_AMBIGUOUS": len(matches) == 1,
            "CONTEXT_COHORT_MISMATCH": group.get("cohort") == cohort,
            "CONTEXT_EVIDENCE_INCOMPLETE": group.get("profitability_verified") is True
                and quality.get("status") == "COMPLETE" and quality.get("binding_status") == "MATCHED_SUBGROUP"
                and (group.get("context_quality") or {}).get("status") == "VALID"
                and metrics.get("financial_evidence_complete") is True,
            "CONTEXT_COSTS_INCOMPLETE": (group.get("cost_coverage") or {}).get("status") == "COMPLETE",
            "CONTEXT_SAMPLE_INSUFFICIENT": count is not None and count == count.to_integral_value()
                and count >= policy.minimum_context_episodes and _number(metrics.get("r_sample_size")) == count,
            "CONTEXT_MEMBERSHIP_UNBOUND": _ids(ids) and count == len(ids)
                and _ids(quality.get("subgroup_episode_ids")) and set(ids) == set(quality["subgroup_episode_ids"])
                and _ids(forward.get("episode_ids")) and set(ids).issubset(forward["episode_ids"]),
            "CONTEXT_SOURCE_CHANGED": bool(watermark) and watermark == forward.get("source_watermark")
                and watermark == quality.get("source_watermark"),
            "CONTEXT_EXPECTANCY_NOT_POSITIVE": expectancy is not None and expectancy > 0,
            "CONTEXT_PROFIT_FACTOR_INSUFFICIENT": pf is not None and pf >= Decimal(policy.minimum_context_profit_factor),
            "CONTEXT_DRAWDOWN_UNKNOWN": drawdown is not None and drawdown >= 0,
            "CONTEXT_PERIOD_UNKNOWN": start is not None and end is not None and start < end,
            "CONTEXT_COST_MODELS_UNKNOWN": all((group.get("cost_model") or {}).get(k) not in (None, "", "UNKNOWN") for k in MODEL_FIELDS),
        }
        blockers = [name for name, passed in checks.items() if not passed]
        row = {"cohort": cohort, "eligible": not blockers, "blocking_reasons": blockers,
               "qualification_status": qualification.get("qualification_status", "UNKNOWN"),
               "metrics": {key: metrics.get(key) for key in ("completed_episode_count", "expectancy_net_r",
                          "net_profit_factor", "max_drawdown_r")},
               "observation_period": period, "cost_model": group.get("cost_model"),
               "evidence": {"cache_run_id": performance.get("run_id") if cache_bound else None, "source_watermark": watermark,
                            "calculation_timestamp": performance.get("calculation_timestamp") if cache_bound else None,
                            "validation_run_ids": chain.get("validation_run_ids", {}),
                            "episode_ids": ids if _ids(ids) else []}}
        output["candidates"].append(row)
        if not blockers:
            ranked.append(((expectancy.copy_negate(), drawdown, count.copy_negate()), row, start, end))
    if not ranked:
        return output
    models = {evidence_json(item[1]["cost_model"]) for item in ranked}
    if len(models) > 1 or max(item[2] for item in ranked) >= min(item[3] for item in ranked):
        return {**output, "status": "NOT_COMPARABLE", "blocking_reasons": ["COST_MODELS_DIFFER_OR_OBSERVATION_PERIODS_DO_NOT_OVERLAP"]}
    ranked.sort(key=lambda item: (item[0], evidence_json(item[1]["cohort"])))
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return {**output, "status": "TIED", "blocking_reasons": ["TOP_CANDIDATES_HAVE_EQUAL_RANK"]}
    output.update(status="RECOMMENDED", recommendation=ranked[0][1],
                  ranking_basis="highest_observed_context_net_expectancy_then_lower_drawdown_then_larger_sample")
    output["selection_id"] = hashlib.sha256(evidence_json(output).encode()).hexdigest()
    return output
