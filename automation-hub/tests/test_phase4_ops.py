"""Phase 4 — data & operations: websocket feed cache/fallback, watchdog rules
+ alert cooldown, funding-rate interpretation."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from data.ledger import SqliteLedger
from data.ws_feed import WebSocketFeed, _to_pair
from services.funding import interpret
from services.watchdog import Watchdog, evaluate


def _rows(n, start_min=0, tf_min=60, price=100.0):
    base = datetime.now(timezone.utc) - timedelta(minutes=start_min)
    return [[int((base + timedelta(minutes=i * tf_min)).timestamp() * 1000),
             price, price + 1, price - 1, price + 0.5, 10.0] for i in range(n)]


# ─────────────────────────── websocket feed ───────────────────────────
def test_ws_feed_ingests_and_updates_in_progress_candle():
    feed = WebSocketFeed(["BTCUSDT"], timeframe="1h")
    rows = _rows(3, start_min=180)
    feed.ingest_rows("BTCUSDT", rows)
    assert len(feed.get_bars("BTCUSDT")) == 3
    # same-timestamp row REPLACES the in-progress candle instead of appending
    # (reuse the SAME rows — rebuilding from now() would shift timestamps)
    again = [list(r) for r in rows]
    again[-1][4] = 123.45
    feed.ingest_rows("BTCUSDT", again)
    bars = feed.get_bars("BTCUSDT")
    assert len(bars) == 3 and bars[-1].close == 123.45


class _AliveThread:
    """Stands in for the stream thread: ws_feed only asks is_alive()."""

    def is_alive(self) -> bool:
        return True


def test_ws_fetcher_serves_cache_when_fresh_and_falls_back_when_not():
    feed = WebSocketFeed(["BTCUSDT"], timeframe="1h")
    calls = []

    def rest(symbol, tf, limit):
        calls.append(symbol)
        return [b for b in feed.get_bars(symbol, limit)] or [], "live (ccxt)"

    fetcher = feed.make_fetcher(rest)
    # empty cache -> REST fallback
    bars, src = fetcher("BTCUSDT", "1h", 50)
    assert src == "live (ccxt)" and calls == ["BTCUSDT"]

    # The cache is only served while the stream that fills it is actually
    # running. ingest_rows is called from the watch loop and nowhere else, so
    # a dead thread means a frozen cache -- serving that as "live (websocket)"
    # would hand a strategy data nothing is updating. Production always has the
    # thread; the test has to say so rather than skip the condition.
    feed._thread = _AliveThread()

    # fresh cache with enough depth -> served from the stream
    feed.ingest_rows("BTCUSDT", _rows(40, start_min=40 * 60, tf_min=60))
    bars, src = fetcher("BTCUSDT", "1h", 30)
    assert src == "live (websocket)" and len(bars) == 30
    assert calls == ["BTCUSDT"]                       # no extra REST call
    # a different timeframe is never served from this stream
    bars, src = fetcher("BTCUSDT", "4h", 50)
    assert src == "live (ccxt)"

    # and when the stream thread dies the cache stops being served, even
    # though the bars in it are unchanged and still look recent.
    feed._thread = None
    bars, src = fetcher("BTCUSDT", "1h", 30)
    assert src == "live (ccxt)", (
        "a frozen cache was served as a live websocket read")


def test_ws_feed_stale_cache_is_not_fresh():
    feed = WebSocketFeed(["BTCUSDT"], timeframe="1h")
    feed.ingest_rows("BTCUSDT", _rows(5, start_min=60 * 24))  # a day old
    assert feed.fresh("BTCUSDT") is False


def test_ws_feed_without_ccxtpro_reports_honestly():
    feed = WebSocketFeed(["BTCUSDT"])
    started = feed.start()
    if not started:                                    # ccxt.pro not installed here
        assert "ccxt.pro" in feed.last_error
        assert feed.status()["available"] is False


def test_to_pair():
    assert _to_pair("BTCUSDT") == "BTC/USDT"
    assert _to_pair("SOL/USDC") == "SOL/USDC"


# ─────────────────────────── watchdog rules (pure) ───────────────────────────
def test_watchdog_flags_stalled_live_engine():
    f = evaluate(running=True, live=True, timeframe="1h",
                 last_activity_age_s=4 * 3600, thread_alive=True,
                 data_source="live (ccxt)")
    assert any(x["key"] == "engine-stalled" for x in f)


def test_watchdog_quiet_when_healthy_or_stopped():
    healthy = evaluate(running=True, live=True, timeframe="1h",
                       last_activity_age_s=120, thread_alive=True,
                       data_source="live (websocket)")
    assert healthy == []
    stopped = evaluate(running=False, live=True, timeframe="1h",
                       last_activity_age_s=None, thread_alive=False,
                       data_source=None)
    assert stopped == []                     # a stopped bot is a choice, not a fault


def test_watchdog_flags_dead_thread_and_fake_feed():
    dead = evaluate(running=True, live=True, timeframe="1h",
                    last_activity_age_s=10, thread_alive=False,
                    data_source="live (ccxt)")
    assert dead[0]["key"] == "engine-down" and dead[0]["severity"] == "critical"
    fake = evaluate(running=True, live=True, timeframe="1h",
                    last_activity_age_s=10, thread_alive=True,
                    data_source="bundled sample")
    assert any(x["key"] == "feed-not-live" for x in fake)


def test_watchdog_cooldown_and_notify():
    class _Eng:
        running, live, timeframe, last_source = True, True, "1h", "bundled sample"
        last_activity = datetime.now(timezone.utc).isoformat()

        class _T:
            @staticmethod
            def is_alive():
                return True
        _thread = _T()

    led = SqliteLedger(":memory:")
    sent = []
    wd = Watchdog(_Eng(), led, lambda kind, t, d: sent.append(t), cooldown_s=3600)
    first = wd.check(now=time.time())
    assert any(f["key"] == "feed-not-live" for f in first)
    assert len(sent) == 1                    # alerted once
    wd.check(now=time.time() + 60)
    assert len(sent) == 1                    # within cooldown -> no spam
    wd.check(now=time.time() + 7200)
    assert len(sent) == 2                    # cooldown elapsed -> re-alert
    assert wd.status()["last_heartbeat"] is not None
    assert any(a["category"] == "watchdog" for a in led.get_alerts())


# ─────────────────────────── funding interpretation ───────────────────────────
def test_funding_interpretation_levels():
    assert interpret(0.0008)["level"] == "extreme-long"
    assert interpret(0.0003)["level"] == "elevated-long"
    assert interpret(0.0001)["level"] == "neutral"
    assert interpret(-0.0003)["level"] == "elevated-short"
    assert interpret(-0.001)["level"] == "extreme-short"
    assert interpret(None)["level"] == "unknown"
    # annualization sanity: 0.01%/8h -> ~10.95%/yr
    assert abs(interpret(0.0001)["annualized_pct"] - 10.95) < 0.1


def test_funding_endpoint_honest_without_ccxt():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    app = FastAPI(); app.include_router(webhook_api.router)
    client = TestClient(app)
    body = client.get("/market/funding", params={"symbols": "BTCUSDT"}).json()
    assert "available" in body               # honest either way; never fabricated
    if not body["available"]:
        assert body.get("reason")
    wd = client.get("/ops/watchdog").json()
    assert "findings" in wd and "running" in wd
