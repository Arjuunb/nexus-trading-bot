"""A Trading Instance must act when a candle closes, not when a timer expires.

PA and SMC take their closed candles pushed from the shared Binance USD-M hub,
so they decide on the close. An instance read the same live sockets but only
through its forward fetcher, once every ``HUB_LIVE_POLL`` seconds (60 by
default). The data was live; the reaction was not. On a 5m chart a decision
landed anywhere up to a minute into the next candle, and the three engines that
are supposed to be compared against each other were not deciding on the same
close.

The hub now hands an instance a closed-candle *notice*. It carries no candle:
the engine owns a durable cursor and re-reads the feed itself, with the same
continuity checks a polled pass makes. The poll remains as the safety net.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from bot.types import Bar, Signal, SignalType
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.forward_paper_hub import ForwardPaperMarketDataHub
from services.signal_pipeline import SignalPipeline

UTC = timezone.utc
TF = timedelta(minutes=5)


class _Flat:
    """Never signals: these tests are about when the engine looks, not what it decides."""

    def __init__(self):
        self.bars = []

    def on_bar(self, bar):
        self.bars.append(bar)
        return None


class _GrowingFeed:
    """A 5m feed the test advances one closed candle at a time.

    Both ends are pinned to wall clock rather than to the 5m grid, because the
    engine decides what is closed (and what is too stale to trade) against the
    real clock and this test cannot move it. The newest candle is held back in
    reserve: it has already closed, so revealing it is a candle close as far as
    the engine can tell, with several minutes of staleness margin either side.
    """

    def __init__(self, count=320):
        # Closes 10s ago, so it counts as closed however long the test takes.
        newest_closed = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=310)
        self._bars = [Bar(newest_closed - TF * (count - index), 100, 101, 99, 100, 1)
                      for index in range(count + 1)]
        self._revealed = len(self._bars) - 1      # the last one stays in reserve
        self.fetches = threading.Semaphore(0)

    def append_closed_candle(self):
        assert self._revealed < len(self._bars), "no reserve candle left"
        self._revealed += 1

    def fetch(self, _symbol, _timeframe, _limit, **_kwargs):
        self.fetches.release()
        return self._bars[:self._revealed], "live (test hub)"


def _engine(feed, **kwargs):
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, 10_000)
    pipeline = SignalPipeline(ledger, paper, TradingControl(), equity=10_000,
                              risk_per_trade_pct=0.01, exposure_limit_pct=0.5)
    return AutoStrategyEngine(
        pipeline, paper, ledger, symbols=["BTCUSDT"], timeframe="5m", live=True,
        strategy_factory=lambda _symbol: _Flat(), fetcher=feed.fetch,
        entry_mode="market", **kwargs)


def _wait_for_bootstrap(engine, feed):
    assert feed.fetches.acquire(timeout=10), "the engine never read the feed"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if engine.status().get("lifecycle_state") == "running":
            return
        time.sleep(0.02)
    raise AssertionError("the engine never finished warming up")


def test_a_closed_candle_is_acted_on_without_waiting_out_the_poll():
    # A poll interval far longer than the test: if the notice did not wake the
    # loop, nothing would read the feed again before the assertion fails.
    feed = _GrowingFeed()
    engine = _engine(feed, live_poll_s=600)
    assert engine.start() is True
    try:
        _wait_for_bootstrap(engine, feed)
        while feed.fetches.acquire(blocking=False):
            pass                                  # drain warm-up reads

        feed.append_closed_candle()
        engine.notify_new_candle()

        assert feed.fetches.acquire(timeout=5), (
            "the instance did not re-read the feed on the candle close; it is "
            "still waiting out its %.0fs poll" % engine.live_poll_s)
    finally:
        engine.stop()


def test_the_cursor_advances_onto_the_notified_candle():
    """Waking early is only useful if the candle is actually processed."""
    feed = _GrowingFeed()
    engine = _engine(feed, live_poll_s=600)
    assert engine.start() is True
    try:
        _wait_for_bootstrap(engine, feed)
        before = engine.last_processed_candle
        feed.append_closed_candle()
        engine.notify_new_candle()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and engine.last_processed_candle == before:
            time.sleep(0.02)
        assert engine.last_processed_candle != before, (
            "the cursor never moved past %s" % before)
    finally:
        engine.stop()


def test_stopping_does_not_wait_for_the_poll_deadline():
    """The loop parks between candles, so stop() has to release that wait.

    Without this a reboot would block for the whole poll interval and the
    caller's join would time out, reporting a worker stopped while its thread
    was still parked.
    """
    feed = _GrowingFeed()
    engine = _engine(feed, live_poll_s=600)
    assert engine.start() is True
    _wait_for_bootstrap(engine, feed)
    started = time.monotonic()
    engine.stop()
    elapsed = time.monotonic() - started
    assert elapsed < 5, "stop() took %.1fs, close to the poll interval" % elapsed
    assert engine._thread is None or not engine._thread.is_alive()


# --------------------------------------------------------------- hub plumbing

class FakeStream:
    def __init__(self, _loader, *, bar_sink=None, quote_sink=None,
                 event_sink=None, **_kwargs):
        self.bar_sink, self.quote_sink, self.event_sink = bar_sink, quote_sink, event_sink
        self.running = False

    def start(self, _symbol, _timeframe):
        self.running = True
        return True

    def stop(self):
        self.running = False

    def status(self):
        return {"state": "SYNCHRONIZED", "reliable": True, "new_entries_paused": False}

    def snapshot(self):
        return {"closed_bars": [], "forming": None, "quote": {}}


def _hub():
    return ForwardPaperMarketDataHub(lambda *_a, **_k: [], stream_factory=FakeStream)


def _bar():
    return Bar(datetime(2026, 9, 13, 12, tzinfo=UTC), 100, 101, 99, 100.5, 1)


def test_the_hub_notifies_on_a_candle_close():
    hub = _hub()
    seen: list[Bar] = []
    subscription = hub.subscription("INSTANCE:one", candle_notice=seen.append)
    subscription.start("BTCUSDT", "5m")
    try:
        hub._for("INSTANCE:one").stream.bar_sink(_bar())
        assert [bar.timestamp for bar in seen] == [_bar().timestamp]
    finally:
        hub.stop()


def test_a_notice_never_makes_the_subscription_unreliable():
    """The reason this is a notice and not a bar_sink.

    A bar_sink candle enters the hub's pending map, which reports the
    subscription unreliable until a shared delivery worker drains it. The
    instance's quote path checks exactly that flag before filling, so taking
    the candle as a delivery would have made its stop-loss fills wait on
    however long PA's and SMC's candle processing took on the same close.
    """
    hub = _hub()
    subscription = hub.subscription("INSTANCE:one", candle_notice=lambda _bar: None)
    subscription.start("BTCUSDT", "5m")
    try:
        hub._for("INSTANCE:one").stream.bar_sink(_bar())
        status = subscription.status()
        assert status["reliable"] is True, status.get("health_reason")
        assert status["subscriber_delivery"]["pending_candle_ids"] == []
    finally:
        hub.stop()


def test_a_failing_notice_cannot_stop_another_consumer_getting_its_candle():
    hub = _hub()
    hub.synchronous_delivery = True
    delivered: list[Bar] = []
    broken = hub.subscription(
        "INSTANCE:broken",
        candle_notice=lambda _bar: (_ for _ in ()).throw(RuntimeError("boom")))
    lab = hub.subscription("PA_LAB", bar_sink=delivered.append)
    broken.start("BTCUSDT", "5m")
    lab.start("BTCUSDT", "5m")
    try:
        hub._for("PA_LAB").stream.bar_sink(_bar())
        assert [bar.timestamp for bar in delivered] == [_bar().timestamp]
        assert "boom" in broken.status()["subscriber_delivery"]["last_error"]
    finally:
        hub.stop()
