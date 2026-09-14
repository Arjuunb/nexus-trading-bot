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


def test_every_strategy_declares_its_warm_up_requirement():
    """READY must mean a stated requirement was met.

    Before this, most strategies declared nothing and relied on the engine's
    generic 150-candle default happening to exceed their longest lookback.
    """
    for entry in registry.all_entries():
        assert entry.warmup_candles > 0, entry.strategy_id
        assert entry.warmup_basis, entry.strategy_id


def test_the_engine_warms_up_to_at_least_what_each_strategy_declares():
    """The declaration is checkable against the runtime, not taken on trust."""
    from services.auto_engine import AutoStrategyEngine

    engine = AutoStrategyEngine.__new__(AutoStrategyEngine)
    engine.warmup = 150
    for entry in registry.all_entries():
        engine.timeframe = entry.supported_timeframes[0]
        strategy = make_builtin_strategy(entry.strategy_id, "BTCUSDT")
        required = AutoStrategyEngine._required_warmup(engine, strategy)
        assert required >= entry.warmup_candles, (
            f"{entry.strategy_id}: engine warms {required}, registry declares "
            f"{entry.warmup_candles}")


#: Settings whose value is NOT a number of candles, identified by the unit in
#: their name. Comparing these against warmup_candles compares a duration, a
#: price fraction or a multiplier against a bar count. SMCConfig.htf_minutes is
#: the one that bites: 240 means four hours of higher-timeframe context, and
#: reading it as 240 candles demanded a warm-up the strategy has never needed
#: (measured: the engine forms its bias by bar 23 and its first setup by bar
#: 56). The bar-count settings -- *_bars, *_length, *_lookback, *_period and the
#: bare indicator periods -- all still count, so the assertion below is exactly
#: as strict as it was.
_NON_CANDLE_UNITS = ("_minutes", "_seconds", "_bps", "_pct", "_fraction",
                     "_mult", "_multiplier", "_ratio")


def _candle_lookbacks(source) -> list:
    return [value for name, value in source.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            and not name.endswith(_NON_CANDLE_UNITS)]


def test_a_declared_warm_up_covers_the_strategys_longest_lookback():
    """The declared figure must actually exceed the indicator it names."""
    for entry in registry.all_entries():
        strategy = make_builtin_strategy(entry.strategy_id, "BTCUSDT")
        lookbacks = _candle_lookbacks(getattr(strategy, "params", {}) or {})
        config = getattr(strategy, "config", None)
        if config is not None:
            lookbacks += _candle_lookbacks(vars(config))
        longest = max(lookbacks) if lookbacks else 0
        assert entry.warmup_candles >= longest, (
            f"{entry.strategy_id} declares {entry.warmup_candles} candles but has a "
            f"{longest}-candle lookback")


def test_the_options_endpoint_publishes_the_warm_up_contract(monkeypatch):
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

    registry_rows = {row["strategy_id"]: row
                     for row in instance_api.instance_options()["strategy_registry"]}

    for entry in registry.all_entries():
        row = registry_rows[entry.strategy_id]
        assert row["warmup_candles"] == entry.warmup_candles
        assert row["required_data"] == list(entry.required_data)
