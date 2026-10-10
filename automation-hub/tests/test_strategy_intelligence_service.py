"""Incremental metadata processing preserves accounting and honest unknowns."""
from services.strategy_intelligence_service import StrategyIntelligenceService
from test_strategy_evidence_runtime import entry, quote, runtime
import pytest


SCOPE = {"owner_id": "owner", "instance_id": "one",
         "simulation_session_id": "session", "account_id": "account-one"}


@pytest.mark.parametrize("available", [False, True])
def test_versioned_research_recomputation_preserves_original_entry_and_financial_records(tmp_path, available):
    from context_intelligence_fixtures import observed_market_context_input
    from services.market_context_classifier import classifier_definition
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    if available:
        payload["market_context_input"] = observed_market_context_input(observed_at=timestamp)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="research-close")
    service = StrategyIntelligenceService(store)
    service.refresh(ledger, scope=SCOPE)
    original = service.read("context", SCOPE, {})["contexts"][0]
    source_classifier = {key: original[key] for key in ("classifier_id", "classifier_version", "parameter_hash")}
    definition = classifier_definition(classifier_version="1.1.0")
    before = ledger.get_authoritative_evidence_snapshot()
    result = service.recompute_research(scope=SCOPE, definition=definition, source_classifier=source_classifier)
    assert result["processed_contexts"] == 1
    rows = service.read("context", SCOPE, {})["contexts"]
    assert [row for row in rows if row["classification_kind"] == "ENTRY"] == [original]
    revised = next(row for row in rows if row["classification_kind"] == "RESEARCH")
    assert revised["classifier_version"] == "1.1.0"
    assert revised["source_snapshot_id"] == original["snapshot_id"]
    assert revised["context_quality"] == ("VALID" if available else "UNKNOWN")
    assert service.recompute_research(scope=SCOPE, definition=definition,
        source_classifier=source_classifier)["processed_contexts"] == 0
    with pytest.raises(ValueError, match="different classifier version"):
        service.recompute_research(scope=SCOPE, definition=classifier_definition(), source_classifier=source_classifier)
    service.refresh(ledger, scope=SCOPE)
    cohort = service.read("cohorts", SCOPE, {})["cohorts"][0]
    groups = service.read("performance", SCOPE, {**cohort, "group_by": ["symbol"]})["groups"]
    assert {group["classifier"]["classifier_version"] for group in groups} == {original["classifier_version"]}
    assert ledger.get_authoritative_evidence_snapshot() == before


def test_missing_original_market_evidence_is_immutable_unknown_not_invented(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="context-close")
    before = ledger.get_authoritative_evidence_snapshot()
    service = StrategyIntelligenceService(store)
    first = service.refresh(ledger, scope=SCOPE)
    contexts = service.read("context", SCOPE, {})["contexts"]
    assert len(contexts) == 1
    assert contexts[0]["trade_id"] == opened.trade_id
    assert contexts[0]["context_quality"] == "UNKNOWN"
    assert contexts[0]["trend_regime"] == "UNKNOWN"
    assert contexts[0]["volatility_regime"] == "UNKNOWN"
    assert contexts[0]["market_data_timestamp"] is None
    assert contexts[0]["reconstruction_status"] == "UNKNOWN"
    assert first["processed_contexts"] == 1
    assert service.refresh(ledger, scope=SCOPE)["processed_contexts"] == 0
    assert service.read("context", SCOPE, {})["contexts"] == contexts
    assert ledger.get_authoritative_evidence_snapshot() == before


def test_context_performance_counts_episode_once_and_blocks_unknown_costs(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5, execution_id="context-partial")
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="context-final")
    service = StrategyIntelligenceService(store)
    service.refresh(ledger, scope=SCOPE)
    cohorts = service.read("cohorts", SCOPE, {})["cohorts"]
    assert len(cohorts) == 1
    result = service.read("performance", SCOPE, {**cohorts[0], "group_by": ["symbol"]})
    assert result["contract_version"] == "strategy_intelligence.v2"
    assert len(result["groups"]) == 1
    group = result["groups"][0]
    assert group["metrics"]["trade_count"] == 1
    assert group["sample_confidence"]["classification"] == "INSUFFICIENT"
    assert group["profitability_verified"] is False
    assert group["cost_coverage"]["funding"] == "UNKNOWN"


def test_restart_reads_persisted_cache_without_recomputing_or_financial_access(tmp_path, monkeypatch):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="context-restart-close")
    service = StrategyIntelligenceService(store)
    service.refresh(ledger, scope=SCOPE)
    cohorts = service.read("cohorts", SCOPE, {})["cohorts"]
    monkeypatch.setattr(store, "get_evidence_completeness_snapshot",
        lambda **kw: (_ for _ in ()).throw(AssertionError("request must not rebuild all evidence")))
    resumed = StrategyIntelligenceService(store)
    result = resumed.read("performance", SCOPE, {**cohorts[0], "group_by": ["symbol"]})
    assert result["groups"][0]["metrics"]["trade_count"] == 1
    assert result["profitability_verified"] is False
    assert result["cache_status"] == "RECONCILIATION_REQUIRED"


def test_entry_context_from_another_account_is_not_attached_to_owned_episode(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    material = store.get_evidence_completeness_snapshot()
    for event in material["events"]:
        if event["kind"] == "TRADE_ENTRY_CONTEXT":
            event.update(owner_id="foreign", account_id="foreign-account")
            event["payload"]["timeframe"] = "foreign-timeframe"
    service = StrategyIntelligenceService(store)
    service._classify_missing(material, SCOPE, timestamp.isoformat())
    rows = service.read("context", SCOPE, {})["contexts"]
    assert len(rows) == 1
    assert rows[0]["entry_timeframe"] is None
    assert rows[0]["context_quality"] == "UNKNOWN"


def test_symbol_filter_reads_a_precomputed_exact_asset_cohort(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="asset-close")
    service = StrategyIntelligenceService(store)
    service.refresh(ledger, scope=SCOPE)
    cohort = service.read("cohorts", SCOPE, {})["cohorts"][0]
    result = service.read("performance", SCOPE, {**cohort, "symbol": "XRPUSDT", "group_by": ["symbol"]})
    assert result["cohort"]["symbol"] == "XRPUSDT"
    assert result["groups"][0]["metrics"]["trade_count"] == 1
    service.invalidate_scope(SCOPE)
    result = service.read("performance", SCOPE, {**cohort, "symbol": "XRPUSDT", "group_by": ["symbol"]})
    assert result["cache_status"] == "RECONCILIATION_REQUIRED"
    assert result["profitability_verified"] is False


def test_restart_invalidates_every_nested_verification_alias(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="alias-close")
    service = StrategyIntelligenceService(store)
    service.refresh(ledger, scope=SCOPE)
    cohort = service.read("cohorts", SCOPE, {})["cohorts"][0]
    cached = service.read("performance", SCOPE, {**cohort, "group_by": ["symbol"]})
    # Simulate a previously verified calculation becoming invalid on restart.
    cached["run_id"] = "previously-verified-alias-fixture"
    cached["profitability_verified"] = True
    group = cached["groups"][0]
    group["profitability_verified"] = True
    group["profitability"] = {"status": "VERIFIED", "verified": True}
    group["evidence_quality"] = {"status": "COMPLETE", "binding_status": "MATCHED_SUBGROUP"}
    group["metrics"].update(profitability_verified=True, ready_for_evidence_review=True,
        history_complete=True, evidence_completeness_status="COMPLETE")
    store.context.record_intelligence_run(cached)
    restarted = StrategyIntelligenceService(store)
    result = restarted.read("performance", SCOPE, {**cohort, "group_by": ["symbol"]})
    group = result["groups"][0]
    assert group["evidence_quality"]["status"] == "UNKNOWN"
    assert group["evidence_quality"]["binding_status"] == "RECONCILIATION_REQUIRED"
    assert group["metrics"]["profitability_verified"] is False
    assert group["metrics"]["ready_for_evidence_review"] is False
    assert group["metrics"]["history_complete"] is False
    assert group["metrics"]["evidence_completeness_status"] == "UNKNOWN"


def test_authority_change_during_aggregation_cannot_publish_ready_report(tmp_path, monkeypatch):
    import services.strategy_intelligence_service as module
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    actual_assessment = module.assess_evidence_completeness
    changed = []
    def concurrent_close(*args, **kwargs):
        report = actual_assessment(*args, **kwargs)
        if not changed:
            changed.append(True)
            # Primary close commits during an evidence observer outage.
            paper.evidence_listener = None
            paper.close(symbol="XRPUSDT", exit_price=110, execution_id="concurrent-context-close")
        return report
    monkeypatch.setattr(module, "assess_evidence_completeness", concurrent_close)
    service = StrategyIntelligenceService(store)
    result = service.refresh(ledger, scope=SCOPE)
    assert result["status"] == "RECOVERING"
    cohort = service.read("cohorts", SCOPE, {})["cohorts"][0]
    report = service.read("performance", SCOPE, {**cohort, "group_by": ["symbol"]})
    assert report["cache_status"] == "RECONCILIATION_REQUIRED"
    assert report["profitability_verified"] is False
    assert len(ledger.get_paper_trades()) == 1


def test_original_observer_context_recovers_after_deferred_fill_outage_and_restart(tmp_path):
    from copy import deepcopy
    from datetime import datetime, timedelta
    from decimal import Decimal

    from context_intelligence_fixtures import observed_market_context_input
    from services.strategy_identity import observed_strategy_identity
    from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy

    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, _ = entry(decisions)
    frozen = observed_market_context_input()
    payload.update(market_context_input=deepcopy(frozen),
        timestamp=frozen["signal_candle_timestamp"], market_data_source=frozen["market_data_source"])
    identity = observed_strategy_identity(AdaptiveTrendPullbackStrategy("XRPUSDT"),
        strategy_id="adaptive_trend_pullback", timeframe="5m")
    payload.update(strategy_identity=identity, recovery_strategy_identity=deepcopy(identity))
    # Losing the entire separate evidence connection cannot gate the accepted
    # deferred order or change its authoritative accounting commits.
    store._c.close()
    assert pipeline.process(payload).fill["action"] == "intent"
    pending = paper.pending_intents()
    original = deepcopy(frozen)
    payload["market_context_input"]["entry_candles"][-1]["close"] = 999
    restarted_ledger, restarted_store, restarted_decisions, restarted_paper, _, restarted_capture = runtime(
        tmp_path, recovered=pending)
    # Simulate the restarted worker still facing the evidence outage when the
    # first eligible post-decision quote arrives.
    restarted_store._c.close()
    fill_clock = datetime.fromisoformat(frozen["signal_observed_at"]) + timedelta(seconds=1)
    opened = restarted_paper.process_quote(quote(fill_clock))[0]
    partial = restarted_paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5,
                                    execution_id="observed-context-partial")
    final = restarted_paper.close(symbol="XRPUSDT", exit_price=110, execution_id="observed-context-final")
    before = restarted_ledger.get_authoritative_evidence_snapshot()
    assert restarted_ledger.get_evidence_outbox_event(opened.execution_id)["context"]["market_context_input"] == original
    assert len(restarted_decisions.list()) == 1

    _, recovered_store, recovered_decisions, _, _, recovered_capture = runtime(tmp_path)
    report = recovered_capture.reconcile_report(restarted_ledger)
    assert report["financial_totals"]["net_pnl_delta"] == "0"
    assert report["financial_totals"]["fees_delta"] == "0"
    assert report["counts"]["missing_events"] == 0
    assert report["status"] == "PARTIAL"  # actual funding coverage remains unknown
    assert len(recovered_decisions.list()) == 1
    entries = recovered_store.evidence_events(kind="TRADE_ENTRY_CONTEXT")
    assert next(row for row in entries if row["trade_id"] == opened.trade_id)["payload"]["market_context_input"] == original

    service = StrategyIntelligenceService(recovered_store)
    assert service.refresh(restarted_ledger, scope=SCOPE)["processed_contexts"] == 1
    contexts = service.read("context", SCOPE, {})["contexts"]
    assert len(contexts) == 1
    assert contexts[0]["context_quality"] == "VALID"
    assert contexts[0]["trend_regime"] != "UNKNOWN"
    assert contexts[0]["volatility_regime"] != "UNKNOWN"
    assert contexts[0]["signal_timestamp"] == original["signal_timestamp"]
    assert contexts[0]["entry_timestamp"] == opened.executed_at
    assert contexts[0]["raw_market_evidence"] == original
    cohorts = service.read("cohorts", SCOPE, {})["cohorts"]
    result = service.read("performance", SCOPE, {**cohorts[0], "group_by": ["symbol"]})
    group = result["groups"][0]
    assert group["metrics"]["trade_count"] == 1
    assert Decimal(group["metrics"]["net_pnl"]) == Decimal(str(partial.pnl)) + Decimal(str(final.pnl))
    assert group["profitability_verified"] is False
    assert group["cost_coverage"]["funding"] == "UNKNOWN"
    immutable_contexts = recovered_store.context.context_snapshot(filters=SCOPE)["contexts"]
    recovered_capture.reconcile_report(restarted_ledger)
    resumed = StrategyIntelligenceService(recovered_store)
    assert resumed.refresh(restarted_ledger, scope=SCOPE)["processed_contexts"] == 0
    assert recovered_store.context.context_snapshot(filters=SCOPE)["contexts"] == immutable_contexts
    assert resumed.read("context", SCOPE, {})["contexts"] == contexts
    assert restarted_ledger.get_authoritative_evidence_snapshot() == before
