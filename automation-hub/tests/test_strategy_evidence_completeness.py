"""Evidence delivery, attribution and financial completeness are distinct."""
from copy import deepcopy
import hashlib

import pytest

from services.strategy_evidence_completeness import assess_evidence_completeness
from services.strategy_intelligence_metrics import EvidenceCohort, calculate_metrics


COHORT = EvidenceCohort(
    strategy_id="adaptive_trend_pullback", strategy_version="1.0.0", config_fingerprint=hashlib.sha256(b"{}").hexdigest(),
    instance_id="one", lab_id=None, simulation_session_id="session", execution_mode="paper",
    source_kind="forward_paper", owner_id="owner", account_id="account", symbol="XRPUSDT")


def complete_fixture():
    scope = COHORT.as_dict()
    scope["strategy_config_hash"] = scope.pop("config_fingerprint")
    context = {"journal_execution": scope}
    receipt = {"entry": "100", "initial_risk_amount": "10", "net_pnl": "9", "gross_pnl": "10",
               "booked_fees": "1", "funding": "0", "funding_coverage": "MODELED"}
    outbox = [dict(execution_id="open", action="OPEN", trade_id="trade", position_id="position",
                   instance_id="one", simulation_session_id="session", context=context,
                   receipt={"entry": "100", "initial_risk_amount": "10"}, observed_at="2026-09-01T00:00:00Z"),
              dict(execution_id="close", action="CLOSE", trade_id="trade", position_id="position",
                   instance_id="one", simulation_session_id="session", context=context,
                   receipt=receipt, observed_at="2026-09-01T01:00:00Z")]
    snapshot = {"source_complete": True, "outbox_supported": True, "trades": [
        dict(id="trade", instance_id="one", simulation_session_id="session", strategy_id="adaptive_trend_pullback",
             symbol="XRPUSDT", status="closed", pnl="9", fees="1")], "positions": [
        dict(id="position", instance_id="one", simulation_session_id="session", status="closed")],
        "executions": [{key: row.get(key) for key in
                        ("execution_id", "action", "trade_id", "position_id", "instance_id", "simulation_session_id")}
                       for row in outbox], "outbox": outbox}
    events = [dict(**scope, event_id="fill:" + row["execution_id"], trade_id="trade", position_id="position",
                   episode_id="episode", observed_at=row["observed_at"], kind="execution_fill", payload={
                       "execution_id": row["execution_id"], "action": row["action"].lower().replace("open", "opened").replace("close", "closed"),
                       "initial_risk": "10" if row["action"] == "OPEN" else None,
                       "net_pnl": "9" if row["action"] == "CLOSE" else None,
                       "gross_pnl": "10" if row["action"] == "CLOSE" else None,
                       "fees": "1" if row["action"] == "CLOSE" else None,
                       "funding": "0" if row["action"] == "CLOSE" else None,
                       "funding_coverage": "MODELED", "fees_coverage": "BOOKED"}) for row in outbox]
    episode = dict(**scope, episode_id="episode", status="closed", trade_ids=["trade"],
                   evidence_kind="executed", net_pnl="9", gross_pnl="10", fees="1", funding="0",
                   initial_risk="10", fees_coverage="BOOKED", funding_coverage="MODELED",
                   opened_at="2026-09-01T00:00:00Z", closed_at="2026-09-01T01:00:00Z")
    journal = dict(**scope, trade_id="trade", status="closed", pnl="9", episode_id="episode",
                   timeline=[{"kind": "trade-closed", "event_id": "trade:closed"}])
    configuration = {"strategy_id": COHORT.strategy_id, "strategy_version": COHORT.strategy_version,
                     "config_fingerprint": COHORT.config_fingerprint, "configuration": {}}
    store = {"source_complete": True, "events": events, "episodes": [episode], "journals": [journal],
             "configurations": [configuration]}
    return snapshot, store


def assess(snapshot, store, **kwargs):
    return assess_evidence_completeness(snapshot, store, cohort=COHORT,
                                       calculated_at="2026-10-09T12:00:00Z", **kwargs)


def test_complete_requires_authoritative_receipts_journals_scope_costs_and_risk():
    snapshot, store = complete_fixture()
    report = assess(snapshot, store)
    assert report["status"] == "COMPLETE"
    assert report["history_complete"] is True
    assert report["counts"]["expected_executions"] == 2
    assert report["financial"]["net_pnl_delta"] == "0"
    assert report["calculated_at"] == "2026-10-09T12:00:00Z"


def test_primary_commit_before_capture_is_partial_and_recovering_is_explicit():
    snapshot, store = complete_fixture()
    store["events"].pop()
    assert assess(snapshot, store)["status"] == "PARTIAL"
    assert assess(snapshot, store)["counts"]["missing_fill_events"] == 1
    assert assess(snapshot, store, recovering=True)["status"] == "RECOVERING"


@pytest.mark.parametrize("part", ["source", "journal"])
def test_incomplete_or_bounded_sources_are_unknown(part):
    snapshot, store = complete_fixture()
    (snapshot if part == "source" else store)["source_complete"] = False
    report = assess(snapshot, store)
    assert report["status"] == "UNKNOWN"
    assert report["history_complete"] is False


def test_duplicate_execution_deliveries_are_conflicted():
    snapshot, store = complete_fixture()
    duplicate = deepcopy(store["events"][-1])
    duplicate["event_id"] = "different-key-same-execution"
    store["events"].append(duplicate)
    report = assess(snapshot, store)
    assert report["status"] == "CONFLICTED"
    assert report["counts"]["duplicate_fill_events"] == 1


@pytest.mark.parametrize("field,value", [("net_pnl", "99"), ("fees", "2"), ("action", "opened")])
def test_receipt_economics_and_action_conflicts_are_detected(field, value):
    snapshot, store = complete_fixture()
    store["events"][-1]["payload"][field] = value
    assert assess(snapshot, store)["status"] == "CONFLICTED"


def test_wrong_account_or_configuration_is_not_silently_excluded():
    snapshot, store = complete_fixture()
    store["events"][-1]["account_id"] = "other"
    report = assess(snapshot, store)
    assert report["status"] == "CONFLICTED"
    assert report["counts"]["conflicting_fill_events"] == 1


@pytest.mark.parametrize("part", ["journal", "close_timeline", "episode"])
def test_missing_journal_and_episode_deliveries_are_partial(part):
    snapshot, store = complete_fixture()
    if part == "journal": store["journals"] = []
    elif part == "close_timeline": store["journals"][0]["timeline"] = []
    else: store["episodes"] = []
    assert assess(snapshot, store)["status"] == "PARTIAL"


def test_unreceipted_legacy_closed_trade_is_unknown_not_zero_history():
    snapshot, store = complete_fixture()
    snapshot["trades"].append(dict(snapshot["trades"][0], id="legacy-trade"))
    report = assess(snapshot, store)
    assert report["status"] == "UNKNOWN"
    assert report["counts"]["unreceipted_legacy_trades"] == 1


@pytest.mark.parametrize("field,value", [("strategy_config_hash", None), ("execution_mode", None),
                                       ("source_kind", None), ("initial_risk", None)])
def test_unknown_identity_mode_or_risk_never_claims_complete(field, value):
    snapshot, store = complete_fixture()
    store["episodes"][0][field] = value
    report = assess(snapshot, store)
    assert report["status"] != "COMPLETE"


def test_unmodeled_funding_is_missing_cost_coverage_even_when_amount_is_zero():
    snapshot, store = complete_fixture()
    store["episodes"][0]["funding_coverage"] = "UNMODELED"
    assert assess(snapshot, store)["status"] == "PARTIAL"
    assert assess(snapshot, store)["counts"]["missing_cost_coverage"] == 1


def test_primary_and_journal_updates_invalidate_combined_watermark():
    snapshot, store = complete_fixture()
    first = assess(snapshot, store)
    snapshot["outbox"][0]["receipt"]["entry"] = "101"
    second = assess(snapshot, store)
    assert first["source_watermark"] != second["source_watermark"]
    store["events"][0]["payload"]["entry"] = "101"
    assert second["source_watermark"] != assess(snapshot, store)["source_watermark"]


def test_last_successful_reconciliation_is_explicit_and_timestamped():
    snapshot, store = complete_fixture()
    report = assess(snapshot, store, last_successful_reconciliation="2026-10-09T11:00:00Z")
    assert report["last_successful_reconciliation"] == "2026-10-09T11:00:00Z"


def test_metrics_unknown_by_default_and_verified_only_against_matching_complete_report():
    snapshot, store = complete_fixture()
    plain = calculate_metrics(store["episodes"], cohort=COHORT, history_complete=True)
    assert plain["evidence_completeness_status"] == "UNKNOWN"
    assert plain["profitability_verified"] is False
    report = assess(snapshot, store)
    result = calculate_metrics(store["episodes"], cohort=COHORT, source_watermark=report["source_watermark"],
                               completeness_report=report, calculation_timestamp=report["calculated_at"])
    assert result["profitability_verified"] is True
    assert result["net_pnl"] == plain["net_pnl"] == "9"
    assert result["history_complete"] is True


@pytest.mark.parametrize("change", ["episode", "source_watermark", "cohort", "status"])
def test_metrics_fail_closed_on_stale_or_mismatched_completeness(change):
    snapshot, store = complete_fixture()
    report = assess(snapshot, store)
    watermark = report["source_watermark"]
    if change == "episode": store["episodes"][0]["initial_risk"] = "20"
    elif change == "source_watermark": watermark = "stale"
    elif change == "cohort": report["cohort"]["strategy_version"] = "2.0.0"
    else: report["status"] = "PARTIAL"
    result = calculate_metrics(store["episodes"], cohort=COHORT, source_watermark=watermark, completeness_report=report)
    assert result["profitability_verified"] is False


def test_real_journal_snapshot_events_alias_is_recognized():
    snapshot, store = complete_fixture()
    store["journals"][0]["events"] = store["journals"][0].pop("timeline")
    assert assess(snapshot, store)["status"] == "COMPLETE"


@pytest.mark.parametrize("change", ["missing", "different_configuration", "wrong_source"])
def test_configuration_snapshot_must_exist_match_canonical_hash_and_source(change):
    snapshot, store = complete_fixture()
    if change == "missing": store["configurations"] = []
    elif change == "different_configuration": store["configurations"][0]["configuration"] = {"risk": 999}
    else:
        store["configurations"][0]["source_hash"] = "a" * 64
        snapshot["outbox"][0]["context"]["strategy_identity"] = {
            **store["configurations"][0], "source_hash": "b" * 64}
    assert assess(snapshot, store)["status"] != "COMPLETE"


def test_episode_scope_conflicting_with_its_fill_cannot_verify():
    snapshot, store = complete_fixture()
    store["episodes"][0]["account_id"] = "other"
    assert assess(snapshot, store)["status"] == "CONFLICTED"


def test_unrecognized_execution_mode_is_unknown_coverage():
    snapshot, store = complete_fixture()
    store["episodes"][0]["execution_mode"] = "banana"
    assert assess(snapshot, store)["status"] != "COMPLETE"
    assert assess(snapshot, store)["counts"]["unknown_execution_modes"] == 1


def test_primary_outbox_ids_must_match_committed_execution_receipt():
    snapshot, store = complete_fixture()
    snapshot["executions"][0]["trade_id"] = "wrong"
    assert assess(snapshot, store)["status"] == "CONFLICTED"


def test_duplicate_authoritative_outbox_rows_cannot_be_hidden_by_dictionary_index():
    snapshot, store = complete_fixture()
    snapshot["outbox"].append(deepcopy(snapshot["outbox"][0]))
    assert assess(snapshot, store)["status"] == "CONFLICTED"


def test_ledger_close_without_committed_close_receipt_is_unknown():
    snapshot, store = complete_fixture()
    snapshot["executions"].pop()
    snapshot["outbox"].pop()
    store["events"].pop()
    store["episodes"][0].update(status="open", closed_at=None)
    store["journals"][0].update(status="open", pnl=None, timeline=[])
    assert assess(snapshot, store)["status"] == "UNKNOWN"


def test_position_without_receipt_lineage_cannot_be_silently_ignored():
    snapshot, store = complete_fixture()
    snapshot["positions"].append({"id": "orphan", "instance_id": "one", "simulation_session_id": "session", "status": "open"})
    assert assess(snapshot, store)["status"] == "UNKNOWN"


@pytest.mark.parametrize("age_seconds", [-1, 301])
def test_metrics_cannot_verify_future_or_expired_completeness_reports(age_seconds):
    from datetime import datetime, timedelta
    snapshot, store = complete_fixture()
    report = assess(snapshot, store)
    calculation_timestamp = (datetime.fromisoformat(report["calculated_at"].replace("Z", "+00:00")) +
                             timedelta(seconds=age_seconds)).isoformat()
    result = calculate_metrics(store["episodes"], cohort=COHORT, completeness_report=report,
                               source_watermark=report["source_watermark"], calculation_timestamp=calculation_timestamp)
    assert result["profitability_verified"] is False
    assert result["evidence_completeness_status"] == "UNKNOWN"


def test_zero_completed_episodes_never_verify_profitability():
    snapshot, store = complete_fixture()
    snapshot.update(trades=[], positions=[], executions=[], outbox=[])
    store.update(events=[], episodes=[], journals=[])
    report = assess(snapshot, store)
    result = calculate_metrics([], cohort=COHORT, completeness_report=report,
                               source_watermark=report["source_watermark"], calculation_timestamp=report["calculated_at"])
    assert result["trade_count"] == 0
    assert result["profitability_verified"] is False


def test_exit_without_observer_context_inherits_only_proven_original_entry():
    snapshot, store = complete_fixture()
    snapshot["outbox"][-1]["context"] = None
    report = assess(snapshot, store)
    assert report["status"] == "COMPLETE"
    assert report["counts"]["missing_configuration_fingerprints"] == 0


def test_recovery_snapshot_masked_live_labels_is_verified_only_after_immutable_storage():
    snapshot, store = complete_fixture()
    for row in snapshot["outbox"]:
        row["context"]["recovery_strategy_identity"] = store["configurations"][0]
        row["context"]["journal_execution"]["strategy_config_hash"] = None
    assert assess(snapshot, store)["status"] == "COMPLETE"
    store["configurations"] = []
    assert assess(snapshot, store)["status"] != "COMPLETE"


def test_old_blank_close_execution_scope_is_resolved_by_exact_authoritative_ids():
    snapshot, store = complete_fixture()
    snapshot["executions"][-1]["instance_id"] = ""
    snapshot["executions"][-1].pop("simulation_session_id")
    report = assess(snapshot, store)
    assert report["status"] == "COMPLETE"
    assert report["counts"]["expected_authoritative_events"] == 2
    assert report["financial_totals"]["net_pnl_delta"] == "0"


def test_known_different_close_execution_scope_remains_conflicted():
    snapshot, store = complete_fixture()
    snapshot["executions"][-1]["instance_id"] = "other"
    assert assess(snapshot, store)["status"] == "CONFLICTED"


@pytest.mark.parametrize("change", ["missing_leg", "missing_entry_event", "wrong_position"])
def test_episode_leg_references_are_checked_when_consistent_snapshot_exposes_them(change):
    snapshot, store = complete_fixture()
    leg = {"leg_key": "leg", "episode_id": "episode", "trade_id": "trade", "position_id": "position",
           "entry_event_id": "fill:open", "parent_trade_id": None}
    store["legs"] = [leg]
    assert assess(snapshot, store)["status"] == "COMPLETE"
    if change == "missing_leg": store["legs"] = []
    elif change == "missing_entry_event": leg["entry_event_id"] = "absent"
    else: leg["position_id"] = "wrong"
    report = assess(snapshot, store)
    assert report["status"] == ("CONFLICTED" if change == "wrong_position" else "PARTIAL")


def test_unknown_authoritative_financial_amount_never_reports_zero_delta_as_reconciled():
    snapshot, store = complete_fixture()
    snapshot["trades"][0]["fees"] = None
    report = assess(snapshot, store)
    assert report["financial_totals"]["fees_delta"] is None
    assert report["financial_totals"]["authoritative_fees"] is None
    assert report["financial_totals"]["known_authoritative_fees"] == "0"
    assert report["status"] == "PARTIAL"
