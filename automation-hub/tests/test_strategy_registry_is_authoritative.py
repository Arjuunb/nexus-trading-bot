"""The backend decides which strategies exist; the dashboard only renders it.

Every claim below is checkable against the code it describes, because the
point of the registry is that a strategy appears in the creation screen only
when the repository can back it up.
"""
import pytest

from services import strategy_registry as registry
from services.strategy_factory import make_builtin_strategy


def test_every_registry_entry_maps_to_a_constructible_strategy():
    """No entry may exist merely because a file with that name does."""
    for entry in registry.all_entries():
        strategy = make_builtin_strategy(entry.strategy_id, "BTCUSDT")
        assert strategy is not None
        assert getattr(strategy, "label", None)


def test_production_strategies_carry_an_immutable_version():
    """A paper record attributed to a bare label is not reproducible."""
    for entry in registry.production_entries():
        assert entry.version != "unversioned", entry.strategy_id


def test_research_strategies_state_why_they_are_not_production():
    for entry in registry.all_entries():
        if entry.lifecycle != registry.PRODUCTION:
            assert entry.lifecycle_reason, entry.strategy_id


def test_declared_decision_timeframe_matches_the_registry():
    """A strategy that refuses a timeframe at start() must not offer it."""
    for entry in registry.all_entries():
        strategy = make_builtin_strategy(entry.strategy_id, "BTCUSDT")
        required = getattr(strategy, "decision_timeframe", None)
        if required:
            assert tuple(entry.supported_timeframes) == (required,), entry.strategy_id


def test_every_supported_timeframe_has_a_native_mtf_policy():
    from services.mtf_policy import policy_for

    for entry in registry.all_entries():
        for timeframe in entry.supported_timeframes:
            assert policy_for(timeframe)


def test_only_production_strategies_are_selectable_for_a_new_instance():
    allowed, _reason = registry.selectable_for_new_instance("brain")
    assert allowed is True

    blocked, reason = registry.selectable_for_new_instance("ema")
    assert blocked is False
    assert "RESEARCH_ONLY" in reason

    unknown, reason = registry.selectable_for_new_instance("does_not_exist")
    assert unknown is False
    assert "Unknown strategy" in reason


def test_donchian_reports_its_pinned_version_not_unversioned():
    """The hand-maintained catalog omitted it, so the UI offered 'unversioned'
    for a strategy that has a pinned 1.0.0 and a signal fixture."""
    from strategies.builtin_versions import builtin_strategy_version

    entry = registry.entry("donchian")
    assert entry.version == builtin_strategy_version("donchian") == "1.0.0"


def test_legacy_catalog_is_derived_from_the_registry():
    import webhook_api

    assert ([row["key"] for row in webhook_api._STRATEGY_CATALOG]
            == [entry.strategy_id for entry in registry.all_entries()])
    for row in webhook_api._STRATEGY_CATALOG:
        assert row["version"] == registry.entry(row["key"]).version


def test_options_endpoint_offers_production_strategies_only(monkeypatch):
    pytest.importorskip("fastapi")
    from data.ledger import SqliteLedger
    from routers import instances as instance_api
    from services.trading_instances import TradingInstanceManager

    def factory(_key, symbol):
        from strategies.brain_strategy import DecisionBrain
        return DecisionBrain(symbol)

    manager = TradingInstanceManager(SqliteLedger(":memory:"), strategy_factory=factory,
                                     live=False, live_poll_s=60)
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)
    options = instance_api.instance_options()

    offered = {row["key"] for row in options["strategies"]}
    assert offered == {entry.strategy_id for entry in registry.production_entries()}
    assert "ema" not in offered and "smc" not in offered
    # The full registry still travels with the response so an operator can see
    # why something is absent rather than assume it was lost.
    assert {row["strategy_id"] for row in options["strategy_registry"]} == {
        entry.strategy_id for entry in registry.all_entries()}
    for row in options["strategies"]:
        assert row["versions"] and row["versions"][0] != "unversioned"


def test_creating_an_instance_with_a_research_strategy_is_refused(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from data.ledger import SqliteLedger
    from routers import instances as instance_api
    from services.trading_instances import TradingInstanceManager

    def factory(_key, symbol):
        from strategies.brain_strategy import DecisionBrain
        return DecisionBrain(symbol)

    manager = TradingInstanceManager(SqliteLedger(":memory:"), strategy_factory=factory,
                                     live=False, live_poll_s=60)
    monkeypatch.setattr(instance_api._wa, "instance_manager", manager)
    monkeypatch.setattr(instance_api._wa, "_check_secret", lambda _secret: None)

    with pytest.raises(HTTPException) as refused:
        instance_api.create_instance(instance_api.InstanceCreate(
            symbol="BTCUSDT", strategy="ema", timeframe="5m",
            risk_per_trade_pct=0.005, capital_allocation=500))
    assert refused.value.status_code == 400
    assert "RESEARCH_ONLY" in str(refused.value.detail)


def test_an_existing_instance_on_a_demoted_strategy_keeps_running(monkeypatch):
    """A packaging decision must never stop a worker holding a paper position."""
    from data.ledger import SqliteLedger
    from services.trading_instances import TradingInstanceManager

    def factory(key, symbol):
        return make_builtin_strategy(key, symbol)

    manager = TradingInstanceManager(SqliteLedger(":memory:"), strategy_factory=factory,
                                     live=False, live_poll_s=60)
    instance = manager.create(symbol="BTCUSDT", strategy_key="ema",
                              strategy_label="EMA Crossover", strategy_version="v1",
                              timeframe="5m", risk_per_trade_pct=0.005,
                              capital_allocation=500)
    manager.start(instance.id)

    row = manager.status(instance.id)
    assert row["strategy_lifecycle"] == registry.RESEARCH_ONLY
    assert manager._runtime[instance.id][0] is not None
    manager.stop(instance.id)
