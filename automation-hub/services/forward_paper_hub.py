"""One Binance USD-M public feed fanned out to isolated paper engines.

The hub owns transport and clock identity.  Consumers receive the exact same
closed :class:`~bot.types.Bar` object and quote snapshot, but they never share
accounts, orders, positions, risk state, pause state, or strategy state.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from bot.types import Bar
from data.market_data_v2 import TF_MS, normalize_symbol
from services.mtf_policy import canonical_candle_id
from services.price_action_stream import PriceActionPublicStream


def candle_id(symbol: str, timeframe: str, bar: Bar) -> str:
    return canonical_candle_id(symbol, timeframe, bar)


@dataclass
class _Consumer:
    consumer_id: str
    bar_sink: Callable[[Bar], None] | None = None
    quote_sink: Callable[[dict], None] | None = None
    event_sink: Callable[[dict], None] | None = None
    # A closed-candle notice, not a delivery. Unlike bar_sink it is never
    # queued, retried or reported as pending, because there is nothing to
    # replay: it tells a consumer that owns its own candle cursor that the
    # cursor has moved, and that consumer then reads the feed itself. It runs
    # inline on the stream's thread, so a notice must do no I/O and take no
    # lock a slow path could hold.
    candle_notice: Callable[[Bar], None] | None = None
    pending: OrderedDict = field(default_factory=OrderedDict)
    delivery_lock: threading.Lock = field(default_factory=threading.Lock)
    last_error: str = ""
    # One worker, so this consumer's quotes and events reach it in arrival
    # order. A shared pool would let two quotes run at once, and the broker
    # keeps a per-symbol quote cursor that rejects anything not newer than the
    # last one it saw, so a reordered pair would have one silently dropped and
    # an intent left unfilled.
    notifier: object | None = None


@dataclass
class _Channel:
    stream: PriceActionPublicStream
    consumers: dict[str, _Consumer] = field(default_factory=dict)
    last_candle_id: str | None = None
    quote_sequence: int = 0


class ForwardPaperMarketDataHub:
    """Own one public stream per symbol/timeframe and fan it out exactly once."""

    def __init__(self, rest_loader: Callable, *, stream_factory=PriceActionPublicStream):
        self.rest_loader = rest_loader
        self.stream_factory = stream_factory
        self._channels: dict[tuple[str, str], _Channel] = {}
        self._consumer_keys: dict[str, tuple[str, str]] = {}
        self._lock = threading.RLock()
        self._retry_timer: threading.Timer | None = None
        # Bar delivery runs here rather than on the caller's thread. Bounded on
        # purpose: a consumer that falls behind must queue in its own pending
        # map, which preserves candle order, instead of spawning a thread per
        # candle. Synchronous delivery stays available for tests, which need
        # the sink to have run by the time the emitting call returns.
        self._delivery: ThreadPoolExecutor | None = None
        self.synchronous_delivery = False

    def _dispatch_notify(self, consumer: _Consumer, sink, event) -> None:
        """Deliver a quote or event without holding up the socket reader.

        Unlike candles these are not queued or replayed: a quote is only
        meaningful at the price it carried, so a consumer that cannot keep up
        must miss one rather than be filled at a stale price later.
        """
        if self.synchronous_delivery:
            self._notify(consumer, sink, event)
            return
        pool = consumer.notifier
        if pool is None:
            return  # detached; its queued quotes are no longer wanted
        try:
            pool.submit(self._notify, consumer, sink, event)
        except RuntimeError:
            pass  # shutting down; a dropped quote is correct here

    def _dispatch(self, consumer: _Consumer) -> None:
        """Hand one consumer's pending candles to the delivery pool."""
        if self.synchronous_delivery:
            self._deliver_pending(consumer)
            return
        with self._lock:
            if self._delivery is None:
                self._delivery = ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="forward-paper-deliver")
            pool = self._delivery
        try:
            pool.submit(self._deliver_pending, consumer)
        except RuntimeError:
            # The pool is shutting down; the retry timer still owns the backlog.
            self._retry_later()

    def _retry_later(self):
        with self._lock:
            if self._retry_timer is None:
                self._retry_timer = threading.Timer(1.0, self.retry_failed)
                self._retry_timer.daemon = True
                self._retry_timer.start()

    def retry_failed(self):
        """Retry only failed subscribers, preserving their candle order."""
        with self._lock:
            self._retry_timer = None
            consumers = [consumer for channel in self._channels.values()
                         for consumer in channel.consumers.values() if consumer.pending]
        for consumer in consumers:
            self._deliver_pending(consumer)

    def _deliver_pending(self, consumer):
        if not consumer.delivery_lock.acquire(blocking=False):
            self._retry_later()
            return
        try:
            while True:
                with self._lock:
                    if not consumer.pending:
                        return
                    cid, bar = consumer.pending.popitem(last=False)
                    consumer.last_error = ""
                try:
                    consumer.bar_sink(bar)
                except Exception as exc:
                    with self._lock:
                        consumer.pending[cid] = bar
                        consumer.pending.move_to_end(cid, last=False)
                        consumer.last_error = f"{type(exc).__name__}: {exc}"
                    self._retry_later()
                    return
        finally:
            consumer.delivery_lock.release()

    @staticmethod
    def _notify(consumer, sink, event):
        try:
            sink(dict(event))
        except Exception as exc:
            # Consumer failures are not transport failures. Candle retries are
            # tracked separately; quotes are never replayed at a later price.
            consumer.last_error = f"{type(exc).__name__}: {exc}"

    def subscription(
        self,
        consumer_id: str,
        *,
        bar_sink: Callable[[Bar], None] | None = None,
        quote_sink: Callable[[dict], None] | None = None,
        event_sink: Callable[[dict], None] | None = None,
        candle_notice: Callable[[Bar], None] | None = None,
    ) -> "ForwardPaperSubscription":
        if not str(consumer_id).strip():
            raise ValueError("forward-paper consumer_id is required")
        return ForwardPaperSubscription(
            self, str(consumer_id), bar_sink=bar_sink,
            quote_sink=quote_sink, event_sink=event_sink,
            candle_notice=candle_notice,
        )

    def _channel(self, key: tuple[str, str]) -> _Channel:
        with self._lock:
            existing = self._channels.get(key)
            if existing is not None:
                return existing
            symbol, timeframe = key
            holder: dict[str, _Channel] = {}

            def on_bar(bar: Bar) -> None:
                channel = holder["channel"]
                cid = candle_id(symbol, timeframe, bar)
                with self._lock:
                    channel.last_candle_id = cid
                    consumers = list(channel.consumers.values())
                for consumer in consumers:
                    if consumer.candle_notice:
                        try:
                            consumer.candle_notice(bar)
                        except Exception as exc:
                            consumer.last_error = f"{type(exc).__name__}: {exc}"
                for consumer in consumers:
                    if consumer.bar_sink:
                        with self._lock:
                            consumer.pending.setdefault(cid, bar)
                        # Deliver off this thread. This callback runs inside the
                        # stream's asyncio loop, and a bar sink is not cheap: it
                        # drives a lab's whole closed-candle path, rebuilding a
                        # market structure engine over hundreds of bars and
                        # writing SQLite, once per subscribed consumer. Running
                        # that inline stopped the loop reading its sockets, so
                        # markPrice at one message per second went eighty
                        # seconds without an update while bookTicker on the
                        # other socket read zero, and the feed failed its
                        # fifteen-second staleness check for reasons that had
                        # nothing to do with Binance. Ordering is unaffected:
                        # pending is an ordered map drained under the
                        # consumer's own delivery lock, which is exactly the
                        # path a retry already takes.
                        self._dispatch(consumer)

            def on_quote(quote: dict) -> None:
                with self._lock:
                    channel = holder["channel"]
                    channel.quote_sequence += 1
                    consumers = list(channel.consumers.values())
                    cid = channel.last_candle_id
                    sequence = int(quote.get("sequence") or channel.quote_sequence)
                    event_timestamp = str(quote.get("event_timestamp") or
                                          quote.get("received_at") or "")
                    identity = {
                        "source": "BINANCE_USDM_PUBLIC_WEBSOCKET",
                        "symbol": symbol, "timeframe": timeframe,
                        "event_timestamp": event_timestamp, "sequence": sequence,
                        "bid": quote.get("bid"), "ask": quote.get("ask"),
                        "mark": quote.get("mark"),
                    }
                    quote_event_id = "quote-" + hashlib.sha256(json.dumps(
                        identity, sort_keys=True, separators=(",", ":")
                    ).encode()).hexdigest()[:32]
                snapshot = {
                    **quote,
                    "candle_id": cid,
                    "event_timestamp": event_timestamp,
                    "sequence": sequence,
                    "quote_event_id": quote_event_id,
                    "market_data_source": "Binance USD-M public WebSocket",
                }
                for consumer in consumers:
                    if consumer.quote_sink:
                        # Off the socket reader's thread, for the same reason
                        # bars are. A quote sink looks cheap and is not: it
                        # takes the lab's runtime and account locks and fills
                        # pending orders. A thread dump caught this callback
                        # parked on an account lock inside the stream's event
                        # loop, which is why markPrice at one message per
                        # second still read eighty seconds old after bar
                        # delivery had already been moved off.
                        self._dispatch_notify(consumer, consumer.quote_sink, snapshot)

            def on_event(event: dict) -> None:
                with self._lock:
                    consumers = list(holder["channel"].consumers.values())
                for consumer in consumers:
                    if consumer.event_sink:
                        self._notify(consumer, consumer.event_sink, event)

            stream = self.stream_factory(
                self.rest_loader, bar_sink=on_bar, quote_sink=on_quote,
                event_sink=on_event,
            )
            channel = _Channel(stream=stream)
            holder["channel"] = channel
            self._channels[key] = channel
            return channel

    def _start(self, consumer: _Consumer, symbol: str, timeframe: str) -> bool:
        symbol = normalize_symbol(symbol)
        if timeframe not in TF_MS:
            raise ValueError(f"unsupported timeframe '{timeframe}'")
        key = (symbol, timeframe)
        self._detach(consumer.consumer_id, stop_empty=True)
        channel = self._channel(key)
        with self._lock:
            channel.consumers[consumer.consumer_id] = consumer
            self._consumer_keys[consumer.consumer_id] = key
        started = channel.stream.start(symbol, timeframe)
        if not started:
            self._detach(consumer.consumer_id, stop_empty=True)
        return started

    def _detach(self, consumer_id: str, *, stop_empty: bool) -> None:
        stream = None
        with self._lock:
            key = self._consumer_keys.pop(consumer_id, None)
            if key is None:
                return
            channel = self._channels.get(key)
            if channel is None:
                return
            consumer = channel.consumers.pop(consumer_id, None)
            if consumer:
                consumer.pending.clear()
                consumer.last_error = ""
                # The notifier deliberately survives. _start() detaches before
                # re-attaching the same consumer to a new channel, so tearing
                # it down here would leave a live subscription unable to
                # deliver another quote for the rest of the process.
            if stop_empty and not channel.consumers:
                self._channels.pop(key, None)
                stream = channel.stream
        if stream is not None:
            stream.stop()

    def _for(self, consumer_id: str) -> _Channel | None:
        with self._lock:
            key = self._consumer_keys.get(consumer_id)
            return self._channels.get(key) if key else None

    def stop(self) -> None:
        with self._lock:
            if self._retry_timer is not None:
                self._retry_timer.cancel()
                self._retry_timer = None
            streams = [channel.stream for channel in self._channels.values()]
            notifiers = [consumer.notifier
                         for channel in self._channels.values()
                         for consumer in channel.consumers.values()
                         if consumer.notifier is not None]
            self._channels.clear()
            self._consumer_keys.clear()
            pool, self._delivery = self._delivery, None
        for stream in streams:
            stream.stop()
        for notifier in notifiers:
            notifier.shutdown(wait=False)
        if pool is not None:
            # Let in-flight candles finish; they hold the consumer's delivery
            # lock and a half-applied one would leave a gap in its history.
            pool.shutdown(wait=True)


class ForwardPaperSubscription:
    """Stream-compatible, consumer-scoped view of the shared hub."""

    def __init__(self, hub: ForwardPaperMarketDataHub, consumer_id: str, **sinks):
        self.hub = hub
        self.consumer = _Consumer(consumer_id=consumer_id, **sinks)
        # Built here, once, rather than lazily on first quote. Creating it
        # under the hub's lock put a global acquisition on the hottest path in
        # the system: every quote, on every channel, while the notify workers
        # were contending for the same lock through subscription.status(). A
        # thread dump caught six stream threads and three notify workers all
        # queued on it, with the app burning six cores. An executor costs
        # nothing until its first submit, so there is no reason to defer it.
        self.consumer.notifier = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="notify-%s" % consumer_id[:16])
        self.symbol = ""
        self.timeframe = ""
        # Context fetches need concurrent native channels (for example 5m,
        # 1h, and 4h).  They must not retune the primary subscription by
        # repeatedly detaching it from one channel and attaching it to another.
        self._native_fetch_subscriptions: dict[tuple[str, str], ForwardPaperSubscription] = {}

    @property
    def consumer_id(self) -> str:
        return self.consumer.consumer_id

    def start(self, symbol: str, timeframe: str) -> bool:
        started = self.hub._start(self.consumer, symbol, timeframe)
        if started:
            self.symbol, self.timeframe = normalize_symbol(symbol), timeframe
        return started

    def stop(self) -> None:
        children = list(self._native_fetch_subscriptions.values())
        self._native_fetch_subscriptions.clear()
        for child in children:
            child.stop()
        self.hub._detach(self.consumer_id, stop_empty=True)
        # This subscription is finished, unlike a detach during a restart, so
        # its worker goes with it. Queued quotes are dropped rather than
        # drained: one is only meaningful at the price it carried.
        notifier, self.consumer.notifier = self.consumer.notifier, None
        if notifier is not None:
            notifier.shutdown(wait=False)

    @property
    def running(self) -> bool:
        channel = self.hub._for(self.consumer_id)
        return bool(channel and channel.stream.running)

    def status(self) -> dict:
        channel = self.hub._for(self.consumer_id)
        if channel is None:
            return {
                "state": "DISCONNECTED", "transport_state": "DISCONNECTED",
                "health_reason": "forward-paper hub subscription is not started",
                "reliable": False, "new_entries_paused": True,
                "market_data_source": "Binance USD-M public WebSocket",
                "consumer_id": self.consumer_id,
            }
        status = {
            **channel.stream.status(),
            "consumer_id": self.consumer_id,
            "candle_id": channel.last_candle_id,
            "market_data_source": "Binance USD-M public WebSocket",
        }
        with self.hub._lock:
            pending = list(self.consumer.pending)
            error = self.consumer.last_error
        if pending:
            status.update({"reliable": False, "new_entries_paused": True,
                           "failing_dependency": "LAB_CANDLE_DELIVERY",
                           "health_reason": error or "subscriber candle delivery pending"})
        status["subscriber_delivery"] = {"pending_candle_ids": pending, "last_error": error or None}
        return status

    def snapshot(self) -> dict:
        channel = self.hub._for(self.consumer_id)
        if channel is None:
            return {
                "closed_bars": [], "forming": None, "quote": {},
                "connection": self.status(),
            }
        raw = channel.stream.snapshot()
        return {
            **raw,
            "connection": self.status(),
            "candle_id": channel.last_candle_id,
        }

    def make_fetcher(self, fallback: Callable | None = None):
        def fetch(symbol: str, timeframe: str, limit: int, **_kwargs):
            requested = (normalize_symbol(symbol), timeframe)
            if requested == (self.symbol, self.timeframe) and self.running:
                source = self
            else:
                source = self._native_fetch_subscriptions.get(requested)
                if source is None:
                    source = self.hub.subscription(
                        f"{self.consumer_id}:native:{requested[0]}:{requested[1]}"
                    )
                    self._native_fetch_subscriptions[requested] = source
                if not source.running and not source.start(*requested):
                    raise RuntimeError(
                        f"Binance USD-M hub could not start {requested[0]} {requested[1]}"
                    )
            bars = source.snapshot()["closed_bars"]
            if not bars:
                if fallback is not None:
                    return fallback(symbol, timeframe, limit, **_kwargs)
                raise RuntimeError("Binance USD-M hub has no closed candles")
            return list(bars[-max(1, int(limit)):]), "live (binance_usdm_hub)"
        return fetch
