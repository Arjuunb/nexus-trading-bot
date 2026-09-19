"""Market data access.

Phase 1 loads historical bars for paper/backtest runs: a bundled sample CSV
when one matches the symbol, otherwise deterministic synthetic data. Live
streaming is ``data/websocket.py`` (Phase 2).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from bot.data.csv_loader import load_csv_bars
from bot.data.resample import TF_SECONDS, resample, tf_seconds, to_timeframe
from bot.data.synthetic import generate_bars
from bot.types import Bar

# repo root holds data/samples/*.csv (the existing engine's bundled data)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SAMPLES = _REPO_ROOT / "data" / "samples"

# Map common venue symbols to bundled sample files.
_SAMPLE_MAP = {
    "BTCUSDT": "BTC-USD", "BTC-USD": "BTC-USD", "BTCUSD": "BTC-USD",
    "ETHUSDT": "ETH-USD", "ETH-USD": "ETH-USD", "ETHUSD": "ETH-USD",
    "AAPL": "AAPL",
}


def _from_local_store(symbol: str, n: int, timeframe: str, since_ms):
    """Read REAL cached candles from the local historical store, if any.

    Falls back to aggregating a FINER stored timeframe when the requested one was
    never synced: one ``/data/sync`` at 1h therefore also answers 4h, 6h, 12h and
    1d, from the same real candles. Without this, asking for 4h after syncing 1h
    would drop through to the bundled sample — swapping real data for demo data
    purely because of a label."""
    try:
        from config import settings
        from data.historical import HistoricalStore
        store = HistoricalStore(settings.market_db)
        bars = store.get_bars(symbol, timeframe, n=n, start_ms=since_ms)
        # need a meaningful amount of real history to use it
        if len(bars) >= min(n, 200):
            return bars

        want = tf_seconds(timeframe)
        if not want:
            return None
        # Coarsest usable source first: fewer rows to read and aggregate for the
        # same output. Only whole divisors — anything else is not an aggregation.
        for src in sorted((t for t, s in TF_SECONDS.items()
                           if s < want and want % s == 0),
                          key=lambda t: TF_SECONDS[t], reverse=True):
            ratio = want // TF_SECONDS[src]
            fine = store.get_bars(symbol, src, n=n * ratio, start_ms=since_ms)
            if len(fine) < min(n * ratio, 200):
                continue
            out = resample(fine, timeframe, source_tf=src)
            if len(out) >= min(n, 200):
                return out
    except Exception:  # noqa: BLE001 — store missing/empty -> fall through
        pass
    return None


def get_bars(symbol: str, n: int = 1500, timeframe: str = "1h",
             seed: int = 1, since_ms: Optional[int] = None,
             require_real: bool = False) -> tuple[list[Bar], str]:
    """Return (bars, source) for a symbol, REAL data first.

    Order: local cache of real Binance candles -> live ccxt (if enabled) ->
    bundled sample -> deterministic synthetic. Set ``HUB_REQUIRE_REAL_DATA=1``
    (env) or pass ``require_real=True`` to forbid the bundled/synthetic
    fallbacks entirely (production / replay: never fake data). ``since_ms``
    (epoch ms) selects history from a specific start time.
    """
    # Paper Trading V2 migration switch. Every established consumer imports
    # this facade, so enabling it moves Paper/Replay/Simulation/Backtest/AI/
    # Journal/Analytics to the one strict provider-backed cache without a
    # risky flag-day import rewrite. No cache entry means unavailable -- it
    # must never continue to a synthetic development fallback in this mode.
    if os.environ.get("HUB_MARKET_DATA_V2", "").lower() in ("1", "true", "yes", "on"):
        try:
            from config import settings
            from data.market_data_v2 import MarketDataService
            bars = MarketDataService(settings.market_data_v2_dir).bars(symbol, timeframe, limit=n)
            if bars:
                return bars, "market-data-v2 (real cache)"
        except Exception:  # noqa: BLE001 -- strict mode reports an honest unavailable state below
            pass
        return [], "unavailable (Market Data V2 cache required)"

    # 0. non-crypto assets (stocks / ETFs / indices / forex / commodities from
    # the symbol catalog): real candles via Yahoo (no key). Fail-closed — if
    # Yahoo is unreachable these return EMPTY with an honest source string;
    # non-crypto bars are never synthesized.
    from data.yahoo_bars import fetch_yahoo_bars, yahoo_symbol_for
    if yahoo_symbol_for(symbol):
        ybars = fetch_yahoo_bars(symbol, timeframe=timeframe, n=n)
        if ybars:
            return ybars, "live (yahoo)"
        return [], "unavailable (yahoo unreachable)"

    # 1. local cache of real candles (populated by the /data/sync engine)
    cached = _from_local_store(symbol, n, timeframe, since_ms)
    if cached:
        return cached, "local store (real)"

    # 2. live ccxt fetch when enabled
    if os.environ.get("HUB_USE_LIVE_DATA", "").lower() in ("1", "true", "yes"):
        from data.live_data import fetch_ohlcv
        exchange = os.environ.get("HUB_EXCHANGE", "binance")
        real = fetch_ohlcv(symbol, timeframe=timeframe, limit=n, exchange=exchange, since_ms=since_ms)
        if real:
            return real, "live (ccxt)"

    require_real = require_real or os.environ.get("HUB_REQUIRE_REAL_DATA", "").lower() in ("1", "true", "yes")
    if require_real:
        return [], "unavailable (real data required — run /data/sync)"

    # 3. bundled sample (real historical CSV shipped with the repo)
    key = symbol.upper().replace("/", "").replace("-", "")
    mapped: Optional[str] = None
    for raw, sample in _SAMPLE_MAP.items():
        if raw.replace("-", "") == key:
            mapped = sample
            break
    if mapped:
        path = _SAMPLES / f"{mapped}.csv"
        if path.exists():
            bars = load_csv_bars(str(path))
            if bars:
                # The CSVs are 1h candles. Returning them unchanged for a 4h or
                # 1d request made every "4h strategy" silently a 1h strategy and
                # let the multi-timeframe sweep compare a series against itself.
                # Aggregate properly instead; refuse when the request would mean
                # inventing candles finer than the data.
                resampled, _note = to_timeframe(bars, timeframe)
                if resampled:
                    return (resampled[-n:] if len(resampled) > n else resampled,
                            "bundled sample")
                # The sample cannot produce this candle size (asking for 15m from
                # 1h data). Fall through to the next source rather than returning
                # the wrong candles — that is what the fallback ladder is for.

    # 4. deterministic synthetic (demo/tests only; not all timeframes supported)
    try:
        return generate_bars(n=n, timeframe=timeframe, seed=seed), "synthetic"
    except ValueError:
        return [], f"unavailable (no real data for {symbol} {timeframe})"


def get_bars_judged(symbol: str, n: int = 1500, timeframe: str = "1h",
                    seed: int = 1, since_ms: Optional[int] = None,
                    require_real: bool = False, now=None):
    """``get_bars`` plus the age of what came back: (bars, source, freshness).

    ``require_real=True`` promises the candles are REAL. It promises nothing
    about WHEN, and the return value gives the caller no way to tell a candle
    that closed 25 seconds ago from one that closed 15.8 hours ago -- both of
    which the local store has actually served. Any consumer whose answer is a
    claim about NOW ("current volatility", today's tradable universe, the ATR
    a position is sized from) needs the second number and could not get it.

    The verdict comes from ``services.market_data_freshness``, which stays the
    only code that decides what fresh means. Nothing here filters, refuses or
    reorders on it: this hands the caller the fact, and what to do about a
    stale series is the caller's decision, made with the number rather than
    without it.
    """
    from services.market_data_freshness import judge_bars
    bars, source = get_bars(symbol, n=n, timeframe=timeframe, seed=seed,
                            since_ms=since_ms, require_real=require_real)
    return bars, source, judge_bars(symbol, timeframe, bars, now=now)
