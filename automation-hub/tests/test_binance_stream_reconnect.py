"""Reconnect, resubscribe and shutdown on the Binance USD-M channels.

The URL defect these tests sit beside was invisible partly because nothing
exercised the socket loop at all. This does, offline, with a scripted fake in
place of `websockets` -- so it asserts what the loop DOES on a drop rather
than whether Binance happens to be reachable from the test host.

Four properties, each one a way the loop could fail quietly:

  * a dropped channel reconnects, and reconnects to the same combined URL,
    which is what resubscription IS on this protocol -- the stream names live
    in the query string, so a reconnect that reached a different URL would
    silently subscribe to something else;
  * a reconnect clears the freshness timestamps, so no message received before
    the drop can make the new socket look fresh;
  * one message delivered once is processed once, across the drop;
  * stop() ends the loop rather than leaving a task retrying forever.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from services.price_action_stream import PriceActionPublicStream

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def kline(open_ms: int, *, closed: bool, close: str = "100.5") -> str:
    """One combined-stream frame, shaped the way Binance sends it."""
    return json.dumps({
        "stream": "btcusdt@kline_5m",
        "data": {"e": "kline", "s": "BTCUSDT", "k": {
            "t": open_ms, "T": open_ms + 299_999, "i": "5m", "x": closed,
            "o": "100.0", "h": "101.0", "l": "99.0", "c": close, "v": "12.5"}},
    })


class Dropped(Exception):
    """The venue closed the socket."""


class FakeSocket:
    def __init__(self, messages: list[str], *, then: Exception | None):
        self._messages, self._then = list(messages), then
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        if self._then is not None:
            raise self._then
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class FakeWebsockets:
    """Hands out scripted sockets and records every URL asked for.

    When the script runs out it stops the feed, so the real loop ends on its
    own terms rather than on a timer the test has to guess.
    """

    def __init__(self, feed, sockets: list[FakeSocket]):
        self._feed, self._sockets = feed, list(sockets)
        self.urls: list[str] = []

    def connect(self, url, **_kwargs):
        self.urls.append(url)
        if not self._sockets:
            self._feed._stop.set()
            raise Dropped("the script is exhausted")
        return self._sockets.pop(0)


def stream(**kwargs) -> PriceActionPublicStream:
    feed = PriceActionPublicStream(lambda *a, **k: [], clock=lambda: NOW, **kwargs)
    feed.symbol, feed.timeframe = "BTCUSDT", "5m"
    return feed


def run_channel(feed, fake, *, channel: str = "market"):
    """Drive the real channel loop to completion, without its backoff sleeps."""
    async def main():
        real_sleep = asyncio.sleep

        async def instant(_seconds, *args, **kwargs):
            return await real_sleep(0)

        asyncio.sleep = instant                      # type: ignore[assignment]
        try:
            url = feed.market_url if channel == "market" else feed.public_url
            await asyncio.wait_for(feed._stream_channel(channel, url, fake), timeout=5)
        finally:
            asyncio.sleep = real_sleep               # type: ignore[assignment]

    asyncio.run(main())


# ───────────────────────────────── reconnect ──────────────────────────────

def test_a_dropped_channel_reconnects_to_the_same_combined_url():
    feed = stream()
    fake = FakeWebsockets(feed, [
        FakeSocket([kline(1_700_000_000_000, closed=True)], then=Dropped("reset")),
        FakeSocket([kline(1_700_000_300_000, closed=True)], then=None),
    ])

    run_channel(feed, fake)

    assert len(fake.urls) >= 2, "the channel never reconnected"
    assert len(set(fake.urls)) == 1, f"reconnected to a different URL: {set(fake.urls)}"
    # Resubscription on this protocol is the URL: the stream names are in it.
    assert fake.urls[0] == (
        "wss://fstream.binance.com/stream?streams="
        "btcusdt@kline_5m/btcusdt@markPrice@1s")


def test_a_reconnect_refuses_to_inherit_the_old_sockets_freshness():
    """Fail-closed across a drop: a candle received before the break must not
    make the replacement socket look synchronized.

    The timestamps are cleared on every connect and can only be set again by
    something the NEW socket delivered. Reconciliation is re-run per connect
    rather than carried over, so freshness is re-established from the venue
    instead of assumed.
    """
    calls: list[tuple] = []

    feed = PriceActionPublicStream(
        lambda *args, **kwargs: calls.append(args) or [], clock=lambda: NOW)
    feed.symbol, feed.timeframe = "BTCUSDT", "5m"
    feed.last_candle_update = NOW
    feed.last_mark_update = NOW
    fake = FakeWebsockets(feed, [FakeSocket([], then=Dropped("reset")),
                                 FakeSocket([], then=None)])

    run_channel(feed, fake)

    # Nothing arrived on either socket, so nothing may claim freshness.
    assert feed.last_candle_update is None
    assert feed.last_mark_update is None
    # Once per connect: the reconnect re-derives history rather than trusting
    # what the dropped socket had already delivered.
    assert len(calls) == 2, f"reconciled {len(calls)} time(s) across 2 connects"


def test_one_message_is_processed_once_across_a_reconnect():
    """No duplicate processing: the same closed candle redelivered after a
    drop is counted as a duplicate, not merged twice."""
    feed = stream()
    same = kline(1_700_000_000_000, closed=True)
    fake = FakeWebsockets(feed, [FakeSocket([same], then=Dropped("reset")),
                                 FakeSocket([same], then=None)])

    run_channel(feed, fake)

    assert feed.duplicate_events == 1
    assert len(feed.snapshot()["closed_bars"]) == 1


# ───────────────────────────────── shutdown ───────────────────────────────

def test_stop_ends_the_channel_loop_instead_of_retrying_forever():
    feed = stream()
    sockets = [FakeSocket([], then=Dropped("reset")) for _ in range(3)]
    fake = FakeWebsockets(feed, sockets)

    run_channel(feed, fake)

    assert feed._stop.is_set()
    assert all(socket.closed for socket in sockets[:2]), "a socket was left open"


def test_stop_on_a_stream_that_never_started_is_safe_and_reports_disconnected():
    feed = stream()

    feed.stop()

    assert feed.running is False
    assert feed.status()["transport_channels"] == {
        "market": "DISCONNECTED", "public": "DISCONNECTED"}


@pytest.mark.parametrize("quotes, expect_book_ticker", [(True, True), (False, False)])
def test_only_a_quote_carrying_channel_dials_book_ticker(monkeypatch, quotes,
                                                         expect_book_ticker):
    """Which sockets get opened is decided in _stream_forever, so that is what
    this drives -- calling the per-channel loop directly would assert nothing
    about the choice.

    A higher-timeframe context channel exists to carry kline alone. Dialling
    bookTicker from it would open a second, redundant quote socket per symbol
    whose messages no fill may ever be driven by.
    """
    import sys

    feed = stream(quotes_enabled=quotes)
    fake = FakeWebsockets(feed, [FakeSocket([], then=None) for _ in range(2)])
    monkeypatch.setitem(sys.modules, "websockets", fake)

    async def main():
        real_sleep = asyncio.sleep

        async def instant(_seconds, *args, **kwargs):
            return await real_sleep(0)

        asyncio.sleep = instant                      # type: ignore[assignment]
        try:
            await asyncio.wait_for(feed._stream_forever(), timeout=5)
        finally:
            asyncio.sleep = real_sleep               # type: ignore[assignment]

    asyncio.run(main())

    dialled_book_ticker = any("bookTicker" in url for url in fake.urls)
    assert dialled_book_ticker is expect_book_ticker, fake.urls
    assert any("kline_5m" in url for url in fake.urls), "kline was never dialled"
    if not quotes:
        assert feed.status()["transport_channels"]["public"] == "DISCONNECTED"
