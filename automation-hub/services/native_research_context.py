"""Causal native-candle context for historical multi-timeframe research.

Research must not turn 5m candles into a supposed Binance 1h/15m vote.  The
same independently sourced, *closed* clocks used by forward paper are sliced
at each historical decision boundary here.  Missing cache data is an explicit
unavailable result, never an all-WARMUP backtest presented as zero trades.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from bot.types import Bar
from services.mtf_policy import TIMEFRAME_SECONDS, evidence_at, policy_for, utc


class NativeResearchTimeline:
    """Expose only native candles closed by one entry candle's close."""

    def __init__(self, symbol: str, entry_timeframe: str,
                 native: Mapping[str, Sequence[Bar]], *, required: Sequence[str],
                 minimum_bars: Mapping[str, int] | None = None):
        primary, secondary = policy_for(entry_timeframe)
        if primary not in required:
            raise ValueError(f"native primary {primary} must be a required strategy timeframe")
        self.symbol = symbol
        self.entry_timeframe = entry_timeframe
        self.primary_timeframe = primary
        self.secondary_timeframe = secondary
        self.required = tuple(required)
        self.minimum_bars = minimum_bars or {}
        self.series: dict[str, tuple[Bar, ...]] = {}
        self.closes: dict[str, tuple[datetime, ...]] = {}
        for timeframe in dict.fromkeys((*required, secondary)):
            if timeframe is None or timeframe == entry_timeframe:
                continue
            bars = tuple(native.get(timeframe) or ())
            if timeframe in required and not bars:
                raise ValueError(f"native {timeframe} history is required for {symbol} research")
            duration = TIMEFRAME_SECONDS[timeframe]
            opens = tuple(utc(bar.timestamp) for bar in bars)
            if any(int(stamp.timestamp()) % duration for stamp in opens):
                raise ValueError(f"native {timeframe} candles are not epoch-aligned")
            if any(left >= right for left, right in zip(opens, opens[1:])):
                raise ValueError(f"native {timeframe} has duplicate or unordered candles")
            self.series[timeframe] = bars
            self.closes[timeframe] = tuple(
                stamp + timedelta(seconds=duration) for stamp in opens)

    def at(self, entry_bars: Sequence[Bar], index: int) -> tuple[dict[str, list[Bar]], dict]:
        bar = entry_bars[index]
        decision_close = utc(bar.timestamp) + timedelta(
            seconds=TIMEFRAME_SECONDS[self.entry_timeframe])
        context = {self.entry_timeframe: list(entry_bars[max(0, index - 599):index + 1])}
        for timeframe, bars in self.series.items():
            end = bisect_right(self.closes[timeframe], decision_close)
            # Forward paper requests twice the indicator warmup for native HTF
            # context.  Use the same bounded lookback in research.
            window = max(140, 2 * int(self.minimum_bars.get(timeframe, 0)))
            context[timeframe] = list(bars[max(0, end - window):end])
        evidence = evidence_at(self.symbol, self.entry_timeframe, context, decision_close)
        return context, evidence

    def apply(self, strategy, entry_bars: Sequence[Bar], index: int) -> None:
        context, evidence = self.at(entry_bars, index)
        native = {tf: context.get(tf, []) for tf in
                  (self.primary_timeframe, self.secondary_timeframe) if tf}
        strategy.set_native_mtf_context(native, evidence)
        strategy.set_timeframe_context(context)


def load_adaptive_history(symbol: str, *, limit: int = 4000,
                          entry_rows: Sequence[Bar] | None = None) -> tuple[list[Bar], dict[str, list[Bar]]]:
    """Load verified Binance USD-M candles from the V2 cache, without fallback.

    An explicitly supplied entry slice (walk-forward/league callers) must be
    identical to the native cache.  Otherwise mixing legacy spot entries with
    USD-M higher timeframes would manufacture a misleading research result.
    """
    from config import settings
    from data.market_data_v2 import MarketDataService
    from strategies.adaptive_trend_pullback.config import AdaptiveTrendPullbackConfig

    service = MarketDataService(settings.market_data_v2_dir)
    now = datetime.now(timezone.utc)
    minimum_bars = AdaptiveTrendPullbackConfig.from_env().minimum_bars

    def read(timeframe: str, *, mandatory: bool) -> list[Bar]:
        state = service.status(symbol, timeframe)
        valid = (state.get("available") and state.get("checksum_ok")
                 and state.get("asset_class") == "crypto"
                 and state.get("providers") == ["binance-usdt-perpetual"]
                 and (state.get("integrity") or {}).get("status") == "healthy")
        if not valid:
            if mandatory:
                raise ValueError(
                    f"Adaptive MTF research requires verified native Binance USD-M "
                    f"{symbol} {timeframe} history in the Market Data V2 cache")
            return []
        bars = service.bars(symbol, timeframe, limit=0)
        if any(utc(bar.timestamp) + timedelta(seconds=TIMEFRAME_SECONDS[timeframe]) > now
               for bar in bars):
            if mandatory:
                raise ValueError(f"native {timeframe} cache contains a forming/future candle")
            return []
        return bars

    all_entry = read("5m", mandatory=True)
    if entry_rows is None:
        rows = all_entry[-max(1, int(limit)):]
    else:
        rows = list(entry_rows)
        indexed = {utc(row.timestamp): row for row in all_entry}
        for row in rows:
            found = indexed.get(utc(row.timestamp))
            if found is None or (found.open, found.high, found.low, found.close, found.volume) != (
                    row.open, row.high, row.low, row.close, row.volume):
                raise ValueError("Adaptive MTF entry candles do not match native Binance USD-M cache")
    if not rows:
        raise ValueError(f"Adaptive MTF research has no native {symbol} 5m candles")
    if any(utc(a.timestamp) >= utc(b.timestamp) for a, b in zip(rows, rows[1:])):
        raise ValueError("Adaptive MTF entry candles are duplicate or unordered")
    entry_step = TIMEFRAME_SECONDS["5m"]
    if any(int(utc(row.timestamp).timestamp()) % entry_step for row in rows):
        raise ValueError("Adaptive MTF entry candles are not epoch-aligned")
    if any((utc(b.timestamp) - utc(a.timestamp)).total_seconds() != entry_step
           for a, b in zip(rows, rows[1:])):
        raise ValueError("Adaptive MTF entry candles contain a gap")

    native = {timeframe: read(timeframe, mandatory=timeframe != "4h")
              for timeframe in ("1h", "15m", "4h")}
    last_close = utc(rows[-1].timestamp) + timedelta(seconds=TIMEFRAME_SECONDS["5m"])
    for timeframe in ("1h", "15m"):
        closed = [bar for bar in native[timeframe]
                  if utc(bar.timestamp) + timedelta(seconds=TIMEFRAME_SECONDS[timeframe]) <= last_close]
        needed = int(minimum_bars[timeframe])
        if len(closed) < needed:
            raise ValueError(
                f"Adaptive MTF research has only {len(closed)} completed native "
                f"{timeframe} candles by the final 5m decision; {needed} are required")
        latest_close = utc(closed[-1].timestamp) + timedelta(
            seconds=TIMEFRAME_SECONDS[timeframe])
        if (last_close - latest_close).total_seconds() >= TIMEFRAME_SECONDS[timeframe]:
            raise ValueError(
                f"Adaptive MTF native {timeframe} history is stale at the final 5m decision")
    return rows, native
