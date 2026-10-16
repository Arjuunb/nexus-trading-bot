"""Read-only near-real-time market feed for the Native SMC visual lab.

The SMC model is deliberately fed confirmed candles only.  The most recent
*forming* exchange candle is returned separately so the chart can move like a
market terminal without letting unconfirmed data affect structure, setups, or
execution (which remains permanently disabled for native SMC).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from time import monotonic
from typing import Callable

from bot.data.resample import TF_SECONDS
from bot.types import Bar
from data.forward_market_data import valid_closed_bars
from services.market_data_freshness import assess_timeframe
from services.native_smc import SMCConfig, SMCMarketStructureEngine


LIVE_VENUES = {
    "binance_usdm": {
        "label": "Binance USDⓈ-M Futures",
        "ccxt_id": "binanceusdm",
        "market": lambda symbol: f"{symbol[:-4]}/USDT:USDT",
        "options": {"defaultType": "future"},
    },
    "mexc_perpetual": {
        "label": "MEXC perpetual",
        "ccxt_id": "mexc",
        "market": lambda symbol: f"{symbol[:-4]}/USDT:USDT",
        "options": {"defaultType": "swap"},
    },
    "kraken_spot": {
        "label": "Kraken spot",
        "ccxt_id": "kraken",
        "market": lambda symbol: f"{symbol[:-4]}/USDT",
        "options": {},
    },
}


class NativeSMCLiveDataUnavailable(RuntimeError):
    """A live visual source did not yield valid closed candles."""


@dataclass
class _LiveVisualFeed:
    """In-memory display cache; it is never a trading worker or data store."""

    engine: SMCMarketStructureEngine
    first_closed_candle: datetime
    loaded_closed_candles: int
    last_raw_count: int
    last_observed_at: datetime
    forming_candle: Bar | None
    # These display-only values are deliberately sourced from the same raw
    # exchange candle as the chart.  They never enter the native SMC engine.
    last_price: float
    price_direction: str
    price_updated_at: datetime
    last_fetch_monotonic: float


_LIVE_FEEDS: dict[tuple[str, str, str, bool], _LiveVisualFeed] = {}
_LIVE_FEEDS_LOCK = RLock()
_LIVE_REFRESH_SECONDS = 2.5


def reconcile_market_state(state: dict, quote: dict | None, *, timeframe: str,
                           now: datetime | None = None) -> dict:
    """Attach a fail-closed Binance candle/quote/mark health decision.

    OHLC polling is useful display evidence, but it is not sufficient execution
    evidence.  A PAPER entry is eligible only when the independent public
    bid/ask and mark snapshot is fresh and reconciles with the displayed price.
    """
    observed = now or datetime.now(timezone.utc)
    live = state.setdefault("live_display", {})
    reason = "independent Binance bid/ask and mark snapshot is unavailable"
    health = "STALE_QUOTE"
    quote_age = deviation_bps = None
    reliable = False
    try:
        if not quote:
            raise ValueError(reason)
        bid, ask, mark = (float(quote[key]) for key in ("bid", "ask", "mark"))
        provider_at = datetime.fromisoformat(str(quote["provider_time"]).replace("Z", "+00:00"))
        quote_age = max(0.0, (observed - provider_at).total_seconds())
        reference = float(live.get("last_price") or state["candles"][-1]["close"])
        deviation_bps = max(abs(bid - reference), abs(ask - reference),
                            abs(mark - reference)) / reference * 10_000
        last_closed = datetime.fromisoformat(
            str(state["data_provenance"]["last_closed_candle"]).replace("Z", "+00:00"))
        # ``last_closed`` is the candle's OPEN timestamp, which is how every
        # provider and every store in this codebase stamps a candle. The old
        # rule here compared that open against ``TF_SECONDS + 20`` and so
        # declared a 5m candle STALE twenty seconds after it closed -- for the
        # remaining 280 seconds of a perfectly current feed, which is 93% of
        # the time. The shared authority measures from the CLOSE and keeps a
        # completed candle fresh until the next one is actually due.
        candle = assess_timeframe(str(state.get("symbol") or ""), timeframe,
                                  last_closed, now=observed)
        closed_age = candle.age_seconds if candle.age_seconds is not None else 0.0
        closed_limit = candle.allowed_age_seconds
        if bid <= 0 or ask < bid:
            health, reason = "STALE_QUOTE", "public bid/ask snapshot is invalid"
        elif mark <= 0:
            health, reason = "STALE_MARK", "public mark-price snapshot is invalid"
        elif quote_age > 15:
            health, reason = "STALE_QUOTE", "public bid/ask and mark snapshot is stale"
        elif not candle.fresh:
            health, reason = "STALE_CANDLES", candle.describe()
        elif deviation_bps > 100:
            health, reason = "QUOTE_MISMATCH", f"candle/quote deviation is {deviation_bps:.2f} bps"
        else:
            health = "SYNCHRONIZED"
            reason = "candles, bid/ask and mark are reconciled and fresh"
            reliable = True
        live.update({"bid": bid, "ask": ask, "mark": mark,
                     "funding_rate": quote.get("funding_rate"),
                     "last_funding_time": quote.get("last_funding_time"),
                     "next_funding_time": quote.get("next_funding_time")})
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        if str(exc):
            reason = str(exc)
    live.update({
        "connection_state": health, "transport_state": "PUBLIC_REST_POLL",
        "health_reason": reason, "reliable": reliable,
        "new_entries_paused": not reliable, "quote_age_seconds": quote_age,
        "mark_age_seconds": quote_age, "candle_quote_deviation_bps": deviation_bps,
        "quote_source": "BINANCE_USDM_PUBLIC_REST" if quote else "UNAVAILABLE",
    })
    state.setdefault("data_provenance", {}).update({
        "connection_state": health, "new_entries_paused": not reliable,
        "market_data_mode": "LIVE", "market_data_source": "Binance USDⓈ-M Futures public market data",
        "exchange": "Binance USDⓈ-M Futures",
    })
    return state


def _display_price(forming: Bar | None, closed: list[Bar]) -> float:
    """Return the one authoritative display price for this visual feed."""
    if forming is not None:
        return float(forming.close)
    if closed:
        return float(closed[-1].close)
    raise NativeSMCLiveDataUnavailable("live visual source has no display price")


def _price_direction(previous: float | None, current: float) -> str:
    if previous is None:
        return "unchanged"
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "unchanged"


def _validate(symbol: str, timeframe: str, venue: str) -> tuple[str, str, str]:
    symbol, timeframe, venue = symbol.upper(), timeframe.lower(), venue.lower()
    if symbol not in {"BTCUSDT", "ETHUSDT", "SOLUSDT"}:
        raise ValueError("symbol must be one of BTCUSDT, ETHUSDT, or SOLUSDT")
    if timeframe not in {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}:
        raise ValueError("timeframe must be one of 1m, 3m, 5m, 15m, 30m, 1h, 4h, 1d, or 1w")
    if venue not in LIVE_VENUES:
        raise ValueError("venue must be one of binance_usdm, mexc_perpetual, or kraken_spot")
    return symbol, timeframe, venue


def fetch_venue_ohlcv(symbol: str, timeframe: str, venue: str, limit: int, *,
                      since_ms: int | None = None) -> list[Bar]:
    """Fetch raw live candles from the explicitly selected visual venue."""
    config = LIVE_VENUES[venue]
    try:
        import ccxt
        exchange_class = getattr(ccxt, config["ccxt_id"])
        exchange = exchange_class({"enableRateLimit": True, "options": config["options"]})
        rows = exchange.fetch_ohlcv(
            config["market"](symbol), timeframe=timeframe, limit=limit, since=since_ms,
        )
    except Exception as exc:
        raise NativeSMCLiveDataUnavailable(
            f"{config['label']} did not provide {symbol} {timeframe}: {type(exc).__name__}: {exc}"
        ) from exc
    bars = [
        Bar(datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc), float(row[1]), float(row[2]),
            float(row[3]), float(row[4]), float(row[5] or 0))
        for row in rows
    ]
    if not bars:
        raise NativeSMCLiveDataUnavailable(f"{config['label']} returned no {symbol} {timeframe} candles")
    return bars


def live_visual_history(symbol: str = "BTCUSDT", timeframe: str = "5m", venue: str = "binance_usdm", *,
                        before: datetime, limit: int = 400, now: datetime | None = None,
                        fetcher: Callable[..., list[Bar]] = fetch_venue_ohlcv) -> dict:
    """Return one genuine, closed-candle page before ``before`` for chart browsing.

    This is deliberately a display-data endpoint.  It neither restores nor
    changes native SMC state: the Visual Lab can extend its historical canvas
    without turning old bars into an execution or research-selection input.
    """
    symbol, timeframe, venue = _validate(symbol, timeframe, venue)
    if before.tzinfo is None:
        before = before.replace(tzinfo=timezone.utc)
    else:
        before = before.astimezone(timezone.utc)
    page_size = max(50, min(int(limit), 1_000))
    observed_at = now or datetime.now(timezone.utc)
    step_seconds = TF_SECONDS[timeframe]
    # Ask for exactly the preceding page.  Exchanges may return a smaller
    # final page; that is an honest end-of-available-history signal.
    since_ms = int((before - timedelta(seconds=step_seconds * page_size)).timestamp() * 1_000)
    raw = fetcher(symbol, timeframe, venue, page_size, since_ms=since_ms)
    closed = [bar for bar in valid_closed_bars(raw, step_seconds, now=observed_at) if bar.timestamp < before]
    closed = closed[-page_size:]
    return {
        "research_only": True,
        "execution_allowed": False,
        "candles": [_candle_payload(bar) for bar in closed],
        "before": before.isoformat(),
        "oldest": closed[0].timestamp.isoformat() if closed else None,
        "newest": closed[-1].timestamp.isoformat() if closed else None,
        "has_more_history": len(closed) >= page_size,
        "data_provenance": {
            "mode": "LIVE_EXCHANGE_HISTORICAL_DISPLAY_PAGE",
            "venue": LIVE_VENUES[venue]["label"],
            "ccxt_exchange": LIVE_VENUES[venue]["ccxt_id"],
            "market": LIVE_VENUES[venue]["market"](symbol),
            "symbol": symbol,
            "timeframe": timeframe,
            "observed_at": observed_at.isoformat(),
            "request_before": before.isoformat(),
            "closed_candles_returned": len(closed),
            "execution_allowed": False,
        },
    }


def _forming_candle(raw: list[Bar], timeframe_seconds: int, now: datetime) -> Bar | None:
    """Return the current provider candle for display without accepting it as fact."""
    candidates = [bar for bar in raw if bar.timestamp + timedelta(seconds=timeframe_seconds) > now]
    if not candidates:
        return None
    candidate = max(candidates, key=lambda bar: bar.timestamp)
    if min(candidate.open, candidate.high, candidate.low, candidate.close) <= 0:
        return None
    if candidate.volume < 0 or candidate.high < max(candidate.open, candidate.close, candidate.low):
        return None
    if candidate.low > min(candidate.open, candidate.close, candidate.high):
        return None
    return candidate


def _candle_payload(bar: Bar | None) -> dict | None:
    if bar is None:
        return None
    return {
        "timestamp": bar.timestamp.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _new_feed(symbol: str, timeframe: str, venue: str, limit: int, now: datetime,
              fetcher: Callable[[str, str, str, int], list[Bar]],
              mtf_context: dict[str, list[Bar]] | None = None) -> _LiveVisualFeed:
    raw = fetcher(symbol, timeframe, venue, max(200, min(int(limit), 1_000)))
    closed = valid_closed_bars(raw, TF_SECONDS[timeframe], now=now)
    if len(closed) < 200:
        raise NativeSMCLiveDataUnavailable(
            f"{LIVE_VENUES[venue]['label']} returned only {len(closed)} valid closed candles; need at least 200"
        )
    engine = SMCMarketStructureEngine(SMCConfig(symbol=symbol, timeframe=timeframe))
    if mtf_context is not None:
        engine.set_native_mtf_context(mtf_context)
    engine.ingest_authoritative_closed_bars(closed, timeframe_seconds=TF_SECONDS[timeframe], now=now)
    forming = _forming_candle(raw, TF_SECONDS[timeframe], now)
    price = _display_price(forming, closed)
    return _LiveVisualFeed(
        engine=engine,
        first_closed_candle=closed[0].timestamp,
        loaded_closed_candles=len(closed),
        last_raw_count=len(raw),
        last_observed_at=now,
        forming_candle=forming,
        last_price=price,
        price_direction="unchanged",
        price_updated_at=now,
        last_fetch_monotonic=monotonic(),
    )


def _refresh_feed(feed: _LiveVisualFeed, symbol: str, timeframe: str, venue: str,
                  now: datetime, fetcher: Callable[[str, str, str, int], list[Bar]],
                  mtf_context: dict[str, list[Bar]] | None = None) -> None:
    """Advance an existing feed with a tiny provider window.

    ``process_closed_bar`` is idempotent, so overlap is intentional: the last
    few provider candles protect against a boundary arriving just as it closes.
    """
    raw = fetcher(symbol, timeframe, venue, 6)
    if mtf_context is not None:
        feed.engine.set_native_mtf_context(mtf_context)
    closed = valid_closed_bars(raw, TF_SECONDS[timeframe], now=now)
    feed.engine.ingest_authoritative_closed_bars(closed, timeframe_seconds=TF_SECONDS[timeframe], now=now)
    feed.loaded_closed_candles = len(feed.engine.bars)
    feed.last_raw_count = len(raw)
    feed.last_observed_at = now
    feed.forming_candle = _forming_candle(raw, TF_SECONDS[timeframe], now)
    current_price = _display_price(feed.forming_candle, closed or feed.engine.bars)
    feed.price_direction = _price_direction(feed.last_price, current_price)
    feed.last_price = current_price
    feed.price_updated_at = now
    feed.last_fetch_monotonic = monotonic()


def live_visual_state(symbol: str = "BTCUSDT", timeframe: str = "5m", venue: str = "binance_usdm", *,
                      limit: int = 800, visible: int = 240, now: datetime | None = None,
                      fetcher: Callable[[str, str, str, int], list[Bar]] = fetch_venue_ohlcv,
                      model_id: str = "SMC_M1_SWEEP_REVERSAL",
                      mtf_evidence: dict | None = None,
                      mtf_context: dict[str, list[Bar]] | None = None) -> dict:
    """Return a real-time display state while isolating native SMC to closed bars.

    Production calls share a small in-memory feed and poll it at most once per
    short interval.  Deterministic callers that provide a clock or a fetcher
    bypass that cache, keeping tests and evidence generation reproducible.
    """
    symbol, timeframe, venue = _validate(symbol, timeframe, venue)
    if not 20 <= int(visible) <= 1_000:
        raise ValueError("visible must be between 20 and 1000")
    supplied_clock_or_fetcher = now is not None or fetcher is not fetch_venue_ohlcv
    observed_at = now or datetime.now(timezone.utc)
    # Native-context and legacy research displays may never share an engine:
    # one has an authoritative HTF clock and the other intentionally does not.
    key = (symbol, timeframe, venue, mtf_context is not None)

    if supplied_clock_or_fetcher:
        feed = _new_feed(symbol, timeframe, venue, limit, observed_at, fetcher, mtf_context)
    else:
        with _LIVE_FEEDS_LOCK:
            feed = _LIVE_FEEDS.get(key)
            if feed is None:
                feed = _new_feed(symbol, timeframe, venue, limit, observed_at, fetcher, mtf_context)
                _LIVE_FEEDS[key] = feed
            else:
                if mtf_context is not None:
                    feed.engine.set_native_mtf_context(mtf_context)
                if monotonic() - feed.last_fetch_monotonic >= _LIVE_REFRESH_SECONDS:
                    _refresh_feed(feed, symbol, timeframe, venue, observed_at, fetcher, mtf_context)

    state = feed.engine.visual_state(candle_window=min(int(visible), len(feed.engine.bars)))
    # The ladder is a read-only projection of the same closed-bar native
    # engine. It cannot create a signal, alter a snapshot, or place an order.
    from services.smc_strategy_ladder import evaluate_ladder
    from services.smc_strategy_v1 import evaluate as evaluate_source_strategy
    state["strategy_ladder"] = evaluate_ladder(feed.engine)
    state["source_strategy"] = evaluate_source_strategy(
        feed.engine, model_id, mtf_evidence=mtf_evidence)
    state["mtf_evidence"] = dict(mtf_evidence or {})
    from services.mtf_policy import display_contract
    state["mtf_policy"] = display_contract(timeframe, mtf_evidence)
    forming = _candle_payload(feed.forming_candle)
    candle_closes_at = (
        (feed.forming_candle.timestamp + timedelta(seconds=TF_SECONDS[timeframe])).isoformat()
        if feed.forming_candle else None
    )
    state["forming_candle"] = forming
    state["live_display"] = {
        "is_forming": forming is not None,
        "observed_at": feed.last_observed_at.isoformat(),
        "refresh_interval_seconds": _LIVE_REFRESH_SECONDS,
        "candle_closes_at": candle_closes_at,
        # The chart, right-axis ticker, toolbar and watchlist consume this one
        # exchange-derived value. Do not add client-side synthetic motion.
        "last_price": feed.last_price,
        "price_direction": feed.price_direction,
        "price_updated_at": feed.price_updated_at.isoformat(),
        "source_mode": "exchange_ohlcv_live_poll",
        "execution_uses_closed_bars_only": True,
    }
    state["data_provenance"] = {
        "mode": "LIVE_EXCHANGE_DISPLAY_WITH_CLOSED_BAR_SMC",
        "venue": LIVE_VENUES[venue]["label"],
        "ccxt_exchange": LIVE_VENUES[venue]["ccxt_id"],
        "market": LIVE_VENUES[venue]["market"](symbol),
        "symbol": symbol,
        "timeframe": timeframe,
        "observed_at": feed.last_observed_at.isoformat(),
        "raw_candles_received": feed.last_raw_count,
        "closed_candles_loaded": feed.loaded_closed_candles,
        "closed_candles_visible": len(state["candles"]),
        "closed_candles_used": feed.loaded_closed_candles,
        "first_closed_candle": feed.first_closed_candle.isoformat(),
        "last_closed_candle": feed.engine.bars[-1].timestamp.isoformat(),
        "forming_candle_excluded": forming is not None,
        "execution_allowed": False,
    }
    return state
