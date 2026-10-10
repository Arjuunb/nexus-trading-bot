"""Context aggregation is descriptive until its own evidence and costs bind."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from services.strategy_evidence_completeness import REPORT_VERSION, metrics_episode_watermark
from services.strategy_intelligence_metrics import EvidenceCohort, calculate_metrics
from services.strategy_intelligence_v2 import (
    CONTRACT_VERSION, SUPPORTED_GROUPINGS, build_subgroup_report, calculate_context_performance,
    sample_confidence,
)


COHORT = EvidenceCohort(strategy_id="adaptive_trend_pullback", strategy_version="1.0.0",
    config_fingerprint="a" * 64, instance_id="paper-one", lab_id=None,
    simulation_session_id=None, execution_mode="forward_paper", source_kind="instance",
    owner_id="owner", account_id="account", symbol=None)
NOW = "2026-10-09T12:00:00Z"


def episode(episode_id="one", **values):
    return {**COHORT.as_dict(), "episode_id": episode_id, "symbol": "XRPUSDT",
        "status": "closed", "evidence_kind": "executed", "identity_status": "observed",
        "opened_at": "2026-09-01T00:00:00Z", "closed_at": "2026-09-01T01:00:00Z",
        "net_pnl": "9", "gross_pnl": "10", "fees": "1", "funding": "0",
        "initial_risk": "10", "fees_coverage": "BOOKED", "funding_coverage": "VERIFIED_ZERO",
        "slippage": "0", "slippage_coverage": "VERIFIED_ZERO", "direction": "BUY",
        "fees_cost_model": "booked-v1", "funding_cost_model": "funding-zero-v1",
        "slippage_cost_model": "no-slippage-v1", **values}


def context(episode_id="one", **values):
    return {**COHORT.as_dict(), "episode_id": episode_id, "symbol": "XRPUSDT",
        "entry_timeframe": "15m", "session": "NEW_YORK", "trend_regime": "BULL",
        "volatility_regime": "NORMAL", "structure_regime": "UNKNOWN",
        "signal_timestamp": "2026-08-31T23:59:00Z", "entry_timestamp": "2026-09-01T00:00:00Z",
        "context_quality": "VALID", "classifier_id": "market_context_classifier",
        "classifier_version": "1.0.0", "parameter_hash": "b" * 64,
        "classification_kind": "ENTRY", **values}


def report(rows, **changes):
    return {"report_version": REPORT_VERSION, "status": "COMPLETE", "history_complete": True,
        "cohort": COHORT.as_dict(), "source_watermark": "source-one",
        "metrics_evidence_watermark": metrics_episode_watermark(rows, COHORT),
        "calculated_at": NOW, "counts": {"missing_events": 0}, **changes}


def calculate(rows, contexts, **kwargs):
    return calculate_context_performance(rows, contexts, cohort=COHORT,
        group_by=("symbol", "session", "trend_regime"), source_watermark="source-one",
        calculation_timestamp=NOW, **kwargs)


def test_v2_preserves_v1_decimal_accounting_and_episodes_with_partial_exits():
    rows = [episode("a", net_pnl="0.20", gross_pnl="0.30", fees="0.10", realised_leg_count=3),
            episode("b", net_pnl="0.10", gross_pnl="0.20", fees="0.10")]
    result = calculate(rows + [deepcopy(rows[0])], [context("a"), context("b")])
    group = result["groups"][0]
    old = calculate_metrics(rows, cohort=COHORT, calculation_timestamp=NOW)
    assert result["contract_version"] == CONTRACT_VERSION == "strategy_intelligence.v2"
    assert group["metrics"]["trade_count"] == 2
    assert group["metrics"]["net_pnl"] == old["net_pnl"] == "0.3"
    assert group["metrics"]["fees"] == "0.2"
    assert group["metrics"]["expectancy_net_r"] == old["expectancy_net_r"]
    assert group["metrics"]["calculation_version"] == "strategy_intelligence.v1"


@pytest.mark.parametrize("dimension,value", [("strategy_id", "smc"), ("strategy_version", "2.0.0"),
    ("config_fingerprint", "c" * 64), ("instance_id", "another"), ("lab_id", "lab"),
    ("simulation_session_id", "session"), ("execution_mode", "replay"),
    ("source_kind", "backtest"), ("owner_id", "another"), ("account_id", "another")])
def test_v2_keeps_every_identity_and_mode_dimension_exact(dimension, value):
    rows = [episode(), episode("other", **{dimension: value})]
    result = calculate(rows, [context(), context("other", **{dimension: value})])
    assert len(result["groups"]) == 1
    assert result["groups"][0]["metrics"]["net_pnl"] == "9"


@pytest.mark.parametrize("kind", ["counterfactual", "rejected", "cancelled", "expired", "shadow"])
def test_nonexecuted_outcomes_never_contribute_to_context_metrics(kind):
    result = calculate([episode(), episode("other", evidence_kind=kind, net_pnl="999")],
                       [context(), context("other")])
    assert result["groups"][0]["metrics"]["trade_count"] == 1
    assert result["groups"][0]["metrics"]["net_pnl"] == "9"


def test_every_supported_combination_is_deterministic_and_episode_totals_do_not_double_count():
    rows = [episode("a"), episode("b", direction="SELL")]
    contexts = [context("a"), context("b", session="LONDON_NEW_YORK_OVERLAP", trend_regime="BEAR")]
    assert ("symbol", "session", "trend_regime") in SUPPORTED_GROUPINGS
    for group_by in SUPPORTED_GROUPINGS:
        result = calculate_context_performance(rows, contexts, cohort=COHORT,
            group_by=group_by, calculation_timestamp=NOW)
        reverse = calculate_context_performance(list(reversed(rows)), list(reversed(contexts)),
            cohort=COHORT, group_by=group_by, calculation_timestamp=NOW)
        assert result == reverse
        assert sum(group["metrics"]["trade_count"] for group in result["groups"]) == 2


def test_unknown_funding_and_slippage_cannot_be_verified_or_coerced_to_zero():
    rows = [episode(funding=None, funding_coverage="UNMODELED", slippage=None, slippage_coverage="UNKNOWN")]
    group = calculate(rows, [context()], completeness_report=report(rows))["groups"][0]
    assert group["cost_coverage"]["fees"] == "COMPLETE"
    assert group["cost_coverage"]["funding"] == "UNKNOWN"
    assert group["cost_coverage"]["slippage"] == "UNKNOWN"
    assert group["metrics"]["funding"] is None
    assert group["metrics"]["slippage"] is None
    assert group["metrics"]["net_pnl"] == "9"
    assert group["profitability_verified"] is False
    assert group["profitability"]["status"] == "UNVERIFIED"
    assert "FUNDING_COVERAGE_UNKNOWN" in group["profitability"]["blockers"]


@pytest.mark.parametrize("count,label", [(0, "INSUFFICIENT"), (29, "INSUFFICIENT"),
    (30, "EARLY_EVIDENCE"), (74, "EARLY_EVIDENCE"), (75, "DEVELOPING"),
    (149, "DEVELOPING"), (150, "STRONG_SAMPLE"), (299, "STRONG_SAMPLE"), (300, "MATURE_SAMPLE")])
def test_sample_confidence_is_a_volume_description_without_proof(count, label):
    rows = [episode(str(i)) for i in range(count)]
    confidence = sample_confidence(rows)
    assert confidence["classification"] == label
    assert confidence["completed_episodes"] == count
    assert confidence["statistical_proof_of_profitability"] is False
    assert "TRADES_NOT_ASSUMED_INDEPENDENT" in confidence["warnings"]


def test_wilson_win_rate_interval_is_bounded_and_uses_each_subgroup_own_count():
    rows = [episode(str(i), net_pnl="1" if i < 50 else "-1",
                    gross_pnl="2" if i < 50 else "0") for i in range(100)]
    interval = sample_confidence(rows)["win_rate_interval"]
    assert interval["method"] == "WILSON"
    assert Decimal("40") < Decimal(interval["lower_pct"]) < Decimal("41")
    assert Decimal("59") < Decimal(interval["upper_pct"]) < Decimal("60")
    assert sample_confidence([episode()])["win_rate_interval"]["upper_pct"] == "100"
    assert sample_confidence([])["win_rate_interval"] is None


def test_small_subgroup_does_not_inherit_parent_sample_or_coverage():
    rows = [episode(str(i)) for i in range(150)]
    contexts = [context(str(i), session="NEW_YORK" if i < 17 else "LONDON") for i in range(150)]
    result = calculate(rows, contexts, completeness_report=report(rows))
    group = next(group for group in result["groups"] if group["group"]["session"] == "NEW_YORK")
    assert group["metrics"]["trade_count"] == 17
    assert group["sample_confidence"]["classification"] == "INSUFFICIENT"
    assert "SMALL_SUBGROUP" in group["sample_confidence"]["warnings"]
    assert group["evidence_quality"]["completed_episodes"] == 17
    assert group["evidence_quality"]["subgroup_episode_ids"] == sorted(str(i) for i in range(17))


def test_fresh_parent_coverage_is_rebound_to_exact_own_context_and_episode_facts():
    rows = [episode()]
    group = calculate(rows, [context()], completeness_report=report(rows))["groups"][0]
    assert group["evidence_quality"]["status"] == "COMPLETE"
    assert group["evidence_quality"]["binding_status"] == "MATCHED_SUBGROUP"
    assert group["profitability_verified"] is True
    assert group["profitability"]["observed_direction"] == "POSITIVE"


@pytest.mark.parametrize("changes,reason", [({"source_watermark": "old"}, "SOURCE_WATERMARK_MISMATCH"),
    ({"metrics_evidence_watermark": "wrong"}, "EPISODE_WATERMARK_MISMATCH"),
    ({"calculated_at": "2026-10-09T11:54:59Z"}, "STALE_COMPLETENESS_REPORT"),
    ({"calculated_at": "2026-10-09T12:00:01Z"}, "FUTURE_REPORT_TIMESTAMP"),
    ({"cohort": replace(COHORT, instance_id="other").as_dict()}, "COHORT_MISMATCH")])
def test_unmatched_parent_report_cannot_be_borrowed(changes, reason):
    rows = [episode()]
    group = calculate(rows, [context()], completeness_report=report(rows, **changes))["groups"][0]
    assert group["evidence_quality"]["status"] == "UNKNOWN"
    assert reason in group["evidence_quality"]["reasons"]
    assert group["profitability_verified"] is False


def test_unknown_or_unassigned_context_blocks_whole_cohort_partition_certification():
    rows = [episode("a"), episode("b")]
    result = calculate(rows, [context("a")], completeness_report=report(rows))
    assert sum(group["metrics"]["trade_count"] for group in result["groups"]) == 2
    assert all(group["profitability_verified"] is False for group in result["groups"])
    assert all(group["evidence_quality"]["missing_context_count"] == 1 for group in result["groups"])
    missing = next(group for group in result["groups"] if group["group"]["session"] is None)
    assert missing["context_quality"]["status"] == "UNKNOWN"


def test_classifier_version_parameter_and_cost_model_boundaries_never_pool():
    rows = [episode("a"), episode("b", funding_cost_model="funding-v2"), episode("c")]
    contexts = [context("a"), context("b"), context("c", classifier_version="2.0.0", parameter_hash="c" * 64)]
    result = calculate(rows, contexts)
    assert len(result["groups"]) == 3
    assert all(group["metrics"]["trade_count"] == 1 for group in result["groups"])


def test_future_signal_context_and_conflicting_immutable_context_fail_closed():
    rows = [episode()]
    with pytest.raises(ValueError, match="signal.*entry"):
        calculate(rows, [context(signal_timestamp="2026-09-01T00:01:00Z")])
    with pytest.raises(ValueError, match="conflicting context"):
        calculate(rows, [context(), context(session="LONDON")])


def test_unsupported_grouping_is_rejected_before_computing():
    with pytest.raises(ValueError, match="unsupported"):
        calculate_context_performance([], [], cohort=COHORT, group_by=["balance"])


def test_unavailable_planned_rr_mae_mfe_and_costs_remain_null():
    metrics = calculate([episode()], [context()])["groups"][0]["metrics"]
    assert metrics["average_planned_rr"] is None
    assert metrics["mean_mae_r"] is None and metrics["mean_mfe_r"] is None
    assert metrics["average_realised_rr"] == "0.9"


def test_extended_metrics_reuse_actual_episode_risk_and_financial_precision():
    rows = [episode("a", planned_rr="2", mae_r="-0.2", mfe_r="1.8"),
            episode("b", net_pnl="-5", gross_pnl="-4", planned_rr="3", mae_r="-1", mfe_r="0.4")]
    metrics = calculate(rows, [context("a"), context("b")])["groups"][0]["metrics"]
    assert metrics["average_winner"] == "9" and metrics["average_loser"] == "-5"
    assert metrics["average_winning_r"] == "0.9" and metrics["average_losing_r"] == "-0.5"
    assert metrics["gross_profit"] == "10" and metrics["gross_loss"] == "4"
    assert metrics["average_planned_rr"] == "2.5"
    assert metrics["mean_mae_r"] == "-0.6" and metrics["mean_mfe_r"] == "1.1"


def test_authoritative_binding_is_to_original_projection_not_enriched_fields():
    original = [episode()]
    enriched = [{**original[0], "planned_rr": "2"}]
    group = calculate(enriched, [context()], evidence_episodes=original,
        completeness_report=report(original))["groups"][0]
    assert group["evidence_quality"]["status"] == "COMPLETE"
    assert group["metrics"]["average_planned_rr"] == "2"
    tampered = [{**enriched[0], "net_pnl": "999"}]
    with pytest.raises(ValueError, match="authoritative episode"):
        calculate(tampered, [context()], evidence_episodes=original,
                  completeness_report=report(original))


def test_concentrated_returns_and_overlapping_samples_are_explicit_warnings():
    rows = [episode("a", net_pnl="100", gross_pnl="101"), episode("b", net_pnl="1", gross_pnl="2")]
    confidence = sample_confidence(rows)
    assert "CONCENTRATED_POSITIVE_RETURNS" in confidence["warnings"]
    assert "OVERLAPPING_EPISODES" in confidence["warnings"]


def test_empty_sample_never_claims_profitability_even_with_complete_evidence():
    result = calculate([], [], completeness_report=report([]))
    assert result["groups"] == []
    assert result["profitability_verified"] is False
    assert result["sample_confidence"]["classification"] == "INSUFFICIENT"


def test_full_unknown_risk_and_missing_net_never_acquire_sample_certainty():
    rows = [episode("a", net_pnl=None, gross_pnl=None, initial_risk=None)]
    group = calculate(rows, [context("a")], completeness_report=report(rows))["groups"][0]
    assert group["metrics"]["net_pnl"] is None and group["metrics"]["expectancy_net_r"] is None
    assert group["sample_confidence"]["win_rate_interval"] is None
    assert "MISSING_NET_PNL" in group["sample_confidence"]["warnings"]
    assert group["profitability"]["observed_direction"] == "UNKNOWN"
    assert group["profitability_verified"] is False


def test_subgroup_helper_rejects_manufactured_membership_even_with_complete_parent():
    rows, contexts = [episode("a"), episode("b")], [context("a"), context("b", session="LONDON")]
    group = calculate(rows, contexts, completeness_report=report(rows))["groups"][0]
    selector = {key: group[key] for key in ("group", "classifier", "cost_model")}
    with pytest.raises(ValueError, match="subgroup membership"):
        build_subgroup_report(report(rows), rows, contexts, cohort=COHORT,
            group_by=("symbol", "session", "trend_regime"), selector=selector,
            subgroup_episodes=rows, source_watermark="source-one", calculation_timestamp=NOW)


def test_partial_authority_does_not_become_complete_in_a_tiny_profitable_group():
    rows = [episode()]
    group = calculate(rows, [context()], completeness_report=report(rows,
        status="PARTIAL", history_complete=False))["groups"][0]
    assert group["evidence_quality"]["status"] == "PARTIAL"
    assert group["sample_confidence"]["classification"] == "INSUFFICIENT"
    assert group["profitability_verified"] is False


def test_unnamed_cost_models_stay_explicit_and_block_verification():
    rows = [episode(fees_cost_model=None)]
    group = calculate(rows, [context()], completeness_report=report(rows))["groups"][0]
    assert group["cost_model"]["fees_cost_model"] == "UNKNOWN"
    assert group["cost_coverage"]["fees"] == "COMPLETE"
    assert "FEES_COST_MODEL_UNKNOWN" in group["profitability"]["blockers"]
    assert group["profitability_verified"] is False


def test_unknown_context_quality_never_reuses_valid_parent_context_confidence():
    rows = [episode()]
    group = calculate(rows, [context(context_quality="STALE_DATA")],
        completeness_report=report(rows))["groups"][0]
    assert group["evidence_quality"]["status"] == "PARTIAL"
    assert group["context_quality"]["counts"] == {"STALE_DATA": 1}
    assert group["profitability_verified"] is False


def test_signed_funding_and_slippage_are_not_subtracted_a_second_time():
    rows = [episode(net_pnl="11", gross_pnl="10", funding="-2", funding_coverage="BOOKED",
                    slippage="0.25", slippage_coverage="MODELED")]
    group = calculate(rows, [context()], completeness_report=report(rows))["groups"][0]
    assert group["metrics"]["funding"] == "-2"
    assert group["metrics"]["slippage"] == "0.25"
    assert group["metrics"]["net_pnl"] == "11"


def test_unknown_direction_remains_unknown_and_does_not_assume_long():
    rows = [episode(direction=None)]
    result = calculate_context_performance(rows, [context()], cohort=COHORT,
        group_by=("direction",), calculation_timestamp=NOW)
    assert result["groups"][0]["group"]["direction"] is None
    assert result["groups"][0]["metrics"]["unknown_direction_episodes"] == 1


def test_future_research_classification_does_not_replace_entry_snapshot_by_default():
    rows = [episode()]
    result = calculate(rows, [context(), context(classification_kind="RESEARCH", session="LONDON",
                         classifier_version="2.0.0", parameter_hash="c" * 64)])
    assert len(result["groups"]) == 1
    assert result["groups"][0]["group"]["session"] == "NEW_YORK"


def test_confirmed_zero_cost_cannot_contain_nonzero_observed_amount():
    rows = [episode(slippage="1", slippage_coverage="VERIFIED_ZERO")]
    with pytest.raises(ValueError, match="verified zero"):
        calculate(rows, [context()])


def test_gross_profit_and_loss_share_v1_confirmed_cost_derivation():
    rows = [episode(gross_pnl=None)]
    metrics = calculate(rows, [context()])["groups"][0]["metrics"]
    assert metrics["gross_pnl"] == "10"
    assert metrics["gross_profit"] == "10" and metrics["gross_loss"] == "0"


def test_scale_in_context_does_not_replace_episode_first_entry_or_count_twice():
    rows = [episode(root_trade_id="root-entry", trade_ids=["root-entry", "scale-entry"], initial_risk="20")]
    contexts = [context(trade_id="root-entry"),
                context(trade_id="scale-entry", session="LONDON", trend_regime="BEAR")]
    group = calculate(rows, contexts, completeness_report=report(rows))["groups"][0]
    assert group["metrics"]["trade_count"] == 1
    assert group["group"]["session"] == "NEW_YORK"
    assert group["group"]["trend_regime"] == "BULL"
    assert group["metrics"]["expectancy_net_r"] == "0.45"


def test_scale_in_context_cannot_substitute_for_unknown_original_entry():
    rows = [episode(root_trade_id="root-entry", trade_ids=["root-entry", "scale-entry"])]
    result = calculate(rows, [context(trade_id="scale-entry")], completeness_report=report(rows))
    assert result["missing_context_count"] == 1
    assert result["groups"][0]["group"]["session"] is None
    assert result["groups"][0]["profitability_verified"] is False


def test_unknown_regime_cannot_become_a_verified_context_from_a_valid_quality_label():
    rows = [episode()]
    group = calculate(rows, [context(trend_regime="UNKNOWN")],
        completeness_report=report(rows))["groups"][0]
    assert group["context_quality"]["status"] == "UNKNOWN"
    assert group["profitability_verified"] is False


def test_unknown_grouping_dimension_does_not_create_verified_context_profitability():
    rows = [episode(direction=None)]
    result = calculate_context_performance(rows, [context()], cohort=COHORT,
        group_by=("direction",), source_watermark="source-one",
        calculation_timestamp=NOW, completeness_report=report(rows))
    assert result["groups"][0]["profitability_verified"] is False
    assert "UNKNOWN_GROUPING_DIMENSION" in result["groups"][0]["profitability"]["blockers"]
