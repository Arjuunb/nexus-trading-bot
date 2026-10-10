"""Identity capture observes actual engine decisions without changing execution."""
from datetime import datetime, timedelta, timezone

from bot.types import Signal, SignalType
from services.auto_engine import AutoStrategyEngine
from strategies.adaptive_trend_pullback import AdaptiveTrendPullbackStrategy
from test_strategy_evidence_runtime import runtime, quote, entry


def auto(tmp_path, *, evidence=True):
    ledger, store, decisions, paper, pipeline, capture = runtime(tmp_path)
    if not evidence:
        pipeline.evidence = None
        pipeline.journal = None
        paper.evidence_listener = None
        paper.evidence_prepare_listener = None
    engine = AutoStrategyEngine(pipeline, paper, ledger, symbols=["XRPUSDT"], timeframe="5m",
                                entry_mode="market", strategy_factory=AdaptiveTrendPullbackStrategy)
    engine.decisions = decisions
    engine.strategy_key = "adaptive_trend_pullback"
    engine.strategy_version = "1.0.0"
    signal = Signal(datetime.now(timezone.utc), "XRPUSDT", SignalType.LONG, 100, 95, 110)
    return engine, signal, ledger, store, decisions, paper, pipeline


def test_actual_strategy_signal_decision_intent_fill_share_immutable_identity(tmp_path):
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    strategy = AdaptiveTrendPullbackStrategy("XRPUSDT")
    result = engine._on_signal("XRPUSDT", signal, strategy)
    assert result["kind"] == "pending"
    fingerprint = signal.strategy_identity["strategy_config_hash"]
    signal_event = store.evidence_events(kind="SIGNAL")[0]["payload"]
    assert signal_event["signal"]["type"] == "long"
    assert signal_event["signal"]["entry"] == signal.entry
    decision = decisions.list()[0]
    assert decision["strategy_id"] == "adaptive_trend_pullback"
    assert decision["strategy_version"] == "1.0.0"
    assert decision["strategy_config_hash"] == fingerprint
    assert not decision["executed"]
    assert store.list() == []
    fill = paper.process_quote(quote(signal.timestamp))[0]
    journal = store.get(fill.trade_id)
    assert journal["strategy_config_hash"] == fingerprint
    assert journal["episode_id"] is not None
    assert journal["decision_timestamp"] == decision["decided_at"]
    assert journal["signal_timestamp"] == signal.timestamp.isoformat()
    assert journal["entry_timestamp"] == fill.executed_at
    assert len(decisions.list()) == 1
    assert decisions.get(decision["id"])["executed"]
    webhook = ledger.get_webhook_events()[0]["payload"]
    import json
    if isinstance(webhook, str):
        webhook = json.loads(webhook)
    assert webhook["observed_decision_timestamp"] == decision["decided_at"]
    assert webhook["fill_eligibility_timestamp"] == signal.timestamp.isoformat()


def test_observer_enabled_disabled_and_unavailable_preserve_execution(tmp_path):
    outcomes = []
    for mode in ("enabled", "disabled", "unavailable"):
        folder = tmp_path / mode
        folder.mkdir()
        engine, signal, ledger, store, decisions, paper, pipeline = auto(folder, evidence=mode != "disabled")
        if mode == "unavailable":
            store._c.close()
        result = engine._on_signal("XRPUSDT", signal, AdaptiveTrendPullbackStrategy("XRPUSDT"))
        fill = paper.process_quote(quote(signal.timestamp))[0]
        closed = paper.close(symbol="XRPUSDT", exit_price=110)
        outcomes.append((result["kind"], fill.action, fill.price, fill.size, closed.price,
                         closed.pnl, closed.fee, paper.balance(), len(ledger.get_paper_trades())))
    assert outcomes[0] == outcomes[1] == outcomes[2]


def test_conflicting_frozen_source_cannot_pool_new_trade_into_old_hash(tmp_path, monkeypatch):
    import services.strategy_identity as module
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    observed = module.observed_strategy_identity
    def upgraded(*args, **kwargs):
        identity = observed(*args, **kwargs)
        identity["source_hash"] = "changed-runtime-source"
        return identity
    monkeypatch.setattr(module, "observed_strategy_identity", upgraded)
    result = engine._on_signal("XRPUSDT", signal, AdaptiveTrendPullbackStrategy("XRPUSDT"))
    assert result["kind"] == "pending"
    fill = paper.process_quote(quote(signal.timestamp))[0]
    assert store.get(fill.trade_id)["strategy_config_hash"] is None
    assert store.episodes()[0]["strategy_config_hash"] is None
    assert decisions.list()[0]["strategy_config_hash"] is None


def test_canonical_evidence_identity_preserves_existing_primary_strategy_attribution(tmp_path):
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    paper.strategy_id = "existing-instance-version-attribution"
    engine._on_signal("XRPUSDT", signal, AdaptiveTrendPullbackStrategy("XRPUSDT"))
    fill = paper.process_quote(quote(signal.timestamp))[0]
    assert ledger.get_paper_trades()[0]["strategy_id"] == "existing-instance-version-attribution"
    assert store.get(fill.trade_id)["strategy_id"] == "adaptive_trend_pullback"


def test_runtime_declared_version_mismatch_remains_unverified_in_projection(tmp_path):
    from services.strategy_intelligence_metrics import EvidenceCohort, calculate_metrics
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    engine.strategy_version = "2.0.0"
    engine._on_signal("XRPUSDT", signal, AdaptiveTrendPullbackStrategy("XRPUSDT"))
    fill = paper.process_quote(quote(signal.timestamp))[0]
    paper.close(symbol="XRPUSDT", exit_price=110)
    journal = store.get(fill.trade_id)
    assert journal["strategy_version"] == "1.0.0"
    assert journal["identity_status"] == "version_mismatch"
    assert store.episodes()[0]["identity_status"] == "version_mismatch"
    cohort = EvidenceCohort(strategy_id="adaptive_trend_pullback", strategy_version="1.0.0",
        config_fingerprint=journal["strategy_config_hash"], instance_id="one", lab_id=None,
        simulation_session_id="session", execution_mode="paper", source_kind="forward_paper",
        owner_id="owner", account_id="account-one", symbol="XRPUSDT")
    assert calculate_metrics(store.episodes(), cohort=cohort)["identity_verified"] is False


def test_nonexecuting_signal_outcomes_do_not_manufacture_decisions_or_trades(tmp_path):
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    strategy = AdaptiveTrendPullbackStrategy("XRPUSDT")
    flat = Signal(signal.timestamp - timedelta(seconds=1), "XRPUSDT", SignalType.FLAT, 100, 95, 110)
    assert engine._on_signal("XRPUSDT", flat, strategy)["kind"] == "rejected"
    assert len(store.evidence_events(kind="REJECTED")) == 1
    assert decisions.list() == []
    assert ledger.get_paper_trades() == []
    engine._on_signal("XRPUSDT", signal, strategy)
    paper.process_quote(quote(signal.timestamp))
    held = Signal(signal.timestamp + timedelta(minutes=5), "XRPUSDT", SignalType.LONG, 100, 95, 110)
    assert engine._on_signal("XRPUSDT", held, strategy)["kind"] == "hold"
    assert len(store.evidence_events(kind="HOLD")) == 1
    assert len(decisions.list()) == 1
    assert len(ledger.get_paper_trades()) == 1


def test_restored_limit_signal_preserves_original_identity_and_signal_clock(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    from services.strategy_identity import observed_strategy_identity
    from strategies.adaptive_trend_pullback.config import AdaptiveTrendPullbackConfig
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    payload, original_time = entry(decisions)
    original = observed_strategy_identity(AdaptiveTrendPullbackStrategy("XRPUSDT"),
        strategy_id="adaptive_trend_pullback", timeframe="5m")
    payload["strategy_identity"] = original
    changed_strategy = AdaptiveTrendPullbackStrategy("XRPUSDT",
        config=replace(AdaptiveTrendPullbackConfig(), target_rr=3.0))
    changed = observed_strategy_identity(changed_strategy,
        strategy_id="adaptive_trend_pullback", timeframe="5m")
    store.save_strategy_identity(changed)
    pipeline.journal_context["strategy_config_hash"] = changed["strategy_config_hash"]
    engine._pending["XRPUSDT"] = {"side": "BUY", "price": 100, "ttl": 3,
        "target": 110, "payload": payload, "decision_id": payload["journal_decision_id"]}
    later = original_time + timedelta(minutes=5)
    engine._check_pending("XRPUSDT", SimpleNamespace(open=100, high=101, low=99, timestamp=later))
    opened = paper.process_quote({**quote(later), "bid": 99.8, "ask": 100, "mark": 99.9})[0]
    journal = store.get(opened.trade_id)
    assert journal["strategy_config_hash"] == original["strategy_config_hash"]
    assert journal["signal_timestamp"] == original_time.isoformat()
    assert opened.receipt["sizing_context"]["decision_timestamp"] == later.isoformat()


def test_restored_legacy_limit_signal_configuration_remains_unknown(tmp_path):
    from types import SimpleNamespace
    engine, signal, ledger, store, decisions, paper, pipeline = auto(tmp_path)
    payload, original_time = entry(decisions)
    engine._pending["XRPUSDT"] = {"side": "BUY", "price": 100, "ttl": 3,
        "target": 110, "payload": payload, "decision_id": payload["journal_decision_id"]}
    later = original_time + timedelta(minutes=5)
    engine._check_pending("XRPUSDT", SimpleNamespace(open=100, high=101, low=99, timestamp=later))
    opened = paper.process_quote({**quote(later), "bid": 99.8, "ask": 100, "mark": 99.9})[0]
    assert store.get(opened.trade_id)["strategy_config_hash"] is None
    assert store.get(opened.trade_id)["signal_timestamp"] == original_time.isoformat()
