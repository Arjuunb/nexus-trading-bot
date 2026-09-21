"""Reconnect, resubscribe and shutdown on the Binance USD-M channels.

The URL defect these tests sit beside was invisible partly because nothing
exercised the socket loop at all. This does, offline, with a scripted fake in
place of `websockets` -- so it asserts what the loop DOES on a drop rather
than whether Binance happens to be reachable from the test host.

Binance routes futures market data by category (/market/ws, /public/ws) and
takes the stream names in a SUBSCRIBE message once the socket is open, so
resubscription is no longer a property of the URL: a reconnect that dials the
right endpoint and forgets to SUBSCRIBE gets a socket that opens, stays open,
answers pings and delivers nothing for as long as it is left running. That is
the same shape as the outage these tests were written for, so it gets its own
case rather than being folded into the reconnect one.

Properties, each one a way the loop could fail quietly:

  * a dropped channel reconnects, and reconnects to the same routed endpoint;
  * every connect re-sends the SUBSCRIBE, including reconnects;
  * a reconnect clears the freshness timestamps, so no message received before
    the drop can make the new socket look fresh;
  * one message delivered once is processed once, across the drop;
  * a subscription acknowledgement is not mistaken for market data;
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
    """One frame, shaped the way Binance sends it on a routed socket.

    The routed endpoint delivers the raw event with no combined-stream
    envelope; ingest_event accepts either shape.
    """
    return json.dumps({
        "e": "kline", "s": "BTCUSDT", "k": {
            "t": open_ms, "T": open_ms + 299_999, "i": "5m", "x": closed,
            "o": "100.0", "h": "101.0", "l": "99.0", "c": close, "v": "12.5"},
    })


class Dropped(Exception):
    """The venue closed the socket."""


class FakeSocket:
    def __init__(self, messages: list[str], *, then: Exception | None):
        self._messages, self._then = list(messages), then
        self.closed = False
        self.sent: list[dict] = []

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

    async def send(self, payload):
        self.sent.append(json.loads(payload))

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
        self.handed_out: list[FakeSocket] = []

    def connect(self, url, **_kwargs):
        self.urls.append(url)
        if not self._sockets:
            self._feed._stop.set()
            raise Dropped("the script is exhausted")
        socket = self._sockets.pop(0)
        self.handed_out.append(socket)
        return socket

    def subscriptions(self) -> list[list[str]]:
        """What each socket was actually asked to subscribe to."""
        return [message["params"] for socket in self.handed_out
                for message in socket.sent if message.get("method") == "SUBSCRIBE"]


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
            if channel == "market":
                url, subs = feed.market_url, feed.market_subscriptions
            else:
                url, subs = feed.public_url, feed.public_subscriptions
            await asyncio.wait_for(
                feed._stream_channel(channel, url, fake, subs), timeout=5)
        finally:
            asyncio.sleep = real_sleep               # type: ignore[assignment]

    asyncio.run(main())


# ───────────────────────────────── reconnect ──────────────────────────────

def test_a_dropped_channel_reconnects_to_the_same_routed_endpoint():
    feed = stream()
    fake = FakeWebsockets(feed, [
        FakeSocket([kline(1_700_000_000_000, closed=True)], then=Dropped("reset")),
        FakeSocket([kline(1_700_000_300_000, closed=True)], then=None),
    ])

    run_channel(feed, fake)

    assert len(fake.urls) >= 2, "the channel never reconnected"
    assert len(set(fake.urls)) == 1, f"reconnected to a different URL: {set(fake.urls)}"
    assert fake.urls[0] == "wss://fstream.binance.com/market/ws"


def test_every_connect_resubscribes_including_the_reconnect():
    """The silent failure this protocol allows: a socket that opens, stays
    open and was never told what to send. The venue does not carry a
    subscription across a reconnect, so neither may this loop."""
    feed = stream()
    fake = FakeWebsockets(feed, [FakeSocket([], then=Dropped("reset")),
                                 FakeSocket([], then=None)])

    run_channel(feed, fake)

    assert fake.subscriptions() == [
        ["btcusdt@kline_5m", "btcusdt@markPrice@1s"],
        ["btcusdt@kline_5m", "btcusdt@markPrice@1s"],
    ], "a connect opened a socket without subscribing"


def test_each_subscribe_carries_its_own_request_id():
    """Two requests sharing an id make the two replies indistinguishable."""
    feed = stream()
    fake = FakeWebsockets(feed, [FakeSocket([], then=Dropped("reset")),
                                 FakeSocket([], then=None)])

    run_channel(feed, fake)

    ids = [message["id"] for socket in fake.handed_out for message in socket.sent]
    assert len(ids) == len(set(ids)), f"reused a request id: {ids}"


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


# ──────────────────────────────── control frames ──────────────────────────

def test_a_subscription_acknowledgement_is_not_market_data():
    """{"result": null, "id": 1} is a receipt, not a candle. ingest_event
    stamps last_update on anything it is handed, so an ack reaching it would
    let a feed that has received nothing report itself fresh."""
    feed = stream()
    ack = json.dumps({"result": None, "id": 1})
    fake = FakeWebsockets(feed, [FakeSocket([ack], then=None)])

    run_channel(feed, fake)

    assert feed.last_update is None, "an acknowledgement was counted as data"
    assert feed.last_candle_update is None


def test_a_refused_subscription_is_recorded_rather_than_ignored():
    """A socket subscribed to nothing is silent exactly like a healthy socket
    in a quiet market. The refusal is the only thing that tells them apart.

    It is asserted on the event sink rather than on last_error because
    last_error is transient by construction: _set_state reassigns it on every
    transport transition, and the reconnect that follows a refusal overwrites
    it within milliseconds. The emitted event is the durable record, and it is
    the one an operator reads back.
    """
    events: list[dict] = []
    feed = PriceActionPublicStream(lambda *a, **k: [], clock=lambda: NOW,
                                   event_sink=events.append)
    feed.symbol, feed.timeframe = "BTCUSDT", "5m"
    refusal = json.dumps({"error": {"code": 2, "msg": "Invalid request"}, "id": 1})
    fake = FakeWebsockets(feed, [FakeSocket([refusal], then=None)])

    run_channel(feed, fake)

    refused = [event for event in events if event["kind"] == "subscription_refused"]
    assert len(refused) == 1, [event["kind"] for event in events]
    assert "Invalid request" in refused[0]["error"]
    assert refused[0]["channel"] == "market"
    assert feed.last_update is None


def test_a_real_candle_still_reaches_ingest_after_the_ack():
    """The filter must take out receipts and nothing else."""
    feed = stream()
    messages = [json.dumps({"result": None, "id": 1}),
                kline(1_700_000_000_000, closed=True)]
    fake = FakeWebsockets(feed, [FakeSocket(messages, then=None)])

    run_channel(feed, fake)

    assert len(feed.snapshot()["closed_bars"]) == 1
    assert feed.last_candle_update == NOW


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
def test_only_a_quote_carrying_channel_subscribes_to_book_ticker(
        monkeypatch, quotes, expect_book_ticker):
    """Which sockets get opened is decided in _stream_forever, so that is what
    this drives -- calling the per-channel loop directly would assert nothing
    about the choice.

    A higher-timeframe context channel exists to carry kline alone. Opening a
    bookTicker subscription from it would add a second, redundant quote feed
    per symbol whose messages no fill may ever be driven by.
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

    subscribed = [name for params in fake.subscriptions() for name in params]
    assert any("bookTicker" in name for name in subscribed) is expect_book_ticker, subscribed
    assert any("kline_5m" in name for name in subscribed), "kline was never subscribed"
    if not quotes:
        assert feed.status()["transport_channels"]["public"] == "DISCONNECTED"
