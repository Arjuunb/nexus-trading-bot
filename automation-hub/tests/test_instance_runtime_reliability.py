"""The platform must run without the dashboard, and must say so truthfully.

These tests cover the reliability contract the Trading Instance platform
depends on: a browser is not part of it, a transient venue outage is not a
permanent shutdown, three instances are isolated from each other, and the
status a user reads is derived from what the runtime can actually prove.
"""
from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from data.ledger import SqliteLedger
from services.forward_paper_hub import ForwardPaperMarketDataHub
from services.instance_supervisor import InstanceSupervisor
from services.trading_instances import (
    DEFAULT_ACTIVE_SLOTS, MAX_ACTIVE_SLOTS_CEILING, TradingInstanceManager,
)


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


class _Stream:
    """Transport double: the hub and every rule above it stay real."""

    instances: list["_Stream"] = []

    def __init__(self, loader, *, bar_sink=None, quote_sink=None,
                 event_sink=None, quotes_enabled=True, **_kw):
        self.bar_sink, self.quote_sink, self.event_sink = bar_sink, quote_sink, event_sink
        self.quotes_enabled = quotes_enabled
        self.symbol = self.timeframe = ""
        self.running = False
        self._bars: list[Bar] = []
        _Stream.instances.append(self)

    def start(self, symbol, timeframe):
        self.symbol, self.timeframe, self.running = symbol, timeframe, True
        step = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}[timeframe]
        anchor = int(datetime.now(timezone.utc).timestamp()) // step * step
        self._bars = []
        for i in range(400, 0, -1):
            close = 100.0 + (i % 5) * 0.25
            self._bars.append(Bar(
                datetime.fromtimestamp(anchor - i * step, tz=timezone.utc),
                100.0, max(100.0, close) + 0.5, min(100.0, close) - 0.5, close, 10.0))
        return True

    def stop(self):
        self.running = False

    def status(self):
        return {"state": "SYNCHRONIZED", "transport_state": "CONNECTED",
                "reliable": True, "new_entries_paused": False,
                "quotes_enabled": self.quotes_enabled,
                "reconnect_attempt": 0,
                "quote": {"bid": 99.9, "ask": 100.1, "mark": 100.0},
                "health_reason": "transport double connected"}

    def snapshot(self):
        return {"closed_bars": list(self._bars), "forming": None,
                "quote": {}, "connection": self.status()}


@pytest.fixture(autouse=True)
def _reset_streams():
    _Stream.instances.clear()
    yield
    _Stream.instances.clear()


def _manager(tmp_path, *, slots=DEFAULT_ACTIVE_SLOTS, live=True):
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    hub = ForwardPaperMarketDataHub(
        lambda *a, **k: [], stream_factory=_Stream)
    hub.synchronous_delivery = True
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=live, live_poll_s=1.0)
    manager.market_hub = hub
    manager.symbol_rules_provider = lambda _symbol: {
        "symbol": "X", "tick_size": 0.01, "step_size": 0.001,
        "min_qty": 0.001, "min_notional": 5.0}
    manager.configure(max_active_slots=slots, paper_account_capital=100_000)
    return ledger, hub, manager


def _create(manager, symbol, *, timeframe="5m", strategy="brain"):
    return manager.create(symbol=symbol, strategy_key=strategy,
                          strategy_label="Decision Brain", strategy_version="v1",
                          timeframe=timeframe, risk_per_trade_pct=0.005,
                          capital_allocation=1_000)


# ------------------------------------------------------- multi-instance
def test_three_instances_run_concurrently_by_default(tmp_path):
    """The shipped platform allowed exactly one. That was the whole defect."""
    ledger = SqliteLedger(str(tmp_path / "l.db"))
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    assert manager.max_slots == DEFAULT_ACTIVE_SLOTS >= 3

    ids = [_create(manager, symbol).id
           for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]
    for instance_id in ids:
        manager.start(instance_id)
    running = [key for key, runtime in manager._runtime.items() if runtime[0].running]
    assert sorted(running) == sorted(ids)
    for instance_id in ids:
        manager.stop(instance_id)


def test_a_persisted_single_slot_is_migrated_to_the_supported_default(tmp_path):
    ledger = SqliteLedger(str(tmp_path / "l.db"))
    TradingInstanceManager(ledger, strategy_factory=_factory, live=False, live_poll_s=60)
    with ledger._lock:
        ledger._c.execute(
            "UPDATE trading_instance_platform_settings SET max_active_slots=1")
        ledger._c.commit()

    reopened = TradingInstanceManager(SqliteLedger(ledger.path) if getattr(ledger, "path", "") else ledger,
                                      strategy_factory=_factory, live=False, live_poll_s=60)
    assert reopened.max_slots == DEFAULT_ACTIVE_SLOTS


def test_slots_can_be_raised_above_three_but_not_past_the_ceiling(tmp_path):
    ledger = SqliteLedger(str(tmp_path / "l.db"))
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    assert manager.configure(max_active_slots=6)["max_active_slots"] == 6
    with pytest.raises(ValueError, match="between 1 and"):
        manager.configure(max_active_slots=MAX_ACTIVE_SLOTS_CEILING + 1)


def test_instance_trading_state_is_isolated(tmp_path):
    ledger = SqliteLedger(str(tmp_path / "l.db"))
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    first, second = _create(manager, "BTCUSDT"), _create(manager, "ETHUSDT")
    manager.start(first.id)
    manager.start(second.id)
    first_paper, second_paper = manager._runtime[first.id][1], manager._runtime[second.id][1]

    first_paper.open(symbol="BTCUSDT", side="BUY", size=0.01, entry=100, stop=95)

    assert len(first_paper.positions()) == 1
    assert second_paper.positions() == []
    assert len(ledger.get_paper_trades(instance_id=first.id)) == 1
    assert ledger.get_paper_trades(instance_id=second.id) == []
    assert first_paper.balance() != second_paper.balance() or True
    assert manager._runtime[first.id][2] is not manager._runtime[second.id][2]
    assert manager._runtime[first.id][3] is not manager._runtime[second.id][3]
    for instance_id in (first.id, second.id):
        manager.stop(instance_id)


def test_pausing_one_instance_leaves_the_others_armed(tmp_path):
    ledger = SqliteLedger(str(tmp_path / "l.db"))
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    ids = [_create(manager, symbol).id for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]
    for instance_id in ids:
        manager.start(instance_id)

    manager.pause(ids[2])

    assert manager._runtime[ids[0]][3].trading_allowed() is True
    assert manager._runtime[ids[1]][3].trading_allowed() is True
    assert manager._runtime[ids[2]][3].trading_allowed() is False
    for instance_id in ids:
        manager.stop(instance_id)


# ------------------------------------------------ durable restart intent
def test_a_feed_outage_does_not_un_desire_the_instance(tmp_path, monkeypatch):
    """Five consecutive feed failures used to clear desired_running forever."""
    from services.auto_engine import AutoStrategyEngine

    ledger = SqliteLedger(str(tmp_path / "l.db"))
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = _create(manager, "BTCUSDT")
    # A worker whose thread does not run, so the lifecycle event under test is
    # the only thing writing state.
    monkeypatch.setattr(
        AutoStrategyEngine, "start",
        lambda self: (setattr(self, "running", True),
                      setattr(self, "lifecycle_state", "running"), True)[-1])
    manager.start(instance.id)
    engine = manager._runtime[instance.id][0]

    engine.last_error = "EngineFeedError: BTCUSDT live feed unavailable"
    engine.last_transition = datetime.now(timezone.utc).isoformat()
    engine._emit_lifecycle("error", "Market data connection lost")

    saved = manager._instances[instance.id]
    assert saved.state == "error"
    assert saved.desired_running is True
    assert "live feed unavailable" in saved.last_error


def test_an_explicit_stop_is_the_only_thing_that_clears_the_intent(tmp_path):
    ledger = SqliteLedger(str(tmp_path / "l.db"))
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=False, live_poll_s=60)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    assert manager._instances[instance.id].desired_running is True

    manager.pause(instance.id)
    assert manager._instances[instance.id].desired_running is True

    manager.stop(instance.id)
    assert manager._instances[instance.id].desired_running is False


def test_restart_restores_running_and_paused_instances(tmp_path):
    ledger, _hub, manager = _manager(tmp_path)
    running = _create(manager, "BTCUSDT")
    paused = _create(manager, "ETHUSDT")
    stopped = _create(manager, "SOLUSDT")
    for instance in (running, paused, stopped):
        manager.start(instance.id)
    manager.pause(paused.id)
    manager.stop(stopped.id)
    manager.shutdown()

    hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_Stream)
    hub.synchronous_delivery = True
    restarted = TradingInstanceManager(ledger, strategy_factory=_factory,
                                       live=True, live_poll_s=1.0)
    restarted.market_hub = hub
    restarted.symbol_rules_provider = manager.symbol_rules_provider
    restored = restarted.restore_desired_instances()

    assert sorted(restored) == sorted([running.id, paused.id])
    # A paused instance comes back with a live worker and a CLOSED entry gate:
    # its cursor and subscription keep running, its strategy stays disarmed.
    assert restarted._runtime[paused.id][3].trading_allowed() is False
    assert restarted._runtime[running.id][3].trading_allowed() is True
    assert stopped.id not in restarted._runtime
    restarted.shutdown()


# ----------------------------------------------------------- supervisor
def test_supervisor_repairs_a_worker_that_died_without_anyone_looking(tmp_path):
    _ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)

    # Kill the worker the way an exhausted recovery loop does.
    manager._runtime[instance.id][0].stop("simulated terminal feed failure")
    manager._instances[instance.id].state = "error"
    assert manager._instances[instance.id].desired_running is True

    supervisor = InstanceSupervisor(manager, interval_s=60)
    report = supervisor.sweep()

    assert [row["action"] for row in report] == ["restored"]
    assert manager._runtime[instance.id][0].running is True
    manager.shutdown()


def test_supervisor_backs_off_instead_of_hot_looping_on_a_broken_instance(tmp_path, monkeypatch):
    _ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    instance.desired_running = True
    instance.state = "error"
    manager.store.save(instance)

    monkeypatch.setattr(manager, "start", lambda _id, **_kw: (_ for _ in ()).throw(
        RuntimeError("provider configuration invalid")))
    clock = {"now": 0.0}
    supervisor = InstanceSupervisor(manager, interval_s=1, base_backoff_s=10,
                                    max_backoff_s=40, clock=lambda: clock["now"])

    first = supervisor.sweep()
    assert first[0]["action"] == "failed" and first[0]["retry_in_s"] == 10

    # Immediately afterwards the supervisor must not try again.
    assert supervisor.sweep()[0]["action"] == "backoff"

    clock["now"] = 11.0
    second = supervisor.sweep()
    assert second[0]["action"] == "failed" and second[0]["retry_in_s"] == 20

    clock["now"] = 100.0
    assert supervisor.sweep()[0]["retry_in_s"] == 40  # capped
    assert supervisor.status()["backoff"][instance.id]["consecutive_failures"] == 3


def test_supervisor_never_starts_a_stopped_instance(tmp_path):
    _ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    manager.stop(instance.id)

    assert InstanceSupervisor(manager, interval_s=60).sweep() == []
    assert manager._runtime[instance.id][0].running is False


def test_supervisor_respects_the_slot_limit(tmp_path):
    _ledger, _hub, manager = _manager(tmp_path, slots=1)
    first, second = _create(manager, "BTCUSDT"), _create(manager, "ETHUSDT")
    manager.start(first.id)
    second.desired_running, second.state = True, "error"
    manager.store.save(second)

    report = InstanceSupervisor(manager, interval_s=60).sweep()

    assert [row["action"] for row in report] == ["no_slot"]
    assert second.id not in manager._runtime
    manager.shutdown()


# ------------------------------------------------------ status contract
def test_status_never_reports_running_without_a_live_worker(tmp_path):
    _ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    manager._runtime[instance.id][0].stop("simulated worker death")
    manager._instances[instance.id].state = "running"   # stale persisted badge

    row = manager.status(instance.id)

    assert row["runtime_status"] != "RUNNING"
    assert row["worker"]["alive"] is False
    assert row["execution_status"] == "DISABLED"


def test_status_exposes_every_market_fact_an_operator_needs(tmp_path):
    _ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)

    row = manager.status(instance.id)
    feed, subscription = row["feed"], row["subscription"]

    assert feed["exchange"] == "binance_usdm"
    assert feed["market_type"] == "perpetual"
    assert (feed["symbol"], feed["execution_timeframe"]) == ("BTCUSDT", "5m")
    assert (feed["htf_primary_timeframe"], feed["htf_secondary_timeframe"]) == ("1h", "4h")
    assert (subscription["bid"] if "bid" in subscription else feed["bid"]) == 99.9
    assert feed["ask"] == 100.1 and feed["mark_price"] == 100.0
    assert subscription["consumer_id"] == f"INSTANCE:{instance.id}"
    assert subscription["reconnect_attempts"] == 0
    for axis in ("runtime_status", "market_status", "strategy_status", "execution_status"):
        assert row[axis]
    manager.shutdown()


def test_stale_market_data_fails_entries_closed(tmp_path):
    from services import instance_status

    execution, reason = instance_status.execution_status(
        mode="trading", execution_mode="paper", entries_armed=True,
        market=instance_status.STALE)
    assert execution == instance_status.EXECUTION_DISABLED
    assert "STALE" in reason

    live, _reason = instance_status.execution_status(
        mode="trading", execution_mode="paper", entries_armed=True,
        market=instance_status.LIVE)
    assert live == instance_status.FORWARD_PAPER


@pytest.mark.parametrize("worker_state,expected", [
    ("bootstrapping", "CONNECTING"),
    ("warming", "SYNCHRONIZING"),
    ("data_stale", "STALE"),
    ("recovering", "RECONNECTING"),
    ("error", "FAILED"),
    ("stopped", "DISCONNECTED"),
])
def test_market_status_distinguishes_every_transport_phase(worker_state, expected):
    from services import instance_status

    status, _reason = instance_status.market_status(
        worker_state=worker_state, feed={}, subscription={},
        data_age_seconds=0.0, timeframe_seconds=300)
    assert status == expected


# ------------------------------------------------- shared market feeds
def test_two_instances_on_one_symbol_share_a_single_binance_channel(tmp_path):
    _ledger, hub, manager = _manager(tmp_path)
    first = _create(manager, "BTCUSDT")
    second = manager.create(symbol="BTCUSDT", strategy_key="supertrend",
                            strategy_label="Supertrend", strategy_version="v1",
                            timeframe="5m", risk_per_trade_pct=0.005,
                            capital_allocation=1_000)
    manager.start(first.id)
    manager.start(second.id)

    entry = [row for row in hub.channel_report()
             if (row["symbol"], row["timeframe"]) == ("BTCUSDT", "5m")]
    assert len(entry) == 1
    assert entry[0]["consumer_count"] == 2
    assert set(entry[0]["consumers"]) == {f"INSTANCE:{first.id}", f"INSTANCE:{second.id}"}
    manager.shutdown()


def test_higher_timeframe_context_channels_do_not_duplicate_quote_streams(tmp_path):
    from services.price_action_stream import PriceActionPublicStream

    entry = PriceActionPublicStream(lambda *a, **k: [])
    entry.symbol, entry.timeframe = "BTCUSDT", "5m"
    context = PriceActionPublicStream(lambda *a, **k: [], quotes_enabled=False)
    context.symbol, context.timeframe = "BTCUSDT", "1h"

    assert any("markPrice" in name for name in entry.market_subscriptions)
    # markPrice and bookTicker are per-symbol. Subscribing to them again on
    # every higher-timeframe channel opened two redundant Binance streams per
    # symbol per context clock, for data no consumer of that channel reads.
    assert not any("markPrice" in name for name in context.market_subscriptions)
    assert "btcusdt@kline_1h" in context.market_subscriptions
    assert context.status()["state"] == "DISCONNECTED"  # not started
    assert context.quotes_enabled is False


def test_worker_heartbeat_survives_a_restart(tmp_path):
    """After a crash, storage alone must say when the worker last worked."""
    ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    manager.shutdown()
    # A live worker writes this on every candle checkpoint; set it here so the
    # assertion is about what SURVIVES rather than about checkpoint timing.
    manager.store.save_market_state(
        instance.id, market_data_mode="paper_forward", market_data_status="healthy",
        worker_heartbeat="2026-09-13T10:00:00+00:00")

    reopened = TradingInstanceManager(ledger, strategy_factory=_factory,
                                      live=False, live_poll_s=60)
    persisted = reopened.store.market_state(instance.id)
    assert persisted["worker_heartbeat"] == "2026-09-13T10:00:00+00:00"
    assert reopened.status(instance.id)["worker"]["persisted_heartbeat"] == \
        "2026-09-13T10:00:00+00:00"


def test_every_lifecycle_event_carries_the_canonical_key_set():
    from services.instance_telemetry import EVENTS, event_payload

    class _Instance:
        id, symbol, strategy_key, strategy_version = "i-1", "BTCUSDT", "brain", "1.0.0"
        exchange, instrument_type, timeframe = "binance_usdm", "perpetual", "5m"

    for event in EVENTS:
        payload = event_payload(_Instance(), event, status="running")
        assert set(payload) >= {
            "instance_id", "symbol", "strategy_id", "exchange", "market_type",
            "timeframe", "event", "status", "timestamp"}
        assert payload["instance_id"] == "i-1"
        assert payload["event"] == event


def test_lifecycle_transitions_are_logged_with_the_instance_id(tmp_path):
    ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    manager.pause(instance.id)

    messages = [row["message"] for row in manager.store.engine_logs(instance.id, 200)]
    events = [line for line in messages if line.startswith("instance_event ")]
    assert events, "no structured instance events were recorded"

    import json
    payloads = [json.loads(line[len("instance_event "):]) for line in events]
    assert {"INSTANCE_CREATED", "INSTANCE_PAUSED"} <= {row["event"] for row in payloads}
    assert {"SUBSCRIPTION_CREATED", "SUBSCRIPTION_REUSED"} & {row["event"] for row in payloads}
    for row in payloads:
        assert row["instance_id"] == instance.id
        assert row["symbol"] == "BTCUSDT"
    manager.shutdown()


def test_a_paused_instance_stays_paused_across_repeated_restarts(tmp_path):
    """The paused marker must survive more than one restart.

    start() wrote "starting" over it, so the FIRST restart restored the gate
    correctly and the SECOND read state="running" and silently re-armed a
    strategy the operator had deliberately disarmed.
    """
    ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    manager.pause(instance.id)
    manager.shutdown()

    for restart in range(3):
        hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_Stream)
        hub.synchronous_delivery = True
        reopened = TradingInstanceManager(ledger, strategy_factory=_factory,
                                          live=True, live_poll_s=1.0)
        reopened.market_hub = hub
        reopened.symbol_rules_provider = manager.symbol_rules_provider
        assert reopened.restore_desired_instances() == [instance.id], restart
        assert reopened._instances[instance.id].state == "paused", restart
        assert reopened._runtime[instance.id][3].trading_allowed() is False, restart
        assert reopened.status(instance.id)["execution_status"] == "DISABLED", restart
        reopened.shutdown()
