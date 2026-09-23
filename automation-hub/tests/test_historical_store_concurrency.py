"""A bulk sync must not take the dashboard down with it.

The live Price Action and SMC labs read candles from the same SQLite file that
``/data/sync`` writes. With a rollback journal and one giant transaction, a
185,000-row sync held a lock over the whole database for minutes: every chart
read behind it failed and the labs showed HTTP 502 with a STALE feed. These
tests pin the three things that fixed it.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from data.historical import HistoricalStore, sync


def _rows(count: int, start_ms: int = 1_700_000_000_000):
    return [(start_ms + i * 300_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0)
            for i in range(count)]


@pytest.fixture()
def store(tmp_path):
    return HistoricalStore(str(tmp_path / "market.db"))


def test_a_reader_is_not_stalled_by_a_bulk_write(store):
    """Read while a large sync writes, and measure the stall.

    Latency is the signal, not errors. Python's SQLite waits rather than
    failing, so the rollback-journal behaviour never raised "database is
    locked" -- it simply blocked, and a blocked request is what a reverse
    proxy eventually turns into a 502. Measured on 185,000 rows, p95 read
    latency was 652ms on the rollback journal and 7ms on WAL.
    """
    store.upsert("BTCUSDT", "5m", _rows(50))       # something to read

    baseline = time.perf_counter()
    store.get_bars("BTCUSDT", "5m", n=10)
    baseline = time.perf_counter() - baseline
    if baseline > 0.05:
        pytest.skip(f"machine too slow to measure contention (idle read {baseline:.3f}s)")

    errors: list = []
    done = threading.Event()

    def writer():
        try:
            store.upsert("BTCUSDT", "5m", _rows(60_000, 1_800_000_000_000))
        except Exception as exc:                    # noqa: BLE001 - recorded, not raised
            errors.append(("writer", exc))
        finally:
            done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    latencies: list = []
    try:
        while not done.is_set() and len(latencies) < 2000:
            started = time.perf_counter()
            try:
                store.get_bars("BTCUSDT", "5m", n=10)
            except sqlite3.OperationalError as exc:
                errors.append(("reader", exc))
                break
            latencies.append(time.perf_counter() - started)
            time.sleep(0.001)
    finally:
        thread.join(timeout=120)

    assert not errors, f"a concurrent read or write failed: {errors}"
    assert latencies, "the write finished before a single read was attempted"
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    assert p95 < 0.25, (
        f"reads stalled behind the write: p95 {p95 * 1000:.0f}ms over "
        f"{len(latencies)} reads -- this is the shape that returns 502")


def test_a_large_upsert_writes_every_row(store):
    """Whatever the transaction shape, nothing may be silently dropped."""
    written = store.upsert("BTCUSDT", "5m", _rows(12_345))
    assert written == 12_345
    assert len(store.get_bars("BTCUSDT", "5m", n=20_000)) == 12_345


def test_the_store_runs_in_wal_mode(store):
    """WAL is the reason a reader is not blocked at all, rather than merely
    blocked for less time."""
    store.upsert("BTCUSDT", "5m", _rows(5))
    connection = sqlite3.connect(store.db_path)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert mode.lower() == "wal"


def test_paging_backward_still_yields_oldest_to_newest(store):
    """The accumulation changed shape, so the ordering needs re-proving.

    ``sync`` pages backward: the first response is the newest window and each
    one after it is older. The stored series must still run oldest to newest,
    or every ATR and every pivot downstream is computed on reversed history.
    """
    # A full page keeps the loop going; a short one ends it. Two 2-row pages
    # would stop after the first and prove nothing about ordering.
    newest = _rows(1000, 2_000_000_000_000)
    older = _rows(500, 1_000_000_000_000)
    pages = [newest, older]
    calls: list = []

    def fetcher(symbol, timeframe, *, limit=1000, end_ms=None, **kw):
        calls.append(end_ms)
        return pages[len(calls) - 1] if len(calls) <= len(pages) else []

    result = sync(store, "BTCUSDT", "5m", target_candles=1500, fetcher=fetcher)
    assert result["fetched"] == 1500
    opens = [int(bar.timestamp.timestamp() * 1000)
             for bar in store.get_bars("BTCUSDT", "5m", n=5000)]
    assert opens == sorted(opens), "history was stored newest-first"
    assert opens[0] == 1_000_000_000_000 and opens[-1] == newest[-1][0]
    assert calls[0] is None and calls[1] == newest[0][0] - 1   # paged backward


def test_a_failed_page_still_writes_what_was_already_fetched(store):
    """A network failure halfway through must not discard the good pages.

    The old code returned an error only when nothing at all had been collected;
    that behaviour is worth keeping, because re-syncing a year because the last
    request timed out is how people start reaching for synthetic data.
    """
    def fetcher(symbol, timeframe, *, limit=1000, end_ms=None, **kw):
        if end_ms is None:
            return [(5_000, 5.0, 5.0, 5.0, 5.0, 1.0)]
        raise RuntimeError("binance said no")

    result = sync(store, "BTCUSDT", "5m", target_candles=50, fetcher=fetcher)
    assert result.get("stored") == 1
    assert "error" not in result
