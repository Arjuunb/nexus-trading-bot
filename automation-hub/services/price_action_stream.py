"""Public Binance USD-M stream for the isolated Price Action Visual Lab.

Only Binance's public routed streams are used. REST remains authoritative for
bootstrap and gap repair. The class is deliberately injectable so parsing,
deduplication, staleness and recovery can be tested without a network connection.
"""
from __future__ import annotations

import asyncio
import json
import random
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable

from bot.types import Bar
from data.market_data_v2 import TF_MS, normalize_symbol


CONNECTION_STATES = {"CONNECTING", "CONNECTED", "DELAYED", "RECONNECTING", "DISCONNECTED", "ERROR"}
HEALTH_STATES = {
    "CONNECTING", "LOADING_HISTORY", "RECONCILING", "SYNCHRONIZED",
    "DELAYED", "STALE_CANDLES", "STALE_QUOTE", "STALE_MARK",
    "QUOTE_MISMATCH", "RECONNECTING", "DISCONNECTED", "DATA_ERROR", "ERROR",
}


class PriceActionPublicStream:
    def __init__(self, rest_loader: Callable, *, max_bars: int = 1500,
                 stale_after_seconds: float = 15.0,
                 event_sink: Callable[[dict], None] | None = None,
                 bar_sink: Callable[[Bar], None] | None = None,
                 quote_sink: Callable[[dict], None] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 quote_mismatch_bps: float = 100.0,
                 quotes_enabled: bool = True):
        self.rest_loader = rest_loader
        self.max_bars = max_bars
        self.stale_after_seconds = stale_after_seconds
        self.event_sink = event_sink
        self.bar_sink = bar_sink
        self.quote_sink = quote_sink
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.quote_mismatch_bps = float(quote_mismatch_bps)
        # markPrice and bookTicker are per-SYMBOL, not per-timeframe. A channel
        # opened purely to supply higher-timeframe candles (the native 1h/4h
        # context every MTF strategy needs) therefore subscribed to the exact
        # same two quote streams the symbol's entry channel was already
        # reading: with three instances on three symbols that was six redundant
        # markPrice subscriptions and six redundant bookTicker sockets. Those
        # channels take kline only; the entry channel remains the single
        # quote authority for its symbol, so no fill can ever be driven by a
        # duplicate quote arriving on a context channel.
        self.quotes_enabled = bool(quotes_enabled)
        self.symbol = ""
        self.timeframe = ""
        self.state = "DISCONNECTED"
        self.last_error = ""
        self.last_update: datetime | None = None
        self.last_candle_update: datetime | None = None
        self.last_quote_update: datetime | None = None
        self.last_mark_update: datetime | None = None
        self.last_closed_update: datetime | None = None
        self.started_at: datetime | None = None
        self.reconnect_attempt = 0
        self.duplicate_events = 0
        self.missing_candles = 0
        self.reconciled_candles = 0
        self.persistence_blocked_events = 0
        self.last_sink_error = ""
        self.history_loaded = False
        self.reconciliation_complete = False
        self._health_state = "DISCONNECTED"
        self._health_reason = "public stream is disconnected"
        self._bars: deque[Bar] = deque(maxlen=max_bars)
        self._forming: Bar | None = None
        self._quote = {"last": None, "bid": None, "ask": None, "mark": None,
                       "funding_rate": None, "next_funding_time": None}
        self._quote_sequence = 0
        self._seen_events: set[tuple] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop = None
        self._channel_states = {"market": "DISCONNECTED", "public": "DISCONNECTED"}
        self._channel_errors = {"market": "", "public": ""}
        self._reconnect_attempts = {"market": 0, "public": 0}
        self._socket = None
        self._public_socket = None

    def _set_state(self, state: str, error: str = "") -> None:
        if state not in CONNECTION_STATES:
            raise ValueError(state)
        changed = state != self.state or error != self.last_error
        self.state, self.last_error = state, error[:500]
        if changed and self.event_sink:
            self.event_sink({"kind": "connection", "state": state, "error": self.last_error,
                             "timestamp": self.clock().isoformat(),
                             "symbol": self.symbol, "timeframe": self.timeframe})

    def _emit_health(self, state: str, reason: str, *, emit_event: bool = True) -> None:
        if state not in HEALTH_STATES:
            raise ValueError(state)
        changed = state != self._health_state or reason != self._health_reason
        self._health_state, self._health_reason = state, reason
        if changed and emit_event and self.event_sink:
            self.event_sink({
                "kind": "market_data_health", "state": state, "reason": reason,
                "timestamp": self.clock().isoformat(), "symbol": self.symbol,
                "timeframe": self.timeframe,
            })

    def _deliver_closed_bar(self, bar: Bar) -> bool:
        """Deliver a candle without translating journal contention to a feed outage."""
        if not self.bar_sink:
            return True
        try:
            self.bar_sink(bar)
            self.last_sink_error = ""
            return True
        except Exception as exc:
            if getattr(exc, "code", None) != "PERSISTENCE_BLOCKED":
                raise
            self.persistence_blocked_events += 1
            self.last_sink_error = str(exc)[:500]
            if self.event_sink:
                self.event_sink({
                    "kind": "persistence", "state": "PERSISTENCE_BLOCKED",
                    "reason": self.last_sink_error, "retryable": True,
                    "timestamp": self.clock().isoformat(), "symbol": self.symbol,
                    "timeframe": self.timeframe,
                })
            return False

    def _deliver_quote(self, event: str, received_at: datetime, *,
                       event_timestamp: datetime | None = None,
                       sequence: int | None = None) -> bool:
        """Publish one immutable public quote snapshot to paper consumers.

        The callback runs only after the stream state has been updated.  It is
        deliberately separate from ``bar_sink`` because paper fills require a
        quote whose receipt time is strictly after the strategy decision.
        """
        if not self.quote_sink:
            return True
        with self._lock:
            self._quote_sequence += 1
            payload = {
                **self._quote,
                "event": event,
                "source": "BINANCE_USDM_PUBLIC_WEBSOCKET",
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "received_at": received_at.isoformat(),
                "event_timestamp": (event_timestamp or received_at).isoformat(),
                "sequence": int(sequence if sequence is not None else self._quote_sequence),
            }
        try:
            self.quote_sink(payload)
            return True
        except Exception as exc:
            if getattr(exc, "code", None) != "PERSISTENCE_BLOCKED":
                raise
            self.persistence_blocked_events += 1
            self.last_sink_error = str(exc)[:500]
            if self.event_sink:
                self.event_sink({
                    "kind": "persistence", "state": "PERSISTENCE_BLOCKED",
                    "reason": self.last_sink_error, "retryable": True,
                    "timestamp": received_at.isoformat(), "symbol": self.symbol,
                    "timeframe": self.timeframe,
                })
            return False

    def _refresh_transport_state(self) -> None:
        """Aggregate the required routed Binance transports into one state."""
        with self._lock:
            states = dict(self._channel_states)
            errors = {key: value for key, value in self._channel_errors.items() if value}
            reconciliation_pending = self.history_loaded and not self.reconciliation_complete
        if all(value == "CONNECTED" for value in states.values()):
            state = "DELAYED" if reconciliation_pending else "CONNECTED"
        elif all(value == "DISCONNECTED" for value in states.values()):
            state = "DISCONNECTED"
        elif any(value == "RECONNECTING" for value in states.values()):
            state = "RECONNECTING"
        elif any(value == "ERROR" for value in states.values()):
            state = "ERROR"
        else:
            state = "CONNECTING"
        error = "; ".join(f"{key}: {value}" for key, value in sorted(errors.items()))
        if state == "DELAYED" and not error:
            error = (self.last_error if self.state == "DELAYED" and self.last_error
                     else "gap reconciliation incomplete")
        self._set_state(state, error)

    def _set_channel_state(self, channel: str, state: str, error: str = "") -> None:
        if channel not in self._channel_states:
            raise ValueError(channel)
        if state not in CONNECTION_STATES:
            raise ValueError(state)
        with self._lock:
            changed = (self._channel_states[channel], self._channel_errors[channel]) != (state, error[:500])
            self._channel_states[channel] = state
            self._channel_errors[channel] = error[:500]
        if changed and self.event_sink:
            self.event_sink({
                "kind": "connection_channel", "channel": channel, "state": state,
                "error": error[:500], "timestamp": self.clock().isoformat(),
                "symbol": self.symbol, "timeframe": self.timeframe,
            })
        self._refresh_transport_state()

    @staticmethod
    def _bar(kline: dict) -> Bar:
        return Bar(datetime.fromtimestamp(int(kline["t"]) / 1000, tz=timezone.utc),
                   float(kline["o"]), float(kline["h"]), float(kline["l"]),
                   float(kline["c"]), float(kline.get("v") or 0))

    def bootstrap(self, symbol: str, timeframe: str) -> list[Bar]:
        symbol = normalize_symbol(symbol)
        if timeframe not in TF_MS:
            raise ValueError(f"unsupported timeframe '{timeframe}'")
        rows = self.rest_loader(symbol, timeframe, limit=self.max_bars)
        now = self.clock()
        step = timedelta(milliseconds=TF_MS[timeframe])
        closed = sorted({row.timestamp: row for row in rows if row.timestamp + step <= now}.values(),
                        key=lambda row: row.timestamp)
        with self._lock:
            self.symbol, self.timeframe = symbol, timeframe
            self._bars = deque(closed[-self.max_bars:], maxlen=self.max_bars)
            self._forming = next((row for row in reversed(rows) if row.timestamp + step > now), None)
            self.last_closed_update = closed[-1].timestamp + step if closed else None
            self.history_loaded = bool(closed)
            self.reconciliation_complete = False
            self.last_update = None
            self.last_candle_update = None
            self.last_quote_update = None
            self.last_mark_update = None
            self._quote = {"last": None, "bid": None, "ask": None, "mark": None,
                           "funding_rate": None, "next_funding_time": None}
            self._seen_events.clear()
        return closed

    def start(self, symbol: str, timeframe: str) -> bool:
        symbol = normalize_symbol(symbol)
        if self._thread and self._thread.is_alive() and (symbol, timeframe) == (self.symbol, self.timeframe):
            return True
        self.stop()
        self._stop.clear()
        with self._lock:
            self._channel_states = {"market": "CONNECTING", "public": "CONNECTING"}
            self._channel_errors = {"market": "", "public": ""}
            self._reconnect_attempts = {"market": 0, "public": 0}
            self.reconnect_attempt = 0
        self._set_state("CONNECTING")
        self.started_at = self.clock()
        try:
            self.bootstrap(symbol, timeframe)
        except Exception as exc:
            self._set_state("ERROR", f"REST bootstrap failed: {exc}")
            return False
        self._thread = threading.Thread(target=self._run, name="pa-public-stream", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        sockets = [socket for socket in (self._socket, self._public_socket) if socket is not None]
        if loop and loop.is_running():
            for socket in sockets:
                try:
                    asyncio.run_coroutine_threadsafe(socket.close(), loop).result(timeout=2)
                except Exception:
                    pass
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._thread = None
        self._socket = None
        self._public_socket = None
        with self._lock:
            self._channel_states = {"market": "DISCONNECTED", "public": "DISCONNECTED"}
            self._channel_errors = {"market": "", "public": ""}
        self._set_state("DISCONNECTED")

    @property
    def running(self) -> bool:
        """Whether the websocket worker is alive for the current identity."""
        thread = self._thread
        return bool(thread and thread.is_alive() and not self._stop.is_set())

    @property
    def market_url(self) -> str:
        lower = self.symbol.lower()
        streams = f"{lower}@kline_{self.timeframe}"
        if self.quotes_enabled:
            streams += f"/{lower}@markPrice@1s"
        return f"wss://fstream.binance.com/market/stream?streams={streams}"

    @property
    def public_url(self) -> str:
        lower = self.symbol.lower()
        return f"wss://fstream.binance.com/public/stream?streams={lower}@bookTicker"

    @property
    def url(self) -> str:
        """Backward-compatible alias for the regular market-data route."""
        return self.market_url

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._stream_forever())
        finally:
            loop.close()
            self._loop = None
            if not self._stop.is_set():
                self._set_state("ERROR", self.last_error or "stream worker stopped")

    async def _stream_forever(self) -> None:
        try:
            import websockets
        except Exception as exc:
            error = f"websockets unavailable: {exc}"
            self._set_channel_state("market", "ERROR", error)
            self._set_channel_state("public", "ERROR", error)
            return
        readers = [self._stream_channel("market", self.market_url, websockets)]
        if self.quotes_enabled:
            readers.append(self._stream_channel("public", self.public_url, websockets))
        else:
            # Nothing will ever connect this channel, so leave it in a state
            # that reads as deliberate rather than as a socket that failed.
            self._set_channel_state("public", "DISCONNECTED")
        await asyncio.gather(*readers)

    async def _stream_channel(self, channel: str, url: str, websockets) -> None:
        while not self._stop.is_set():
            try:
                attempt = self._reconnect_attempts[channel]
                self._set_channel_state(channel, "CONNECTING" if attempt == 0 else "RECONNECTING")
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              close_timeout=5, max_queue=2048) as socket:
                    if channel == "market":
                        self._socket = socket
                    else:
                        self._public_socket = socket
                    self._reconnect_attempts[channel] = 0
                    self.reconnect_attempt = max(self._reconnect_attempts.values())
                    self._set_channel_state(channel, "CONNECTED")
                    with self._lock:
                        # Values may remain useful for visual continuity, but no
                        # prior socket receipt may prove the new connection fresh.
                        if channel == "market":
                            self.last_candle_update = None
                            self.last_mark_update = None
                            self.reconciliation_complete = False
                        else:
                            self.last_quote_update = None
                    if channel == "market":
                        await self.reconcile()
                    async for raw in socket:
                        if self._stop.is_set():
                            break
                        result = self.ingest_event(json.loads(raw))
                        if channel == "market" and result.get("closed") and self.state == "DELAYED":
                            await self.reconcile()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._reconnect_attempts[channel] += 1
                self.reconnect_attempt = max(self._reconnect_attempts.values())
                self._set_channel_state(channel, "RECONNECTING", f"{type(exc).__name__}: {exc}")
                # Full jitter on the backoff. Without it every channel that
                # dropped in the same Binance blip retries on the same tick:
                # three instances is nine channels, and they would reconnect in
                # lockstep for as long as the outage lasted, which is how a
                # recovering venue turns into a rate-limit ban. The window is
                # unchanged; only the moment inside it is randomised.
                ceiling = min(2 ** min(self._reconnect_attempts[channel], 5), 30)
                await asyncio.sleep(random.uniform(ceiling / 2, ceiling))
            finally:
                if channel == "market":
                    self._socket = None
                else:
                    self._public_socket = None
        self._set_channel_state(channel, "DISCONNECTED")

    async def reconcile(self) -> int:
        """Recover missing closed bars using REST before entries may resume."""
        try:
            rows = await asyncio.to_thread(self.rest_loader, self.symbol, self.timeframe, limit=self.max_bars)
            now = self.clock()
            step = timedelta(milliseconds=TF_MS[self.timeframe])
            count = 0
            for row in sorted(rows, key=lambda item: item.timestamp):
                if row.timestamp + step > now:
                    continue
                with self._lock:
                    known = any(existing.timestamp == row.timestamp for existing in self._bars)
                if not known:
                    if self._merge_closed(row):
                        count += 1
                        self._deliver_closed_bar(row)
            self.reconciled_candles += count
            with self._lock:
                self.reconciliation_complete = self._unresolved_gaps() == 0
            self._refresh_transport_state()
            return count
        except Exception as exc:
            with self._lock:
                self.reconciliation_complete = False
            self._set_state("DELAYED", f"gap reconciliation failed: {exc}")
            return 0

    def _unresolved_gaps(self) -> int:
        if len(self._bars) < 2 or not self.timeframe:
            return 0
        step = timedelta(milliseconds=TF_MS[self.timeframe])
        return sum(
            max(0, int((right.timestamp - left.timestamp) / step) - 1)
            for left, right in zip(self._bars, list(self._bars)[1:])
        )

    def _message_identity(self, message: dict, data: dict, event: str | None) -> tuple[str | None, str | None]:
        stream = str(message.get("stream") or "")
        stream_symbol = stream.split("@", 1)[0].upper() if "@" in stream else None
        symbol = data.get("s") or (data.get("k") or {}).get("s") or stream_symbol
        timeframe = (data.get("k") or {}).get("i") if event == "kline" else None
        return (normalize_symbol(str(symbol)) if symbol else None, str(timeframe) if timeframe else None)

    def _merge_closed(self, bar: Bar) -> bool:
        with self._lock:
            if any(row.timestamp == bar.timestamp for row in self._bars):
                self.duplicate_events += 1
                return False
            step = timedelta(milliseconds=TF_MS[self.timeframe])
            if self._bars and bar.timestamp > self._bars[-1].timestamp + step:
                self.missing_candles += int((bar.timestamp - self._bars[-1].timestamp) / step) - 1
                self.reconciliation_complete = False
                self._set_state("DELAYED", "missing completed candle detected; REST reconciliation required")
            if self._bars and bar.timestamp < self._bars[-1].timestamp:
                merged = {row.timestamp: row for row in self._bars}
                merged[bar.timestamp] = bar
                self._bars = deque(sorted(merged.values(), key=lambda row: row.timestamp)[-self.max_bars:],
                                   maxlen=self.max_bars)
            else:
                self._bars.append(bar)
            closed_at = bar.timestamp + step
            self.last_closed_update = max(self.last_closed_update, closed_at) if self.last_closed_update else closed_at
            return True

    def ingest_event(self, message: dict) -> dict:
        data = message.get("data", message)
        event = data.get("e")
        now = self.clock()
        event_symbol, event_timeframe = self._message_identity(message, data, event)
        if event_symbol and event_symbol != self.symbol:
            reason = f"ignored {event or 'unknown'} event for {event_symbol}; active stream is {self.symbol}"
            self._emit_health("DATA_ERROR", reason)
            return {"accepted": False, "identity_mismatch": True, "reason": reason}
        if event_timeframe and event_timeframe != self.timeframe:
            reason = f"ignored kline event for {event_timeframe}; active timeframe is {self.timeframe}"
            self._emit_health("DATA_ERROR", reason)
            return {"accepted": False, "identity_mismatch": True, "reason": reason}
        with self._lock:
            self.last_update = now
        if event == "kline":
            kline = data["k"]
            key = ("kline", int(kline["t"]), bool(kline["x"]), str(kline["c"]), str(kline.get("v")))
            if key in self._seen_events:
                self.duplicate_events += 1
                return {"accepted": False, "duplicate": True}
            self._seen_events.add(key)
            if len(self._seen_events) > 10000:
                self._seen_events = set(list(self._seen_events)[-5000:])
            bar = self._bar(kline)
            with self._lock:
                self._quote["last"] = bar.close
                self.last_candle_update = now
            if bool(kline["x"]):
                accepted = self._merge_closed(bar)
                self._forming = None
                persisted = self._deliver_closed_bar(bar) if accepted else True
                return {"accepted": accepted, "closed": True, "bar": bar,
                        "sink_persisted": persisted,
                        "persistence_state": (
                            "AVAILABLE" if persisted else "PERSISTENCE_BLOCKED")}
            self._forming = bar
            return {"accepted": True, "closed": False, "bar": bar}
        if event == "bookTicker" or {"b", "a", "s"}.issubset(data):
            with self._lock:
                self._quote.update({"bid": float(data["b"]), "ask": float(data["a"])})
                self.last_quote_update = now
            event_at = datetime.fromtimestamp(int(data.get("E") or data.get("T") or
                                                   int(now.timestamp() * 1000)) / 1000,
                                              tz=timezone.utc)
            persisted = self._deliver_quote(
                "bookTicker", now, event_timestamp=event_at,
                sequence=int(data.get("u") or data.get("U") or 0) or None,
            )
            return {"accepted": True, "quote": True, "sink_persisted": persisted}
        if event == "markPriceUpdate":
            with self._lock:
                self._quote.update({"mark": float(data["p"]), "funding_rate": float(data.get("r") or 0),
                                    "next_funding_time": datetime.fromtimestamp(int(data["T"]) / 1000, tz=timezone.utc).isoformat()})
                self.last_mark_update = now
            event_at = datetime.fromtimestamp(int(data.get("E") or
                                                   int(now.timestamp() * 1000)) / 1000,
                                              tz=timezone.utc)
            persisted = self._deliver_quote(
                "markPriceUpdate", now, event_timestamp=event_at,
                sequence=int(data.get("E") or 0) or None,
            )
            return {"accepted": True, "mark": True, "sink_persisted": persisted}
        return {"accepted": False, "ignored": True}

    def status(self, *, now: datetime | None = None) -> dict:
        with self._lock:
            observed = now or self.clock()
            age_for = lambda stamp: max(0.0, (observed - stamp).total_seconds()) if stamp else None
            age = age_for(self.last_update)
            candle_age = age_for(self.last_candle_update)
            quote_age = age_for(self.last_quote_update)
            mark_age = age_for(self.last_mark_update)
            closed_age = age_for(self.last_closed_update)
            timeframe_seconds = TF_MS.get(self.timeframe, 60_000) / 1000
            completed_candle_threshold = timeframe_seconds + max(
                self.stale_after_seconds, min(timeframe_seconds * .25, 300.0))
            connecting_age = age_for(self.started_at)
            unresolved = self._unresolved_gaps()
            bid, ask, mark, last = (self._quote.get(key) for key in ("bid", "ask", "mark", "last"))
            bid_ask_valid = all(isinstance(value, (int, float)) and value > 0 for value in (bid, ask)) and bid <= ask
            mark_valid = isinstance(mark, (int, float)) and mark > 0
            quote_valid = bid_ask_valid and mark_valid
            reference = last or (self._forming.close if self._forming else (self._bars[-1].close if self._bars else None))
            mismatch_bps = None
            if quote_valid and reference and reference > 0:
                mismatch_bps = max(abs(float(bid) - reference), abs(float(ask) - reference),
                                   abs(float(mark) - reference)) / reference * 10_000

            if self.state in {"DISCONNECTED", "ERROR", "RECONNECTING", "CONNECTING"}:
                health = self.state
                reason = self.last_error or f"transport is {self.state.lower()}"
                if self.state == "CONNECTING" and connecting_age is not None and connecting_age > 20:
                    health = "ERROR"
                    reason = "required Binance websocket channel did not connect within 20 seconds"
            elif not self.history_loaded:
                health, reason = "LOADING_HISTORY", "completed-candle history has not loaded"
            elif not self.reconciliation_complete or unresolved:
                health, reason = "RECONCILING", f"{unresolved} completed candle(s) remain missing"
            elif closed_age is None or closed_age > completed_candle_threshold or candle_age is None or candle_age > self.stale_after_seconds:
                health, reason = "STALE_CANDLES", "completed history or live kline stream is stale"
            elif not self.quotes_enabled:
                # A context channel carries kline only by design. Judging it
                # against quote freshness would report a permanent STALE_QUOTE
                # for a stream that was never asked to carry quotes, and that
                # false red is exactly the kind of untrue status this platform
                # must not show. Its candles are still held to every freshness
                # and reconciliation rule above.
                health, reason = "SYNCHRONIZED", "higher-timeframe candles reconciled and fresh (kline-only channel)"
            elif quote_age is None or quote_age > self.stale_after_seconds or not bid_ask_valid:
                health, reason = "STALE_QUOTE", "bid/ask stream is missing, invalid or stale"
            elif mark_age is None or mark_age > self.stale_after_seconds or not mark_valid:
                health, reason = "STALE_MARK", "mark-price stream is missing or stale"
            elif mismatch_bps is not None and mismatch_bps > self.quote_mismatch_bps:
                health, reason = "QUOTE_MISMATCH", f"candle/quote deviation is {mismatch_bps:.2f} bps"
            else:
                health, reason = "SYNCHRONIZED", "candles, bid/ask and mark are reconciled and fresh"
            reliable = health == "SYNCHRONIZED"
            if not self.history_loaded:
                failing_dependency = "BINANCE_USDM_REST_HISTORY"
            elif self._channel_states.get("market") != "CONNECTED":
                failing_dependency = "BINANCE_USDM_MARKET_WEBSOCKET"
            elif self.quotes_enabled and self._channel_states.get("public") != "CONNECTED":
                failing_dependency = "BINANCE_USDM_BOOK_TICKER_WEBSOCKET"
            elif not self.reconciliation_complete or unresolved:
                failing_dependency = "COMPLETED_CANDLE_RECONCILIATION"
            elif health == "STALE_CANDLES":
                failing_dependency = "BINANCE_USDM_KLINE_STREAM"
            elif health == "STALE_QUOTE":
                failing_dependency = "BINANCE_USDM_BOOK_TICKER_STREAM"
            elif health == "STALE_MARK":
                failing_dependency = "BINANCE_USDM_MARK_PRICE_STREAM"
            elif health == "QUOTE_MISMATCH":
                failing_dependency = "CANDLE_QUOTE_MARK_RECONCILIATION"
            else:
                failing_dependency = None
            successful = [
                ("closed_candle", self.last_closed_update),
                ("kline", self.last_candle_update),
                ("bid_ask", self.last_quote_update),
                ("mark_price", self.last_mark_update),
            ]
            successful = [(name, stamp) for name, stamp in successful if stamp is not None]
            latest_success = max(successful, key=lambda row: row[1]) if successful else None
            payload = {"state": health, "transport_state": self.state,
                    "health_reason": reason, "reliable": reliable, "new_entries_paused": not reliable,
                    "symbol": self.symbol, "timeframe": self.timeframe,
                    "last_update": self.last_update.isoformat() if self.last_update else None,
                    "last_candle_update": self.last_candle_update.isoformat() if self.last_candle_update else None,
                    "last_quote_update": self.last_quote_update.isoformat() if self.last_quote_update else None,
                    "last_mark_update": self.last_mark_update.isoformat() if self.last_mark_update else None,
                    "last_closed_update": self.last_closed_update.isoformat() if self.last_closed_update else None,
                    "age_seconds": age, "candle_age_seconds": candle_age,
                    "quote_age_seconds": quote_age, "mark_age_seconds": mark_age,
                    "closed_candle_age_seconds": closed_age,
                    "freshness_thresholds_seconds": {
                        "stream": self.stale_after_seconds,
                        "completed_candle": completed_candle_threshold,
                    },
                    "history_loaded": self.history_loaded,
                    "reconciliation_complete": self.reconciliation_complete and unresolved == 0,
                    "unresolved_missing_candles": unresolved,
                    "candle_quote_deviation_bps": mismatch_bps,
                    "reconnect_attempt": self.reconnect_attempt,
                    "retry_state": {
                        "attempt": self.reconnect_attempt,
                        "channel_attempts": dict(self._reconnect_attempts),
                        "maximum_backoff_seconds": 30,
                        "automatic_retry": not self._stop.is_set(),
                    },
                    "connecting_age_seconds": connecting_age,
                    "failing_dependency": failing_dependency,
                    "last_successful_event": ({
                        "kind": latest_success[0], "at": latest_success[1].isoformat(),
                    } if latest_success else None),
                    "quotes_enabled": self.quotes_enabled,
                    # The live bid/ask/mark themselves, not just their ages.
                    # A status payload that reports "STALE_QUOTE" without ever
                    # showing the quote leaves an operator unable to check the
                    # claim. Small and already under the lock; the bar buffer
                    # stays out of status, which is on a hot path.
                    "quote": dict(self._quote),
                    "transport_channels": dict(self._channel_states),
                    "transport_errors": dict(self._channel_errors),
                    "duplicate_events": self.duplicate_events, "missing_candles": self.missing_candles,
                    "reconciled_candles": self.reconciled_candles, "last_error": self.last_error,
                    "persistence_blocked_events": self.persistence_blocked_events,
                    "last_sink_error": self.last_sink_error or None,
                    "public_streams": {
                        "market": ["kline", "markPrice"], "public": ["bookTicker"],
                    },
                    "private_key_required": False, "real_execution_allowed": False}
        # A read-only status request must never synchronously call a consumer
        # callback: lab callbacks may hold their account lock while a worker
        # asks for status, which turns health hydration into a deadlock/504.
        self._emit_health(health, reason, emit_event=False)
        return payload

    def snapshot(self) -> dict:
        with self._lock:
            payload = {"closed_bars": list(self._bars), "forming": self._forming,
                       "quote": dict(self._quote)}
        return {**payload, "connection": self.status()}
