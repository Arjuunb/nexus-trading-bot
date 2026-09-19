"""A cache read that answers a question about NOW must carry its age.

get_bars(require_real=True) promises the candles are REAL and says nothing
about WHEN. The local store has served candles 25 seconds old and candles 15.8
hours old through that same call, indistinguishably.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _bars(newest_at, count=30, step_minutes=60):
    start = newest_at - timedelta(minutes=step_minutes * (count - 1))
    return [Bar(start + timedelta(minutes=step_minutes * i),
                100.0, 101.0, 99.0, 100.0 + i, 5.0) for i in range(count)]


# ─────────────────────── the judged read itself ───────────────────────

def test_the_judged_read_returns_the_age_beside_the_data(monkeypatch):
    import data.market_data as md
    monkeypatch.setattr(md, "get_bars",
                        lambda *a, **k: (_bars(NOW - timedelta(hours=1)), "cache"))
    bars, source, freshness = md.get_bars_judged("BTCUSDT", n=30, timeframe="1h",
                                                 now=NOW)
    assert bars and source == "cache"
    assert freshness.status == "FRESH"


def test_the_judged_read_flags_a_series_that_is_behind(monkeypatch):
    import data.market_data as md
    monkeypatch.setattr(md, "get_bars",
                        lambda *a, **k: (_bars(NOW - timedelta(hours=16)), "cache"))
    _, _, freshness = md.get_bars_judged("BTCUSDT", n=30, timeframe="1h", now=NOW)
    assert freshness.status == "STALE" and freshness.blocker == "STALE_CANDLES"


def test_an_empty_series_is_missing_not_fresh(monkeypatch):
    import data.market_data as md
    monkeypatch.setattr(md, "get_bars", lambda *a, **k: ([], "none"))
    _, _, freshness = md.get_bars_judged("BNBUSDT", n=30, timeframe="1h", now=NOW)
    assert freshness.status == "MISSING"


def test_the_judged_read_does_not_filter_or_refuse(monkeypatch):
    """It hands the caller the fact. Deciding what to do with a stale series
    belongs to the caller, not to the reader."""
    import data.market_data as md
    rows = _bars(NOW - timedelta(days=9))
    monkeypatch.setattr(md, "get_bars", lambda *a, **k: (rows, "cache"))
    bars, _, freshness = md.get_bars_judged("BTCUSDT", n=30, timeframe="1h", now=NOW)
    assert not freshness.fresh
    assert bars == rows, "a stale series must still be returned, just labelled"


# ───────────────────── the symbol quote that used it ─────────────────────

def _info(monkeypatch, newest_at, timeframe="1h"):
    """market_info() for a crypto symbol, over candles ending at `newest_at`.

    The timeframe matters to the verdict and not just to the data: 16 hours
    old is STALE for an hourly series and perfectly FRESH for a daily one,
    which is the authority's whole point and why nothing here hard-codes a
    staleness threshold of its own.
    """
    import services.symbol_universe as su
    import data.market_data as md
    monkeypatch.setattr(md, "get_bars",
                        lambda *a, **k: (_bars(newest_at, step_minutes=60), "cache"))
    return su.market_info("BTCUSDT", timeframe=timeframe)


def test_a_quote_built_from_stale_candles_says_so(monkeypatch):
    """price / change_24h_pct / volume_24h read as a live quote. Built from
    day-old candles and reported without an age, they are simply wrong."""
    info = _info(monkeypatch, datetime.now(timezone.utc) - timedelta(hours=16))
    if not info.get("price_available"):
        pytest.skip("BTCUSDT is not a crypto entry in this universe build")
    assert info["stale"] is True
    assert info["freshness"]["status"] == "STALE"
    assert info["as_of"], "a price must say which candle it came from"


def test_a_quote_from_current_candles_is_not_flagged(monkeypatch):
    """The mark has to be able to say no, or it says nothing."""
    info = _info(monkeypatch, datetime.now(timezone.utc) - timedelta(minutes=30))
    if not info.get("price_available"):
        pytest.skip("BTCUSDT is not a crypto entry in this universe build")
    assert info["stale"] is False


def test_the_same_age_is_judged_per_timeframe_not_by_a_fixed_rule(monkeypatch):
    """16 hours is stale hourly and fresh daily. A threshold compiled into
    the quote would get one of those wrong."""
    at = datetime.now(timezone.utc) - timedelta(hours=16)
    hourly = _info(monkeypatch, at, timeframe="1h")
    daily = _info(monkeypatch, at, timeframe="1d")
    if not hourly.get("price_available") or not daily.get("price_available"):
        pytest.skip("BTCUSDT is not a crypto entry in this universe build")
    assert hourly["stale"] is True and daily["stale"] is False
