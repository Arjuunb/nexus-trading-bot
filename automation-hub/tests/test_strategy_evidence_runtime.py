from datetime import datetime, timedelta, timezone

from data.decision_store import DecisionStore
from data.journal_store import JournalStore
from data.ledger import SqliteLedger
from execution.paper_engine import ForwardPaperExecutionEngine
from services.controls import TradingControl
from services.decision_journal import DecisionJournal
from services.signal_pipeline import SignalPipeline
from services.trading_instances import InstanceLedger


def runtime(tmp_path, *, recovered=None):
    from services.strategy_evidence_capture import StrategyEvidenceCapture
    from services.strategy_identity import observed_strategy_identity
    from strategies.adaptive_trend_pullback.strategy import AdaptiveTrendPullbackStrategy
    ledger = InstanceLedger(SqliteLedger(tmp_path / "ledger.db"), "one", "session")
    store = JournalStore(tmp_path / "journal.db")
    decisions = DecisionStore(str(tmp_path / "decisions.db"))
    paper = ForwardPaperExecutionEngine(ledger, 10000, initial_intents=recovered)
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=10000)
    pipeline.journal = DecisionJournal(store)
    identity = observed_strategy_identity(AdaptiveTrendPullbackStrategy("XRPUSDT"),
                                         strategy_id="adaptive_trend_pullback", timeframe="5m")
    store.save_strategy_identity(identity)
    pipeline.journal_context = {
        "instance_id": "one", "simulation_session_id": "session",
        "strategy_id": "adaptive_trend_pullback", "strategy_version": "1.0.0",
        "strategy_config_hash": identity["strategy_config_hash"], "execution_mode": "paper",
        "market_data_mode": "forward_paper", "owner_id": "owner",
        "account_id": "account-one", "lab_id": None, "source_kind": "forward_paper",
        "evidence_class": "EXECUTED_FORWARD_PAPER",
    }
    capture = StrategyEvidenceCapture(pipeline.journal, decisions=decisions)
    pipeline.evidence = capture
    paper.evidence_listener = capture.observe_fill
    paper.evidence_prepare_listener = capture.prepare_exit
    return ledger, store, decisions, paper, pipeline, capture


def entry(decisions):
    timestamp = datetime.now(timezone.utc)
    decision_id = decisions.record({
        "symbol": "XRPUSDT", "strategy": "Adaptive MTF", "side": "long",
        "decision": "accepted", "instance_id": "one", "ts": timestamp.isoformat(),
        "decision_identity": "decision-one",
    })
    return {
        "alert_id": "order-one", "symbol": "XRPUSDT", "side": "BUY",
        "entry": 100, "stop": 95, "target": 110, "strategy": "Adaptive MTF",
        "timeframe": "5m", "timestamp": timestamp.isoformat(),
        "journal_decision_id": decision_id, "decision_identity": "decision-one",
        "decision_observed_at": decisions.get(decision_id)["decided_at"],
    }, timestamp


def quote(timestamp):
    return {"bid": 100, "ask": 100.2, "mark": 100.1,
            "received_at": (timestamp + timedelta(seconds=1)).isoformat()}


def test_deferred_fill_has_original_decision_and_actual_fill_journal(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    assert pipeline.process(payload).fill["action"] == "intent"
    assert store.list() == []
    fills = paper.process_quote(quote(timestamp))
    assert len(fills) == 1
    row = store.get(fills[0].trade_id)
    assert row["strategy_config_hash"] == pipeline.journal_context["strategy_config_hash"]
    assert row["sections"]["entry_decision"]["decision_reference"] == payload["journal_decision_id"]
    assert row["entry_timestamp"] == quote(timestamp)["received_at"]
    assert decisions.get(payload["journal_decision_id"])["executed"]
    assert len(decisions.list()) == 1
    events_before = row["events"]
    capture.observe_fill(fills[0])
    assert store.get(fills[0].trade_id)["events"] == events_before
    assert len(ledger.get_paper_trades()) == 1


def test_worker_restart_recovers_intent_context_without_new_decision(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    pending = paper.pending_intents()
    # New process objects, same durable accounting, journal and decision stores.
    ledger2, store2, decisions2, paper2, pipeline2, capture2 = runtime(
        tmp_path, recovered=pending)
    fills = paper2.process_quote(quote(timestamp))
    assert store2.get(fills[0].trade_id)["strategy_config_hash"] == pipeline2.journal_context["strategy_config_hash"]
    assert len(decisions2.list()) == 1
    assert len(ledger2.get_paper_trades()) == 1


def test_partial_exit_then_final_close_counts_one_complete_episode(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    first = paper.process_quote(quote(timestamp))[0]
    partial = paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5,
                           execution_id="partial-one")
    assert store.completed_evidence_episodes(instance_id="one") == []
    final = paper.close(symbol="XRPUSDT", exit_price=110, execution_id="close-one")
    rows = store.completed_evidence_episodes(instance_id="one")
    assert len(rows) == 1
    from decimal import Decimal
    assert Decimal(rows[0]["net_pnl"]) == Decimal(str(partial.pnl)) + Decimal(str(final.pnl))
    assert len(ledger.get_paper_trades()) == 2


def test_evidence_failure_does_not_change_fill_or_balances(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.evidence_listener = lambda _fill: (_ for _ in ()).throw(RuntimeError("capture offline"))
    fills = paper.process_quote(quote(timestamp))
    assert fills[0].action == "opened"
    assert len(ledger.get_positions("open")) == 1
    assert len(ledger.get_paper_trades()) == 1
    assert fills[0].price == 100.2


def test_committed_open_recovers_journal_after_callback_failure(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.evidence_listener = lambda _: (_ for _ in ()).throw(RuntimeError("worker crashed"))
    opened = paper.process_quote(quote(timestamp))[0]
    assert store.get(opened.trade_id) is None
    _, store2, decisions2, _, _, capture2 = runtime(tmp_path)
    assert capture2.reconcile(ledger) == 1
    assert store2.get(opened.trade_id)["decision_id"] == str(payload["journal_decision_id"])
    assert len(decisions2.list()) == 1
    before = store2.evidence_events()
    capture2.reconcile(ledger)
    assert store2.evidence_events() == before


def test_committed_partial_exit_recovers_same_episode_after_callback_failure(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    paper.evidence_listener = lambda _: (_ for _ in ()).throw(RuntimeError("worker crashed"))
    partial = paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5,
                           execution_id="partial-one")
    _, store2, _, paper2, _, capture2 = runtime(tmp_path)
    capture2.reconcile(ledger)
    assert store2.get(opened.trade_id)["status"] == "closed"
    assert store2.get(partial.remainder_trade_id)["status"] == "open"
    assert store2.completed_evidence_episodes(instance_id="one") == []
    paper2.close(symbol="XRPUSDT", exit_price=110, execution_id="close-one")
    assert len(store2.completed_evidence_episodes(instance_id="one")) == 1


def test_uncommitted_exit_preparation_is_never_a_fill(tmp_path, monkeypatch):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    def fail(**kwargs):
        raise RuntimeError("transaction rolled back")
    monkeypatch.setattr(ledger, "reduce_position_and_trade", fail)
    import pytest
    with pytest.raises(RuntimeError, match="rolled back"):
        paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5,
                     execution_id="uncommitted")
    capture.reconcile(ledger)
    assert store.get(opened.trade_id)["status"] == "open"
    assert len(store.episodes(instance_id="one")) == 1
    assert store.episodes(instance_id="one")[0]["realised_leg_count"] == 0


def test_pipeline_exit_keeps_original_reason_and_excursions(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    pipeline.process({"alert_id": "stop-close", "symbol": "XRPUSDT", "side": "FLATTEN",
                      "entry": 95, "exit_reason": "stop-loss", "mfe_r": .4, "mae_r": -1})
    exit_section = store.get(opened.trade_id)["sections"]["exit_decision"]
    assert exit_section["exit_reason"] == "stop-loss"
    assert exit_section["max_profit_r"] == .4
    assert exit_section["max_drawdown_r"] == -1


def test_runtime_episode_is_queryable_by_exact_metrics_cohort(tmp_path):
    from services.strategy_intelligence_metrics import EvidenceCohort, calculate_metrics
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    paper.process_quote(quote(timestamp))
    paper.reduce(symbol="XRPUSDT", exit_price=105, fraction=.5, execution_id="partial-one")
    paper.close(symbol="XRPUSDT", exit_price=110, execution_id="close-one")
    provenance = pipeline.journal_context
    cohort = EvidenceCohort(
        strategy_id=provenance["strategy_id"], strategy_version=provenance["strategy_version"],
        config_fingerprint=provenance["strategy_config_hash"], instance_id="one",
        lab_id=None, simulation_session_id="session", execution_mode="paper",
        source_kind="forward_paper", owner_id="owner", account_id="account-one", symbol="XRPUSDT")
    result = calculate_metrics(store.episodes(), cohort=cohort)
    assert result["trade_count"] == 1
    assert result["completed_episode_count"] == 1
    assert result["net_pnl"] == store.episodes()[0]["net_pnl"]
    assert result["coverage"]["funding_complete"] is False


def test_persisted_context_excludes_credentials_and_freezes_original_inputs(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    payload.update(secret="credential", nested={"api_key": "nested-credential", "value": [1]})
    pipeline.process(payload)
    payload["nested"]["value"].append(2)
    intent = store.evidence_events(kind="ORDER_INTENT")[0]["payload"]
    assert "secret" not in intent
    assert intent["nested"] == {"value": [1]}
    paper.process_quote(quote(timestamp))
    import json
    assert "credential" not in json.dumps(store.evidence_events())


def test_retry_after_rejected_fill_retains_original_decision_and_new_intent_context(tmp_path, monkeypatch):
    from execution.paper_engine import FillResult
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    real_open = paper.open
    monkeypatch.setattr(paper, "open", lambda **kwargs: FillResult(
        "rejected", "XRPUSDT", "long", 0, 100, reason="temporary execution rejection"))
    assert not pipeline.process(payload).accepted
    monkeypatch.setattr(paper, "open", real_open)
    # Existing execution semantics release rejected claims for a legitimate
    # retry. Its context can differ without creating a second decision.
    retry_payload = {**payload, "journal_engine": {"retry_context": "observed-second-attempt"}}
    assert pipeline.process(retry_payload).fill["action"] == "intent"
    opened = paper.process_quote(quote(timestamp))[0]
    journal = store.get(opened.trade_id)
    assert journal is not None
    assert journal["decision_id"] == str(payload["journal_decision_id"])
    assert len(decisions.list()) == 1
    assert len(store.episodes()) == 1
    assert len(store.evidence_events(kind="ORDER_INTENT")) == 2


def test_cancelled_pending_decisions_have_no_trade_or_realised_pnl(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    pipeline.process(payload)
    statements = []
    ledger._ledger._c.set_trace_callback(statements.append)
    capture.cancel_pending(ledger, instance_id="one", simulation_session_id="session",
                           reason="account restart committed")
    cancelled = store.evidence_events(kind="CANCELLED")
    assert len(cancelled) == 1
    assert cancelled[0]["decision_id"] == str(payload["journal_decision_id"])
    assert not decisions.get(payload["journal_decision_id"])["executed"]
    assert store.episodes() == []
    assert ledger.get_paper_trades() == []
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    capture.cancel_pending(ledger, instance_id="one", simulation_session_id="session",
                           reason="account restart committed")
    assert len(store.evidence_events(kind="CANCELLED")) == 1


def test_foreign_decision_reference_cannot_be_credited_by_another_account_fill(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    foreign_id = decisions.record({"symbol": "XRPUSDT", "side": "long", "decision": "accepted",
        "instance_id": "other-instance", "decision_identity": "foreign-decision"})
    pipeline.process({**payload, "journal_decision_id": foreign_id})
    opened = paper.process_quote(quote(timestamp))[0]
    assert not decisions.get(foreign_id)["executed"]
    journal = store.get(opened.trade_id)
    assert journal["decision_id"] is None
    assert journal["sections"]["entry_decision"]["decision_reference_status"] == "CONFLICT"
    assert len(ledger.get_paper_trades()) == 1


def test_queued_signal_keeps_original_configuration_after_active_configuration_changes(tmp_path):
    from dataclasses import replace
    from services.strategy_identity import observed_strategy_identity
    from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy
    from strategies.adaptive_trend_pullback.config import AdaptiveTrendPullbackConfig
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    original = observed_strategy_identity(AdaptiveTrendPullbackStrategy("XRPUSDT"),
        strategy_id="adaptive_trend_pullback", timeframe="5m")
    changed = observed_strategy_identity(AdaptiveTrendPullbackStrategy("XRPUSDT",
        config=replace(AdaptiveTrendPullbackConfig(), target_rr=3)),
        strategy_id="adaptive_trend_pullback", timeframe="5m")
    store.save_strategy_identity(changed)
    payload, timestamp = entry(decisions)
    original_decision = decisions.record({"symbol": "XRPUSDT", "side": "long", "decision": "accepted",
        "decision_identity": "original-config-signal", **pipeline.journal_context})
    pipeline.journal_context.update(strategy_config_hash=changed["strategy_config_hash"],
                                    source_hash=changed["source_hash"])
    payload.update(strategy_identity=original, journal_decision_id=original_decision,
                   decision_identity="original-config-signal")
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    journal = store.get(opened.trade_id)
    assert original["strategy_config_hash"] != changed["strategy_config_hash"]
    assert journal["strategy_config_hash"] == original["strategy_config_hash"]
    assert journal["decision_id"] == str(original_decision)
    assert decisions.get(original_decision)["executed"]
    assert store.episodes()[0]["strategy_config_hash"] == original["strategy_config_hash"]
    assert ledger.get_webhook_events()[0]["payload"]["strategy_config_hash"] == original["strategy_config_hash"]


def test_legacy_queued_signal_does_not_acquire_current_configuration(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    payload["original_strategy_configuration_unknown"] = True
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    assert store.get(opened.trade_id)["strategy_config_hash"] is None
    assert store.episodes()[0]["strategy_config_hash"] is None


def test_delayed_signal_journal_keeps_original_signal_and_decision_clocks(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    original = timestamp - timedelta(minutes=5)
    payload["original_signal_timestamp"] = original.isoformat()
    pipeline.process(payload)
    opened = paper.process_quote(quote(timestamp))[0]
    journal = store.get(opened.trade_id)
    assert journal["signal_timestamp"] == original.isoformat()
    assert journal["decision_timestamp"] == payload["decision_observed_at"]
    assert journal["entry_timestamp"] == quote(timestamp)["received_at"]
    assert opened.receipt["sizing_context"]["decision_timestamp"] == timestamp.isoformat()
    assert opened.receipt["sizing_context"]["signal_timestamp"] == original.isoformat()


def test_rejected_decision_cannot_be_credited_by_an_execution(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    reference = decisions.record({"symbol": "XRPUSDT", "side": "long", "decision": "rejected",
        "decision_identity": "rejected-signal", **pipeline.journal_context})
    pipeline.process({**payload, "journal_decision_id": reference, "decision_identity": "rejected-signal"})
    opened = paper.process_quote(quote(timestamp))[0]
    assert not decisions.get(reference)["executed"]
    assert store.get(opened.trade_id)["decision_id"] is None
    assert store.get(opened.trade_id)["sections"]["entry_decision"]["decision_reference_status"] == "CONFLICT"
    assert len(ledger.get_paper_trades()) == 1


def test_opposite_side_decision_cannot_be_credited_by_an_execution(tmp_path):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    payload, timestamp = entry(decisions)
    reference = decisions.record({"symbol": "XRPUSDT", "side": "short", "decision": "accepted",
        "decision_identity": "short-signal", **pipeline.journal_context})
    pipeline.process({**payload, "journal_decision_id": reference, "decision_identity": "short-signal"})
    opened = paper.process_quote(quote(timestamp))[0]
    assert not decisions.get(reference)["executed"]
    assert store.get(opened.trade_id)["decision_id"] is None
    assert len(ledger.get_paper_trades()) == 1
