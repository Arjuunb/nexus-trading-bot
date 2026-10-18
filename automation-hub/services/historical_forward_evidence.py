"""Read-only binding of immutable historical research to actual paper evidence.

Execution mode is a provenance partition, not part of strategy identity. Owners,
accounts and sessions remain authorization boundaries. Legacy summaries without
observed identity or reconciled costs are inspectable but cannot qualify.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re

from services.strategy_identity import configuration_fingerprint


IDENTITY = ("strategy_id", "strategy_version", "config_fingerprint")
SCOPE = ("account_id", "instance_id", "simulation_session_id")
JOB_METHODS = {"HISTORICAL_BACKTEST": "historical_backtest", "OUT_OF_SAMPLE_VALIDATION": "out_of_sample",
               "ANCHORED_WALK_FORWARD": "walk_forward", "ROLLING_WALK_FORWARD": "walk_forward",
               "ROBUSTNESS_EVALUATION": "robustness", "COST_STRESS": "cost_stress"}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _count(value):
    parsed = number(value)
    return int(parsed) if parsed is not None and 0 <= parsed <= 1_000_000 and parsed == parsed.to_integral_value() else None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def historical_metrics(result):
    """Use explicit closed financial samples; never zero-fill legacy results.

    Slippage is disclosed separately and already reflected in executed prices;
    booked net is reconciled against gross, fees and signed funding, never
    charged for slippage twice. The existing simulator's R-only rows lack these
    facts and intentionally remain incomplete.
    """
    result = _mapping(result)
    trades = result.get("trades")
    trades = trades if isinstance(trades, list) else []
    reasons, outcomes, pnls, opened, closed, seen = set(), [], [], [], [], set()
    with localcontext() as precision:
        precision.prec = 50
        for trade in trades:
            if not isinstance(trade, dict):
                reasons.add("INVALID_FINANCIAL_SAMPLE")
                continue
            episode_id = trade.get("episode_id")
            if not isinstance(episode_id, str) or not episode_id or episode_id in seen:
                reasons.add("MISSING_OR_DUPLICATE_EPISODE_ID")
            seen.add(episode_id if isinstance(episode_id, str) else None)
            amounts = {key: number(trade.get(key)) for key in
                       ("net_pnl", "gross_pnl", "fees", "funding", "slippage", "initial_risk")}
            start, end = timestamp(trade.get("entry_time")), timestamp(trade.get("exit_time"))
            if trade.get("status") != "closed" or start is None or end is None or end < start:
                reasons.add("INCOMPLETE_EPISODE_LIFECYCLE")
            else:
                opened.append(start); closed.append(end)
            if any(value is None for value in amounts.values()) or (amounts["initial_risk"] or 0) <= 0:
                reasons.add("MISSING_FINANCIAL_AMOUNTS_OR_ORIGINAL_RISK")
            else:
                if amounts["gross_pnl"] - amounts["fees"] - amounts["funding"] != amounts["net_pnl"]:
                    reasons.add("FINANCIAL_RECONCILIATION_MISMATCH")
                outcomes.append(amounts["net_pnl"] / amounts["initial_risk"])
                pnls.append(amounts["net_pnl"])
            for component in ("fees", "funding", "slippage"):
                coverage = trade.get(component + "_coverage")
                if (coverage not in {"BOOKED", "MODELED", "VERIFIED_ZERO"}
                        or trade.get(component + "_cost_model") in (None, "", "UNKNOWN")
                        or (coverage == "VERIFIED_ZERO" and amounts[component] != 0)
                        or (component != "funding" and amounts[component] is not None and amounts[component] < 0)):
                    reasons.add(component.upper() + "_COST_EVIDENCE_INCOMPLETE")
        if not trades:
            reasons.add("NO_COMPLETED_FINANCIAL_SAMPLES")
        if "total_trades" in result and _count(result["total_trades"]) != len(trades):
            reasons.add("FINANCIAL_SAMPLE_COUNT_MISMATCH")
        complete = not reasons
        net = sum(outcomes, Decimal(0)) if complete else None
        wins = sum((value for value in pnls if value > 0), Decimal(0))
        losses = -sum((value for value in pnls if value < 0), Decimal(0))
        pf = wins / losses if complete and losses else None
        expectancy = net / len(trades) if complete else None
        equity = peak = drawdown = Decimal(0)
        if complete:
            for _, value in sorted(zip(closed, outcomes)):
                equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
        text = lambda value: str(value) if value is not None else None
        return {"trades": len(trades), "trade_count": len(trades), "completed_episode_count": len(trades),
                "r_sample_size": len(outcomes), "net_r": text(net), "profit_factor": text(pf),
                "net_pnl": text(sum(pnls, Decimal(0))) if complete else None,
                "net_profit_factor": text(pf), "expectancy_r": text(expectancy), "expectancy_net_r": text(expectancy),
                "max_drawdown_r": text(drawdown) if complete else None,
                "financial_evidence_complete": complete, "evidence_quality": "COMPLETE" if complete else "INCOMPLETE",
                "cost_coverage": {"status": "COMPLETE" if complete else "UNKNOWN"},
                "first_opened_at": min(opened).isoformat() if opened else None,
                "last_closed_at": max(closed).isoformat() if closed else None,
                "financial_samples": deepcopy(trades),
                "financial_evidence_reasons": sorted(reasons)}


def _complete_metrics(metrics):
    metrics = _mapping(metrics)
    count = _count(metrics.get("completed_episode_count"))
    flags = (metrics.get("financial_evidence_complete") is True and count is not None and count > 0
            and _count(metrics.get("r_sample_size")) == count
            and metrics.get("evidence_quality") == "COMPLETE"
            and (metrics.get("cost_coverage") or {}).get("status") == "COMPLETE"
            and number(metrics.get("net_r")) is not None and number(metrics.get("expectancy_net_r")) is not None)
    if not flags or not isinstance(metrics.get("financial_samples"), list):
        return False
    original = historical_metrics({"trades": metrics["financial_samples"]})
    return original["financial_evidence_complete"] and all(metrics.get(key) == original[key] for key in (
        "completed_episode_count", "r_sample_size", "net_r", "net_pnl", "expectancy_net_r", "net_profit_factor",
        "max_drawdown_r", "first_opened_at", "last_closed_at"))


def _within(metrics, start, end):
    first, last = timestamp(metrics.get("first_opened_at")), timestamp(metrics.get("last_closed_at"))
    return first is not None and last is not None and start <= first <= last <= end


def _oos_complete(report, row, start, end):
    boundaries = report.get("partitions") or {}
    train_end, validation_end, holdout_end = (timestamp(boundaries.get(key)) for key in
        ("train_end", "validation_end", "holdout_end"))
    if (row.get("holdout_access_count") != 1 or type(row.get("holdout_access_count")) is not int
            or train_end is None or validation_end is None or holdout_end is None
            or not start < train_end < validation_end < holdout_end <= end):
        return False
    def sample_ids(part):
        samples = _mapping(report.get(part)).get("financial_samples")
        return samples if isinstance(samples, list) else []
    samples = [set(sample["episode_id"] for sample in sample_ids(part)
                   if isinstance(sample, dict) and isinstance(sample.get("episode_id"), str))
               for part in ("train", "validation", "holdout")]
    if any(samples[i] & samples[j] for i, j in ((0, 1), (0, 2), (1, 2))):
        return False
    return all(_complete_metrics(report.get(part) or {}) and _within(report[part], left, right)
        and (part == "train" or timestamp(report[part]["first_opened_at"]) > left)
        for part, left, right in (("train", start, train_end), ("validation", train_end, validation_end),
                                  ("holdout", validation_end, holdout_end)))


def _walk_forward_complete(report, start, end):
    windows, previous_end = report.get("windows") or [], None
    if not isinstance(windows, list) or not windows:
        return False
    for window in windows:
        if not isinstance(window, dict):
            return False
        left, right = timestamp(window.get("evaluation_start")), timestamp(window.get("evaluation_end"))
        development, evaluation = _mapping(window.get("development")), _mapping(window.get("evaluation"))
        dev_end = timestamp(development.get("last_closed_at"))
        if (left is None or right is None or not start < left <= right <= end
                or (previous_end is not None and left <= previous_end)
                or dev_end is None or dev_end >= left
                or not _complete_metrics(development) or not _complete_metrics(evaluation)
                or not _within(development, start, left) or not _within(evaluation, left, right)):
            return False
        previous_end = right
    return True


def _stress_complete(report):
    stress = _mapping(report.get("stress"))
    values = {key: number(stress.get(key)) for key in ("gross_r", "net_r", "fee", "slippage", "funding")}
    if (any(value is None for value in values.values()) or values["fee"] < 0 or values["slippage"] < 0
            or values["gross_r"] != number(_mapping(report.get("base")).get("net_r"))):
        return False
    with localcontext() as precision:
        precision.prec = 50
        return values["gross_r"] - values["fee"] - values["slippage"] - values["funding"] == values["net_r"]


def _authorized(row, scope):
    # Isolated research may be reused only by its owner with the exact frozen
    # identity. Partly bound or other-account research is never a wildcard.
    return row.get("owner_id") == scope.get("owner_id") and (
        all(key in row and row[key] == scope.get(key) for key in SCOPE)
        or all(key in row and row[key] is None for key in SCOPE))


def _identity_matches(observed, frozen, identity):
    if not isinstance(observed, dict) or not isinstance(frozen, dict):
        return False
    try:
        return (observed.get("identity_status") == frozen.get("identity_status") == "observed"
                and all(observed.get(key) == frozen.get(key) == identity.get(key) for key in IDENTITY[:2])
                and observed.get("strategy_config_hash") == frozen.get("strategy_config_hash") == identity.get("config_fingerprint")
                and configuration_fingerprint(observed.get("configuration")) == identity.get("config_fingerprint")
                and observed.get("configuration") == frozen.get("configuration")
                and bool(observed.get("source_hash")) and observed["source_hash"] == frozen.get("source_hash"))
    except (ValueError, TypeError):
        return False


def build_evidence_chain(*, scope, identity, frozen, records, performance, jobs=(), bounded=False):
    """Join provenance, never economic samples or execution permissions."""
    reasons, latest, selected, bound_methods = [], {}, {}, set()
    symbol = identity.get("symbol")
    timeframe = ((frozen or {}).get("configuration") or {}).get("timeframes", {}).get("entry_timeframe")
    for row in sorted(records, key=lambda item: timestamp(item.get("created_at")) or datetime.max.replace(tzinfo=timezone.utc), reverse=True):
        if not _authorized(row, scope) or any(row.get(key) != identity.get(key) for key in IDENTITY):
            continue
        dataset = _mapping(_mapping(row.get("report")).get("dataset"))
        # Other assets/timeframes are separate cohorts, not failed retries.
        if dataset.get("symbol") not in (None, symbol) or dataset.get("timeframe") not in (None, timeframe):
            continue
        if row.get("execution_mode") != "backtest":
            continue
        latest.setdefault(row.get("method"), row)
    for job in jobs:
        if not _authorized(job, scope) or any(job.get(key) != identity.get(key) for key in IDENTITY):
            continue
        request = job.get("request") or {}
        if request.get("symbol") != symbol or request.get("timeframe") != timeframe:
            continue
        method = JOB_METHODS.get(job.get("job_type"))
        previous = latest.get(method)
        if method and job.get("status") != "COMPLETED" and (
                previous is None or (timestamp(job.get("requested_at")) or datetime.max.replace(tzinfo=timezone.utc))
                > (timestamp(previous.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc))):
            latest[method] = {"status": job.get("status"), "run_id": None}

    base = latest.get("historical_backtest") or {}
    base_dataset = _mapping(_mapping(base.get("report")).get("dataset"))
    for method in ("historical_backtest", "out_of_sample", "walk_forward", "robustness", "cost_stress"):
        row = latest.get(method) or {}
        report = _mapping(row.get("report"))
        dataset = _mapping(report.get("dataset"))
        start, end = timestamp(dataset.get("period_start")), timestamp(dataset.get("period_end"))
        binding = (not bounded and bool(scope.get("owner_id")) and bool(symbol) and bool(timeframe)
            and row.get("status") in {"COMPLETE", "COMPLETED", "PASS"}
            and report.get("status") in {"COMPLETE", "PASS"}
            and report.get("identity_binding_status") == "MATCHED"
            and _identity_matches(report.get("identity"), frozen, identity)
            and bool(dataset.get("dataset_id")) and bool(re.fullmatch(r"[0-9a-f]{64}", str(dataset.get("content_hash") or "")))
            and row.get("dataset_identity") == dataset.get("dataset_id")
            and row.get("data_revision") == dataset.get("content_hash")
            and dataset.get("symbol") == symbol and dataset.get("timeframe") == timeframe
            and (dataset.get("coverage") or {}).get("status") in {"COMPLETE", "VERIFIED"}
            and start is not None and end is not None and start < end
            and timestamp(row.get("period_start")) == start and timestamp(row.get("period_end")) == end
            and timestamp(row.get("completed_at")) is not None and timestamp(row["completed_at"]) >= end
            and all(dataset.get(key) == base_dataset.get(key) for key in
                    ("dataset_id", "content_hash", "symbol", "timeframe", "period_start", "period_end")))
        financial = report.get("metrics") if method == "historical_backtest" else report.get("base")
        if method == "out_of_sample":
            financial_ok = bool(binding and _oos_complete(report, row, start, end))
        elif method == "walk_forward":
            financial_ok = bool(binding and _walk_forward_complete(report, start, end))
        else:
            financial_ok = bool(binding and _complete_metrics(financial or {}) and _within(financial, start, end))
            if method == "cost_stress":
                financial_ok = financial_ok and _stress_complete(report)
        if binding and financial_ok:
            selected[method] = {**report, "status": report.get("result_status", report.get("status"))}
            bound_methods.add(method)
        else:
            reasons.append(method.upper() + "_EVIDENCE_UNBOUND_OR_INCOMPLETE")
            selected[method] = {"status": "FAIL"} if row.get("status") == "FAIL" or report.get("status") == "FAIL" else None

    groups = performance.get("groups") or []
    group = groups[0] if len(groups) == 1 else {}
    metrics = group.get("metrics") or {}
    ids = group.get("episode_ids") or []
    count = _count(metrics.get("completed_episode_count"))
    period = group.get("observation_period") or {}
    first, last = timestamp(period.get("first_opened_at")), timestamp(period.get("last_closed_at"))
    historical_end = timestamp(base_dataset.get("period_end"))
    completed = [timestamp((latest.get(method) or {}).get("completed_at")) for method in selected]
    independent = (first is not None and historical_end is not None and historical_end < first
                   and all(value is not None and value < first for value in completed))
    expected = {**identity, **scope}
    exact = all(group.get("cohort", {}).get(key) == expected.get(key) for key in
                (*IDENTITY, "symbol", "owner_id", *SCOPE, "lab_id", "execution_mode", "source_kind"))
    complete = (not bounded and exact and identity.get("execution_mode") == "paper"
        and identity.get("source_kind") == "forward_paper" and performance.get("cache_status") == "READY"
        and bool(performance.get("source_watermark")) and group.get("profitability_verified") is True
        and (group.get("evidence_quality") or {}).get("status") == "COMPLETE"
        and (group.get("cost_coverage") or {}).get("status") == "COMPLETE"
        and metrics.get("financial_evidence_complete") is True
        and count is not None and count > 0 and _count(metrics.get("r_sample_size")) == count
        and isinstance(ids, list) and len(ids) == count and all(isinstance(value, str) and value for value in ids)
        and len(set(ids)) == count
        and first is not None and last is not None and first <= last)
    forward = {"status": "PASS" if complete and independent else "UNKNOWN", "completed_episodes": count,
        "episode_ids": list(ids), "expectancy_net_r": metrics.get("expectancy_net_r"),
        "evidence_quality": "COMPLETE" if complete else "UNKNOWN",
        "cost_coverage_status": (group.get("cost_coverage") or {}).get("status", "UNKNOWN"),
        "independence_status": "VERIFIED_INDEPENDENT" if independent else "UNKNOWN",
        "observation_period": period, "source_watermark": performance.get("source_watermark"),
        "basis": "reconciled_actual_closed_position_episodes"}
    if not complete: reasons.append("ACTUAL_FORWARD_PAPER_EVIDENCE_INCOMPLETE")
    if not independent: reasons.append("HISTORICAL_TO_FORWARD_CHRONOLOGY_UNVERIFIED")
    if bounded: reasons.append("EVIDENCE_QUERY_BOUND_EXCEEDED")
    return {"contract_version": "historical_forward_evidence.v1", "identity": dict(identity),
        "status": "COMPLETE" if not reasons else "INCOMPLETE", "blocking_reasons": reasons,
        "historical": {"run_id": base.get("run_id"), "metrics": (selected.get("historical_backtest") or {}).get("metrics", {})},
        "forward_paper": forward, "reports": selected,
        "stages": {method: {"status": (report or {}).get("status", "UNKNOWN"),
                              "binding_status": "BOUND" if method in bound_methods else "UNVERIFIED"}
                   for method, report in selected.items()},
        "validation_run_ids": {method: row.get("run_id") for method, row in latest.items()}}
