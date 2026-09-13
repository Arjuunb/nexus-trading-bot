from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from data.ledger import SqliteLedger
from execution.paper_broker_v2 import PaperBrokerV2
from execution.paper_engine import ForwardPaperExecutionEngine
from services.forward_paper_hub import ForwardPaperMarketDataHub, candle_id
from services.trading_instances import InstanceLedger


class FakePublicStream:
    """No-network stream used to prove one channel fans out identical facts."""

    def __init__(self, _loader, *, bar_sink=None, quote_sink=None,
                 event_sink=None, **_kwargs):
        self.bar_sink, self.quote_sink, self.event_sink = bar_sink, quote_sink, event_sink
        self.symbol = ""
        self.timeframe = ""
        self.running = False
        self._bars = []
        self._quote = {}

    def start(self, symbol, timeframe):
        self.symbol, self.timeframe, self.running = symbol, timeframe, True
        return True

    def stop(self):
        self.running = False

    def status(self):
        return {
            "state": "SYNCHRONIZED", "transport_state": "CONNECTED",
            "reliable": True, "new_entries_paused": False,
            "health_reason": "deterministic public-feed fixture",
        }

    def snapshot(self):
        return {
            "closed_bars": list(self._bars), "forming": None,
            "quote": dict(self._quote), "connection": self.status(),
        }

    def emit_bar(self, bar):
        self._bars.append(bar)
        self.bar_sink(bar)

    def emit_quote(self, quote):
        self._quote = dict(quote)
        self.quote_sink(dict(quote))


def _hub():
    hub = ForwardPaperMarketDataHub(
        lambda *_args, **_kwargs: [], stream_factory=FakePublicStream)
    # These tests assert on fills the moment a quote is emitted, because what
    # they check is fill correctness: which engines fill, at what price, from
    # which quote. Production delivers to each consumer on its own single
    # worker so a sink cannot stall the socket reader, and that is a schedule
    # change, not a trading one. Keeping delivery synchronous here keeps the
    # assertions about the trade rather than about timing.
    hub.synchronous_delivery = True
    return hub


def _broker(path, account_type):
    return PaperBrokerV2(
        path, starting_balance=10_000, account_type=account_type,
        execution_engine=account_type, fee_rate=0, spread_bps=0,
        slippage_bps=0, participation_rate=1,
    )


def test_native_mtf_fetches_keep_primary_and_parallel_channels_attached():
    hub = _hub()
    subscription = hub.subscription("INSTANCE:one")
    assert subscription.start("BTCUSDT", "5m")
    sample = Bar(datetime(2026, 9, 7, 8, tzinfo=timezone.utc), 100, 101, 99, 100, 1)
    fetch = subscription.make_fetcher(
        lambda _symbol, _timeframe, _limit, **_kwargs: [sample])

    assert fetch("BTCUSDT", "1h", 50) == [sample]
    assert fetch("BTCUSDT", "4h", 50) == [sample]
    assert (subscription.symbol, subscription.timeframe) == ("BTCUSDT", "5m")
    assert set(hub._channels) == {
        ("BTCUSDT", "5m"), ("BTCUSDT", "1h"), ("BTCUSDT", "4h"),
    }

    subscription.stop()
    assert hub._channels == {}


def test_same_closed_candle_and_next_quote_create_three_isolated_positions(tmp_path):
    hub = _hub()
    pa = _broker(tmp_path / "pa.db", "PA_LAB")
    smc = _broker(tmp_path / "smc.db", "SMC_LAB")
    instance_ledger = SqliteLedger(str(tmp_path / "instance.db"))
    instance = ForwardPaperExecutionEngine(
        InstanceLedger(instance_ledger, "instance-one"), 10_000)

    observed = {"PA": [], "SMC": [], "INSTANCE": []}
    consumers = {
        "PA": hub.subscription(
            "PA", bar_sink=lambda bar: observed["PA"].append(bar),
            quote_sink=lambda quote: pa.process_tick("BTCUSDT", quote)),
        "SMC": hub.subscription(
            "SMC", bar_sink=lambda bar: observed["SMC"].append(bar),
            quote_sink=lambda quote: smc.process_tick("BTCUSDT", quote)),
        "INSTANCE": hub.subscription(
            "INSTANCE", bar_sink=lambda bar: observed["INSTANCE"].append(bar),
            quote_sink=lambda quote: instance.process_quote(quote)),
    }
    for consumer in consumers.values():
        assert consumer.start("BTCUSDT", "5m")

    channel = next(iter(hub._channels.values()))
    decision_bar = Bar(
        datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc),
        100, 102, 99, 101, 50,
    )
    channel.stream.emit_bar(decision_bar)
    expected_id = candle_id("BTCUSDT", "5m", decision_bar)
    assert len({id(observed[name][0]) for name in observed}) == 1
    assert all(consumer.status()["candle_id"] == expected_id
               for consumer in consumers.values())

    decision_time = decision_bar.timestamp + timedelta(minutes=5)
    metadata = {
        "signal_timestamp": decision_bar.timestamp.isoformat(),
        "decision_timestamp": decision_time.isoformat(),
        "signal_price": decision_bar.close, "requested_price": decision_bar.close,
        "strategy": "fixture", "strategy_version": "1", "timeframe": "5m",
        "market_data_source": "Binance USD-M public WebSocket",
        "candle_id": expected_id,
    }
    pa.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=1,
              protection_stop_loss=95, protection_take_profit=111, **metadata)
    smc.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=1,
               protection_stop_loss=95, protection_take_profit=111, **metadata)
    intent = instance.open(
        symbol="BTCUSDT", side="BUY", size=1, entry=decision_bar.close,
        stop=95, target=111, alert_id=expected_id,
        sizing_context={"decision_timestamp": decision_time.isoformat(),
                        "candle_id": expected_id},
    )
    assert intent.action == "intent"
    assert pa.positions() == [] and smc.positions() == [] and instance.positions() == []

    # Equal-time quote is not future evidence and must not fill.
    channel.stream.emit_quote({
        "bid": 102, "ask": 102.2, "mark": 102.1,
        "received_at": decision_time.isoformat(),
    })
    assert pa.positions() == [] and smc.positions() == [] and instance.positions() == []

    fill_time = decision_time + timedelta(milliseconds=184)
    channel.stream.emit_quote({
        "bid": 102, "ask": 102.2, "mark": 102.1,
        "received_at": fill_time.isoformat(),
    })
    assert len(pa.positions()) == len(smc.positions()) == len(instance.positions()) == 1
    assert pa.fills()[0]["fill_timestamp"] == fill_time.isoformat()
    assert smc.fills()[0]["fill_timestamp"] == fill_time.isoformat()
    assert pa.fills()[0]["price"] != decision_bar.close
    assert smc.fills()[0]["price"] != decision_bar.close
    assert instance.ledger.get_paper_trades()[0]["instance_id"] == "instance-one"
    instance_fill = next(
        row["payload"] for row in instance.ledger.get_webhook_events()
        if ":fill:" in row["alert_id"])
    assert instance_fill["fill_timestamp"] == fill_time.isoformat()
    assert instance_fill["decision_timestamp"] == decision_time.isoformat()
    assert instance_fill["execution_engine"] == "INSTANCE"
    assert instance_fill["blocker"] == "NONE"
    assert pa.account()["account_id"] != smc.account()["account_id"]


def test_lab_account_identity_restart_and_funding_are_isolated(tmp_path):
    pa_path, smc_path = tmp_path / "pa.db", tmp_path / "smc.db"
    pa, smc = _broker(pa_path, "PA_LAB"), _broker(smc_path, "SMC_LAB")
    decision = datetime(2026, 9, 2, 12, 35, tzinfo=timezone.utc)
    pa.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=1,
              decision_timestamp=decision.isoformat())
    pa.process_tick("BTCUSDT", {
        "bid": 100, "ask": 100.2, "mark": 100.1,
        "received_at": (decision + timedelta(milliseconds=1)).isoformat(),
    })
    account_id = pa.account()["account_id"]
    before_smc = smc.account()["balance"]
    assert pa.apply_funding("BTCUSDT", .001, 100)["applied"]
    assert smc.account()["balance"] == before_smc
    reopened = _broker(pa_path, "PA_LAB")
    assert reopened.account()["account_id"] == account_id
    assert reopened.positions()[0]["symbol"] == "BTCUSDT"


def test_stale_or_incomplete_quote_cannot_create_a_fill(tmp_path):
    broker = _broker(tmp_path / "pa.db", "PA_LAB")
    decision = datetime(2026, 9, 2, 12, 35, tzinfo=timezone.utc)
    broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=1,
                  decision_timestamp=decision.isoformat())
    with pytest.raises(ValueError, match="bid, ask and mark"):
        broker.process_tick("BTCUSDT", {
            "bid": 100, "ask": 100.2,
            "received_at": (decision + timedelta(seconds=1)).isoformat(),
        })
    assert broker.positions() == []


def test_forward_paper_source_has_no_exchange_order_submission():
    from pathlib import Path

    roots = [
        Path(__file__).parents[1] / "services" / "forward_paper_hub.py",
        Path(__file__).parents[1] / "execution" / "paper_broker_v2.py",
        Path(__file__).parents[1] / "execution" / "paper_engine.py",
    ]
    assert all("create_order(" not in path.read_text() for path in roots)
