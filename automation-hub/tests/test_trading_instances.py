from data.ledger import SqliteLedger
from data.decision_store import DecisionStore
from execution.paper_engine import PaperExecutionEngine
from services.trading_instances import InstanceLedger, ResearchExecutionEngine, TradingInstanceManager
from datetime import datetime, timedelta, timezone
import time
import pytest


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


def test_instance_scoped_ledger_keeps_positions_and_trades_isolated():
    ledger = SqliteLedger(":memory:")
    first = PaperExecutionEngine(InstanceLedger(ledger, "one"), 1_000)
    second = PaperExecutionEngine(InstanceLedger(ledger, "two"), 1_000)
    first.open(symbol="BTCUSDT", side="BUY", size=0.1, entry=100, stop=95)
    assert len(first.positions()) == 1
    assert second.positions() == []
    assert len(first.ledger.get_paper_trades()) == 1
    assert second.ledger.get_paper_trades() == []


def test_autonomous_idempotency_is_permanent_and_instance_scoped():
    from services.dedup import DuplicateGuard

    ledger = SqliteLedger(":memory:")
    one, two = InstanceLedger(ledger, "one"), InstanceLedger(ledger, "two")
    alert_id = "auto:one:BTCUSDT:5m:2026-08-09T00:00:00+00:00:buy"
    one.insert_webhook_event(alert_id=alert_id, symbol="BTCUSDT", side="BUY",
                             entry=100, stop=95, payload={}, status="accepted")
    # A zero normal retry window would already have elapsed, but autonomous
    # candle IDs remain protected for the lifetime of the ledger.
    assert DuplicateGuard(one, window_seconds=0).is_duplicate(alert_id)
    assert not DuplicateGuard(two, window_seconds=0).is_duplicate(alert_id)


def test_instances_keep_pair_strategy_version_and_metrics_separate():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    btc = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                         strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                         capital_allocation=1_000)
    eth = manager.create(symbol="ETHUSDT", strategy_key="ema", strategy_label="EMA Crossover",
                         strategy_version="v2", timeframe="5m", risk_per_trade_pct=0.005,
                         capital_allocation=2_000)
    assert btc.id != eth.id
    assert manager.status(btc.id)["metrics"]["instance_id"] == btc.id
    assert manager.status(eth.id)["metrics"]["instance_id"] == eth.id
    assert manager.leaderboard()[0]["strategy_version"] in {"v1", "v2"}


def test_instance_worker_receives_server_risk_policy():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(
        ledger, strategy_factory=_factory, live=False, live_poll_s=60,
        max_drawdown_pct=0.08, max_daily_loss_pct=0.02,
        max_consecutive_losses=3, cooldown_after_loss_min=90,
        session_start=7, session_end=20, max_weekly_loss_pct=0.04,
        max_trades_per_day=8, trading_days_mask=31,
    )
    instance = manager.create(
        symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
        strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
        capital_allocation=1_000,
    )
    manager.start(instance.id)
    pipeline = manager._runtime[instance.id][2]
    assert pipeline.max_drawdown_pct == 0.08
    assert pipeline.max_daily_loss_pct == 0.02
    assert pipeline.max_consecutive_losses == 3
    assert pipeline.cooldown_after_loss_min == 90
    assert (pipeline.session_start, pipeline.session_end) == (7, 20)
    assert pipeline.max_weekly_loss_pct == 0.04
    assert pipeline.max_trades_per_day == 8
    assert pipeline.trading_days_mask == 31
    manager.stop(instance.id)


def test_instance_learning_books_are_worker_scoped():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    manager.configure(max_active_slots=2)
    first = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                           strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                           capital_allocation=500)
    second = manager.create(symbol="ETHUSDT", strategy_key="brain", strategy_label="Decision Brain",
                            strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                            capital_allocation=500)
    manager.start(first.id)
    manager.start(second.id)
    first_learning = manager._runtime[first.id][2].learning
    second_learning = manager._runtime[second.id][2].learning
    assert first_learning is not second_learning
    first_learning.adjustments["symbol:BTCUSDT"] = {"multiplier": 0.5}
    assert second_learning.risk_multiplier("BTCUSDT") == 1.0
    manager.stop(first.id)
    manager.stop(second.id)


def test_instance_learning_book_survives_local_sqlite_restart(tmp_path):
    ledger_path = tmp_path / "ledger.db"
    ledger = SqliteLedger(str(ledger_path))
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                              strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=500)
    manager.start(instance.id)
    learning = manager._runtime[instance.id][2].learning
    learning.adjustments["symbol:BTCUSDT"] = {"type": "risk_multiplier", "multiplier": 0.5}
    learning._save()
    manager.stop(instance.id)

    restarted = TradingInstanceManager(SqliteLedger(str(ledger_path)), strategy_factory=_factory,
                                       live=False, live_poll_s=60)
    restarted.start(instance.id)
    restored = restarted._runtime[instance.id][2].learning
    assert restored.risk_multiplier("BTCUSDT") == 0.5
    assert restored.path == str(tmp_path / "instance-learning" / f"{instance.id}.json")
    restarted.stop(instance.id)


def test_research_instance_execution_adapter_never_places_orders():
    ledger = SqliteLedger(":memory:")
    paper = ResearchExecutionEngine(InstanceLedger(ledger, "research"), 1_000)
    fill = paper.open(symbol="BTCUSDT", side="BUY", size=0.1, entry=100, stop=95)
    assert fill.action == "rejected"
    assert paper.positions() == []
    assert paper.history() == []


def test_instance_platform_settings_persist_and_global_risk_sees_all_scoped_positions():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    first = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                           strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                           capital_allocation=1_000)
    manager.configure(max_active_slots=2, max_global_risk_pct=0.02)
    reloaded = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    assert reloaded.max_slots == 2
    assert reloaded.max_global_risk_pct == 0.02
    InstanceLedger(ledger, first.id).open_position(symbol="BTCUSDT", side="long", size=10, entry=100, stop=95)
    allowed, reason = reloaded._global_guard(first.id, "ETHUSDT", 100, 95, 1)
    assert not allowed
    assert "Global account risk exceeded" in reason
    snapshot = reloaded.platform_status()
    assert snapshot["total_open_positions"] == 1
    assert snapshot["global_risk_status"] == "warning"


def test_instances_api_returns_platform_status_and_validates_slot_change(monkeypatch):
    pytest.importorskip("fastapi")
    from routers import instances as instance_api

    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)
    monkeypatch.setattr(instance_api._wa, "_check_secret", lambda _secret: None)
    payload = instance_api.list_instances()
    assert payload["max_active_slots"] == 1
    updated = instance_api.configure_platform(instance_api.PlatformConfig(max_active_slots=3))
    assert updated["max_active_slots"] == 3


def test_delete_api_returns_actionable_service_unavailable_for_persistence_failure(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from routers import instances as instance_api

    class FailingManager:
        store = type("Store", (), {"available": True})()

        def delete(self, _instance_id):
            raise RuntimeError("Supabase delete unavailable")

    monkeypatch.setattr(instance_api._wa, "instance_manager", FailingManager())
    monkeypatch.setattr(instance_api._wa, "_check_secret", lambda _secret: None)

    with pytest.raises(HTTPException) as exc:
        instance_api.delete_instance("instance-one")

    assert exc.value.status_code == 503
    assert exc.value.detail == "Supabase delete unavailable"


def test_instance_status_exposes_only_its_scoped_last_decision_and_real_aggregate_fields():
    ledger = SqliteLedger(":memory:")
    decisions = DecisionStore(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60,
                                     decision_store=decisions)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                              strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000)
    decisions.record({"symbol": "BTCUSDT", "decision": "accepted", "instance_id": instance.id,
                      "strategy": "Decision Brain", "timeframe": "5m"})
    decisions.record({"symbol": "ETHUSDT", "decision": "rejected", "instance_id": "other"})
    status = manager.status(instance.id)
    assert status["last_decision"]["instance_id"] == instance.id
    assert status["last_decision"]["final_state"] == "QUALIFIED"
    assert status["ui_status"] == "BLOCKED"
    assert status["worker_counts"] == {"signals": 0, "accepted": 0, "rejections": 0}
    assert status["mtf_policy"]["label"] == "Entry 5m · HTF 1h waiting · Bias 4h waiting"
    assert status["strategy_identity"]["configured_id"] == "brain"
    assert status["configuration"]["capital_allocation"] == 1_000
    assert status["execution"]["current_equity"] == 1_000
    assert status["market_data"]["market_data_status"] == "stopped"
    platform = manager.platform_status()
    assert platform["total_allocated_capital"] == 1_000
    assert platform["total_instances"] == 1


def test_instance_execution_configuration_is_persisted_and_does_not_inherit_legacy_values():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                              strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000, sizing_mode="fixed", fixed_position_size=0.02,
                              entry_mode="market")
    reloaded = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    saved = reloaded.status(instance.id)
    assert saved["configuration"]["symbol"] == "BTCUSDT"
    assert saved["configuration"]["strategy_key"] == "brain"
    assert saved["configuration"]["timeframe"] == "5m"
    assert saved["configuration"]["sizing_mode"] == "fixed_quantity"
    assert saved["configuration"]["fixed_position_size"] == 0.02
    assert saved["configuration"]["entry_mode"] == "market"


def test_active_duplicate_and_overallocation_are_rejected():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60,
                                     paper_account_capital=1_000)
    first = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                           strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                           capital_allocation=700)
    first.state = "running"  # duplicate protection is about active workers.
    with pytest.raises(ValueError, match="already active"):
        manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                       strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                       capital_allocation=100)
    with pytest.raises(ValueError, match="exceeds paper account capacity"):
        manager.create(symbol="ETHUSDT", strategy_key="ema", strategy_label="EMA Crossover",
                       strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                       capital_allocation=400)


@pytest.mark.parametrize("field,value,message", [
    ("capital_allocation", float("nan"), "capital_allocation must be a finite value"),
    ("capital_allocation", float("inf"), "capital_allocation must be a finite value"),
    ("risk_per_trade_pct", float("nan"), "risk_per_trade_pct must be a finite value"),
    ("risk_per_trade_pct", float("inf"), "risk_per_trade_pct must be a finite value"),
])
def test_instance_create_rejects_non_finite_money_and_risk(field, value, message):
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    values = {"capital_allocation": 500, "risk_per_trade_pct": 0.005, field: value}

    with pytest.raises(ValueError, match=message):
        manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                       strategy_version="v1", timeframe="5m", **values)

    assert manager.list() == []


def test_instance_create_rejects_non_finite_optional_risk_controls():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)

    with pytest.raises(ValueError, match="maximum_risk_amount must be a finite value"):
        manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                       strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                       capital_allocation=500, maximum_risk_amount=float("nan"))
    with pytest.raises(ValueError, match="minimum_equity must be finite"):
        manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                       strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                       capital_allocation=500, minimum_equity=float("inf"))

    assert manager.list() == []


def test_worker_start_uses_persisted_instance_execution_not_legacy_runtime(monkeypatch):
    from services.auto_engine import AutoStrategyEngine
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    instance = manager.create(symbol="ETHUSDT", strategy_key="ema", strategy_label="EMA Crossover",
                              strategy_version="v2", timeframe="15m", risk_per_trade_pct=0.01,
                              capital_allocation=800, sizing_mode="fixed", fixed_position_size=3.0,
                              entry_mode="market", max_open_positions=1)
    monkeypatch.setattr(AutoStrategyEngine, "start", lambda self: True)
    manager.start(instance.id)
    engine, _paper, pipeline, _controls = manager._runtime[instance.id]
    assert engine.symbols == ["ETHUSDT"]
    assert engine.timeframe == "15m"
    assert engine.entry_mode == "market"
    assert pipeline.position_sizing_mode == "fixed_quantity"
    assert pipeline.fixed_position_size == 3.0
    assert pipeline.max_open_positions == 1


def test_instance_worker_wires_journal_memory_and_server_owned_provenance(monkeypatch):
    from services.auto_engine import AutoStrategyEngine

    ledger = SqliteLedger(":memory:")
    journal = object()
    memory = object()
    manager = TradingInstanceManager(
        ledger, strategy_factory=_factory, live=False, live_poll_s=60,
        decision_journal=journal, trade_memory=memory,
    )
    instance = manager.create(
        symbol="ETHUSDT", strategy_key="supertrend", strategy_label="Supertrend",
        strategy_version="1.0.0", timeframe="15m", risk_per_trade_pct=0.005,
        capital_allocation=1_000, fill_model="RealisticFill", exchange="kraken",
    )
    monkeypatch.setattr(AutoStrategyEngine, "start", lambda self: True)

    manager.start(instance.id)
    pipeline = manager._runtime[instance.id][2]

    assert pipeline.journal is journal
    assert pipeline.trade_memory is memory
    assert pipeline.journal_context == {
        "instance_id": instance.id,
        "simulation_session_id": instance.simulation_session_id,
        "instance_name": f"ETHUSDT Supertrend 15m #{instance.id[:6].upper()}",
        "strategy_id": "supertrend",
        "strategy_name": "Supertrend",
        "strategy_version": "1.0.0",
        "market_data_mode": "live",
        "market_data_source": None,
        "fill_model": "RealisticFill",
        "execution_mode": "paper",
        "exchange": "kraken",
        "instrument_type": "spot",
    }


def test_instance_fill_model_is_persisted_and_constructed_per_worker(monkeypatch):
    from services.auto_engine import AutoStrategyEngine
    from services.fill_model import PerfectFill, RealisticFill

    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60,
                                     max_slots=2, paper_account_capital=2_000)
    realistic = manager.create(
        symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
        strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
        capital_allocation=1_000,
    )
    ideal = manager.create(
        symbol="ETHUSDT", strategy_key="ema", strategy_label="EMA Crossover",
        strategy_version="v1", timeframe="15m", risk_per_trade_pct=0.005,
        capital_allocation=1_000, fill_model="perfect",
    )
    assert realistic.fill_model == "RealisticFill"
    assert ideal.fill_model == "PerfectFill"

    monkeypatch.setattr(AutoStrategyEngine, "start", lambda self: True)
    manager.start(realistic.id)
    manager.start(ideal.id)
    assert isinstance(manager._runtime[realistic.id][1].fill_model, RealisticFill)
    assert isinstance(manager._runtime[ideal.id][1].fill_model, PerfectFill)

    restored = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    assert restored.status(realistic.id)["configuration"]["fill_model"] == "RealisticFill"
    assert restored.status(ideal.id)["configuration"]["fill_model"] == "PerfectFill"


def test_instance_fill_model_can_change_only_through_safe_worker_rebuild(monkeypatch):
    from services.auto_engine import AutoStrategyEngine
    from services.fill_model import PerfectFill

    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    instance = manager.create(
        symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
        strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
        capital_allocation=1_000,
    )
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True), setattr(self, "lifecycle_state", "running"), True)[-1],
    )
    manager.start(instance.id)

    updated = manager.update_configuration(instance.id, fill_model="PerfectFill")

    assert updated.state == "running"
    assert updated.fill_model == "PerfectFill"
    assert isinstance(manager._runtime[instance.id][1].fill_model, PerfectFill)
    with pytest.raises(ValueError, match="fill_model must be one of"):
        manager.update_configuration(instance.id, fill_model="UnknownFill")

    paper = manager._runtime[instance.id][1]
    paper.open(symbol="BTCUSDT", side="BUY", size=0.01, entry=100, stop=95)
    paper.close(symbol="BTCUSDT", exit_price=101)
    with pytest.raises(ValueError, match="immutable after the first trade"):
        manager.update_configuration(instance.id, fill_model="RealisticFill")


def test_instance_quick_configuration_persists_and_rebuilds_running_worker(monkeypatch):
    from services.auto_engine import AutoStrategyEngine
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                              strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000, max_open_positions=3)
    monkeypatch.setattr(AutoStrategyEngine, "start", lambda self: (setattr(self, "running", True), setattr(self, "lifecycle_state", "running"), True)[-1])
    manager.start(instance.id)

    updated = manager.update_configuration(
        instance.id, strategy_key="ema", strategy_label="EMA Crossover",
        strategy_version="v2", timeframe="15m", risk_per_trade_pct=0.0075,
        capital_allocation=900, max_open_positions=1,
    )

    assert updated.state == "running"
    status = manager.status(instance.id)
    assert status["configuration"]["strategy_key"] == "ema"
    assert status["configuration"]["strategy_version"] == "v2"
    assert status["configuration"]["timeframe"] == "15m"
    assert status["configuration"]["risk_per_trade_pct"] == 0.0075
    assert status["configuration"]["capital_allocation"] == 900
    assert status["configuration"]["max_open_positions"] == 1
    assert status["execution"]["max_open_positions"] == 1

    reloaded = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    saved = reloaded.status(instance.id)["configuration"]
    assert saved["strategy_key"] == "ema"
    assert saved["timeframe"] == "15m"
    assert saved["max_open_positions"] == 1


def test_instance_quick_configuration_refuses_changes_with_open_position():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                              strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000)
    InstanceLedger(ledger, instance.id).open_position(symbol="BTCUSDT", side="long", size=0.1, entry=100, stop=95)

    with pytest.raises(ValueError, match="Close the instance's open position"):
        manager.update_configuration(instance.id, timeframe="15m")


def test_invalid_strategy_replacement_does_not_stop_healthy_worker(monkeypatch):
    from services.auto_engine import AutoStrategyEngine

    def selective_factory(key, symbol):
        if key == "broken":
            raise RuntimeError("strategy package unavailable")
        return _factory(key, symbol)

    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=selective_factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain",
                              strategy_label="Decision Brain", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000)
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1],
    )
    manager.start(instance.id)
    original_engine = manager._runtime[instance.id][0]

    with pytest.raises(ValueError, match="Strategy validation failed before restart"):
        manager.update_configuration(instance.id, strategy_key="broken",
                                     strategy_label="Broken Strategy")

    assert original_engine.running is True
    assert manager._runtime[instance.id][0] is original_engine
    assert manager.status(instance.id)["strategy_key"] == "brain"


def test_terminal_worker_error_disables_automatic_restart_and_preserves_detail(monkeypatch):
    from services.auto_engine import AutoStrategyEngine

    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain",
                              strategy_label="Decision Brain", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000)
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1],
    )
    manager.start(instance.id)
    engine = manager._runtime[instance.id][0]
    engine.last_error = "StrategyExecutionError: indicator state corrupt"
    engine.last_transition = datetime.now(timezone.utc).isoformat()
    engine._emit_lifecycle("error", "Strategy execution failed")

    saved = manager._instances[instance.id]
    assert saved.state == "error"
    assert saved.desired_running is False
    assert saved.last_error == "StrategyExecutionError: indicator state corrupt"
    snapshot = manager.platform_status(runtime_states=[manager.status(instance.id)])
    assert snapshot["global_status"] != "critical"


def test_restore_failure_is_not_retried_on_every_container_restart(monkeypatch):
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain",
                              strategy_label="Decision Brain", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000)
    instance.desired_running = True
    instance.state = "running"
    manager.store.save(instance)
    monkeypatch.setattr(manager, "start", lambda _id: (_ for _ in ()).throw(
        RuntimeError("provider configuration invalid")))

    assert manager.restore_desired_instances() == []
    assert instance.state == "error"
    assert instance.desired_running is False
    assert "provider configuration invalid" in instance.last_error


def test_paused_instance_cannot_be_reactivated_by_worker_lifecycle_event(monkeypatch):
    from services.auto_engine import AutoStrategyEngine

    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain",
                              strategy_label="Decision Brain", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000)
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1],
    )
    manager.start(instance.id)
    manager.pause(instance.id)
    engine = manager._runtime[instance.id][0]

    engine.last_transition = datetime.now(timezone.utc).isoformat()
    engine._emit_lifecycle("running", "Fresh closed market data confirmed")

    assert manager._instances[instance.id].state == "paused"
    assert manager._instances[instance.id].desired_running is False


def test_pause_waits_for_worker_acknowledgement_before_reporting_paused(monkeypatch):
    from services.auto_engine import AutoStrategyEngine

    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain",
                              strategy_label="Decision Brain", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000)
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1],
    )
    manager.start(instance.id)
    engine, _paper, _pipeline, controls = manager._runtime[instance.id]
    observed = []

    def acknowledge():
        observed.append((controls.trading_allowed(), instance.state))
        return {"pending_orders": 0, "acknowledged_at": "now"}

    monkeypatch.setattr(engine, "acknowledge_entry_pause", acknowledge)
    result = manager.pause(instance.id)

    assert observed == [(False, "running")]
    assert result.state == "paused"
    logs = ledger._c.execute(
        "SELECT message FROM instance_engine_logs WHERE instance_id=?", (instance.id,)
    ).fetchall()
    assert any("pause_acknowledged" in row["message"] for row in logs)


def test_pause_acknowledgement_failure_is_degraded_and_not_success(monkeypatch):
    from services.auto_engine import AutoStrategyEngine

    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain",
                              strategy_label="Decision Brain", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000)
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1],
    )
    manager.start(instance.id)
    monkeypatch.setattr(
        manager._runtime[instance.id][0], "acknowledge_entry_pause",
        lambda: (_ for _ in ()).throw(TimeoutError("busy cycle")),
    )

    with pytest.raises(RuntimeError, match="Pause acknowledgement failed"):
        manager.pause(instance.id)
    assert instance.state == "degraded"
    assert instance.desired_running is False


def test_shutdown_checkpoints_all_instances_and_preserves_restart_intent(monkeypatch):
    from services.auto_engine import AutoStrategyEngine

    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60, max_slots=2,
                                     paper_account_capital=5_000)
    manager.max_slots = 2
    instances = [manager.create(
        symbol=symbol, strategy_key="brain", strategy_label="Decision Brain",
        strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
        capital_allocation=1_000,
    ) for symbol in ("BTCUSDT", "ETHUSDT")]
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1],
    )
    for instance in instances:
        manager.start(instance.id)
        monkeypatch.setattr(
            manager._runtime[instance.id][0], "acknowledge_entry_pause",
            lambda timeout_s=5: {"pending_orders": 0, "acknowledged_at": "now"},
        )

    result = manager.shutdown(timeout_s=2)

    assert result == {"requested": 2, "acknowledged": 2, "errors": []}
    assert all(instance.desired_running is True for instance in instances)
    assert all(not runtime[3].trading_allowed() for runtime in manager._runtime.values())
    assert all(not runtime[0].running for runtime in manager._runtime.values())


def test_resume_rebuilds_terminal_runtime_instead_of_labelling_dead_worker_running(monkeypatch):
    from services.auto_engine import AutoStrategyEngine

    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain",
                              strategy_label="Decision Brain", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=1_000)
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1],
    )
    manager.start(instance.id)
    original = manager._runtime[instance.id][0]
    original.running = False
    original.lifecycle_state = "error"
    instance.state = "error"

    resumed = manager.resume(instance.id)

    assert resumed.state == "running"
    assert manager._runtime[instance.id][0] is not original
    assert manager._runtime[instance.id][0].running is True


def test_instances_start_and_stop_independently_and_slot_limit_is_backend_enforced(monkeypatch):
    from services.auto_engine import AutoStrategyEngine
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    manager.configure(max_active_slots=2)
    one = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                         strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005, capital_allocation=500)
    two = manager.create(symbol="ETHUSDT", strategy_key="ema", strategy_label="EMA Crossover",
                         strategy_version="v1", timeframe="15m", risk_per_trade_pct=0.005, capital_allocation=500)
    three = manager.create(symbol="SOLUSDT", strategy_key="ema", strategy_label="EMA Crossover",
                           strategy_version="v1", timeframe="15m", risk_per_trade_pct=0.005, capital_allocation=500)
    monkeypatch.setattr(AutoStrategyEngine, "start", lambda self: (setattr(self, "running", True), setattr(self, "lifecycle_state", "running"), True)[-1])
    manager.start(one.id); manager.start(two.id)
    with pytest.raises(ValueError, match="Maximum active trading slots"):
        manager.start(three.id)
    manager.stop(one.id)
    assert manager.status(one.id)["state"] == "stopped"
    assert manager.status(two.id)["state"] == "running"


def test_new_instance_has_created_state_before_first_start():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                              strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=500)
    assert instance.state == "created"
    assert manager.status(instance.id)["state"] == "created"


def test_restore_never_exceeds_the_persisted_active_slot_limit(monkeypatch):
    from services.auto_engine import AutoStrategyEngine
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    manager.configure(max_active_slots=2)
    for symbol, strategy in (("BTCUSDT", "brain"), ("ETHUSDT", "ema"), ("SOLUSDT", "donchian")):
        instance = manager.create(symbol=symbol, strategy_key=strategy, strategy_label=strategy,
                                  strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                                  capital_allocation=500)
        instance.state, instance.desired_running = "running", True
        manager.store.save(instance)

    reloaded = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    monkeypatch.setattr(AutoStrategyEngine, "start", lambda self: (setattr(self, "running", True), setattr(self, "lifecycle_state", "running"), True)[-1])

    restored = reloaded.restore_desired_instances()

    assert len(restored) == 2
    rows = reloaded.list()
    assert sum(row["state"] == "running" for row in rows) == 2
    paused = [row for row in rows if row["state"] == "paused"]
    assert len(paused) == 1
    assert "maximum active trading slots" in paused[0]["last_error"]


def test_create_first_second_and_third_instances_initializes_distinct_market_cursors():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    created = [
        manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                       strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                       capital_allocation=500),
        manager.create(symbol="ETHUSDT", strategy_key="brain", strategy_label="Decision Brain",
                       strategy_version="v2", timeframe="15m", risk_per_trade_pct=0.0075,
                       capital_allocation=600),
        manager.create(symbol="SOLUSDT", strategy_key="donchian", strategy_label="Donchian",
                       strategy_version="v3", timeframe="30m", risk_per_trade_pct=0.004,
                       capital_allocation=700),
    ]

    assert len({row.id for row in created}) == 3
    with ledger._lock:
        rows = [dict(row) for row in ledger._c.execute(
            "SELECT * FROM instance_market_state ORDER BY instance_id")]
    assert {row["instance_id"] for row in rows} == {row.id for row in created}
    assert all(row["market_data_mode"] == "paper_forward" for row in rows)
    assert all(row["last_processed_candle_timestamp"] is None for row in rows)


def test_schema_failure_happens_before_instance_row_is_inserted(monkeypatch):
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    writes = {"create": 0}

    monkeypatch.setattr(manager.store, "assert_runtime_schema",
                        lambda: (_ for _ in ()).throw(RuntimeError("missing instance_market_state")))
    original_create = manager.store.create
    monkeypatch.setattr(manager.store, "create",
                        lambda instance: (writes.__setitem__("create", writes["create"] + 1),
                                          original_create(instance))[-1])

    with pytest.raises(RuntimeError, match="instance_market_state"):
        manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                       strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                       capital_allocation=500)
    assert writes["create"] == 0
    assert manager.list() == []


def test_three_workers_run_simultaneously_with_isolated_runtime_objects(monkeypatch):
    from services.auto_engine import AutoStrategyEngine
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    manager.configure(max_active_slots=3)
    rows = [
        manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                       strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                       capital_allocation=500),
        manager.create(symbol="ETHUSDT", strategy_key="brain", strategy_label="Decision Brain",
                       strategy_version="v2", timeframe="15m", risk_per_trade_pct=0.0075,
                       capital_allocation=600),
        manager.create(symbol="SOLUSDT", strategy_key="donchian", strategy_label="Donchian",
                       strategy_version="v3", timeframe="30m", risk_per_trade_pct=0.004,
                       capital_allocation=700),
    ]
    monkeypatch.setattr(AutoStrategyEngine, "start", lambda self: (
        setattr(self, "running", True), setattr(self, "lifecycle_state", "running"), True)[-1])

    for row in rows:
        manager.start(row.id)

    runtimes = [manager._runtime[row.id] for row in rows]
    assert manager.platform_status()["active_slots"] == 3
    assert all(runtime[0].running for runtime in runtimes)
    for component in range(4):
        assert len({id(runtime[component]) for runtime in runtimes}) == 3
    assert [(runtime[0].symbols, runtime[0].timeframe) for runtime in runtimes] == [
        (["BTCUSDT"], "5m"), (["ETHUSDT"], "15m"), (["SOLUSDT"], "30m")]
    runtimes[0][0]._pending["BTCUSDT"] = {"price": 100}
    assert runtimes[1][0]._pending == {}
    assert runtimes[2][0]._pending == {}


def test_backend_restart_restores_all_workers_with_their_own_cursor(monkeypatch):
    from services.auto_engine import AutoStrategyEngine
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    manager.configure(max_active_slots=3)
    rows = [
        manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                       strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                       capital_allocation=500),
        manager.create(symbol="ETHUSDT", strategy_key="brain", strategy_label="Decision Brain",
                       strategy_version="v2", timeframe="15m", risk_per_trade_pct=0.0075,
                       capital_allocation=600),
        manager.create(symbol="SOLUSDT", strategy_key="donchian", strategy_label="Donchian",
                       strategy_version="v3", timeframe="30m", risk_per_trade_pct=0.004,
                       capital_allocation=700),
    ]
    cursors = {
        row.id: (datetime(2026, 8, 9, tzinfo=timezone.utc) + timedelta(minutes=index * 15)).isoformat()
        for index, row in enumerate(rows)
    }
    for row in rows:
        manager.store.save_market_state(row.id, last_processed_candle_timestamp=cursors[row.id])
        row.state, row.desired_running = "running", True
        manager.store.save(row)
    manager.store.save_pending_orders(rows[0].id, {
        "BTCUSDT": {"side": "BUY", "price": 100.0, "target": 103.0,
                    "ttl": 2, "payload": {"symbol": "BTCUSDT", "stop": 99.0},
                    "decision_id": "decision-one"},
    })
    manager.store.save_market_state(rows[0].id, last_processed_candle_timestamp=cursors[rows[0].id])

    monkeypatch.setattr(AutoStrategyEngine, "start", lambda self: (
        setattr(self, "running", True), setattr(self, "lifecycle_state", "running"), True)[-1])
    restored_manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    restored = restored_manager.restore_desired_instances()

    assert set(restored) == {row.id for row in rows}
    assert restored_manager.platform_status()["active_slots"] == 3
    for row in rows:
        engine = restored_manager._runtime[row.id][0]
        assert engine.last_processed_candle == cursors[row.id]
        assert engine.symbols == [row.symbol]
        assert engine.timeframe == row.timeframe
    assert set(restored_manager._runtime[rows[0].id][0]._pending) == {"BTCUSDT"}
    assert restored_manager._runtime[rows[1].id][0]._pending == {}
    assert restored_manager._runtime[rows[2].id][0]._pending == {}


def test_deleting_one_stopped_instance_does_not_affect_another():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    first = manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                           strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                           capital_allocation=500)
    second = manager.create(symbol="ETHUSDT", strategy_key="brain", strategy_label="Decision Brain",
                            strategy_version="v2", timeframe="15m", risk_per_trade_pct=0.0075,
                            capital_allocation=600)
    manager.store.save_market_state(second.id, last_processed_candle_timestamp="2026-08-09T00:15:00+00:00")

    assert manager.delete(first.id) == first.id

    assert [row["id"] for row in manager.list()] == [second.id]
    assert manager.store.market_state(second.id)["last_processed_candle_timestamp"] == "2026-08-09T00:15:00+00:00"
    with ledger._lock:
        assert ledger._c.execute("SELECT COUNT(*) FROM instance_market_state WHERE instance_id=?", (first.id,)).fetchone()[0] == 0
        assert ledger._c.execute("SELECT COUNT(*) FROM instance_market_state WHERE instance_id=?", (second.id,)).fetchone()[0] == 1


def test_deleting_errored_instance_closes_its_inactive_websocket_resource():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                              strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=500)

    class InactiveEngine:
        running = False

        def __init__(self):
            self.stop_reason = None
            self.ws_feed = self
            self.feed_stopped = False

        def stop(self, reason=None):
            if reason is None:
                self.feed_stopped = True
            else:
                self.stop_reason = reason

    engine = InactiveEngine()
    paper = type("Paper", (), {"equity_listener": lambda _value: None})()
    engine._lifecycle_callback = lambda _event: None
    engine._candle_checkpoint = lambda _timestamp: None
    engine._pending_orders_checkpoint = lambda _pending: None
    manager._runtime[instance.id] = (engine, paper, object(), object())
    instance.state = "error"
    manager.store.save(instance)

    assert manager.delete(instance.id) == instance.id
    assert engine.stop_reason == "Trading Instance deleted"
    assert engine.feed_stopped is True
    assert engine._lifecycle_callback is None
    assert engine._candle_checkpoint is None
    assert engine._pending_orders_checkpoint is None
    assert paper.equity_listener is None
    assert instance.id not in manager._runtime
    assert manager.list() == []


def test_delete_refuses_open_position_and_preserves_instance_and_sibling():
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    first = manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                           strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                           capital_allocation=500)
    sibling = manager.create(symbol="ETHUSDT", strategy_key="brain", strategy_label="Decision Brain",
                             strategy_version="v2", timeframe="15m", risk_per_trade_pct=0.005,
                             capital_allocation=500)
    InstanceLedger(ledger, first.id).open_position(
        symbol="BTCUSDT", side="long", size=0.1, entry=100, stop=95,
    )
    manager.store.save_market_state(first.id, last_processed_candle_timestamp="2026-08-09T00:05:00+00:00")
    manager.store.save_market_state(sibling.id, last_processed_candle_timestamp="2026-08-09T00:15:00+00:00")

    with pytest.raises(ValueError, match="Close this instance's open positions"):
        manager.delete(first.id)

    assert {row["id"] for row in manager.list()} == {first.id, sibling.id}
    assert len(InstanceLedger(ledger, first.id).get_positions("open")) == 1
    assert manager.store.market_state(first.id)["last_processed_candle_timestamp"] == "2026-08-09T00:05:00+00:00"
    assert manager.store.market_state(sibling.id)["last_processed_candle_timestamp"] == "2026-08-09T00:15:00+00:00"


def test_persistence_failure_does_not_stop_or_remove_in_memory_instance(monkeypatch):
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                              strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=500)

    class InactiveEngine:
        running = False
        stop_calls = 0
        ws_feed = None

        def stop(self, _reason=None):
            self.stop_calls += 1

    engine = InactiveEngine()
    manager._runtime[instance.id] = (engine, object(), object(), object())

    def reject_delete(_instance_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(manager.store, "delete", reject_delete)
    with pytest.raises(RuntimeError, match="database unavailable"):
        manager.delete(instance.id)

    assert engine.stop_calls == 0
    assert instance.id in manager._runtime
    assert list(manager._instances) == [instance.id]


def test_two_real_forward_workers_are_alive_at_the_same_time():
    """Runtime proof: actual worker threads, not merely two database rows."""
    from bot.types import Bar
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=0.01)
    manager.configure(max_active_slots=2)
    btc = manager.create(symbol="BTCUSDT", strategy_key="supertrend", strategy_label="Supertrend",
                         strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                         capital_allocation=500)
    eth = manager.create(symbol="ETHUSDT", strategy_key="brain", strategy_label="Decision Brain",
                         strategy_version="v2", timeframe="15m", risk_per_trade_pct=0.005,
                         capital_allocation=500)

    def forward_bars(_symbol, timeframe, limit):
        minutes = 5 if timeframe == "5m" else 15
        forming = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        forming -= timedelta(minutes=forming.minute % minutes)
        start = forming - timedelta(minutes=minutes * (limit - 1))
        return ([Bar(start + timedelta(minutes=minutes * index), 100, 101, 99, 100, 1)
                 for index in range(limit)], "live (test)")

    manager.forward_fetcher = forward_bars
    try:
        manager.start(btc.id)
        manager.start(eth.id)
        deadline = time.time() + 2
        while time.time() < deadline:
            states = [manager._runtime[row.id][0].lifecycle_state for row in (btc, eth)]
            if states == ["running", "running"]:
                break
            time.sleep(0.01)
        first_engine, second_engine = manager._runtime[btc.id][0], manager._runtime[eth.id][0]
        assert first_engine.running and second_engine.running
        assert first_engine.lifecycle_state == second_engine.lifecycle_state == "running"
        assert first_engine._thread is not second_engine._thread
        assert first_engine._thread is not None and first_engine._thread.is_alive()
        assert second_engine._thread is not None and second_engine._thread.is_alive()
        assert manager.platform_status()["active_slots"] == 2
        assert manager.store.market_state(btc.id)["last_processed_candle_timestamp"] is not None
        assert manager.store.market_state(eth.id)["last_processed_candle_timestamp"] is not None
        # Warm-up history establishes each cursor but is never replayed as a
        # signal/trade. Repeated provider windows remain duplicate-only.
        assert first_engine.stats["bars"] == second_engine.stats["bars"] == 0
        assert InstanceLedger(ledger, btc.id).get_paper_trades() == []
        assert InstanceLedger(ledger, eth.id).get_paper_trades() == []
    finally:
        manager.stop(btc.id)
        manager.stop(eth.id)
