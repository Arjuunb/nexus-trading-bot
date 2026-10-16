"""A slow bar sink must not stall the thread that reads the sockets.

The hub's bar callback runs inside the market stream's asyncio loop. A bar sink
is not cheap: it drives a lab's whole closed-candle path, rebuilding a market
structure engine over hundreds of bars and writing SQLite, once per subscribed
consumer. While that ran inline, the loop stopped reading, and the measured
result on the VPS was markPrice at one message per second going eighty seconds
without an update while bookTicker on the other socket read zero seconds old.
The feed then failed its fifteen-second staleness check and every fill stopped,
for reasons that had nothing to do with Binance.

Delivery is therefore dispatched to a pool. Candle order per consumer still
comes from the ordered pending map drained under that consumer's delivery lock.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from bot.types import Bar
from services.forward_paper_hub import ForwardPaperMarketDataHub

UTC = timezone.utc
BASE = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _bar(index: int) -> Bar:
    return Bar(BASE + timedelta(minutes=5 * index), 100, 101, 99, 100.5, 1)


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

    def emit_bar(self, bar):
        self.bar_sink(bar)


def _hub():
    return ForwardPaperMarketDataHub(lambda *_a, **_k: [], stream_factory=FakeStream)


def test_slow_audit_does_not_block_socket_event_delivery():
    hub = _hub()
    entered, release = threading.Event(), threading.Event()
    def audit(_event):
        entered.set()
        release.wait(3)
    sub = hub.subscription("AUDIT", event_sink=audit)
    sub.start("BTCUSDT", "5m")
    try:
        before = time.monotonic()
        hub._for("AUDIT").stream.event_sink({"state": "CONNECTED"})
        assert time.monotonic() - before < .5
        assert entered.wait(1)
    finally:
        release.set()
        sub.stop()


def test_a_slow_sink_does_not_block_the_emitting_thread():
    hub = _hub()
    released = threading.Event()
    entered = threading.Event()

    def slow(_bar):
        entered.set()
        released.wait(10)

    subscription = hub.subscription("SLOW", bar_sink=slow)
    subscription.start("BTCUSDT", "5m")
    stream = hub._for("SLOW").stream
    try:
        started = time.monotonic()
        stream.emit_bar(_bar(0))
        elapsed = time.monotonic() - started
        # The sink is still parked inside its call; emit_bar must already have
        # returned. Inline delivery would have made this take the full wait.
        assert entered.wait(5), "the sink never ran"
        assert elapsed < 2, "emit_bar blocked on the sink for %.1fs" % elapsed
    finally:
        released.set()
        hub.stop()


def test_a_slow_consumer_cannot_stall_a_sibling():
    """One lab falling behind must not hold up another lab's candles."""
    hub = _hub()
    released = threading.Event()
    fast: list[Bar] = []

    def slow(_bar):
        released.wait(10)

    slow_subscription = hub.subscription("SLOW", bar_sink=slow)
    fast_subscription = hub.subscription("FAST", bar_sink=fast.append)
    slow_subscription.start("BTCUSDT", "5m")
    fast_subscription.start("BTCUSDT", "5m")
    stream = hub._for("SLOW").stream
    try:
        stream.emit_bar(_bar(0))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not fast:
            time.sleep(0.02)
        assert fast, "the fast consumer never received its candle"
    finally:
        released.set()
        hub.stop()


def test_candle_order_is_preserved_per_consumer():
    hub = _hub()
    seen: list[Bar] = []
    gate = threading.Event()

    def record(bar):
        # Hold the first candle briefly so the next ones queue behind it,
        # which is the case where ordering could break.
        if not seen:
            gate.wait(5)
        seen.append(bar)

    subscription = hub.subscription("ORDER", bar_sink=record)
    subscription.start("BTCUSDT", "5m")
    stream = hub._for("ORDER").stream
    try:
        for index in range(4):
            stream.emit_bar(_bar(index))
        gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(seen) < 4:
            time.sleep(0.02)
        assert len(seen) == 4, "expected four candles, saw %d" % len(seen)
        assert [bar.timestamp for bar in seen] == [_bar(i).timestamp for i in range(4)]
    finally:
        gate.set()
        hub.stop()


def test_quotes_reach_a_consumer_in_arrival_order():
    """Order matters more for quotes than for candles.

    The broker keeps a per-symbol quote cursor and rejects anything not newer
    than the last quote it saw. Two quotes running concurrently would have one
    dropped as OUT_OF_ORDER_QUOTE and an intent left unfilled, so each consumer
    gets a single worker rather than a share of a pool.
    """
    hub = _hub()
    seen: list[int] = []
    gate = threading.Event()

    def record(quote):
        if not seen:
            gate.wait(5)          # hold the first so the rest queue behind it
        seen.append(int(quote["sequence"]))

    subscription = hub.subscription("QUOTES", quote_sink=record)
    subscription.start("BTCUSDT", "5m")
    stream = hub._for("QUOTES").stream
    try:
        for index in range(6):
            stream.quote_sink({"bid": 1, "ask": 2, "mark": 1.5,
                               "sequence": index, "received_at": "2026-09-13T00:00:%02dZ" % index})
        gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(seen) < 6:
            time.sleep(0.02)
        assert seen == sorted(seen), "quotes arrived out of order: %s" % seen
        assert len(seen) == 6
    finally:
        gate.set()
        hub.stop()


def test_a_slow_quote_sink_does_not_block_the_emitting_thread():
    hub = _hub()
    released = threading.Event()
    entered = threading.Event()

    def slow(_quote):
        entered.set()
        released.wait(10)

    subscription = hub.subscription("SLOWQ", quote_sink=slow)
    subscription.start("BTCUSDT", "5m")
    stream = hub._for("SLOWQ").stream
    try:
        started = time.monotonic()
        stream.quote_sink({"bid": 1, "ask": 2, "mark": 1.5, "sequence": 1,
                           "received_at": "2026-09-13T00:00:00Z"})
        elapsed = time.monotonic() - started
        assert entered.wait(5), "the quote sink never ran"
        assert elapsed < 2, "emitting a quote blocked for %.1fs" % elapsed
    finally:
        released.set()
        hub.stop()


def test_quotes_still_arrive_after_a_restart():
    """start() detaches before re-attaching, and must not kill the worker.

    A subscription that survives a restart with no notifier would go silently
    deaf: it stays attached, its sink is still registered, and not one further
    quote ever reaches it.
    """
    hub = _hub()
    seen: list[dict] = []
    subscription = hub.subscription("RESTART", quote_sink=seen.append)
    try:
        subscription.start("BTCUSDT", "5m")
        subscription.start("BTCUSDT", "5m")     # same identity
        subscription.start("ETHUSDT", "5m")     # and a different one
        hub._for("RESTART").stream.quote_sink(
            {"bid": 1, "ask": 2, "mark": 1.5, "sequence": 1,
             "received_at": "2026-09-13T00:00:00Z"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not seen:
            time.sleep(0.02)
        assert seen, "no quote arrived after the subscription restarted"
    finally:
        hub.stop()


def test_dispatching_a_quote_takes_no_hub_lock():
    """Handing a quote to a worker must not touch the global lock.

    Building the worker lazily put a second acquisition on the hottest path in
    the system: one per quote, per channel, on the same lock the notify workers
    need for status(). A dump caught six stream threads and three workers
    queued on it with the app burning six cores.

    on_quote still takes the lock briefly to read the channel and build the
    snapshot, which is short and predates this. What must not block is the
    hand-off, so that is what this measures.
    """
    hub = _hub()
    delivered = threading.Event()
    subscription = hub.subscription("NOLOCK", quote_sink=lambda _q: delivered.set())
    subscription.start("BTCUSDT", "5m")
    try:
        hub._lock.acquire()          # hold it for the whole hand-off
        try:
            handed_off = threading.Event()
            threading.Thread(
                target=lambda: (hub._dispatch_notify(
                    subscription.consumer, subscription.consumer.quote_sink,
                    {"bid": 1, "ask": 2, "mark": 1.5}), handed_off.set()),
                daemon=True).start()
            assert handed_off.wait(5), "the hand-off blocked on the hub lock"
            assert delivered.wait(5), "the quote never reached the sink"
        finally:
            hub._lock.release()
    finally:
        hub.stop()


def test_synchronous_delivery_still_available_for_deterministic_callers():
    hub = _hub()
    hub.synchronous_delivery = True
    seen: list[Bar] = []
    subscription = hub.subscription("SYNC", bar_sink=seen.append)
    subscription.start("BTCUSDT", "5m")
    try:
        hub._for("SYNC").stream.emit_bar(_bar(0))
        assert seen, "synchronous delivery must complete before emit_bar returns"
    finally:
        hub.stop()


def test_stop_waits_for_an_in_flight_candle():
    """A half-applied candle would leave a gap in the consumer's history."""
    hub = _hub()
    finished = threading.Event()

    def slow(_bar):
        time.sleep(0.4)
        finished.set()

    subscription = hub.subscription("DRAIN", bar_sink=slow)
    subscription.start("BTCUSDT", "5m")
    hub._for("DRAIN").stream.emit_bar(_bar(0))
    hub.stop()
    assert finished.is_set(), "stop() returned while a candle was still being applied"
