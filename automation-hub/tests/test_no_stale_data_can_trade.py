"""Adversarial: try to get a new entry authorised on data that is not fresh.

The acceptance rule is absolute -- a single demonstrated path where stale or
freshness-unverified market data can authorise a new entry is a failure. So
these are not scenarios, they are attacks. Each one takes a real fallback that
exists in this codebase and tries to walk it all the way to an order.

The fallbacks attacked here are the ones that actually exist:

  data/market_data.py::get_bars degrades through
      local store (real) -> bundled sample CSV -> deterministic synthetic
  services/auto_engine.py keeps a cached higher-timeframe context
  services/auto_engine.py runs a replay loop fed by seeded synthetic candles
  data/ws_feed.py serves its in-memory cache in place of a provider read

Every one of them is legitimate somewhere -- charts, research, backtests,
simulation. None of them may authorise a live entry. What follows proves the
difference is enforced rather than intended.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from services.auto_engine import AutoStrategyEngine, EngineFeedError, MarketDataStaleError
from services.market_data_freshness import (
    BACKFILL_IN_PROGRESS,
    FRESHNESS_UNVERIFIED,
    MARKET_DATA_DISCONNECTED,
    MARKET_DATA_GAP,
    STALE_CANDLES,
    STALE_HTF_CANDLE,
    SUBSCRIPTION_UNHEALTHY,
    assess_feed,
)

UTC = timezone.utc
NOW = datetime(2026, 3, 2, 10, 0, 30, tzinfo=UTC)


def _entry_allowed(**kw) -> bool:
    """Would the platform authorise a NEW entry on this market-data state?"""
    kw.setdefault("now", NOW)
    kw.setdefault("last_event_at", NOW - timedelta(seconds=2))
    kw.setdefault("entry_timeframe", "5m")
    required = kw.pop("required", {"5m": datetime(2026, 3, 2, 9, 55, tzinfo=UTC),
                                   "1h": datetime(2026, 3, 2, 9, 0, tzinfo=UTC)})
    return assess_feed("BTCUSDT", required, **kw).allow_new_entry


# ------------------------------------------------ attack the source fallbacks

class _Probe:
    """The engine surface _forward_fetch_for_timeframe touches."""

    def __init__(self, source):
        self._source = source
        self.calls = 0

    def _fetcher(self, symbol, timeframe, limit):
        self.calls += 1
        bar = Bar(NOW - timedelta(minutes=5), 100, 101, 99, 100.5, 10)
        return [bar], self._source


@pytest.mark.parametrize("source", [
    "local store (real)",          # the database fallback
    "sample (bundled csv)",        # the historical-file fallback
    "synthetic",                   # the manufactured fallback
    "unavailable (real data required — run /data/sync)",
    "cache",                       # a last-known-good fallback
    "",                            # no provenance at all
    None,
])
def test_no_non_live_source_can_feed_a_forward_entry(source):
    """get_bars degrades to cache, CSV and synthetic. None may reach an entry.

    services/auto_engine.py::_forward_fetch_for_timeframe is the only door the
    live loop fetches through, and it refuses anything whose provenance is not
    a live provider read -- before the candles are even looked at.
    """
    probe = _Probe(source)
    with pytest.raises(EngineFeedError) as caught:
        AutoStrategyEngine._forward_fetch_for_timeframe(probe, "BTCUSDT", "5m", 50)
    assert "requires live" in str(caught.value)


@pytest.mark.parametrize("source", ["live (ccxt)", "live (websocket)", "live (test hub)"])
def test_a_live_provider_read_is_the_only_thing_that_gets_through(source):
    probe = _Probe(source)
    bars, got = AutoStrategyEngine._forward_fetch_for_timeframe(probe, "BTCUSDT", "5m", 50)
    assert got == source and bars


def test_a_live_source_label_still_does_not_excuse_a_stale_candle():
    """Provenance and freshness are two separate proofs; both are required.

    A provider can hand back genuinely live-sourced candles that are simply
    old -- a slow REST read, a venue lagging. Passing the source check must not
    be mistaken for passing the freshness check.
    """
    class _Snapshot:
        timeframe = "5m"
        market_data_status = ""
        last_blocker = ""
        last_blocker_timestamp = ""
        last_closed_candle = ""
        next_expected_candle = ""

    snapshot = _Snapshot()
    ancient = Bar(datetime.now(timezone.utc) - timedelta(hours=3), 100, 101, 99, 100.5, 10)
    with pytest.raises(MarketDataStaleError):
        AutoStrategyEngine._record_market_snapshot(snapshot, "BTCUSDT", [ancient])
    assert "STALE" in snapshot.last_blocker


# ------------------------------------------------- attack the timeframe gate

def test_a_stale_htf_cannot_ride_in_on_a_fresh_entry_candle():
    assert _entry_allowed(required={
        "5m": datetime(2026, 3, 2, 9, 55, tzinfo=UTC),      # fresh
        "1h": datetime(2026, 3, 2, 7, 0, tzinfo=UTC),       # stale
    }) is False


def test_a_stale_entry_candle_cannot_ride_in_on_a_fresh_htf():
    assert _entry_allowed(required={
        "5m": datetime(2026, 3, 2, 9, 0, tzinfo=UTC),       # stale
        "1h": datetime(2026, 3, 2, 9, 0, tzinfo=UTC),       # fresh
    }) is False


def test_the_blocker_names_which_clock_failed():
    """Both block; the operator still needs to know which one."""
    htf = assess_feed("BTCUSDT",
                      {"5m": datetime(2026, 3, 2, 9, 55, tzinfo=UTC),
                       "1h": datetime(2026, 3, 2, 7, 0, tzinfo=UTC)},
                      now=NOW, last_event_at=NOW - timedelta(seconds=2),
                      entry_timeframe="5m")
    entry = assess_feed("BTCUSDT",
                        {"5m": datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
                         "1h": datetime(2026, 3, 2, 9, 0, tzinfo=UTC)},
                        now=NOW, last_event_at=NOW - timedelta(seconds=2),
                        entry_timeframe="5m")
    assert htf.blocker == STALE_HTF_CANDLE
    assert entry.blocker == STALE_CANDLES


def test_a_fresh_ticker_cannot_stand_in_for_stale_candles():
    """The header price updating is not evidence the strategy's candles are.

    A mark/bookTicker stream can keep arriving -- so events are recent and the
    socket is healthy -- while the kline stream for the strategy's timeframe
    has stopped. Recent events must not launder an old candle.
    """
    assert _entry_allowed(
        required={"5m": datetime(2026, 3, 2, 8, 0, tzinfo=UTC)},
        last_event_at=NOW - timedelta(seconds=1),    # ticker is alive
    ) is False


# --------------------------------------------------- attack the transport

def test_connected_is_not_evidence():
    assert _entry_allowed(last_event_at=NOW - timedelta(seconds=300)) is False


def test_a_reconnect_does_not_authorise_anything_by_itself():
    """The specific regression: socket back, continuity not yet proven."""
    assert _entry_allowed(connection_state="CONNECTED", backfilling=True) is False
    assert _entry_allowed(connection_state="RECONNECTING") is False


def test_a_gap_blocks_even_when_the_newest_candle_is_current():
    """Continuity is its own proof. A hole behind a fresh candle still blocks."""
    assert _entry_allowed(gaps=["5m"]) is False


def test_unverified_provenance_costs_exactly_what_stale_costs():
    """"We could not prove it" must not be cheaper than "we proved it is old"."""
    feed = assess_feed("BTCUSDT",
                       {"5m": datetime(2026, 3, 2, 9, 55, tzinfo=UTC)},
                       now=NOW, last_event_at=NOW - timedelta(seconds=2),
                       verified=False)
    assert feed.blocker == FRESHNESS_UNVERIFIED
    assert feed.allow_new_entry is False


# ------------------------------------- attack every blocker as a whole set

@pytest.mark.parametrize("state,expected", [
    ({"connection_state": "DISCONNECTED"}, MARKET_DATA_DISCONNECTED),
    ({"backfilling": True}, BACKFILL_IN_PROGRESS),
    ({"gaps": ["5m"]}, MARKET_DATA_GAP),
    ({"last_event_at": NOW - timedelta(seconds=600)}, SUBSCRIPTION_UNHEALTHY),
    ({"subscribed": {"kline_5m": False}}, SUBSCRIPTION_UNHEALTHY),
    ({"verified": False}, FRESHNESS_UNVERIFIED),
])
def test_every_unhealthy_state_blocks_and_names_itself(state, expected):
    kw = {"now": NOW, "last_event_at": NOW - timedelta(seconds=2),
          "entry_timeframe": "5m"}
    kw.update(state)
    feed = assess_feed("BTCUSDT", {"5m": datetime(2026, 3, 2, 9, 55, tzinfo=UTC)}, **kw)
    assert feed.blocker == expected
    assert feed.allow_new_entry is False


def test_there_is_no_state_that_reports_fresh_without_a_verified_candle():
    """Sweep the space: only a verified, current, continuous feed opens the gate.

    Anything that is not simultaneously connected, delivering, subscribed, not
    backfilling, gap-free, attested and current must be shut. This enumerates
    the combinations rather than trusting the branch order.
    """
    from itertools import product

    opened = {"5m": datetime(2026, 3, 2, 9, 55, tzinfo=UTC)}
    allowed_combo = None
    for connected, delivering, backfilling, gapped, verified in product(
            (True, False), repeat=5):
        feed = assess_feed(
            "BTCUSDT", opened, now=NOW,
            connection_state="CONNECTED" if connected else "DISCONNECTED",
            last_event_at=NOW - timedelta(seconds=2 if delivering else 600),
            backfilling=backfilling, gaps=["5m"] if gapped else (),
            verified=verified, entry_timeframe="5m")
        if feed.allow_new_entry:
            allowed_combo = (connected, delivering, backfilling, gapped, verified)
            assert allowed_combo == (True, True, False, False, True), (
                f"an entry was authorised in state {allowed_combo}")
    assert allowed_combo is not None, "no state opened the gate; the sweep proved nothing"


# --------------------------------------------- the full acceptance sequence

def test_fresh_then_stale_then_recovered_end_to_end():
    """FRESH -> STALE -> blocked -> reconnect -> backfill -> verified -> FRESH.

    The failure this guards is resuming on the reconnect instead of on proven
    data, so every intermediate step asserts the gate is still shut.
    """
    fresh = {"5m": datetime(2026, 3, 2, 9, 55, tzinfo=UTC),
             "1h": datetime(2026, 3, 2, 9, 0, tzinfo=UTC)}
    stale = {"5m": datetime(2026, 3, 2, 8, 0, tzinfo=UTC),
             "1h": datetime(2026, 3, 2, 7, 0, tzinfo=UTC)}

    assert _entry_allowed(required=fresh) is True                       # trading
    assert _entry_allowed(required=fresh,
                          last_event_at=NOW - timedelta(seconds=300)) is False
    assert _entry_allowed(required=stale,
                          connection_state="RECONNECTING") is False     # reconnecting
    assert _entry_allowed(required=stale, backfilling=True) is False     # backfilling
    assert _entry_allowed(required=stale) is False                       # backfill ran,
    #                                                       candles still not current
    assert _entry_allowed(required=fresh, gaps=["5m"]) is False           # continuity
    assert _entry_allowed(required=fresh) is True                        # verified, resumed


def test_the_replay_loop_that_uses_synthetic_candles_is_not_the_live_loop():
    """Synthetic candles exist for simulation and must stay there.

    AutoStrategyEngine._run_replay seeds candles from data/market_data.py's
    synthetic generator. It is selected by ``self.live`` and the live branch
    never reaches it, which is what keeps manufactured data out of a forward
    paper entry.
    """
    import inspect

    source = inspect.getsource(AutoStrategyEngine._run)
    assert "if self.live:" in source
    assert "self._run_live()" in source
    assert "self._run_replay()" in source
    # and the replay loader is never called from the live loop
    live_source = inspect.getsource(AutoStrategyEngine._run_live)
    assert "_load_batch" not in live_source, (
        "the live loop reached the synthetic replay loader")
