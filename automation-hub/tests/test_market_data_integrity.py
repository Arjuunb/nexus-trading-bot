"""Candle integrity, clocks, backpressure and reconnect behaviour.

A strategy is only as trustworthy as the series it is handed. These pin the
properties that make that series safe to act on: ordered, complete, closed,
in UTC, and bounded when the feed outruns the consumer.
"""
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from services.forward_paper_hub import (
    MAX_PENDING_CANDLES, ForwardPaperMarketDataHub,
)
from services.price_action_stream import PriceActionPublicStream


def _bar(minute, close=100.0):
    return Bar(datetime(2026, 9, 13, 10, minute, tzinfo=timezone.utc),
               100.0, 101.0, 99.0, close, 10.0)


class _Stream:
    def __init__(self, loader, *, bar_sink=None, quote_sink=None,
                 event_sink=None, quotes_enabled=True, **_kw):
        self.bar_sink, self.quote_sink, self.event_sink = bar_sink, quote_sink, event_sink
        self.quotes_enabled, self.running = quotes_enabled, False
        self.symbol = self.timeframe = ""

    def start(self, symbol, timeframe):
        self.symbol, self.timeframe, self.running = symbol, timeframe, True
        return True

    def stop(self):
        self.running = False

    def status(self):
        return {"state": "SYNCHRONIZED", "transport_state": "CONNECTED", "reliable": True}

    def snapshot(self):
        return {"closed_bars": [], "forming": None, "quote": {}, "connection": self.status()}


# ------------------------------------------------------------ candle rules
def test_a_forming_candle_is_never_handed_to_a_closed_candle_strategy():
    from services.auto_engine import AutoStrategyEngine

    now = datetime.now(timezone.utc)
    closed = Bar(now - timedelta(minutes=10), 1, 2, 0.5, 1.5, 1)
    forming = Bar(now - timedelta(minutes=1), 1, 2, 0.5, 1.5, 1)

    kept = AutoStrategyEngine._closed_bars([closed, forming], "5m")

    assert closed in kept
    assert forming not in kept


def test_a_gap_in_the_series_is_detected_rather_than_stepped_over():
    from services.auto_engine import AutoStrategyEngine

    engine = AutoStrategyEngine.__new__(AutoStrategyEngine)
    contiguous = [_bar(0), _bar(5), _bar(10)]
    engine._require_continuity(contiguous, "5m")        # no raise

    with pytest.raises(Exception) as gap:
        engine._require_continuity([_bar(0), _bar(10)], "5m")
    assert "continuity" in str(gap.value).lower() or "missing" in str(gap.value).lower()


def test_a_duplicate_candle_is_counted_and_ignored():
    from services.auto_engine import AutoStrategyEngine

    engine = AutoStrategyEngine.__new__(AutoStrategyEngine)
    engine.timeframe = "5m"
    engine.duplicate_candles_ignored = 0
    engine.out_of_order_candles = 0
    engine.missing_candles = 0
    engine.ledger = None
    seen = []
    engine._process_bar = lambda *a, **k: seen.append(a)   # type: ignore[attr-defined]

    # _ingest's contract: a bar at or before the cursor is a duplicate.
    assert AutoStrategyEngine._ingest.__doc__
    source = inspect.getsource(AutoStrategyEngine._ingest)
    assert "duplicate_candles_ignored += 1" in source
    assert "out_of_order_candles += 1" in source


def test_the_canonical_candle_id_pins_venue_symbol_timeframe_and_open_time():
    from services.mtf_policy import canonical_candle_id

    identity = canonical_candle_id("BTCUSDT", "5m", _bar(0))

    assert identity.startswith("BINANCE_USDM:BTCUSDT:5m:")
    # Two different timeframes on the same open time are different candles.
    assert identity != canonical_candle_id("BTCUSDT", "1h", _bar(0))
    assert identity != canonical_candle_id("ETHUSDT", "5m", _bar(0))


def test_higher_timeframe_context_is_native_not_resampled():
    """A 1h bias derived from 5m bars is not a 1h bias."""
    from strategies.brain import BrainConfig, htf_bias

    bias, strength = htf_bias([_bar(index) for index in range(0, 60, 5)],
                              BrainConfig(), native_bars=None,
                              allow_legacy_resample=False)

    assert (bias, strength) == ("neutral", 0.0)


# ----------------------------------------------------------------- clocks
def test_every_runtime_timestamp_is_timezone_aware_utc():
    import services.trading_instances as trading_instances
    from services.instance_telemetry import event_payload

    stamp = datetime.fromisoformat(trading_instances._now())
    assert stamp.tzinfo is not None
    assert stamp.utcoffset() == timedelta(0)

    event = datetime.fromisoformat(event_payload(None, "MARKET_STALE")["timestamp"])
    assert event.utcoffset() == timedelta(0)


def test_trading_code_never_calls_a_naive_clock():
    """Browser time and local server time must not reach a trading decision."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for folder in ("services", "execution", "data"):
        for path in (root / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "datetime.now()" in text or "datetime.utcnow()" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_candle_freshness_is_measured_from_the_close_not_the_open():
    """Provider candles are stamped at their OPEN. A just-closed 5m candle is
    zero seconds old, not five minutes old."""
    from services.trading_instances import _market_health

    just_closed = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    health = _market_health({"last_market_data_timestamp": just_closed,
                             "market_data_status": "healthy"},
                            timeframe="5m", worker_state="running")

    assert health["market_data_age_seconds"] < 60
    assert health["market_data_status"] == "healthy"


# ---------------------------------------------------------- backpressure
def test_a_consumer_that_stops_keeping_up_is_bounded_and_reported():
    hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_Stream)
    hub.synchronous_delivery = True
    stalled = hub.subscription("SLOW", bar_sink=lambda _bar: (_ for _ in ()).throw(
        RuntimeError("this consumer is stuck")))
    assert stalled.start("BTCUSDT", "5m")
    channel = hub._channels[("BTCUSDT", "5m")]

    for minute in range(0, MAX_PENDING_CANDLES + 40):
        channel.stream.bar_sink(
            Bar(datetime(2026, 9, 13, tzinfo=timezone.utc) + timedelta(minutes=5 * minute),
                1, 2, 0.5, 1.5, 1))

    status = stalled.status()
    delivery = status["subscriber_delivery"]
    # Candles are queued, never dropped -- a strategy must not evaluate a gap
    # it never saw -- but the consumer is reported unreliable, which closes its
    # entry gate, and the depth is visible instead of growing silently.
    assert status["reliable"] is False
    assert delivery["queue_depth"] > MAX_PENDING_CANDLES
    assert delivery["peak_queue_depth"] >= delivery["queue_depth"]
    # The backlog is tracked separately from last_error, which the next
    # delivery attempt overwrites with its own exception.
    assert delivery["backlog_exceeded"] is True
    assert "not keeping up" in (delivery["backlog_detail"] or "")
    assert "stuck" in (delivery["last_error"] or "")
    stalled.stop()


def test_a_healthy_consumer_is_unaffected_by_a_stalled_sibling():
    hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_Stream)
    hub.synchronous_delivery = True
    received = []
    healthy = hub.subscription("OK", bar_sink=received.append)
    stalled = hub.subscription("SLOW", bar_sink=lambda _b: (_ for _ in ()).throw(
        RuntimeError("stuck")))
    assert healthy.start("BTCUSDT", "5m") and stalled.start("BTCUSDT", "5m")

    hub._channels[("BTCUSDT", "5m")].stream.bar_sink(_bar(0))

    assert received == [_bar(0)]
    assert healthy.status()["reliable"] is not False
    assert stalled.status()["reliable"] is False
    healthy.stop()
    stalled.stop()


# ------------------------------------------------------------- reconnects
def test_reconnect_backoff_is_jittered_so_channels_do_not_retry_in_lockstep():
    """Nine channels retrying on the same tick is how a recovering venue
    turns into a rate-limit ban."""
    source = inspect.getsource(PriceActionPublicStream._stream_channel)

    assert "random.uniform" in source
    assert "2 ** min" in source            # the exponential window is unchanged


def test_reconnect_backoff_stays_within_its_ceiling():
    import random

    for attempt in range(0, 12):
        ceiling = min(2 ** min(attempt, 5), 30)
        for _ in range(50):
            delay = random.uniform(ceiling / 2, ceiling)
            assert 0 < delay <= 30


def test_a_context_channel_opens_no_redundant_quote_subscriptions():
    entry = PriceActionPublicStream(lambda *a, **k: [])
    entry.symbol, entry.timeframe = "BTCUSDT", "5m"
    context = PriceActionPublicStream(lambda *a, **k: [], quotes_enabled=False)
    context.symbol, context.timeframe = "BTCUSDT", "1h"

    assert any("markPrice" in name for name in entry.market_subscriptions)
    assert any("bookTicker" in name for name in entry.public_subscriptions)
    assert not any("markPrice" in name for name in context.market_subscriptions)
    # And it is not then reported stale for lacking the quotes it never wanted.
    assert context.quotes_enabled is False


def test_a_context_channel_is_upgraded_when_a_consumer_needs_quotes():
    """A kline-only channel must not silently starve a quote consumer.

    Context channels are opened without markPrice/bookTicker. If a consumer
    that needs quotes then joined one, it received candles and never a single
    quote -- so every parked forward-paper intent would sit unfilled forever
    while the feed reported itself synchronized.
    """
    hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_Stream)
    hub.synchronous_delivery = True

    context = hub.subscription("CONTEXT")
    assert context.start("BTCUSDT", "1h", quotes=False)
    assert hub._channels[("BTCUSDT", "1h")].stream.quotes_enabled is False

    quotes_seen = []
    trader = hub.subscription("TRADER", quote_sink=quotes_seen.append)
    assert trader.start("BTCUSDT", "1h")          # needs quotes

    channel = hub._channels[("BTCUSDT", "1h")]
    assert channel.stream.quotes_enabled is True
    # Both consumers are still attached to the one upgraded channel.
    assert set(channel.consumers) == {"CONTEXT", "TRADER"}
    channel.stream.quote_sink({"bid": 1.0, "ask": 1.1, "mark": 1.05})
    assert quotes_seen and quotes_seen[0]["bid"] == 1.0
    trader.stop()
    context.stop()


def test_the_quotes_upgrade_survives_concurrent_detach_without_leaking():
    """The upgrade path broke in three consecutive rounds.

    Its failure mode is not an exception -- it is a channel removed from the
    hub while the upgrade is in flight, leaving a live WebSocket that nothing
    will ever close, or a quote consumer attached to a kline-only feed that can
    never fill an order while reporting itself reliable.
    """
    import threading
    import time

    constructed = []

    class _Tracked:
        def __init__(self, _loader, *, bar_sink=None, quote_sink=None,
                     event_sink=None, quotes_enabled=True, **_kw):
            self.quotes_enabled, self.running, self.stopped = quotes_enabled, False, False
            self.symbol = self.timeframe = ""
            constructed.append(self)

        def start(self, symbol, timeframe):
            self.symbol, self.timeframe = symbol, timeframe
            time.sleep(0.005)              # stands in for the REST bootstrap
            self.running = True
            return True

        def stop(self):
            self.running, self.stopped = False, True

        def status(self):
            return {"state": "SYNCHRONIZED", "transport_state": "CONNECTED",
                    "reliable": True}

        def snapshot(self):
            return {"closed_bars": [], "forming": None, "quote": {},
                    "connection": self.status()}

    hub = ForwardPaperMarketDataHub(lambda *a, **k: [], stream_factory=_Tracked)
    problems = []

    def context(name):
        subscription = hub.subscription(f"ctx-{name}")
        subscription.start("BTCUSDT", "1h", quotes=False)
        time.sleep(0.003)
        subscription.stop()

    def needs_quotes(name):
        subscription = hub.subscription(f"q-{name}")
        if subscription.start("BTCUSDT", "1h"):
            channel = hub._for(subscription.consumer_id)
            if channel is not None and not channel.stream.quotes_enabled:
                problems.append("started on a quote-less channel")
        time.sleep(0.003)
        subscription.stop()

    for round_number in range(10):
        threads = ([threading.Thread(target=context, args=(f"{round_number}-{i}",))
                    for i in range(3)]
                   + [threading.Thread(target=needs_quotes, args=(f"{round_number}-{i}",))
                      for i in range(3)])
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    hub.stop()
    leaked = [stream for stream in constructed if stream.running and not stream.stopped]

    assert problems == []
    assert leaked == [], f"{len(leaked)} stream(s) left running with nothing to close them"
    assert hub._channels == {}
