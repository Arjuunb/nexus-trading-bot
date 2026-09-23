#!/usr/bin/env python3
"""Which candle sources this host can actually reach, and how old each one is.

The Instance Visual Lab draws from three real sources in order: the running
strategy's own series, a direct venue read, then the local real-candle cache.
When the chart says STALE the useful question is which of those answered, and
this says so without having to read it off a chart.

Every verdict comes from services/market_data_freshness.py -- the same
authority the instances and the labs answer to -- so a source this calls fresh
is fresh everywhere on the platform.

Read-only. It fetches and reads; it writes nothing, and it cannot place an
order or change an instance.

    python scripts/candle_source_check.py
    python scripts/candle_source_check.py --symbol BNBUSDT --timeframe 5m
    python scripts/candle_source_check.py --venue binance_usdm

The running strategy's own series is deliberately not checked here: it lives in
the app process's memory, and this runs in a separate process, so it could only
be guessed at. The Lab's own /candles response reports it.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Run from a shell as `python scripts/candle_source_check.py`: sys.path[0] is
# then scripts/, not the app root, so services/ and data/ would not import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _verdict(symbol: str, timeframe: str, rows) -> dict:
    from services.market_data_freshness import assess_timeframe

    newest = rows[-1].timestamp if rows else None
    return assess_timeframe(symbol, timeframe, newest).to_dict()


def _report(out, label: str, rows, symbol: str, timeframe: str,
            detail: str = "") -> dict:
    verdict = _verdict(symbol, timeframe, rows)
    mark = "OK  " if verdict["status"] == "FRESH" else "STALE" if rows else "DOWN"
    print(f"  [{mark:<5}] {label}", file=out)
    if not rows:
        print(f"           {detail or 'no candles'}", file=out)
        return {"label": label, "candles": 0, "status": "UNAVAILABLE",
                "detail": detail}
    print(f"           {len(rows)} closed candles · newest closes "
          f"{verdict['last_close']}", file=out)
    print(f"           age {_age(verdict['age_seconds'])} · this timeframe allows "
          f"{_age(verdict['allowed_age_seconds'])} · {verdict['status']}"
          f"{' · ' + verdict['blocker'] if verdict['blocker'] else ''}", file=out)
    return {"label": label, "candles": len(rows), **verdict}


def check(symbol: str, timeframe: str, venue: str, limit: int, out) -> dict:
    print(f"Candle sources for {symbol} {timeframe} -- READ ONLY", file=out)
    print(f"  host time  {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
          file=out)
    print(f"  venue      {venue}\n", file=out)

    results = []

    # 1. The direct venue read, exactly as the labs do it.
    from bot.data.resample import TF_SECONDS
    from data.forward_market_data import valid_closed_bars
    from services.native_smc_live_visual import (
        NativeSMCLiveDataUnavailable,
        fetch_venue_ohlcv,
    )
    try:
        raw = fetch_venue_ohlcv(symbol, timeframe, venue, limit)
        rows = valid_closed_bars(raw, TF_SECONDS[timeframe])
        results.append(_report(out, f"venue read ({venue})", rows, symbol, timeframe,
                               "the venue answered, but with no closed candles"))
    except (NativeSMCLiveDataUnavailable, KeyError, ValueError) as exc:
        results.append(_report(out, f"venue read ({venue})", [], symbol, timeframe,
                               f"{type(exc).__name__}: {exc}"))

    # 2. The local real-candle cache.
    from data.market_data import get_bars
    try:
        rows, source = get_bars(symbol, n=limit, timeframe=timeframe, require_real=True)
    except (ValueError, RuntimeError) as exc:
        rows, source = [], f"{type(exc).__name__}: {exc}"
    results.append(_report(out, "local real-candle cache", rows, symbol, timeframe,
                           source))

    fresh = [row for row in results if row.get("status") == "FRESH"]
    print(file=out)
    if fresh:
        print(f"CHART WILL BE FRESH: {fresh[0]['label']} is current, and the Lab "
              "tries it before the cache.", file=out)
    elif any(row["candles"] for row in results):
        print("CHART WILL SAY STALE: every reachable source is behind. That is the "
              "honest state, not a display bug.", file=out)
        print("  The Lab shows these candles with the age above; it does not "
              "substitute anything.", file=out)
    else:
        print("NO REAL CANDLES ON THIS HOST: the Lab will fail closed and draw an "
              "empty frame.", file=out)
        print("  Neither source answered. Check outbound access to the venue, then "
              "the /data/sync cache.", file=out)
    return {"symbol": symbol, "timeframe": timeframe, "venue": venue,
            "sources": results, "any_fresh": bool(fresh)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--venue", default="binance_usdm")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    result = check(args.symbol.upper(), args.timeframe, args.venue, args.limit,
                   sys.stdout)
    return 0 if result["any_fresh"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
