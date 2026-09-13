"""Nothing in this list may silently corrupt the platform.

Each case is one of the failure modes the runtime is expected to survive:
a venue outage and its recovery, a malformed message, a duplicate
subscription, a missing higher-timeframe candle, a database error, and the
ordinary operator actions (pause, stop, delete) taken while other instances
keep trading.
"""
from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from data.ledger import SqliteLedger
from services.forward_paper_hub import ForwardPaperMarketDataHub
from services.trading_instances import TradingInstanceManager


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


class _Stream:
    def __init__(self, loader, *, bar_sink=None, quote_sink=None,
                 event_sink=None, quotes_enabled=True, **_kw):
        self.bar_sink, self.quote_sink, self.event_sink = bar_sink, quote_sink, event_sink
        self.quotes_enabled, self.running = quotes_enabled, False
        self.symbol = self.timeframe = ""
        self.starts = 0
        self._bars: list[Bar] = []

    def start(self, symbol, timeframe):
        self.symbol, self.timeframe, self.running = symbol, timeframe, True
        self.starts += 1
        step = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}[timeframe]
        anchor = int(datetime.now(timezone.utc).timestamp()) // step * step
        self._bars = [Bar(datetime.fromtimestamp(anchor - i * step, tz=timezone.utc),
                          100.0, 100.8, 99.2, 100.0 + (i % 4) * 0.2, 10.0)
                      for i in range(400, 0, -1)]
        return True

    def stop(self):
        self.running = False

    def status(self):
        return {"state": "SYNCHRONIZED", "transport_state": "CONNECTED",
                "reliable": True, "quote": {"bid": 99.9, "ask": 100.1, "mark": 100.0},
                "reconnect_attempt": 0, "health_reason": "double connected"}

    def snapshot(self):
        return {"closed_bars": list(self._bars), "forming": None,
                "quote": {}, "connection": self.status()}


def _manager(tmp_path, slots=3):
    ledger = SqliteLedger(str(tmp_path / "l.db"))
    hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_Stream)
    hub.synchronous_delivery = True
    manager = TradingInstanceManager(ledger, strategy_factory=_factory,
                                     live=True, live_poll_s=1.0)
    manager.market_hub = hub
    manager.symbol_rules_provider = lambda _s: {
        "symbol": "X", "tick_size": 0.01, "step_size": 0.001,
        "min_qty": 0.001, "min_notional": 5.0}
    manager.configure(max_active_slots=slots, paper_account_capital=100_000)
    return ledger, hub, manager


def _create(manager, symbol):
    return manager.create(symbol=symbol, strategy_key="brain",
                          strategy_label="Decision Brain", strategy_version="v1",
                          timeframe="5m", risk_per_trade_pct=0.005,
                          capital_allocation=1_000)


def test_a_duplicate_subscription_request_is_idempotent(tmp_path):
    _ledger, hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    channels_before = set(hub._channels)

    manager.start(instance.id)   # the API allows this; it must not layer a feed
    manager.start(instance.id)

    assert set(hub._channels) == channels_before
    entry = [row for row in hub.channel_report()
             if (row["symbol"], row["timeframe"]) == ("BTCUSDT", "5m")][0]
    assert entry["consumer_count"] == 1
    manager.shutdown()


def test_restarting_a_dead_worker_does_not_leak_a_second_feed(tmp_path):
    _ledger, hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    manager._runtime[instance.id][0].stop("simulated death")

    manager.start(instance.id)

    entry = [row for row in hub.channel_report()
             if (row["symbol"], row["timeframe"]) == ("BTCUSDT", "5m")]
    assert len(entry) == 1 and entry[0]["consumer_count"] == 1
    manager.shutdown()


def test_a_malformed_market_message_cannot_corrupt_the_candle_series():
    from services.price_action_stream import PriceActionPublicStream

    stream = PriceActionPublicStream(lambda *a, **k: [])
    stream.symbol, stream.timeframe = "BTCUSDT", "5m"
    before = list(stream._bars)

    for message in ({}, {"data": None}, {"data": {"e": "kline"}},
                    {"data": {"e": "kline", "k": {"x": True}}},
                    {"data": {"e": "kline", "k": {"t": "not-a-number", "x": True}}},
                    {"stream": "x", "data": {"e": "bookTicker", "b": "nope", "a": None}}):
        try:
            stream.ingest_event(message)
        except Exception as exc:  # a parse failure is allowed; corruption is not
            assert not isinstance(exc, SystemExit)
    assert list(stream._bars) == before


def test_a_missing_higher_timeframe_candle_blocks_entries_rather_than_guessing():
    from services import instance_status

    status, reason = instance_status.strategy_status(
        market=instance_status.LIVE, worker_state="running",
        warmup_bars=400, warmup_required=400, blocker=None,
        htf_ready=False, health_status="Healthy")
    assert status == instance_status.WAITING_FOR_HTF
    assert "higher-timeframe" in reason

    execution, _reason = instance_status.execution_status(
        mode="trading", execution_mode="paper", entries_armed=True,
        market=instance_status.WAITING_FOR_DATA_MARKET)
    assert execution == instance_status.EXECUTION_DISABLED


def test_a_database_error_during_a_sweep_does_not_kill_the_supervisor(tmp_path):
    from services.instance_supervisor import InstanceSupervisor

    _ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    instance.desired_running, instance.state = True, "error"
    manager.store.save(instance)
    supervisor = InstanceSupervisor(manager, interval_s=60)

    class _Locked:
        available = True

        def __getattr__(self, _name):
            raise RuntimeError("database is locked")

    real_store, manager.store = manager.store, _Locked()
    try:
        # The sweep records the failure and schedules a retry rather than
        # letting the exception end the supervisor thread.
        report = supervisor.sweep()
    finally:
        manager.store = real_store
    assert [row["action"] for row in report] == ["failed"]
    assert "database is locked" in report[0]["error"]
    assert supervisor.running is False  # never started; the thread is unharmed

    # And the next sweep, once the database answers again, still repairs it.
    supervisor._next_attempt.clear()
    assert [row["action"] for row in supervisor.sweep()] == ["restored"]
    manager.shutdown()


def test_a_degraded_store_makes_the_supervisor_stand_down_quietly(tmp_path):
    from services.instance_supervisor import InstanceSupervisor

    _ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    instance.desired_running, instance.state = True, "error"
    manager.store.save(instance)
    manager.store.available = False

    supervisor = InstanceSupervisor(manager, interval_s=60)

    assert supervisor.sweep() == []
    assert instance.id not in manager._runtime


def test_deleting_one_instance_leaves_the_others_trading(tmp_path):
    _ledger, hub, manager = _manager(tmp_path)
    keep_a, keep_b, doomed = (_create(manager, "BTCUSDT"), _create(manager, "ETHUSDT"),
                              _create(manager, "SOLUSDT"))
    for instance in (keep_a, keep_b, doomed):
        manager.start(instance.id)

    manager.stop(doomed.id)      # the UI requires this before Delete
    manager.delete(doomed.id)

    assert doomed.id not in manager._instances
    assert manager._runtime[keep_a.id][0].running is True
    assert manager._runtime[keep_b.id][0].running is True
    assert not [row for row in hub.channel_report() if row["symbol"] == "SOLUSDT"]
    manager.shutdown()


def test_deleting_an_instance_with_an_open_position_is_refused(tmp_path):
    _ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    manager._runtime[instance.id][1].ledger.open_position(
        symbol="BTCUSDT", side="long", size=0.01, entry=100.0, stop=95.0)
    manager.stop(instance.id)

    with pytest.raises(ValueError, match="open positions"):
        manager.delete(instance.id)

    assert instance.id in manager._instances
    manager.shutdown()


def test_a_third_instance_can_be_created_while_two_are_running(tmp_path):
    _ledger, _hub, manager = _manager(tmp_path)
    first, second = _create(manager, "BTCUSDT"), _create(manager, "ETHUSDT")
    manager.start(first.id)
    manager.start(second.id)

    third = _create(manager, "SOLUSDT")
    manager.start(third.id)

    running = [key for key, runtime in manager._runtime.items() if runtime[0].running]
    assert sorted(running) == sorted([first.id, second.id, third.id])
    manager.shutdown()


def test_stale_shared_candles_cannot_reach_the_wrong_instance(tmp_path):
    """One channel, two consumers, and each consumer's own delivery queue."""
    _ledger, hub, _manager_obj = _manager(tmp_path)
    seen = {"a": [], "b": []}
    first = hub.subscription("A", bar_sink=lambda bar: seen["a"].append(bar))
    second = hub.subscription("B", bar_sink=lambda bar: (_ for _ in ()).throw(
        RuntimeError("consumer B is broken")))
    assert first.start("BTCUSDT", "5m")
    assert second.start("BTCUSDT", "5m")

    channel = hub._channels[("BTCUSDT", "5m")]
    bar = Bar(datetime(2026, 9, 1, tzinfo=timezone.utc), 1, 2, 0.5, 1.5, 10)
    channel.stream.bar_sink(bar)

    # A failing consumer keeps its own pending backlog and reports itself
    # unreliable; the healthy consumer is unaffected and still receives it.
    assert seen["a"] == [bar]
    assert second.status()["reliable"] is False
    assert first.status()["reliable"] is not False
    first.stop()
    second.stop()


def test_pausing_while_the_worker_is_recovering_still_closes_the_gate(tmp_path):
    """A pause must win regardless of what the feed is doing at that moment."""
    _ledger, _hub, manager = _manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    engine = manager._runtime[instance.id][0]
    # The worker is mid-recovery: stale feed, reconnect scheduled.
    engine.lifecycle_state = "recovering"
    engine.market_data_status = "stale"

    manager.pause(instance.id)

    assert manager._runtime[instance.id][3].trading_allowed() is False
    assert manager._instances[instance.id].state == "paused"
    # The entry gate closed; the worker and its subscription stayed.
    assert manager._runtime[instance.id][0].running is True
    row = manager.status(instance.id)
    assert row["execution_status"] == "DISABLED"
    manager.shutdown()


def test_a_backend_restart_between_an_intent_and_its_fill_does_not_double_open(tmp_path):
    """The intent is durable; the position it already created is authoritative."""
    from datetime import datetime, timezone

    from execution.paper_engine import ForwardPaperExecutionEngine
    from services.trading_instances import InstanceLedger

    ledger = SqliteLedger(str(tmp_path / "l.db"))
    scoped = InstanceLedger(ledger, "inst-1")
    saved: dict = {}
    first = ForwardPaperExecutionEngine(scoped, 10_000,
                                        intents_listener=saved.update)
    first.open(symbol="BTCUSDT", side="BUY", size=0.01, entry=100.0, stop=95.0,
               alert_id="crash-1",
               sizing_context={"decision_timestamp": datetime.now(timezone.utc).isoformat()})
    quote = {"symbol": "BTCUSDT", "last": 100.0, "bid": 99.9, "ask": 100.1,
             "mark": 100.0, "sequence": 1,
             "received_at": datetime.now(timezone.utc).isoformat(),
             "event_timestamp": datetime.now(timezone.utc).isoformat(),
             "quote_event_id": "q1", "candle_id": "BINANCE_USDM:BTCUSDT:5m:1"}
    first.process_quote(quote)
    assert len(first.positions()) == 1

    # The process dies before the intent checkpoint cleared, so the successor
    # is handed an intent whose position already exists.
    successor = ForwardPaperExecutionEngine(scoped, 10_000,
                                            initial_intents=dict(saved))
    fills = successor.process_quote({**quote, "sequence": 2, "quote_event_id": "q2"})

    assert fills == []                      # reconciled, not re-opened
    assert len(successor.positions()) == 1


def test_a_duplicate_subscription_request_reuses_one_channel_per_symbol(tmp_path):
    _ledger, hub, manager = _manager(tmp_path)
    first = _create(manager, "BTCUSDT")
    second = manager.create(symbol="BTCUSDT", strategy_key="brain",
                            strategy_label="Decision Brain", strategy_version="v1",
                            timeframe="5m", risk_per_trade_pct=0.005,
                            capital_allocation=1_000)
    manager.start(first.id)
    manager.start(second.id)
    manager.start(first.id)        # repeated, must not add a consumer
    manager.start(second.id)

    entry = [row for row in hub.channel_report()
             if (row["symbol"], row["timeframe"]) == ("BTCUSDT", "5m")]
    assert len(entry) == 1
    assert entry[0]["consumer_count"] == 2
    manager.shutdown()
