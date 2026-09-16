"""One live closed-candle source, for every consumer that needs current data.

``data/market_data.py::get_bars`` reads the local candle store, which is only
as current as the last manual ``/data/sync``. That is the right source for a
reproducible research run and the wrong one for anything claiming to show or
measure the market as it stands: on a host where the sync has not run for a
day, every consumer of that cache is quietly a day behind.

This reads closed candles straight from the venue instead -- the same
``fetch_venue_ohlcv`` and ``valid_closed_bars`` the SMC labs use, judged by the
same ``services/market_data_freshness`` authority the instances answer to -- so
two pages looking at the same market cannot report different ages.

Longer windows are paged forward, because an exchange caps a single OHLCV
request well below the few thousand candles a strategy comparison wants.

Nothing here is synthetic and nothing falls back to a sample series: an
unavailable venue raises, and the caller decides whether a cached series or a
refusal is the honest answer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from time import monotonic
from typing import Callable, Optional

#: Conservative per-request page. Venues cap OHLCV responses (Binance futures
#: at 1500); asking for more silently returns fewer, which would look like the
#: end of available history rather than a page boundary.
PAGE_CANDLES = 1000
#: A ceiling on paging, so a timeframe/limit combination cannot walk an
#: exchange indefinitely. 12 pages is ~12k candles: more than any caller here
#: asks for, and a bounded number of requests either way.
MAX_PAGES = 12
#: How long a failed venue read is remembered. An unreachable exchange takes
#: the ccxt timeout to say so, and every poller would otherwise pay it on every
#: tick. Short enough that recovery is noticed within one cycle, and it never
#: suppresses the error -- the remembered reason is raised again.
FAILURE_COOLDOWN_SECONDS = 15.0
#: One entry per symbol/timeframe/venue anyone has asked for. A display and
#: measurement cache, never a store.
MAX_CACHED_SERIES = 32


class LiveCandlesUnavailable(RuntimeError):
    """The venue did not yield usable closed candles."""


#: series keyed by market, each held with the request size that produced it.
_SERIES: dict[tuple[str, str, str], tuple[list, int]] = {}
_FAILURES: dict[tuple[str, str, str], tuple[float, str]] = {}
_LOCK = RLock()


def judge(symbol: str, timeframe: str, rows) -> object:
    """The platform's one verdict on how current a series is."""
    from services.market_data_freshness import assess_timeframe

    newest = rows[-1].timestamp if rows else None
    return assess_timeframe(symbol, timeframe, newest)


def reset_cache() -> None:
    """Drop the held series and failures. For tests and operator tooling."""
    with _LOCK:
        _SERIES.clear()
        _FAILURES.clear()


def _fetch_pages(symbol: str, timeframe: str, venue: str, wanted: int,
                 fetcher: Callable, anchor: datetime, *,
                 max_pages: int = MAX_PAGES) -> list:
    """Walk forward across the window of ``wanted`` candles ending at ``anchor``.

    Forward rather than backward because that is the direction ``since`` takes
    on every venue: ask for the oldest candle wanted, then keep asking from the
    last one received. A page that makes no progress ends the walk, which is
    how the genuine start of available history is reached without a special
    case for it.
    """
    from bot.data.resample import TF_SECONDS

    step = TF_SECONDS[timeframe]
    start = anchor - timedelta(seconds=step * (wanted + 2))
    since_ms = int(start.timestamp() * 1000)
    collected: dict[datetime, object] = {}

    for _ in range(max_pages):
        page = fetcher(symbol, timeframe, venue, PAGE_CANDLES, since_ms=since_ms)
        if not page:
            break
        newest = max(bar.timestamp for bar in page)
        before = len(collected)
        for bar in page:
            collected[bar.timestamp] = bar
        if len(collected) == before:
            break                       # no new candles: the venue is repeating
        if len(collected) >= wanted + 2 or newest + timedelta(seconds=step) >= anchor:
            break
        since_ms = int((newest + timedelta(seconds=step)).timestamp() * 1000)

    return [collected[key] for key in sorted(collected)]


def pages_for(limit: int) -> int:
    """Pages a window of ``limit`` candles needs, plus one for the boundary."""
    return max(1, -(-limit // PAGE_CANDLES) + 1)


def live_series(symbol: str, timeframe: str, venue: str = "binance_usdm", *,
                limit: int = 300, now: Optional[datetime] = None,
                until: Optional[datetime] = None,
                fetcher: Optional[Callable] = None,
                max_pages: int = MAX_PAGES, use_cache: bool = True) -> list:
    """Closed candles from the venue, newest last, at most ``limit`` of them.

    A held series is reused only while its newest candle is still FRESH and it
    is long enough for the caller. That is not a timer: by the freshness
    authority's own rule a closed candle is current until the next one of that
    timeframe is due, so reusing a fresh series cannot serve stale data, and
    the moment it could the next call refetches.

    ``max_pages`` bounds the walk. The default suits a chart or a strategy
    comparison; a research replay wanting a year of 5M candles must raise it
    deliberately and say so, rather than silently receiving a short window and
    reporting a year. ``use_cache=False`` is for exactly that caller: a year of
    candles is not something to leave resident in a serving process.

    ``until`` anchors the window's END. Without it the walk returns the newest
    ``limit`` candles, which is right for a chart and wrong for a dated replay:
    asking for a year of 5M candles to study 2025 would return the year ending
    today, and only its first months would fall inside the window. A historical
    fetch is never cached -- it is not "the current series" for anything.
    """
    from bot.data.resample import TF_SECONDS
    from data.forward_market_data import valid_closed_bars
    from services.native_smc_live_visual import (
        NativeSMCLiveDataUnavailable,
        fetch_venue_ohlcv,
    )

    if timeframe not in TF_SECONDS:
        raise LiveCandlesUnavailable(f"unsupported timeframe '{timeframe}'")
    symbol = symbol.upper()
    fetcher = fetcher or fetch_venue_ohlcv
    observed = now or datetime.now(timezone.utc)
    anchor = until or observed
    if until is not None:
        use_cache = False
    key = (symbol, timeframe, venue)

    with _LOCK:
        held = _SERIES.get(key) if use_cache else None
        failed_at, reason = _FAILURES.get(key, (0.0, ""))
    # Current enough AND fetched for a window at least this wide. The second
    # half matters: a series cached for a 300-candle chart must not be handed
    # to a 2500-candle strategy comparison as if it were the whole window. The
    # comparison is against the size ASKED FOR, not the size returned -- a
    # market with less history than requested would otherwise never be reused
    # and would be refetched on every single call.
    if held:
        cached, asked_for = held
        if asked_for >= limit and judge(symbol, timeframe, cached).fresh:
            return cached[-limit:]
    if reason and monotonic() - failed_at < FAILURE_COOLDOWN_SECONDS:
        raise LiveCandlesUnavailable(
            f"{reason} (retried within {int(FAILURE_COOLDOWN_SECONDS)}s)")

    try:
        if limit <= PAGE_CANDLES and until is None:
            raw = fetcher(symbol, timeframe, venue, min(limit + 2, PAGE_CANDLES))
        else:
            raw = _fetch_pages(symbol, timeframe, venue, limit, fetcher, anchor,
                               max_pages=max_pages)
    except (NativeSMCLiveDataUnavailable, KeyError, ValueError) as exc:
        with _LOCK:
            _FAILURES[key] = (monotonic(), str(exc))
        raise LiveCandlesUnavailable(str(exc)) from exc

    rows = valid_closed_bars(raw, TF_SECONDS[timeframe], now=observed)
    if not rows:
        message = f"{venue} returned no closed {symbol} {timeframe} candles"
        with _LOCK:
            _FAILURES[key] = (monotonic(), message)
        raise LiveCandlesUnavailable(message)

    with _LOCK:
        _FAILURES.pop(key, None)
        if use_cache:
            _SERIES[key] = (rows, limit)
            while len(_SERIES) > MAX_CACHED_SERIES:
                _SERIES.pop(next(iter(_SERIES)))
    return rows[-limit:]
