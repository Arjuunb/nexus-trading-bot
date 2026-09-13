"""Closing the browser, logging out, and logging back in change nothing.

The reported symptom was "market data breaks after a new login". These tests
pin the property that makes that impossible by construction: no authentication
path touches the trading runtime, and every fact the dashboard renders comes
from durable server state rather than from a client session.
"""
import pytest

from data.ledger import SqliteLedger
from services.trading_instances import TradingInstanceManager


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


def test_no_auth_endpoint_touches_the_trading_runtime():
    """Grep-as-a-test: a future logout handler that stops instances fails here."""
    import inspect
    import app as app_module

    runtime_verbs = ("instance_manager", "instance_supervisor", "market_hub",
                     "price_action_runtime", "smc_runtime", "research_observer")
    for name in ("logout", "auth_logout", "login", "auth_login"):
        handler = getattr(app_module, name, None)
        if handler is None:
            continue
        source = inspect.getsource(handler)
        for verb in runtime_verbs:
            assert verb not in source, f"{name}() reaches into {verb}"


def test_logging_out_leaves_every_instance_running(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from services.auto_engine import AutoStrategyEngine
    import app as app_module
    import webhook_api

    # A worker whose thread does not run, so the only thing that could stop it
    # during this test is the logout request itself.
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1])
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain",
                              strategy_label="Decision Brain", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=500)
    manager.start(instance.id)
    original, webhook_api.instance_manager = webhook_api.instance_manager, manager
    try:
        # Not a context manager: leaving one fires the app's shutdown event,
        # which legitimately quiesces every worker. What is under test is the
        # logout request, not process shutdown.
        client = TestClient(app_module.app)
        assert client.post("/auth/logout").status_code == 200
        assert manager._runtime[instance.id][0].running is True
        assert manager._instances[instance.id].desired_running is True

        # And logging back in returns the same durable state, not a new one.
        assert client.get("/auth/status").status_code == 200
        assert manager._instances[instance.id].desired_running is True
        # Not RUNNING here only because this fixture's worker has no thread;
        # what matters is that neither request changed the durable intent or
        # the worker, which is what a real login/logout cycle must also do.
        assert manager.status(instance.id)["desired_running"] is True
        assert manager._runtime[instance.id][0].running is True
    finally:
        manager.stop(instance.id)
        webhook_api.instance_manager = original


def test_reading_status_is_side_effect_free_for_the_worker():
    """A dashboard poll must not be what keeps -- or stops -- a worker alive."""
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="brain",
                              strategy_label="Decision Brain", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=500)
    manager.start(instance.id)
    engine = manager._runtime[instance.id][0]
    before = (engine.running, manager._instances[instance.id].desired_running)

    for _ in range(5):
        manager.status(instance.id)

    assert (engine.running, manager._instances[instance.id].desired_running) == before
    manager.stop(instance.id)


def test_instance_configuration_is_reconstructible_from_storage_alone():
    """Everything needed to rebuild a worker survives without any session."""
    ledger = SqliteLedger(":memory:")
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="ETHUSDT", strategy_key="brain",
                              strategy_label="Decision Brain", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.004,
                              capital_allocation=750)
    manager.start(instance.id)

    reloaded = {row.id: row for row in manager.store.list()}[instance.id]

    for field in ("symbol", "strategy_key", "strategy_version", "timeframe",
                  "exchange", "instrument_type", "risk_per_trade_pct",
                  "capital_allocation", "sizing_mode", "entry_mode", "fill_model",
                  "execution_mode", "market_data_mode", "simulation_session_id",
                  "starting_equity", "desired_running"):
        assert getattr(reloaded, field) == getattr(instance, field), field
    manager.stop(instance.id)


def test_the_dashboard_has_no_strategy_list_of_its_own():
    """The frontend must read strategies from the backend, never carry a copy."""
    import pathlib
    import re

    page = (pathlib.Path(__file__).resolve().parents[2]
            / "automation-hub-dashboard" / "src" / "pages" / "TradingInstances.tsx")
    if not page.exists():
        pytest.skip("dashboard source is not part of this checkout")
    source = page.read_text()
    for strategy_id in ("price_action_rejection", "adaptive_trend_pullback",
                        "liquidity_sweep", "supertrend", "donchian"):
        assert f'"{strategy_id}"' not in source and f"'{strategy_id}'" not in source, (
            f"{strategy_id} is hardcoded in the dashboard")
    assert re.search(r"options\.data\?\.strategies", source), (
        "the creation form must render the backend's strategy list")
