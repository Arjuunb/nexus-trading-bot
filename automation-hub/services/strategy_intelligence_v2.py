"""Pure, read-only context partitions over authoritative position episodes.

The v1 financial calculator remains unchanged. This module adds independently
bound context subgroups, descriptive sample-volume labels and explicit missing
cost coverage. None of these outputs grants strategy promotion or order rights.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
from typing import Iterable, Mapping

from services.strategy_evidence import evidence_json
from services.strategy_intelligence_metrics import (
    EvidenceCohort, _completeness_binding, _decimal, _normalised_scope,
    _timestamp, _text, calculate_metrics,
)


CONTRACT_VERSION = "strategy_intelligence.v2"
SUBGROUP_REPORT_VERSION = "strategy_context_evidence.v1"
SUPPORTED_GROUPINGS = (
    (), ("symbol",), ("session",), ("trend_regime",), ("volatility_regime",),
    ("direction",), ("entry_timeframe",), ("symbol", "session"),
    ("symbol", "trend_regime"), ("symbol", "volatility_regime"),
    ("symbol", "session", "trend_regime"), ("entry_timeframe", "direction"),
)
_CLASSIFIER_FIELDS = ("classifier_id", "classifier_version", "parameter_hash")
_MODEL_FIELDS = ("fees_cost_model", "funding_cost_model", "slippage_cost_model")
_KNOWN_COST_COVERAGE = {"BOOKED", "MODELED", "VERIFIED_ZERO"}
_STATUSES = {"COMPLETE", "PARTIAL", "UNKNOWN", "CONFLICTED", "RECOVERING"}
_NONEXECUTED = {"counterfactual", "counterfactual_research", "shadow", "research", "rejected",
                "rejected_decision", "hypothetical", "cancelled", "expired"}


def _hash(value):
    return hashlib.sha256(evidence_json(value).encode()).hexdigest()


def _status(statuses):
    """Conservative summary; statuses never average into a better status."""
    values = set(statuses)
    for status in ("CONFLICTED", "UNKNOWN", "RECOVERING", "PARTIAL"):
        if status in values:
            return status
    return "COMPLETE" if values else "UNKNOWN"


def _scope_matches(row, cohort):
    scope = cohort.as_dict()
    return all(key in row and row[key] == value for key, value in scope.items() if key != "symbol") and (
        scope["symbol"] is None or row.get("symbol") == scope["symbol"])


def _selected_episodes(episodes, cohort, *, include_open=False):
    selected, excluded, duplicates = {}, Counter(), 0
    for original in episodes:
        row = _normalised_scope(original)
        if not _scope_matches(row, cohort):
            excluded["missing_or_other_scope"] += 1
            continue
        if (str(row.get("evidence_kind", "")).lower() != "executed" or
                str(row.get("source_kind", "")).lower() in _NONEXECUTED):
            excluded["nonexecuted"] += 1
            continue
        status = str(row.get("status", "")).lower()
        if status != "closed" and not (include_open and status == "open"):
            excluded["not_completed"] += 1
            continue
        episode_id = row.get("episode_id")
        if not isinstance(episode_id, str) or not episode_id:
            excluded["missing_episode_id"] += 1
            continue
        if episode_id in selected:
            if row != selected[episode_id]:
                raise ValueError(f"conflicting episode evidence: {episode_id}")
            duplicates += 1
            continue
        selected[episode_id] = row
    return [selected[key] for key in sorted(selected)], dict(excluded), duplicates


def _context_scope(context):
    row = dict(context)
    if "config_fingerprint" not in row:
        for alias in ("strategy_config_hash", "configuration_hash"):
            if alias in row:
                row["config_fingerprint"] = row[alias]
                break
    return row


def _contexts_for(episodes, contexts, *, include_research=False):
    by_episode = {row["episode_id"]: row for row in episodes}
    found, missing, unknown = {}, [], []
    for original in contexts:
        context = _context_scope(original)
        if context.get("classification_kind", "ENTRY") != "ENTRY" and not include_research:
            continue
        episode = by_episode.get(context.get("episode_id"))
        if episode is None:
            continue
        # Scale-ins retain their own immutable entry conditions, while the
        # completed statistical episode is grouped by its original entry.
        # Reductions/remainder rows cannot select a later market context.
        if episode.get("root_trade_id") and context.get("trade_id") != episode["root_trade_id"]:
            continue
        fields = tuple(EvidenceCohort.__dataclass_fields__)
        if any(key not in context for key in fields):
            continue
        if any(context[key] != episode.get(key) for key in fields):
            raise ValueError(f"context conflicts with authoritative episode scope: {episode['episode_id']}")
        signal, opened = _timestamp(context.get("signal_timestamp")), _timestamp(episode.get("opened_at"))
        if signal and opened and signal > opened:
            raise ValueError("context signal timestamp is after authoritative entry timestamp")
        classifier = tuple(context.get(key) for key in _CLASSIFIER_FIELDS)
        identity = (episode["episode_id"], *classifier)
        if identity in found and found[identity] != context:
            raise ValueError(f"conflicting context snapshot: {episode['episode_id']}")
        found[identity] = context
    by_id = {}
    for (episode_id, *_), context in sorted(found.items(), key=lambda item: evidence_json(item[0])):
        by_id.setdefault(episode_id, []).append(context)
    for row in episodes:
        values = by_id.get(row["episode_id"], [])
        if not values:
            missing.append(row["episode_id"])
        elif any(_context_quality(row, value) != "VALID" for value in values):
            unknown.append(row["episode_id"])
    return by_id, missing, unknown, _hash([found[key] for key in sorted(found, key=evidence_json)])


def _context_quality(episode, context):
    if not context:
        return "UNKNOWN"
    quality = context.get("context_quality") or "UNKNOWN"
    if quality == "VALID" and (not all(context.get(key) for key in _CLASSIFIER_FIELDS) or
            not _timestamp(context.get("signal_timestamp")) or not _timestamp(episode.get("opened_at")) or
            any(context.get(key) in (None, "", "UNKNOWN") for key in ("session", "trend_regime", "volatility_regime"))):
        return "UNKNOWN"
    return quality


def _direction(row):
    value = row.get("direction", row.get("side"))
    return {"BUY": "LONG", "SELL": "SHORT", "LONG": "LONG", "SHORT": "SHORT"}.get(str(value).upper())


def _selector(row, context, group_by):
    return {
        "group": {key: (row.get("symbol") if key == "symbol" else _direction(row) if key == "direction"
                        else (context or {}).get(key)) for key in group_by},
        "classifier": {key: (context or {}).get(key) for key in _CLASSIFIER_FIELDS},
        "cost_model": {key: row.get(key) or "UNKNOWN" for key in _MODEL_FIELDS},
    }


def _validate_enrichment(enriched, authority, cohort):
    """Only add observations: authoritative identity/economics are immutable."""
    enriched_rows, _, _ = _selected_episodes(enriched, cohort, include_open=True)
    original_rows, _, _ = _selected_episodes(authority, cohort, include_open=True)
    original_by_id = {row["episode_id"]: row for row in original_rows}
    if {row["episode_id"] for row in enriched_rows} != set(original_by_id):
        raise ValueError("enriched projection changes authoritative episode membership")
    for row in enriched_rows:
        original = original_by_id[row["episode_id"]]
        if any(row.get(key) != value for key, value in original.items()):
            raise ValueError(f"enriched projection changes authoritative episode: {row['episode_id']}")


def _prepare_partition_binding(parent_report, material, contexts, *, cohort, group_by,
                               source_watermark, calculation_timestamp, authority,
                               include_research=False):
    _validate_enrichment(material, authority, cohort)
    parent_status, parent_binding = _completeness_binding(
        authority, cohort, source_watermark, parent_report, calculation_timestamp)
    eligible, _, _ = _selected_episodes(material, cohort, include_open=True)
    context_by_episode, missing, unknown, context_watermark = _contexts_for(
        eligible, contexts, include_research=include_research)
    partitions = {}
    for row in eligible:
        if str(row.get("status", "")).lower() != "closed":
            continue
        for context in context_by_episode.get(row["episode_id"]) or [None]:
            selector = _selector(row, context, group_by)
            partition = partitions.setdefault(evidence_json(selector), {"selector": selector, "rows": {}})
            partition["rows"][row["episode_id"]] = row
    return {"parent_status": parent_status, "parent_binding": parent_binding,
            "missing": missing, "unknown": unknown, "context_watermark": context_watermark,
            "partitions": partitions, "eligible": eligible, "context_by_episode": context_by_episode}


def build_subgroup_report(parent_report, all_episodes, all_contexts, *, cohort, group_by,
                          selector, subgroup_episodes, source_watermark,
                          calculation_timestamp, evidence_episodes=None, include_research=False):
    """Bind a subgroup to complete authority and an exact context partition.

    A parent's trade count, confidence or profitability is never copied. Parent
    coverage can certify this deterministic subset only after the original v1
    episode digest matches and every eligible cohort episode has an attributable
    immutable context. Unknown memberships prevent certification of all subsets.
    This is a scoped proof over a producer-assessed source view, not a second
    ledger assessment or authentication of a client-supplied report.
    """
    authority = list(all_episodes if evidence_episodes is None else evidence_episodes)
    material = list(all_episodes)
    binding = _prepare_partition_binding(parent_report, material, list(all_contexts), cohort=cohort,
        group_by=group_by, source_watermark=source_watermark, calculation_timestamp=calculation_timestamp,
        authority=authority, include_research=include_research)
    return _build_bound_subgroup(binding, cohort=cohort, group_by=group_by, selector=selector,
        subgroup_episodes=list(subgroup_episodes), source_watermark=source_watermark,
        calculation_timestamp=calculation_timestamp)


def _build_bound_subgroup(binding, *, cohort, group_by, selector, subgroup_episodes,
                          source_watermark, calculation_timestamp):
    parent_status, parent_binding = binding["parent_status"], binding["parent_binding"]
    missing, unknown = binding["missing"], binding["unknown"]
    expected = binding["partitions"].get(evidence_json(selector), {}).get("rows", {})
    expected_ids = sorted(expected)
    actual_ids = sorted(row["episode_id"] for row in subgroup_episodes)
    if actual_ids != expected_ids or any(row != expected.get(row["episode_id"]) for row in subgroup_episodes):
        raise ValueError("subgroup membership does not match immutable context partition")
    reasons = []
    if parent_binding != "MATCHED":
        reasons.append(parent_binding)
    if missing:
        reasons.append("UNASSIGNED_COHORT_CONTEXT")
    if unknown:
        reasons.append("UNRELIABLE_COHORT_CONTEXT")
    status = parent_status
    if status == "COMPLETE" and missing:
        status = "UNKNOWN"
    elif status == "COMPLETE" and unknown:
        status = "PARTIAL"
    if status not in _STATUSES:
        status = "UNKNOWN"
    if status != "COMPLETE" and not reasons:
        reasons.append("AUTHORITATIVE_EVIDENCE_" + status)
    subgroup_watermark = _hash(sorted(subgroup_episodes, key=lambda row: row["episode_id"]))
    return {
        "report_version": SUBGROUP_REPORT_VERSION, "status": status,
        "binding_status": "MATCHED_SUBGROUP" if not reasons else "SUBGROUP_NOT_VERIFIED",
        "reasons": reasons, "cohort": cohort.as_dict(), "group_by": list(group_by),
        "selector": selector, "source_watermark": source_watermark,
        "context_watermark": binding["context_watermark"], "subgroup_evidence_watermark": subgroup_watermark,
        "subgroup_episode_ids": actual_ids, "completed_episodes": len(actual_ids),
        "missing_context_count": len(missing), "missing_context_episode_ids": sorted(missing)[:100],
        "missing_context_episode_ids_truncated": len(missing) > 100,
        "unreliable_context_count": len(unknown), "unreliable_context_episode_ids": sorted(unknown)[:100],
        "unreliable_context_episode_ids_truncated": len(unknown) > 100,
        "calculated_at": calculation_timestamp,
        "parent_evidence_status": parent_status, "parent_binding_status": parent_binding,
        "coverage_basis": "authoritative_cohort_assessment_and_exact_immutable_context_partition",
    }


def _cost_coverage(rows):
    result, details, blockers = {}, {}, []
    for component in ("fees", "funding", "slippage"):
        statuses, known = Counter(), 0
        for row in rows:
            coverage = str(row.get(component + "_coverage") or "UNKNOWN").upper()
            statuses[coverage] += 1
            value = _decimal(row.get(component), component)
            if coverage == "VERIFIED_ZERO" and value is not None and value != 0:
                raise ValueError(f"{component} verified zero conflicts with observed amount")
            if coverage in _KNOWN_COST_COVERAGE and value is not None:
                if component == "fees" and value < 0:
                    raise ValueError("fees must be a nonnegative cost")
                known += 1
        status = "COMPLETE" if rows and known == len(rows) else "PARTIAL" if known else "UNKNOWN"
        result[component] = status
        details[component] = {"known_episodes": known, "completed_episodes": len(rows),
                              "source_coverage_statuses": dict(sorted(statuses.items()))}
        if status != "COMPLETE":
            blockers.append(component.upper() + "_COVERAGE_" + status)
        if rows and any(not row.get(component + "_cost_model") or
                        row.get(component + "_cost_model") == "UNKNOWN" for row in rows):
            blockers.append(component.upper() + "_COST_MODEL_UNKNOWN")
    result.update(status="COMPLETE" if rows and not blockers else "PARTIAL" if rows else "UNKNOWN",
                  details=details, verification_blockers=blockers,
                  net_pnl_basis="authoritative_booked_net_not_costs_subtracted_again",
                  slippage_basis="explicit_observed_or_modeled_cost_not_inferred_from_filled_price")
    return result


def sample_confidence(episodes: Iterable[Mapping]) -> dict:
    """Wilson describes net-positive episode frequency, not a profitable edge."""
    rows = [dict(row) for row in episodes]
    count = len(rows)
    label = ("INSUFFICIENT" if count < 30 else "EARLY_EVIDENCE" if count < 75 else
             "DEVELOPING" if count < 150 else "STRONG_SAMPLE" if count < 300 else "MATURE_SAMPLE")
    warnings = ["TRADES_NOT_ASSUMED_INDEPENDENT"]
    if count < 30:
        warnings.append("SMALL_SUBGROUP")
    net = [_decimal(row.get("net_pnl"), "net_pnl") for row in rows]
    interval = None
    concentration = None
    with localcontext() as context:
        context.prec = 50
        if count and all(value is not None for value in net):
            successes = sum(value > 0 for value in net)
            n, z = Decimal(count), Decimal("1.959963984540054")
            p, z2 = Decimal(successes) / n, z * z
            denominator = 1 + z2 / n
            center = (p + z2 / (2 * n)) / denominator
            half = z * (p * (1 - p) / n + z2 / (4 * n * n)).sqrt() / denominator
            lower, upper = max(Decimal(0), center - half), min(Decimal(1), center + half)
            if successes == 0:
                lower = Decimal(0)
            if successes == count:
                upper = Decimal(1)
            interval = {"method": "WILSON", "confidence_level": "0.95", "sample_size": count,
                        "successes": successes, "lower_pct": _text(lower * 100),
                        "upper_pct": _text(upper * 100), "basis": "net_positive_completed_episode_frequency",
                        "assumption": "nominal_binomial_interval_dependence_can_invalidate_coverage"}
            positive = [value for value in net if value > 0]
            if positive:
                concentration = max(positive) / sum(positive, Decimal(0))
                if len(positive) > 1 and concentration >= Decimal("0.5"):
                    warnings.append("CONCENTRATED_POSITIVE_RETURNS")
        elif count:
            warnings.append("MISSING_NET_PNL")
        durations = []
        for row in rows:
            opened, closed = _timestamp(row.get("opened_at")), _timestamp(row.get("closed_at"))
            if opened and closed and closed >= opened:
                durations.append((opened, closed))
        latest = None
        for opened, closed in sorted(durations):
            if latest is not None and opened < latest:
                warnings.append("OVERLAPPING_EPISODES")
                break
            latest = max(latest, closed) if latest else closed
        if count and len(durations) != count:
            warnings.append("INDEPENDENCE_TIMING_UNKNOWN")
        signals = Counter(row["signal_id"] for row in rows if row.get("signal_id"))
        if any(value > 1 for value in signals.values()):
            warnings.append("SHARED_SIGNAL_CLUSTERS")
        return {"classification": label, "label": label, "completed_episodes": count,
                "win_rate_interval": interval, "warnings": sorted(set(warnings)),
                "largest_positive_episode_share": _text(concentration),
                "statistical_proof_of_profitability": False,
                "definition": "descriptive_completed_episode_volume_not_independent_sample_proof"}


def _extended_metrics(rows, v1):
    net = [_decimal(row.get("net_pnl"), "net_pnl") for row in rows]
    gross = []
    for row, value in zip(rows, net):
        amount = _decimal(row.get("gross_pnl"), "gross_pnl")
        fees, funding = _decimal(row.get("fees"), "fees"), _decimal(row.get("funding"), "funding")
        if (amount is None and value is not None and fees is not None and funding is not None
                and str(row.get("fees_coverage") or "UNKNOWN").upper() in _KNOWN_COST_COVERAGE
                and str(row.get("funding_coverage") or "UNKNOWN").upper() in _KNOWN_COST_COVERAGE):
            amount = value + fees + funding
        gross.append(amount)
    all_net, all_gross = all(v is not None for v in net), all(v is not None for v in gross)
    wins = [v for v in net if v is not None and v > 0]
    losses = [v for v in net if v is not None and v < 0]
    winning_r, losing_r = [], []
    for row, value in zip(rows, net):
        risk = _decimal(row.get("initial_risk", row.get("initial_risk_amount")), "initial_risk")
        if value is not None and risk is not None and risk > 0:
            if value > 0:
                winning_r.append(value / risk)
            elif value < 0:
                losing_r.append(value / risk)
    mean = lambda values: _text(sum(values, Decimal(0)) / len(values)) if values else None
    result = {
        "loss_rate_pct": _text(Decimal(len(losses)) * 100 / len(rows)) if rows and all_net else None,
        "gross_profit": _text(sum((v for v in gross if v > 0), Decimal(0))) if all_gross else None,
        "gross_loss": _text(-sum((v for v in gross if v < 0), Decimal(0))) if all_gross else None,
        "average_winner": mean(wins) if all_net else None,
        "average_loser": mean(losses) if all_net else None,
        "average_winning_r": mean(winning_r), "average_losing_r": mean(losing_r),
        "winning_r_sample_size": len(winning_r), "losing_r_sample_size": len(losing_r),
        "average_realised_rr": v1["expectancy_net_r"],
        "realised_rr_basis": "authoritative_episode_net_pnl_divided_by_original_observed_episode_risk",
        "slippage": None,
    }
    slippage = [_decimal(row.get("slippage"), "slippage") if str(row.get("slippage_coverage") or "UNKNOWN").upper()
                in _KNOWN_COST_COVERAGE else None for row in rows]
    if rows and all(value is not None for value in slippage):
        result["slippage"] = _text(sum(slippage, Decimal(0)))
    for field, name in (("planned_rr", "average_planned_rr"), ("mae_r", "mean_mae_r"), ("mfe_r", "mean_mfe_r")):
        values = [_decimal(row.get(field), field) for row in rows]
        known = [value for value in values if value is not None]
        result[name] = mean(known) if rows and len(known) == len(rows) else None
        result["known_" + name] = mean(known)
        result[field + "_sample_size"] = len(known)
    result["direction_performance"] = {}
    for direction in ("LONG", "SHORT"):
        subset = [row for row in rows if _direction(row) == direction]
        values = [_decimal(row.get("net_pnl"), "net_pnl") for row in subset]
        result["direction_performance"][direction] = {
            "completed_episodes": len(subset),
            "net_pnl": _text(sum(values, Decimal(0))) if all(v is not None for v in values) else None,
            "wins": sum(v > 0 for v in values) if all(v is not None for v in values) else None,
            "assessment": "descriptive_subsample_requires_own_context_group_for_verification",
        }
    result["unknown_direction_episodes"] = sum(_direction(row) is None for row in rows)
    return result


def calculate_context_performance(episodes: Iterable[Mapping], contexts: Iterable[Mapping], *,
        cohort: EvidenceCohort, group_by=("symbol", "session", "trend_regime"),
        source_watermark=None, completeness_report=None, calculation_timestamp=None,
        evidence_episodes=None, include_research=False) -> dict:
    """Calculate exact-cohort context groups without changing v1 or the ledger."""
    group_by = tuple(group_by)
    if group_by not in SUPPORTED_GROUPINGS:
        raise ValueError("unsupported strategy intelligence grouping")
    calculated_at = calculation_timestamp or datetime.now(timezone.utc).isoformat()
    if _timestamp(calculated_at) is None:
        raise ValueError("calculation_timestamp must be an aware timestamp")
    material = [dict(row) for row in episodes]
    authority = material if evidence_episodes is None else [dict(row) for row in evidence_episodes]
    contexts = [dict(row) for row in contexts]
    binding = _prepare_partition_binding(completeness_report, material, contexts, cohort=cohort,
        group_by=group_by, source_watermark=source_watermark, calculation_timestamp=calculated_at,
        authority=authority, include_research=include_research)
    eligible = binding["eligible"]
    _, excluded, duplicates = _selected_episodes(material, cohort, include_open=True)
    selected = [row for row in eligible if str(row.get("status")).lower() == "closed"]
    context_by_episode, missing, unreliable, context_watermark = (
        binding["context_by_episode"], binding["missing"], binding["unknown"], binding["context_watermark"])
    groups = {}
    for row in selected:
        for context in context_by_episode.get(row["episode_id"]) or [None]:
            selector = _selector(row, context, group_by)
            key = _hash({"cohort": cohort.as_dict(), "group_by": list(group_by), "selector": selector})
            group = groups.setdefault(key, {"selector": selector, "rows": {}, "contexts": {}})
            group["rows"][row["episode_id"]] = row
            group["contexts"][row["episode_id"]] = context
    output = []
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        for key, group in sorted(groups.items(), key=lambda item: evidence_json(item[1]["selector"])):
            rows = [group["rows"][episode_id] for episode_id in sorted(group["rows"])]
            subgroup_report = _build_bound_subgroup(binding,
                cohort=cohort, group_by=group_by, selector=group["selector"], subgroup_episodes=rows,
                source_watermark=source_watermark, calculation_timestamp=calculated_at)
            v1 = calculate_metrics(rows, cohort=cohort, source_watermark=source_watermark,
                                   calculation_timestamp=calculated_at)
            costs = _cost_coverage(rows)
            quality_counts = Counter(_context_quality(row, group["contexts"][row["episode_id"]]) for row in rows)
            context_status = "VALID" if quality_counts and set(quality_counts) == {"VALID"} else "UNKNOWN"
            blockers = list(subgroup_report["reasons"]) + list(costs["verification_blockers"])
            if not v1["identity_verified"]:
                blockers.append("STRATEGY_IDENTITY_UNVERIFIED")
            if not v1["financial_evidence_complete"]:
                blockers.append("FINANCIAL_EVIDENCE_INCOMPLETE")
            if v1["coverage"]["valid_close_time_episodes"] != v1["trade_count"]:
                blockers.append("ACTUAL_CLOSE_TIMES_INCOMPLETE")
            if v1["evidence_partition"] in ("UNVERIFIED_EXECUTION_MODE", "NONEXECUTED_RESEARCH_OR_DECISION"):
                blockers.append("EXECUTION_PARTITION_UNVERIFIED")
            if context_status != "VALID":
                blockers.append("CONTEXT_QUALITY_UNVERIFIED")
            if any(value in (None, "", "UNKNOWN") for value in group["selector"]["group"].values()):
                blockers.append("UNKNOWN_GROUPING_DIMENSION")
            verified = bool(rows and subgroup_report["status"] == "COMPLETE" and not blockers)
            net = _decimal(v1["net_pnl"], "net_pnl")
            observed_direction = "UNKNOWN" if net is None else "POSITIVE" if net > 0 else "NEGATIVE" if net < 0 else "BREAKEVEN"
            metrics = {**v1, **_extended_metrics(rows, v1)}
            # v1 correctly sees no v1 report for this subset. v2 exposes its own
            # independently bound report rather than pretending a parent is v1.
            metrics.update(profitability_verified=verified,
                evidence_completeness_status=subgroup_report["status"],
                completeness_binding_status=subgroup_report["binding_status"],
                history_complete=subgroup_report["status"] == "COMPLETE", ready_for_evidence_review=verified,
                verification_contract_version=SUBGROUP_REPORT_VERSION)
            output.append({"group_key": key, **group["selector"], "cohort": cohort.as_dict(),
                "episode_ids": [row["episode_id"] for row in rows], "metrics": metrics,
                "evidence_quality": subgroup_report,
                "context_quality": {"status": context_status, "counts": dict(sorted(quality_counts.items()))},
                "cost_coverage": costs, "sample_confidence": sample_confidence(rows),
                "profitability_verified": verified,
                "profitability": {"status": "VERIFIED" if verified else "UNVERIFIED",
                    "verified": verified, "observed_direction": observed_direction,
                    "blockers": sorted(set(blockers)),
                    "basis": "booked_episode_result_evidence_verification_not_statistical_edge_validation"}})
        overall_costs = _cost_coverage(selected)
        confidence = sample_confidence(selected)
    parent_status, parent_binding = binding["parent_status"], binding["parent_binding"]
    overall_status = _status(group["evidence_quality"]["status"] for group in output) if output else parent_status
    return {"contract_version": CONTRACT_VERSION, "calculation_version": CONTRACT_VERSION,
        "financial_calculation_version": "strategy_intelligence.v1", "calculation_timestamp": calculated_at,
        "cohort": cohort.as_dict(), "group_by": list(group_by), "source_watermark": source_watermark,
        "context_watermark": context_watermark, "groups": output, "evidence_quality": overall_status,
        "cost_coverage": overall_costs, "sample_confidence": confidence,
        "profitability_verified": bool(output and all(group["profitability_verified"] for group in output)),
        "completed_episode_count": len(selected), "excluded": excluded, "duplicate_episode_rows": duplicates,
        "missing_context_count": len(missing), "unreliable_context_count": len(unreliable),
        "parent_evidence_status": parent_status, "parent_binding_status": parent_binding,
        "entry_context_policy": "original_episode_entry_secondary_scale_in_contexts_preserved_separately",
        "partition_policy": "exact_cohort_plus_classifier_identity_plus_cost_models",
        "partition_totals_note": "episode_counts_are_not_additive_across_classifier_versions_or_research_classifications"}
