"""Committed primary outbox recovery across total journal outages."""
from dataclasses import replace
from decimal import Decimal
import pytest

from services.strategy_identity import observed_strategy_identity
from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy
from strategies.adaptive_trend_pullback.config import AdaptiveTrendPullbackConfig
from test_strategy_identity_runtime import auto
from test_strategy_evidence_runtime import entry, quote, runtime


def financial_rows(ledger):
    return ledger.get_paper_trades(), ledger.get_positions(), ledger.get_execution_receipts()


def test_entire_journal_outage_preserves_original_configuration_and_all_partial_parents(tmp_path):
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    strategy = AdaptiveTrendPullbackStrategy("XRPUSDT",
        config=replace(AdaptiveTrendPullbackConfig(), target_rr=3))
    original = observed_strategy_identity(strategy, strategy_id="adaptive_trend_pullback", timeframe="5m")
    store._c.close()
    engine._on_signal("XRPUSDT", signal, strategy)
    opened = paper.process_quote(quote(signal.timestamp))[0]
    first = paper.reduce(symbol="XRPUSDT", exit_price=103, fraction=.25, execution_id="partial-one")
    second = paper.reduce(symbol="XRPUSDT", exit_price=106, fraction=.5, execution_id="partial-two")
    final = paper.close(symbol="XRPUSDT", exit_price=110, execution_id="final-close")
    before = financial_rows(ledger)
    assert len(ledger.get_evidence_outbox()) == 4
    _, restored, restored_decisions, _, _, capture = runtime(tmp_path)
    report = capture.reconcile_report(ledger)
    episodes = restored.completed_evidence_episodes(instance_id="one")
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["strategy_config_hash"] == original["strategy_config_hash"]
    assert Decimal(episode["net_pnl"]) == sum(map(lambda fill: Decimal(str(fill.pnl)), (first, second, final)))
    assert report["counts"]["missing_events"] == 0
    assert report["counts"]["unresolved_episode_references"] == 0
    assert report["counts"]["missing_configuration_fingerprints"] == 0
    assert Decimal(report["financial_totals"]["net_pnl_delta"]) == 0
    assert financial_rows(ledger) == before
    assert len(restored_decisions.list()) == 1
    assert restored.get(opened.trade_id)["decision_id"] == str(restored_decisions.list()[0]["id"])
    events = restored.evidence_events()
    capture.reconcile_report(ledger)
    assert restored.evidence_events() == events
    assert financial_rows(ledger) == before


def test_missing_exit_preparation_recovers_from_exact_committed_outbox(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    def offline(*args, **kwargs):
        raise ConnectionError("evidence network unavailable")
    paper.evidence_prepare_listener = offline
    paper.evidence_listener = offline
    partial = paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5, execution_id="reduce-outage")
    final = paper.close(symbol="XRPUSDT", exit_price=110, execution_id="close-outage")
    before = financial_rows(ledger)
    assert store.evidence_events(kind="EXIT_INTENT") == []
    report = capture.reconcile_report(ledger)
    assert report["counts"]["missing_events"] == 0
    assert len(store.completed_evidence_episodes(instance_id="one")) == 1
    assert store.get(opened.trade_id)["status"] == "closed"
    assert store.get(partial.remainder_trade_id)["status"] == "closed"
    assert financial_rows(ledger) == before


def test_journal_outage_after_committed_fill_replays_once(tmp_path, monkeypatch):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    actual = capture.journal.record_entry
    monkeypatch.setattr(capture.journal, "record_entry",
                        lambda **kwargs: (_ for _ in ()).throw(ConnectionError("journal offline")))
    opened = paper.process_quote(quote(timestamp))[0]
    assert store.get(opened.trade_id) is None
    monkeypatch.setattr(capture.journal, "record_entry", actual)
    capture.reconcile_report(ledger)
    first_events = store.get(opened.trade_id)["events"]
    for _ in range(3):
        capture.reconcile_report(ledger)
        capture.observe_fill(opened)
    assert store.get(opened.trade_id)["events"] == first_events
    assert len(ledger.get_paper_trades()) == 1


def test_authoritative_read_failure_is_unknown_not_verified(tmp_path, monkeypatch):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    monkeypatch.setattr(ledger, "get_authoritative_evidence_snapshot",
        lambda: (_ for _ in ()).throw(ConnectionError("primary read unavailable")))
    report = capture.reconcile_report(ledger)
    assert report["status"] == "UNKNOWN"
    assert report["last_successful_reconciliation"] is None


def test_direct_execution_recovery_never_manufactures_signal_or_decision(tmp_path):
    from execution.paper_engine import PaperExecutionEngine
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    isolated = PaperExecutionEngine(ledger, 10000)
    opened = isolated.open(symbol="XRPUSDT", side="BUY", size=1, entry=100,
        stop=95, target=110, alert_id="direct-primary")
    isolated.close(symbol="XRPUSDT", exit_price=110, execution_id="direct-close")
    before = financial_rows(ledger)
    report = capture.reconcile_report(ledger)
    journal = store.get(opened.trade_id)
    assert journal is not None
    assert journal["signal_timestamp"] is None
    assert journal["decision_timestamp"] is None
    assert journal["decision_id"] is None
    assert not {"setup-detected", "quality-gate-passed", "risk-check-passed", "risk-sized"} & {
        event["kind"] for event in journal["events"]}
    assert decisions.list() == []
    assert report["status"] in {"UNKNOWN", "PARTIAL"}
    assert financial_rows(ledger) == before


def test_periodic_recovery_repairs_committed_journal_without_worker_restart(tmp_path):
    import threading
    from services.strategy_evidence_recovery import EvidenceRecoveryLoop
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.evidence_listener = lambda fill: (_ for _ in ()).throw(ConnectionError("observer offline"))
    opened = paper.process_quote(quote(timestamp))[0]
    assert store.get(opened.trade_id) is None
    before = financial_rows(ledger)
    stopped, repaired = threading.Event(), threading.Event()
    actual = capture.reconcile_report
    def notify(ledger):
        result = actual(ledger)
        repaired.set()
        return result
    capture.reconcile_report = notify
    loop = EvidenceRecoveryLoop(capture, ledger, stop_event=stopped,
        worker_alive=lambda: True, interval_seconds=.01)
    try:
        assert loop.start()
        assert not loop.start()
        assert repaired.wait(2)
        assert store.get(opened.trade_id) is not None
        assert financial_rows(ledger) == before
    finally:
        stopped.set()
        loop._thread.join(timeout=2)
    assert not loop._thread.is_alive()


def test_source_change_during_assessment_invalidates_completeness(tmp_path, monkeypatch):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    actual = ledger.get_authoritative_evidence_snapshot
    calls = []
    def changing_source():
        snapshot = actual()
        calls.append(snapshot)
        if len(calls) >= 3:
            snapshot["source_watermark"] = "different-authoritative-view"
        return snapshot
    monkeypatch.setattr(ledger, "get_authoritative_evidence_snapshot", changing_source)
    report = capture.reconcile_report(ledger)
    assert report["status"] == "UNKNOWN"
    assert report["history_complete"] is False
    assert any(error["error"] == "SourceChangedDuringReconciliation" for error in report["recovery_errors"])


def test_final_recovery_does_not_inherit_partial_exit_reason_or_excursions(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    paper.evidence_listener = lambda fill: (_ for _ in ()).throw(ConnectionError("observer offline"))
    capture.prepare_close_context(opened.trade_id, {"exit_reason": "take-profit", "mfe_r": 1.2, "mae_r": -.3})
    partial = paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5, execution_id="partial-context")
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="final-no-context")
    capture.reconcile_report(ledger)
    first = store.get(opened.trade_id)["sections"]["exit_decision"]
    final = store.get(partial.remainder_trade_id)["sections"]["exit_decision"]
    assert first["exit_reason"] == "take-profit"
    assert first["max_profit_r"] == 1.2
    assert final["exit_reason"] == "executed-close"
    assert final["max_profit_r"] == "not tracked"
    assert final["max_drawdown_r"] == "not tracked"


@pytest.mark.parametrize("action", ["opened", "closed"])
def test_pipeline_defers_journal_fallback_when_booked_receipt_read_fails(tmp_path, monkeypatch, action):
    from execution.paper_engine import PaperExecutionEngine
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    if action == "opened":
        paper = PaperExecutionEngine(ledger, 10000)
        paper.evidence_listener = capture.observe_fill
        paper.evidence_prepare_listener = capture.prepare_exit
        pipeline.paper = paper
    else:
        pipeline.process(payload)
        opened = paper.process_quote(quote(timestamp))[0]
        payload = {"alert_id": "close-read-outage", "symbol": "XRPUSDT", "side": "FLATTEN",
                   "entry": 106, "stop": 0, "timestamp": timestamp.isoformat()}
    actual = ledger.get_evidence_outbox_event
    monkeypatch.setattr(ledger, "get_evidence_outbox_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("booked receipt unavailable")))
    result = pipeline.process(payload)
    assert result.accepted and result.fill["action"] == action
    trade_id = result.fill["trade_id"]
    if action == "opened":
        assert store.get(trade_id) is None
    else:
        assert trade_id == opened.trade_id
        assert store.get(trade_id)["status"] == "open"
    before = financial_rows(ledger)
    monkeypatch.setattr(ledger, "get_evidence_outbox_event", actual)
    capture.reconcile_report(ledger)
    assert store.get(trade_id)["status"] == ("open" if action == "opened" else "closed")
    assert financial_rows(ledger) == before


def test_original_producer_retry_is_idempotent_but_changed_fill_is_conflicted(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    from dataclasses import replace
    before = store.evidence_events()
    capture.observe_fill(opened)
    assert store.evidence_events() == before
    with pytest.raises(ValueError, match="immutable evidence event conflict"):
        capture.observe_fill(replace(opened, price=opened.price + .01))
    assert store.evidence_events() == before


def test_replay_retries_unavailable_canonical_decision_without_rewriting_capture(tmp_path, monkeypatch):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    decision_id = payload["journal_decision_id"]
    actual = decisions.get
    monkeypatch.setattr(decisions, "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("decision store offline")))
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    monkeypatch.setattr(decisions, "get", actual)
    assert decisions.get(decision_id)["executed"] is False
    events, before = store.evidence_events(), financial_rows(ledger)
    capture.reconcile_report(ledger)
    assert decisions.get(decision_id)["executed"] is True
    assert len(decisions.list()) == 1
    assert store.evidence_events() == events
    assert financial_rows(ledger) == before
    assert store.get(opened.trade_id)["decision_id"] == str(decision_id)
