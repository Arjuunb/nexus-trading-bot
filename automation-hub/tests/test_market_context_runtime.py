"""Original market evidence is telemetry, never another execution gate."""
from copy import deepcopy
from types import SimpleNamespace

from test_strategy_identity_runtime import auto
from test_strategy_evidence_runtime import quote
from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy


def test_original_signal_market_input_survives_total_journal_outage(tmp_path):
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    original = {"signal_timestamp": signal.timestamp.isoformat(), "entry_candles": [],
                "higher_candles": [], "market_data_source": "explicit-test-observation",
                "entry_timeframe": "5m", "higher_timeframe": "1h"}
    engine._market_context_observer = SimpleNamespace(freeze=lambda *args, **kwargs: deepcopy(original))
    store._c.close()
    engine._on_signal("XRPUSDT", signal, AdaptiveTrendPullbackStrategy("XRPUSDT"))
    assert signal.market_context_input == original
    opened = paper.process_quote(quote(signal.timestamp))[0]
    outbox = ledger.get_evidence_outbox_event(opened.execution_id)
    assert outbox["context"]["market_context_input"] == original
    signal.market_context_input["market_data_source"] = "changed-after-signal"
    assert ledger.get_evidence_outbox_event(opened.execution_id)["context"]["market_context_input"] == original


def test_market_observation_failure_cannot_change_signal_fill_or_accounting(tmp_path):
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    def unavailable(*args, **kwargs):
        raise ConnectionError("context observer unavailable")
    engine._market_context_observer = SimpleNamespace(freeze=unavailable)
    engine._on_signal("XRPUSDT", signal, AdaptiveTrendPullbackStrategy("XRPUSDT"))
    fill = paper.process_quote(quote(signal.timestamp))[0]
    assert fill.action == "opened"
    assert len(ledger.get_positions("open")) == 1
    assert len(ledger.get_paper_trades()) == 1
    assert fill.price == 100.2


def test_cache_invalidation_failure_cannot_interrupt_committed_fill_capture(tmp_path):
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    calls = []
    def unavailable():
        calls.append(True)
        raise ConnectionError("cache invalidation unavailable")
    pipeline.evidence.evidence_changed_listener = unavailable
    engine._on_signal("XRPUSDT", signal, AdaptiveTrendPullbackStrategy("XRPUSDT"))
    fill = paper.process_quote(quote(signal.timestamp))[0]
    assert fill.action == "opened"
    assert calls == [True]
    assert store.episode_for_trade(fill.trade_id) is not None
    assert len(store.get(fill.trade_id)["events"]) > 0


def test_existing_recovery_loop_isolates_intelligence_processing_failure(tmp_path):
    from services.strategy_evidence_recovery import EvidenceRecoveryLoop
    import threading
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    def unavailable(*args, **kwargs):
        raise ConnectionError("intelligence calculation unavailable")
    loop = EvidenceRecoveryLoop(pipeline.evidence, ledger, stop_event=threading.Event(),
        worker_alive=lambda: True, after_reconcile=unavailable)
    report = loop.run_once()
    assert report == loop.last_report
    assert loop.last_intelligence_result["status"] == "ERROR"
    assert ledger.get_paper_trades() == []


def test_recovery_callback_keeps_original_worker_account_after_session_changes():
    from services.trading_instances import TradingInstanceManager
    calls = []
    manager = object.__new__(TradingInstanceManager)
    manager.intelligence_service = SimpleNamespace(
        refresh=lambda ledger, **kwargs: calls.append((ledger, kwargs)))
    instance = SimpleNamespace(owner_id="owner", id="one", simulation_session_id="original")
    callback = manager._intelligence_callback(instance)
    instance.simulation_session_id = "new"
    callback("authority", {"status": "PARTIAL"})
    assert calls == [("authority", {"scope": {
        "owner_id": "owner", "instance_id": "one", "simulation_session_id": "original",
        "account_id": "instance:one:original"}, "reconciliation_report": {"status": "PARTIAL"}})]


def test_enrichment_preserves_explicit_cost_models_and_does_not_invent_missing_models():
    from services.strategy_intelligence_service import StrategyIntelligenceService
    material = {"episodes": [{"episode_id": "e", "root_trade_id": "t", "trade_ids": ["t"],
        "closed_at": "2026-01-01T01:00:00+00:00", "funding_coverage": "MODELED"}],
        "journals": [], "events": [
            {"kind": "execution_fill", "episode_id": "e", "payload": {"action": "opened", "side": "long", "receipt": {}}},
            {"kind": "execution_fill", "episode_id": "e", "payload": {"action": "closed", "receipt": {
                "fees_cost_model": "captured-fees-v1", "funding_cost_model": "captured-funding-v2",
                "funding_coverage": "MODELED"}}}]}
    row = StrategyIntelligenceService._enrich_episodes(material)[0]
    assert row["fees_cost_model"] == "captured-fees-v1"
    assert row["funding_cost_model"] == "captured-funding-v2"
    assert row["slippage_cost_model"] == "UNKNOWN"
    assert row["slippage"] is None
    material["events"][-1]["payload"]["receipt"] = {"funding_coverage": "not_modeled"}
    row = StrategyIntelligenceService._enrich_episodes(material)[0]
    assert row["fees_cost_model"] == "UNKNOWN"
    assert row["funding_cost_model"] == "UNMODELED"
