"""Historical data engine: local store CRUD/dedupe/incremental, sync pagination
(with an injected fetcher — no network), and the /data endpoints.

The fetcher fixtures here are *storage-logic* fixtures, not market price fakes:
they exercise the cache/pagination, and production never serves them as candles.
"""
import pytest

from data.historical import HistoricalStore, sync, update, SYMBOLS, TIMEFRAMES, _TF_MS


def _kline(open_time, price):
    # Binance row shape: [openTime, open, high, low, close, volume, closeTime, ...]
    return [open_time, price, price + 1, price - 1, price + 0.5, 10.0, open_time + 1]


def test_store_upsert_get_and_dedupe(tmp_path):
    st = HistoricalStore(str(tmp_path / "m.db"))
    rows = [(1000, 1, 2, 0.5, 1.5, 9), (2000, 2, 3, 1.5, 2.5, 9)]
    assert st.upsert("BTCUSDT", "4h", rows) == 2
    # re-upserting the same open_time replaces, doesn't duplicate
    st.upsert("BTCUSDT", "4h", [(1000, 9, 9, 9, 9, 9)])
    bars = st.get_bars("BTCUSDT", "4h")
    assert len(bars) == 2                      # still 2 (deduped on primary key)
    assert bars[0].open == 9                    # replaced
    assert bars[0].timestamp < bars[1].timestamp  # sorted ascending


def test_store_get_n_and_window(tmp_path):
    st = HistoricalStore(str(tmp_path / "m.db"))
    st.upsert("ETHUSDT", "1d", [(i * 1000, i, i, i, i, 1) for i in range(1, 11)])
    assert len(st.get_bars("ETHUSDT", "1d", n=3)) == 3                 # last 3
    assert len(st.get_bars("ETHUSDT", "1d", start_ms=5000)) == 6       # >= 5000
    cov = st.coverage("ETHUSDT", "1d")
    assert cov["candles"] == 10 and cov["first"] and cov["last"]


def _reference_bars(st, symbol, timeframe, n=None, start_ms=None, end_ms=None):
    """What get_bars returned before it read only the newest n rows: the whole
    filtered series in time order, then its last n."""
    c = st._conn()
    try:
        q = "SELECT open_time FROM candles WHERE symbol=? AND timeframe=?"
        args = [symbol, timeframe]
        if start_ms is not None:
            q += " AND open_time>=?"; args.append(start_ms)
        if end_ms is not None:
            q += " AND open_time<=?"; args.append(end_ms)
        rows = [r[0] for r in c.execute(q + " ORDER BY open_time", args)]
    finally:
        c.close()
    return rows[-n:] if n is not None and len(rows) > n else rows


def test_the_newest_n_bars_are_exactly_what_a_full_read_kept(tmp_path):
    st = HistoricalStore(str(tmp_path / "m.db"))
    st.upsert("ETHUSDT", "1d", [(i * 1000, i, i, i, i, 1) for i in range(1, 41)])
    st.upsert("BTCUSDT", "1d", [(i * 1000, i, i, i, i, 1) for i in range(1, 6)])   # another series
    for n in (None, 0, 1, 3, 10, 40, 100):
        for start in (None, 5000, 39000):
            for end in (None, 20000, 500):
                got = [int(b.timestamp.timestamp() * 1000)
                       for b in st.get_bars("ETHUSDT", "1d", n=n, start_ms=start, end_ms=end)]
                assert got == _reference_bars(st, "ETHUSDT", "1d", n, start, end), (n, start, end)


def test_the_newest_n_bars_are_read_without_reading_the_whole_series(tmp_path, monkeypatch):
    st = HistoricalStore(str(tmp_path / "m.db"))
    st.upsert("BTCUSDT", "5m", [(i * 300_000, 1, 2, 0.5, 1.5, 9) for i in range(1, 5001)])
    statements = []
    real = st._conn

    def traced():
        c = real()
        c.set_trace_callback(statements.append)
        return c
    monkeypatch.setattr(st, "_conn", traced)
    bars = st.get_bars("BTCUSDT", "5m", n=10)
    assert [int(b.timestamp.timestamp() * 1000) for b in bars] == [i * 300_000 for i in range(4991, 5001)]
    [select] = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert "DESC LIMIT 10" in select.upper()

def test_sync_paginates_backward_with_injected_fetcher(tmp_path):
    st = HistoricalStore(str(tmp_path / "m.db"))
    step = _TF_MS["4h"]
    now = 100 * step

    def fake_fetch(symbol, interval, *, limit=1000, end_ms=None, start_ms=None, hosts=None):
        # serve full batches walking backward until we cross 0
        end = end_ms if end_ms is not None else now
        rows = []
        t = end - (end % step)
        for _ in range(min(limit, 1000)):
            if t < 0:
                break
            rows.append(_kline(t, 100 + t / step))
            t -= step
        rows.reverse()                          # ascending, like Binance
        return rows

    res = sync(st, "BTCUSDT", "4h", target_candles=50, fetcher=fake_fetch)
    assert res["stored"] >= 50 and res["source"] == "binance (real)"
    assert st.coverage("BTCUSDT", "4h")["candles"] >= 50


def test_sync_rejects_unsupported(tmp_path):
    st = HistoricalStore(str(tmp_path / "m.db"))
    assert "error" in sync(st, "FAKEUSDT", "4h", fetcher=lambda *a, **k: [])
    assert "error" in sync(st, "BTCUSDT", "3m", fetcher=lambda *a, **k: [])


def test_sync_reports_failure_without_faking(tmp_path):
    st = HistoricalStore(str(tmp_path / "m.db"))
    def boom(*a, **k):
        raise RuntimeError("network down")
    res = sync(st, "BTCUSDT", "4h", fetcher=boom)
    assert res["stored"] == 0 and "error" in res     # no fabricated candles


def test_update_incremental(tmp_path):
    st = HistoricalStore(str(tmp_path / "m.db"))
    step = _TF_MS["4h"]
    st.upsert("SOLUSDT", "4h", [(step, 1, 1, 1, 1, 1)])
    # last stored is `step`; update should fetch from step+interval forward
    calls = {}
    def fetch_fwd(symbol, interval, *, limit=1000, start_ms=None, end_ms=None, hosts=None):
        calls["start_ms"] = start_ms
        return [_kline(start_ms, 5), _kline(start_ms + step, 6)]
    res = update(st, "SOLUSDT", "4h", fetcher=fetch_fwd)
    assert calls["start_ms"] == 2 * step           # last + one interval
    assert res["stored"] == 2


def test_supported_universe():
    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"}.issubset(set(SYMBOLS))
    assert {"1w", "1d", "4h", "1h", "30m", "15m", "5m"}.issubset(set(TIMEFRAMES))


# ---- endpoints ----
@pytest.fixture()
def client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    webhook_api.market_store = HistoricalStore(str(tmp_path / "m.db"))
    app = FastAPI(); app.include_router(webhook_api.router)
    return TestClient(app)


SECRET = "dev-control-key"


def test_coverage_endpoint(client):
    body = client.get("/data/coverage").json()
    assert body["symbols"] == list(SYMBOLS)
    assert body["timeframes"] == list(TIMEFRAMES)
    assert isinstance(body["coverage"], list)


def test_sync_endpoint_is_gated(client):
    assert client.post("/data/sync").status_code == 401     # secret required
    # with secret it runs (network down in CI -> returns an error, not fake data)
    r = client.post("/data/sync", params={"symbol": "BTCUSDT", "timeframe": "4h", "target_candles": 100},
                    headers={"X-Webhook-Secret": SECRET})
    assert r.status_code == 200
    assert "stored" in r.json() or "error" in r.json()
