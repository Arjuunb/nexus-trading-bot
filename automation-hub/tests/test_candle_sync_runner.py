"""The cache refresher: what it syncs, what it leaves alone, what it admits.

/data/sync was a manual POST with no scheduler, so the store drifted until
someone noticed -- 15.8h behind for BTCUSDT and empty for BNBUSDT on the
running host, with ~30 call sites reading it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.candle_sync_runner import CandleSyncRunner

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, held=None):
        self.held = dict(held or {})       # {(symbol, tf): datetime|None}

    def last_open_time(self, symbol, timeframe):
        return self.held.get((symbol, timeframe))


class FakeLedger:
    def __init__(self):
        self.logs = []

    def log(self, **kw):
        self.logs.append(kw)


def _runner(store, *, sync_fn=None, ledger=None, symbols=("BTCUSDT",),
            timeframes=("5m",)):
    return CandleSyncRunner(store, ledger or FakeLedger(), symbols=symbols,
                            timeframes=timeframes, sync_fn=sync_fn)


def _ok(_store, symbol, timeframe, target_candles=0):
    return {"stored": target_candles}


# ───────────────────────────── what needs work ─────────────────────────────

def test_a_current_pair_is_not_refetched():
    """Refetching what has not changed is load on the venue and noise that
    hides a pair that genuinely failed."""
    store = FakeStore({("BTCUSDT", "5m"): NOW - timedelta(minutes=5)})
    calls = []
    out = _runner(store, sync_fn=lambda *a, **k: calls.append(a) or _ok(*a, **k)).check(now=NOW)
    assert out["stale"] == 0 and out["synced"] == 0 and calls == []


def test_a_pair_behind_the_venue_is_refreshed():
    """The observed case: real candles, 15.8 hours old."""
    store = FakeStore({("BTCUSDT", "5m"): NOW - timedelta(hours=16)})
    calls = []

    def sync_fn(_s, symbol, timeframe, target_candles=0):
        calls.append((symbol, timeframe, target_candles))
        return {"stored": 1000}

    out = _runner(store, sync_fn=sync_fn).check(now=NOW)
    assert out["stale"] == 1 and out["synced"] == 1 and out["failed"] == 0
    assert calls == [("BTCUSDT", "5m", 1000)], "a top-up must be one venue page"


def test_a_pair_holding_nothing_is_backfilled_not_topped_up():
    """BNBUSDT held no candles at all. There is no history to extend and the
    consumers need depth, not the last few bars."""
    store = FakeStore({("BNBUSDT", "5m"): None})
    calls = []

    def sync_fn(_s, symbol, timeframe, target_candles=0):
        calls.append(target_candles)
        return {"stored": target_candles}

    _runner(store, sync_fn=sync_fn, symbols=("BNBUSDT",)).check(now=NOW)
    assert calls == [3000], "an empty pair was topped up like a stale one"


def test_freshness_is_asked_never_decided_here():
    """A threshold compiled into the scheduler would drift from the one the
    dashboard shows and the gates enforce."""
    import services.candle_sync_runner as mod
    src = (mod.__file__ or "")
    text = open(src).read()
    assert "assess_timeframe" in text
    for invented in ("age_seconds >", "> 3600", "timedelta(hours=", "STALE_AFTER"):
        assert invented not in text, f"scheduler invents its own staleness: {invented}"


# ───────────────────────── failure must stay visible ─────────────────────────

def test_a_failed_sync_is_reported_not_swallowed():
    """The data is still stale afterwards. A scheduler that hid its own
    failures would leave the staleness and remove the reason to look."""
    store = FakeStore({("BTCUSDT", "5m"): NOW - timedelta(hours=16)})
    ledger = FakeLedger()
    out = _runner(store, ledger=ledger,
                  sync_fn=lambda *a, **k: {"error": "network unreachable"}).check(now=NOW)
    assert out["failed"] == 1 and out["synced"] == 0
    assert out["pairs"][0]["error"] == "network unreachable"
    warn = [l for l in ledger.logs if l.get("level") == "warning"]
    assert warn and "stay behind" in warn[0]["message"]


def test_a_raising_sync_is_caught_and_counted_as_failed():
    store = FakeStore({("BTCUSDT", "5m"): NOW - timedelta(hours=16)})

    def boom(*_a, **_k):
        raise ConnectionError("dns")

    out = _runner(store, sync_fn=boom).check(now=NOW)
    assert out["failed"] == 1
    assert "ConnectionError" in out["pairs"][0]["error"]


def test_one_failing_pair_does_not_stop_the_others():
    store = FakeStore({("BTCUSDT", "5m"): NOW - timedelta(hours=16),
                       ("ETHUSDT", "5m"): NOW - timedelta(hours=16)})

    def sync_fn(_s, symbol, timeframe, target_candles=0):
        if symbol == "BTCUSDT":
            raise ConnectionError("dns")
        return {"stored": 1000}

    out = _runner(store, sync_fn=sync_fn,
                  symbols=("BTCUSDT", "ETHUSDT")).check(now=NOW)
    assert out["synced"] == 1 and out["failed"] == 1


def test_an_unreadable_store_counts_as_stale_rather_than_fresh():
    """Failing to read the cache must not read as 'the cache is fine'."""
    class Broken(FakeStore):
        def last_open_time(self, symbol, timeframe):
            raise RuntimeError("db locked")

    out = _runner(Broken(), sync_fn=_ok).check(now=NOW)
    assert out["stale"] == 1


def test_status_reports_the_last_pass_without_redoing_it():
    store = FakeStore({("BTCUSDT", "5m"): NOW - timedelta(minutes=5)})
    runner = _runner(store, sync_fn=_ok)
    assert runner.status()["last_check"] is None
    runner.check(now=NOW)
    st = runner.status()
    assert st["last_check"] and st["result"]["stale"] == 0
    assert st["running"] is False


# ───────────────────── a pass has to stay bounded ─────────────────────
#
# A cold cache has every pair stale at once. Syncing all forty in one pass is
# forty sequential venue calls before the loop reports anything -- and with an
# unreachable venue, forty hanging ones.

def _all_stale(symbols, timeframes):
    return FakeStore({(s, tf): None for s in symbols for tf in timeframes})


SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT",
        "DOGEUSDT", "ADAUSDT", "LINKUSDT")
TFS = ("5m", "15m", "1h", "4h", "1d")


def test_a_cold_cache_is_spread_across_passes():
    calls = []

    def sync_fn(_s, symbol, timeframe, target_candles=0):
        calls.append((symbol, timeframe))
        return {"stored": target_candles}

    store = _all_stale(SYMS, TFS)
    out = _runner(store, sync_fn=sync_fn, symbols=SYMS,
                  timeframes=TFS).check(now=NOW)
    assert out["stale"] == 40
    assert len(calls) == 8, "one pass tried to sync the whole cold cache"
    assert out["synced"] == 8 and out["deferred"] == 32


def test_deferred_work_is_reported_not_silently_dropped():
    """32 pairs are still stale after that pass. Reporting 'synced 8' alone
    would read as done."""
    store = _all_stale(SYMS, TFS)
    out = _runner(store, sync_fn=_ok, symbols=SYMS, timeframes=TFS).check(now=NOW)
    assert out["deferred"] == 32
    assert out["stale"] == out["synced"] + out["failed"] + out["deferred"]


def test_an_unreachable_venue_ends_the_pass_early():
    """Every remaining pair fails the same way, slowly. There is nothing to
    learn from the other attempts."""
    calls = []

    def dead(_s, symbol, timeframe, target_candles=0):
        calls.append(symbol)
        raise ConnectionError("unreachable")

    store = _all_stale(SYMS, TFS)
    out = _runner(store, sync_fn=dead, symbols=SYMS, timeframes=TFS).check(now=NOW)
    assert len(calls) == 3, f"kept trying a dead venue {len(calls)} times"
    assert out["gave_up_early"] is True and out["failed"] == 3


def test_a_recovering_venue_resets_the_failure_run():
    """One flaky pair must not end the pass for the healthy ones."""
    seen = []

    def flaky(_s, symbol, timeframe, target_candles=0):
        seen.append(symbol)
        if len(seen) in (1, 3):
            raise ConnectionError("blip")
        return {"stored": target_candles}

    store = _all_stale(SYMS, TFS)
    out = _runner(store, sync_fn=flaky, symbols=SYMS, timeframes=TFS).check(now=NOW)
    assert out["gave_up_early"] is False
    assert out["synced"] == 6 and out["failed"] == 2
