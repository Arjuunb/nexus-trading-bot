"""Switching timeframe must not leave live channels behind.

Changing the timeframe is a normal, manual thing to do: both labs render a
button per entry timeframe in ENTRY_HTF and an instance's form offers the same
list. Each switch retunes the primary subscription, and the higher-timeframe
channels the old timeframe needed are a different set from the new one's:

    5m  -> primary 1h, secondary 4h
    4h  -> primary 1d, secondary none

stop() releases those child subscriptions. start() did not, so every switch
stranded them — each one an attached consumer holding a channel open, and a
channel is two Binance websockets, a reader thread and a 1500-bar REST
reconciliation. Flipping through all five entry timeframes left six channels
running where three were needed, and switching symbol stranded the old
symbol's clocks entirely. On a box already short of CPU that compounds with
every click.
"""
from datetime import datetime, timezone

from bot.types import Bar
from services.forward_paper_hub import ForwardPaperMarketDataHub

BASE = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)


class FakeStream:
    def __init__(self, _loader, *, bar_sink=None, quote_sink=None,
                 event_sink=None, **_kwargs):
        self.bar_sink, self.quote_sink, self.event_sink = bar_sink, quote_sink, event_sink
        self.running = False
        self.symbol = self.timeframe = ""

    def start(self, symbol, timeframe):
        self.symbol, self.timeframe, self.running = symbol, timeframe, True
        return True

    def stop(self):
        self.running = False

    def status(self):
        return {"state": "SYNCHRONIZED", "reliable": True, "new_entries_paused": False}

    def snapshot(self):
        return {"closed_bars": [Bar(BASE, 100, 101, 99, 100.5, 1)],
                "forming": None, "quote": {}}


def _hub():
    return ForwardPaperMarketDataHub(lambda *_a, **_k: [], stream_factory=FakeStream)


def _live(hub):
    """The channels the hub is actually running right now."""
    return {key for key, channel in hub._channels.items() if channel.stream.running}


def test_switching_timeframe_releases_the_old_higher_timeframes():
    hub = _hub()
    subscription = hub.subscription("PA_LAB")
    try:
        subscription.start("BTCUSDT", "5m")
        fetch = subscription.make_fetcher()
        fetch("BTCUSDT", "1h", 50)          # 5m policy: primary 1h
        fetch("BTCUSDT", "4h", 50)          # 5m policy: secondary 4h
        assert _live(hub) == {("BTCUSDT", "5m"), ("BTCUSDT", "1h"), ("BTCUSDT", "4h")}

        # The operator clicks 4h. Its policy is primary 1d, secondary none, so
        # the 1h clock is no longer wanted by anyone.
        subscription.start("BTCUSDT", "4h")
        assert ("BTCUSDT", "1h") not in _live(hub), (
            "the 5m era's 1h channel is still running with nobody reading it")
    finally:
        hub.stop()


def test_switching_symbol_releases_the_old_symbols_clocks():
    hub = _hub()
    subscription = hub.subscription("PA_LAB")
    try:
        subscription.start("BTCUSDT", "5m")
        fetch = subscription.make_fetcher()
        fetch("BTCUSDT", "1h", 50)
        fetch("BTCUSDT", "4h", 50)

        subscription.start("ETHUSDT", "5m")
        stranded = {key for key in _live(hub) if key[0] == "BTCUSDT"}
        assert not stranded, "left BTCUSDT channels running after switching: %s" % stranded
    finally:
        hub.stop()


def test_a_clock_both_timeframes_need_is_not_torn_down():
    """5m and 15m share primary 1h and secondary 4h; pruning must not churn them."""
    hub = _hub()
    subscription = hub.subscription("PA_LAB")
    try:
        subscription.start("BTCUSDT", "5m")
        fetch = subscription.make_fetcher()
        fetch("BTCUSDT", "1h", 50)
        fetch("BTCUSDT", "4h", 50)
        before = hub._channels[("BTCUSDT", "1h")]

        subscription.start("BTCUSDT", "15m")
        assert ("BTCUSDT", "1h") in _live(hub), "1h is still required by the 15m policy"
        assert hub._channels[("BTCUSDT", "1h")] is before, (
            "the 1h channel was rebuilt, forcing a needless REST reload")
    finally:
        hub.stop()


def test_flipping_through_every_entry_timeframe_does_not_accumulate():
    """The behaviour an operator actually produces by exploring the buttons."""
    hub = _hub()
    subscription = hub.subscription("PA_LAB")
    try:
        for timeframe in ("1m", "5m", "15m", "1h", "4h", "5m"):
            subscription.start("BTCUSDT", timeframe)
            fetch = subscription.make_fetcher()
            from services.mtf_policy import native_timeframes
            for htf in native_timeframes(timeframe):
                fetch("BTCUSDT", htf, 50)
        # Back on 5m: entry plus primary 1h plus secondary 4h. Nothing else.
        assert _live(hub) == {("BTCUSDT", "5m"), ("BTCUSDT", "1h"), ("BTCUSDT", "4h")}, (
            "accumulated channels: %s" % sorted(_live(hub)))
    finally:
        hub.stop()


def test_an_unsupported_entry_timeframe_still_starts():
    """1d is a valid feed clock but has no ENTRY_HTF policy.

    Pruning must not turn that into a crash on start(); callers that need a
    policy fail later, where the message names the policy.
    """
    hub = _hub()
    subscription = hub.subscription("DIRECT")
    try:
        assert subscription.start("BTCUSDT", "1d") is True
    finally:
        hub.stop()
