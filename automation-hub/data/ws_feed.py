"""WebSocket market feed — push candles instead of polling REST.

Runs ccxt.pro ``watch_ohlcv`` streams on a background asyncio thread and keeps
a rolling, thread-safe cache of bars per symbol. The engine consumes it through
``make_fetcher``: when the stream is fresh the engine reads from the cache
(millisecond latency, zero REST rate-limit pressure); the moment the stream is
stale or unavailable it falls back to the given REST fetcher — the bot degrades
gracefully instead of going blind.

No ccxt.pro / no network -> ``available`` is False and the fetcher is a pure
pass-through. The status endpoint reports exactly which mode is serving data.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from bot.types import Bar
from services.market_data_freshness import DEFAULT_SILENCE_SECONDS

# Candle durations come from bot.data.resample — the one definition. This used
# to be a local copy, and six copies of the same fact had already drifted apart.
from bot.data.resample import TF_SECONDS as _TF_S  # noqa: E402
def _to_pair(symbol: str) -> str:
    s = symbol.upper().replace("/", "")
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if s.endswith(quote):
            return f"{s[:-len(quote)]}/{quote}"
    return symbol


class WebSocketFeed:
    def __init__(self, symbols: list[str], timeframe: str = "1h",
                 exchange: str = "binance", max_bars: int = 600):
        self.symbols = list(symbols)
        self.timeframe = timeframe
        self.exchange = exchange
        self.max_bars = max_bars
        self._bars: dict[str, deque] = {s: deque(maxlen=max_bars) for s in self.symbols}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop = None
        self._stream_task = None
        self._stop = threading.Event()
        self.available = False          # a stream is (or was) connected
        self.last_error: str = ""
        self.updates = 0                # candles ingested (all symbols)
        self.last_update: Optional[str] = None
        self.reconnect_attempt = 0
        self.websocket_reads = 0
        self.rest_fallback_reads = 0
        #: How long a subscription may deliver nothing before it is recycled.
        #: Shared with the freshness authority so the socket is recycled at the
        #: same instant the platform stops trusting its data.
        self.silence_timeout_seconds = float(DEFAULT_SILENCE_SECONDS)
        self.silent_recycles = 0

    # ------------------------------------------------------------- ingestion
    def ingest_rows(self, symbol: str, rows: list) -> None:
        """Merge raw OHLCV rows [[ms, o, h, l, c, v], ...] into the cache.
        Updates the in-progress candle in place; appends newly closed ones."""
        bars = [Bar(datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                    float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                    float(r[5] or 0.0)) for r in rows]
        self.available = True
        with self._lock:
            dq = self._bars.setdefault(symbol, deque(maxlen=self.max_bars))
            for b in bars:
                if dq and dq[-1].timestamp == b.timestamp:
                    dq[-1] = b                       # refresh in-progress candle
                elif not dq or b.timestamp > dq[-1].timestamp:
                    dq.append(b)
            self.updates += len(bars)
            self.last_update = datetime.now(timezone.utc).isoformat()

    # --------------------------------------------------------------- queries
    def get_bars(self, symbol: str, limit: int = 250) -> list[Bar]:
        with self._lock:
            dq = self._bars.get(symbol) or ()
            return list(dq)[-limit:]

    def newest_closed(self, symbol: str, *, now: Optional[datetime] = None):
        """The newest candle that has actually closed, or None.

        ``get_bars`` returns the raw cache, whose last entry is normally the
        still-forming candle -- it is refreshed in place on every tick. Judging
        freshness on that bar is meaningless: it is always seconds old by
        construction, which is why the previous ``fresh()`` answered True
        almost unconditionally and was never a real gate.
        """
        now = now or datetime.now(timezone.utc)
        interval = _TF_S.get(self.timeframe, 3600)
        for bar in reversed(self.get_bars(symbol, 5)):
            stamp = bar.timestamp
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp + timedelta(seconds=interval) <= now:
                return bar
        return None

    def freshness(self, symbol: str, *, now: Optional[datetime] = None):
        """This feed's verdict from the one platform-wide authority."""
        from services.market_data_freshness import assess_feed

        now = now or datetime.now(timezone.utc)
        closed = self.newest_closed(symbol, now=now)
        last_event = None
        if self.last_update:
            try:
                last_event = datetime.fromisoformat(self.last_update)
            except ValueError:
                last_event = None
        running = self._thread is not None and self._thread.is_alive()
        state = "CONNECTED" if (running and self.available) else "DISCONNECTED"
        return assess_feed(
            symbol, {self.timeframe: closed.timestamp if closed else None},
            now=now, connection_state=state, last_event_at=last_event,
            source=f"{self.exchange}-websocket", reconnects=self.reconnect_attempt)

    def fresh(self, symbol: str) -> bool:
        """True only when the shared freshness authority says so.

        Delegated rather than recomputed: this used to be a fourth opinion on
        what "fresh" means, measured off the forming candle against two whole
        intervals.
        """
        return self.freshness(symbol).allow_new_entry

    def status(self) -> dict:
        with self._lock:
            depth = {s: len(dq) for s, dq in self._bars.items()}
        return {"running": self._thread is not None and self._thread.is_alive(),
                "available": self.available, "exchange": self.exchange,
                "timeframe": self.timeframe, "symbols": self.symbols,
                "bars_cached": depth, "updates": self.updates,
                "reconnect_attempt": self.reconnect_attempt,
                "silent_recycles": self.silent_recycles,
                "silence_timeout_seconds": self.silence_timeout_seconds,
                "websocket_reads": self.websocket_reads,
                "rest_fallback_reads": self.rest_fallback_reads,
                "last_update": self.last_update, "last_error": self.last_error}

    # ------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        """Start the stream thread. Returns False (with last_error set) when
        ccxt.pro isn't installed — callers keep using their REST fetcher."""
        try:
            import ccxt.pro  # noqa: F401
        except Exception as e:  # noqa: BLE001
            self.last_error = f"ccxt.pro unavailable: {e}"
            return False
        if self._thread and self._thread.is_alive():
            if not self._stop.is_set():
                return True
            # A rapid stop/start can observe the prior async client while it is
            # still closing. Never report a successful reconnect while that
            # thread still owns the stop flag and is about to exit.
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                self.last_error = "previous WebSocket worker is still stopping; REST fallback remains active"
                return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ws-feed", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        loop, task = self._loop, self._stream_task
        if loop is not None and task is not None and loop.is_running():
            # watch_ohlcv may block until the venue publishes another candle.
            # Cancel it on its own event loop so Stop/Restart does not leave a
            # ghost feed alive for an entire timeframe.
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # loop completed between the state check and callback
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # Keep shutdown bounded so a slow venue cannot hang an API action.
            thread.join(timeout=5.0)
        if thread is None or not thread.is_alive():
            self.available = False

    def _run(self) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        task = loop.create_task(self._stream())
        self._stream_task = task
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # startup/client failures must remain visible
            self.available = False
            self.last_error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            pending = asyncio.all_tasks(loop)
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._stream_task = None
            self._loop = None
            self.available = False

    async def _stream(self) -> None:
        import asyncio
        import ccxt.pro as ccxtpro
        ex = getattr(ccxtpro, self.exchange)({"enableRateLimit": True})
        try:
            async def watch(sym: str):
                pair = _to_pair(sym)
                while not self._stop.is_set():
                    try:
                        # A dead socket does not raise. ccxt.pro will await a
                        # frame that never arrives, so without this deadline a
                        # stream that silently stops delivering stays "open"
                        # and "CONNECTED" forever while the cache quietly ages
                        # -- the exact failure a connection-state check cannot
                        # see. The timeout turns silence into the reconnect
                        # path below, which resubscribes on the next pass.
                        rows = await asyncio.wait_for(
                            ex.watch_ohlcv(pair, self.timeframe),
                            timeout=self.silence_timeout_seconds)
                        self.available = True
                        self.reconnect_attempt = 0
                        self.last_error = ""
                        self.ingest_rows(sym, rows)
                    except asyncio.TimeoutError:
                        self.last_error = (
                            f"{pair} {self.timeframe} delivered no event for "
                            f"{self.silence_timeout_seconds:.0f}s; recycling the socket")
                        self.available = False
                        self.silent_recycles += 1
                        self.reconnect_attempt += 1
                        await asyncio.sleep(min(2 ** self.reconnect_attempt, 30))
                    except Exception as e:  # noqa: BLE001 — reconnect, don't die
                        self.last_error = str(e)
                        self.available = False
                        self.reconnect_attempt += 1
                        await asyncio.sleep(min(2 ** self.reconnect_attempt, 30))
            await asyncio.gather(*(watch(s) for s in self.symbols))
        finally:
            try:
                await ex.close()
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- fetcher
    def make_fetcher(self, fallback: Callable[[str, str, int], tuple]):
        """A drop-in engine fetcher: WS cache when fresh, REST fallback when not.
        Seeds the cache from the fallback so streams start with warm history."""
        def fetcher(symbol: str, timeframe: str, limit: int, **kwargs):
            # A cursor-relative recovery request needs a precise REST window;
            # the rolling WS cache contains only the newest bars and must not
            # masquerade as historical backfill.
            cursor_request = kwargs.get("since_ms") is not None
            if not cursor_request and self.available and timeframe == self.timeframe and self.fresh(symbol):
                bars = self.get_bars(symbol, limit)
                if len(bars) >= limit:
                    self.websocket_reads += 1
                    return bars, "live (websocket)"
            bars, src = fallback(symbol, timeframe, limit, **kwargs)
            self.rest_fallback_reads += 1
            # keep the cache warm so the stream picks up with full history
            if bars and timeframe == self.timeframe and not self.get_bars(symbol, 1):
                with self._lock:
                    dq = self._bars.setdefault(symbol, deque(maxlen=self.max_bars))
                    dq.extend(bars[-self.max_bars:])
            return bars, src
        return fetcher
