"""Reconciliation blocks rather than pretends, and metrics tell the difference
between "no setup" and "never evaluated".
"""
import pytest

from data.ledger import SqliteLedger
from services.instance_metrics import instance_metrics, platform_metrics
from services.instance_reconciliation import BLOCKING, reconcile
from services.trading_instances import TradingInstanceManager


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


@pytest.fixture
def fast_worker(monkeypatch):
    from services.auto_engine import AutoStrategyEngine
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1])


def _manager(path=":memory:"):
    ledger = SqliteLedger(path)
    return ledger, TradingInstanceManager(ledger, strategy_factory=_factory,
                                          live=False, live_poll_s=60)


def _create(manager, symbol="BTCUSDT"):
    return manager.create(symbol=symbol, strategy_key="brain",
                          strategy_label="Decision Brain", strategy_version="v1",
                          timeframe="5m", risk_per_trade_pct=0.005,
                          capital_allocation=1_000)


def _park_thread(manager, instance_id):
    """Give the fake worker a live thread.

    The supervisor and reconciliation both define "alive" as a running engine
    WITH a live thread, which is the whole point -- an engine object whose
    thread is gone is exactly the dead worker this platform used to report as
    running. Tests that need an alive worker therefore need a real thread.
    """
    import threading
    idle = threading.Event()
    engine = manager._runtime[instance_id][0]
    engine._thread = threading.Thread(target=idle.wait, daemon=True)
    engine._thread.start()
    return idle


def _checks(result):
    return {item.check for item in result.findings if item.severity == BLOCKING}


def test_a_clean_instance_reconciles_without_findings(fast_worker):
    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    idle = _park_thread(manager, instance.id)

    assert reconcile(manager, instance.id).ok is True
    idle.set()


def test_an_open_position_without_its_trade_row_blocks(fast_worker):
    ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    # A position written without the trade row that records it -- the shape a
    # process death between two writes leaves behind.
    idle = _park_thread(manager, instance.id)
    ledger.open_position(symbol="BTCUSDT", side="long", size=0.01, entry=100.0,
                         stop=95.0, instance_id=instance.id,
                         simulation_session_id=instance.simulation_session_id)

    result = reconcile(manager, instance.id)
    idle.set()

    assert result.blocked is True
    assert "position_without_trade" in _checks(result)


def test_durable_state_claiming_running_with_no_worker_blocks():
    _ledger, manager = _manager()
    instance = _create(manager)
    instance.state = "running"
    manager.store.save(instance)

    assert "worker_matches_state" in _checks(reconcile(manager, instance.id))
    # ...but not at startup, where no worker is expected yet and demanding one
    # would block every clean restart.
    assert reconcile(manager, instance.id, expect_worker=False).ok is True


def test_a_worker_running_for_a_deleted_instance_blocks():
    _ledger, manager = _manager()
    result = reconcile(manager, "an-instance-that-was-deleted")
    assert result.blocked is True
    assert "instance_exists" in _checks(result)


def test_a_lease_held_by_another_process_blocks(tmp_path, fast_worker):
    path = str(tmp_path / "shared.db")
    _ledger, manager = _manager(path)
    instance = _create(manager)
    manager.start(instance.id)
    idle = _park_thread(manager, instance.id)
    with manager.ledger._lock:
        manager.ledger._c.execute(
            "UPDATE instance_worker_leases SET worker_id='elsewhere'")
        manager.ledger._c.commit()

    assert "single_execution_owner" in _checks(reconcile(manager, instance.id))
    idle.set()


def test_a_quarantined_intent_blocks(fast_worker):
    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    idle = _park_thread(manager, instance.id)
    manager.store.save_pending_orders(instance.id, {
        "forward_paper_intents": {}, "strategy_limit_intents": {},
        "quarantined_intents": {"BTCUSDT": {"alert_id": "stale"}}})

    assert "intent_ownership" in _checks(reconcile(manager, instance.id))
    idle.set()


def test_startup_blocks_an_instance_whose_records_disagree(tmp_path, fast_worker):
    path = str(tmp_path / "l.db")
    ledger, manager = _manager(path)
    instance = _create(manager)
    manager.start(instance.id)
    manager.shutdown()
    ledger.open_position(symbol="BTCUSDT", side="long", size=0.01, entry=100.0,
                         stop=95.0, instance_id=instance.id,
                         simulation_session_id=instance.simulation_session_id)

    reopened = TradingInstanceManager(ledger, strategy_factory=_factory,
                                      live=False, live_poll_s=60)
    restored = reopened.restore_desired_instances()

    assert restored == []
    blocked = reopened._instances[instance.id]
    assert blocked.state == "blocked"
    assert "RECONCILIATION_FAILED" in blocked.last_error
    # The intent is kept: this needs a person, not a silent forget.
    assert blocked.desired_running is True
    # And a blocked instance never reads as executing.
    row = reopened.status(instance.id)
    assert row["runtime_status"] == "BLOCKED"
    assert row["execution_status"] == "DISABLED"


def test_the_supervisor_does_not_retry_a_blocked_instance(fast_worker):
    from services.instance_supervisor import InstanceSupervisor

    _ledger, manager = _manager()
    instance = _create(manager)
    instance.desired_running, instance.state = True, "blocked"
    instance.last_error = "RECONCILIATION_FAILED: open position without trade"
    manager.store.save(instance)

    report = InstanceSupervisor(manager, interval_s=60).sweep()

    assert [row["action"] for row in report] == ["blocked"]
    assert instance.id not in manager._runtime


# --------------------------------------------------- configuration revision
def test_editing_configuration_bumps_the_revision(fast_worker):
    _ledger, manager = _manager()
    instance = _create(manager)
    assert instance.config_revision == 1

    manager.update_configuration(instance.id, risk_per_trade_pct=0.004)

    assert manager._instances[instance.id].config_revision == 2


def test_status_reports_a_worker_running_an_older_revision(fast_worker):
    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    idle = _park_thread(manager, instance.id)

    assert manager.status(instance.id)["configuration_revision"]["stale"] is False

    # An edit the running worker has not adopted.
    instance.config_revision = 7
    manager.store.save(instance)
    revision = manager.status(instance.id)["configuration_revision"]

    assert revision["configured"] == 7
    assert revision["running"] == 1
    assert revision["stale"] is True
    idle.set()


# ------------------------------------------------------------- metrics
def test_metrics_separate_no_setup_from_never_evaluated(fast_worker):
    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    engine = manager._runtime[instance.id][0]

    never_evaluated = instance_metrics(manager, instance.id)
    assert never_evaluated["strategy_evaluation_count"] == 0
    assert never_evaluated["signals_generated"] == 0

    # The strategy has now looked at candles and found nothing. Same zero
    # trades, completely different meaning.
    engine.stats["bars"] = 288
    looked_and_found_nothing = instance_metrics(manager, instance.id)
    assert looked_and_found_nothing["strategy_evaluation_count"] == 288
    assert looked_and_found_nothing["signals_generated"] == 0


def test_platform_metrics_expose_every_named_counter(fast_worker):
    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)

    metrics = platform_metrics(manager)

    for key in ("active_instances", "running_workers", "market_connections",
                "active_subscriptions", "reconnect_count", "stale_feed_count",
                "strategy_evaluation_count", "signals_generated",
                "orders_generated", "orders_rejected", "queue_depth",
                "max_market_message_age_seconds"):
        assert key in metrics, key
    assert metrics["active_instances"] == 1
    assert metrics["running_workers"] == 0        # the fake worker has no thread
    assert metrics["instances"][0]["instance_id"] == instance.id
    assert metrics["process"]["pid"] > 0


def test_platform_metrics_are_owner_scoped(fast_worker):
    _ledger, manager = _manager()
    manager.create(symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
                   strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                   capital_allocation=500, owner_id="user-a")
    manager.create(symbol="ETHUSDT", strategy_key="brain", strategy_label="Decision Brain",
                   strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
                   capital_allocation=500, owner_id="user-b")

    assert platform_metrics(manager, owner_id="user-a")["active_instances"] == 1
    assert platform_metrics(manager)["active_instances"] == 2


def test_runtime_routes_are_not_swallowed_by_the_instance_id_parameter():
    """FastAPI matches in registration order.

    "/instances/{instance_id}/metrics" declared first would capture the literal
    segment "runtime" and answer 404 for "/instances/runtime/metrics", which is
    exactly what happened before these were moved above the parameter routes.
    """
    pytest.importorskip("fastapi")
    from routers import instances as instance_api

    paths = [route.path for route in instance_api.router.routes]
    for literal, parameterised in (("/instances/runtime/metrics", "/instances/{instance_id}/metrics"),
                                   ("/instances/runtime/health", "/instances/{instance_id}/status")):
        assert paths.index(literal) < paths.index(parameterised), literal


def test_the_status_endpoint_carries_the_configuration_revision(monkeypatch, fast_worker):
    pytest.importorskip("fastapi")
    from routers import instances as instance_api

    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)

    row = instance_api.instance_status(instance.id)

    assert row["configuration_revision"]["configured"] == 1
    assert "worker_id" in row["worker"]


def test_the_runtime_health_route_answers(monkeypatch, fast_worker):
    """It had no request parameter after being moved, so it raised NameError.

    The route that answers "is the backend running my instances?" was a 500
    for every caller, and nothing covered it.
    """
    pytest.importorskip("fastapi")
    from routers import instances as instance_api

    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)

    payload = instance_api.instance_runtime_health()

    assert payload["max_active_slots"] >= 3
    assert [row["instance_id"] for row in payload["workers"]] == [instance.id]
    assert "supervisor" in payload and "market_data_channels" in payload


def test_every_instance_route_answers_without_a_request_object(monkeypatch, fast_worker):
    """Direct callers pass no Request; none of these may raise on that."""
    pytest.importorskip("fastapi")
    from routers import instances as instance_api

    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)

    for call in (lambda: instance_api.list_instances(),
                 lambda: instance_api.instance_detail(instance.id),
                 lambda: instance_api.instance_status(instance.id),
                 lambda: instance_api.instance_positions(instance.id),
                 lambda: instance_api.instance_orders(instance.id),
                 lambda: instance_api.instance_metrics(instance.id),
                 lambda: instance_api.instance_trades(instance.id),
                 lambda: instance_api.instance_logs(instance.id),
                 lambda: instance_api.instance_open_positions(instance.id),
                 lambda: instance_api.instance_reconciliation(instance.id),
                 lambda: instance_api.instance_runtime_health(),
                 lambda: instance_api.instance_runtime_metrics()):
        assert call() is not None


def test_editing_a_stopped_instance_does_not_lose_its_runtime(fast_worker):
    """halt_runtime pops the runtime; the live-apply fallback must re-check.

    A rebuild for a stopped or errored instance takes neither restart branch,
    so indexing the popped runtime raised KeyError and left the instance
    persisted as "starting" with no worker.
    """
    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    manager.stop(instance.id)                 # leaves a stale runtime entry
    assert instance.id in manager._runtime

    updated = manager.update_configuration(instance.id, capital_allocation=750)

    assert updated.capital_allocation == 750
    assert manager._instances[instance.id].state != "starting"


def test_a_configuration_edit_is_not_logged_as_an_instance_error(fast_worker):
    _ledger, manager = _manager()
    instance = _create(manager)
    manager.start(instance.id)
    manager.update_configuration(instance.id, timeframe="15m")

    import json
    events = [json.loads(row["message"][len("instance_event "):])
              for row in manager.store.engine_logs(instance.id, 200)
              if row["message"].startswith("instance_event ")]
    rebuilds = [row for row in events
                if "configuration change" in str(row.get("detail", ""))]
    assert rebuilds, "the rebuild was not recorded at all"
    assert all(row["event"] != "INSTANCE_ERROR" for row in rebuilds)
    assert manager._instances[instance.id].last_error == ""
