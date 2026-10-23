"""Versioned, observational performance calculations over completed episodes."""
from dataclasses import replace
from decimal import Decimal

import pytest

from services.strategy_intelligence_metrics import EvidenceCohort, calculate_metrics


COHORT = EvidenceCohort(
    strategy_id="adaptive_trend_pullback", strategy_version="1.0.0",
    config_fingerprint="a" * 64, instance_id="xrp-paper", lab_id=None,
    simulation_session_id=None, execution_mode="forward_paper",
    source_kind="instance", owner_id="owner", account_id="account", symbol="XRPUSDT",
)


def episode(episode_id="one", **values):
    return {
        **COHORT.as_dict(), "episode_id": episode_id, "evidence_kind": "executed",
        "status": "closed", "opened_at": "2026-09-01T00:00:00+00:00",
        "closed_at": "2026-09-01T01:00:00+00:00", "initial_risk": "10",
        "net_pnl": "9", "gross_pnl": "10", "fees": "1", "funding": "0",
        "fees_coverage": "BOOKED", "funding_coverage": "MODELED", **values,
    }


def calculate(rows, **kwargs):
    return calculate_metrics(rows, cohort=COHORT, history_complete=True,
                             calculation_timestamp="2026-10-09T12:00:00Z", **kwargs)


def test_financial_precision_costs_and_net_r_are_not_double_subtracted():
    rows = [episode("a", gross_pnl="0.30", fees="0.10", net_pnl="0.20"),
            episode("b", gross_pnl="0.20", fees="0.10", net_pnl="0.10")]
    result = calculate(rows)
    assert result["net_pnl"] == "0.3"
    assert result["gross_pnl"] == "0.5"
    assert result["fees"] == "0.2"
    assert result["expectancy_net_r"] == "0.015"
    assert result["financial_precision"] == "decimal_from_source_text"


@pytest.mark.parametrize("dimension,value", [
    ("strategy_id", "smc"), ("strategy_version", "2.0.0"),
    ("config_fingerprint", "b" * 64), ("instance_id", "other"),
    ("lab_id", "pa"), ("simulation_session_id", "replay-2"),
    ("execution_mode", "replay"), ("source_kind", "backtest"),
    ("owner_id", "other"), ("account_id", "other"), ("symbol", "BTCUSDT"),
])
def test_every_cohort_dimension_is_exactly_isolated(dimension, value):
    result = calculate([episode(), episode("other", **{dimension: value})])
    assert result["trade_count"] == 1
    assert result["net_pnl"] == "9"


@pytest.mark.parametrize("kind", ["rejected", "counterfactual", "expired", "cancelled", "shadow"])
def test_unexecuted_and_hypothetical_outcomes_never_contribute(kind):
    result = calculate([episode(), episode("not-a-trade", evidence_kind=kind, net_pnl="999")])
    assert result["trade_count"] == 1
    assert result["excluded"]["nonexecuted"] == 1


def test_partial_exit_is_not_a_completed_trade_and_episode_is_counted_once():
    row = episode(realised_leg_count=3, trade_ids=["leg1", "leg2", "leg3"])
    result = calculate([row, dict(row), episode("open", status="open", net_pnl="999")])
    assert result["trade_count"] == 1
    assert result["net_pnl"] == "9"
    assert result["duplicate_episode_rows"] == 1


def test_conflicting_same_episode_rows_fail_instead_of_picking_a_winner():
    with pytest.raises(ValueError, match="conflicting episode"):
        calculate([episode(), episode(net_pnl="999")])


def test_missing_or_zero_entry_risk_is_unknown_and_excluded_from_r():
    rows = [episode("a"), episode("b", initial_risk=None), episode("c", initial_risk="0")]
    result = calculate(rows)
    assert result["trade_count"] == 3
    assert result["r_sample_size"] == 1
    assert result["expectancy_net_r"] == "0.9"
    assert result["coverage"]["unknown_or_nonpositive_risk"] == 2


def test_unknown_funding_and_missing_net_are_visible_not_coerced_to_zero():
    result = calculate([episode("a", funding=None, funding_coverage="UNMODELED"),
                        episode("b", net_pnl=None, gross_pnl=None)])
    assert result["net_pnl"] is None
    assert result["known_net_pnl"] == "9"
    assert result["funding"] is None
    assert result["win_rate_pct"] is None
    assert result["coverage"]["known_net_episodes"] == 1
    assert result["coverage"]["funding_complete"] is False
    assert result["financial_evidence_complete"] is False


def test_gross_can_only_be_derived_from_confirmed_costs():
    result = calculate([episode(gross_pnl=None)])
    assert result["gross_pnl"] == "10"
    unknown = calculate([episode(gross_pnl=None, funding=None, funding_coverage="UNKNOWN")])
    assert unknown["gross_pnl"] is None


def test_max_drawdown_uses_actual_close_order_with_deterministic_ties():
    rows = [episode("late-win", net_pnl="5", gross_pnl="6", closed_at="2026-09-03T00:00:00Z"),
            episode("early-win", net_pnl="10", gross_pnl="11", closed_at="2026-09-01T00:00:00Z"),
            episode("loss", net_pnl="-8", gross_pnl="-7", closed_at="2026-09-02T00:00:00Z")]
    result = calculate(rows)
    assert result["max_drawdown_net_pnl"] == "8"
    assert result["net_profit_factor"] == "1.875"
    assert abs(Decimal(result["gross_profit_factor"]) - Decimal(17) / Decimal(7)) < Decimal("1e-27")
    assert result == calculate(list(reversed(rows)))


@pytest.mark.parametrize("nets,expected", [([], "NO_TRADES"), (["1", "2"], "NO_LOSSES"),
                                        (["-1", "-2"], "FINITE"), (["0"], "ALL_BREAKEVEN")])
def test_profit_factor_edge_cases_are_labeled_without_invented_99(nets, expected):
    rows = [episode(str(i), net_pnl=v, gross_pnl=str(Decimal(v) + 1)) for i, v in enumerate(nets)]
    result = calculate(rows)
    assert result["net_profit_factor_state"] == expected
    assert result["net_profit_factor"] != "99"


def test_legacy_missing_configuration_stays_unknown():
    cohort = replace(COHORT, strategy_version=None, config_fingerprint=None)
    row = episode(strategy_version=None, config_fingerprint=None)
    result = calculate_metrics([row], cohort=cohort, history_complete=True)
    assert result["trade_count"] == 1
    assert result["cohort"]["config_fingerprint"] is None
    assert result["identity_verified"] is False


def test_truncated_history_and_missing_scope_do_not_claim_complete_evidence():
    row = episode("missing")
    del row["instance_id"]
    result = calculate_metrics([episode(), row], cohort=COHORT, history_complete=False)
    assert result["trade_count"] == 1
    assert result["excluded"]["missing_scope"] == 1
    assert result["history_complete"] is False
    assert result["ready_for_evidence_review"] is False


@pytest.mark.parametrize("value", [float("nan"), "Infinity", True, "bad"])
def test_invalid_financial_values_are_rejected(value):
    with pytest.raises(ValueError, match="finite decimal"):
        calculate([episode(net_pnl=value)])


def test_financial_source_inconsistency_is_not_silently_overwritten():
    with pytest.raises(ValueError, match="financial reconciliation"):
        calculate([episode(net_pnl="100")])


def test_naive_or_invalid_closure_time_disables_chronological_metrics():
    result = calculate([episode(closed_at="2026-09-01T01:00:00")])
    assert result["max_drawdown_net_pnl"] is None
    assert result["coverage"]["valid_close_time_episodes"] == 0


def test_actual_paper_mode_needs_explicit_forward_source_to_be_classified():
    cohort = replace(COHORT, execution_mode="paper", source_kind="forward_paper")
    row = episode(execution_mode="paper", source_kind="forward_paper")
    result = calculate_metrics([row], cohort=cohort, history_complete=True)
    assert result["evidence_partition"] == "EXECUTED_FORWARD_PAPER"
    assert result["ready_for_evidence_review"] is False
    assert result["evidence_completeness_status"] == "UNKNOWN"
    assert result["profitability_verified"] is False
    unknown = replace(cohort, source_kind="legacy")
    row["source_kind"] = "legacy"
    result = calculate_metrics([row], cohort=unknown, history_complete=True)
    assert result["evidence_partition"] == "UNVERIFIED_EXECUTION_MODE"
    assert result["ready_for_evidence_review"] is False


def test_signed_funding_income_is_not_charged_twice():
    result = calculate([episode(gross_pnl="10", fees="1", funding="-2", net_pnl="11")])
    assert result["gross_pnl"] == "10"
    assert result["net_pnl"] == "11"
    assert result["funding"] == "-2"


def test_scope_gaps_prevent_claiming_evidence_completeness_even_with_full_history_flag():
    missing = episode("unknown")
    del missing["account_id"]
    result = calculate([episode(), missing])
    assert result["history_complete"] is False
    assert result["history_complete_claimed"] is True
    assert result["cohort_evidence_complete"] is False
    assert result["ready_for_evidence_review"] is False


@pytest.mark.parametrize("source_kind", ["counterfactual", "shadow", "rejected", "hypothetical"])
def test_hypothetical_source_cannot_become_realised_even_if_row_is_labelled_executed(source_kind):
    cohort = replace(COHORT, source_kind=source_kind)
    rows = [episode(source_kind=source_kind, net_pnl="999")]
    result = calculate_metrics(rows, cohort=cohort, history_complete=True)
    assert result["trade_count"] == 0
    assert result["known_net_pnl"] == "0"
    assert result["ready_for_evidence_review"] is False


def test_declared_observed_version_mismatch_is_not_verified_identity():
    result = calculate([episode(identity_status="version_mismatch")])
    assert result["trade_count"] == 1
    assert result["identity_verified"] is False
    assert result["ready_for_evidence_review"] is False
