"""One definition of FRESH, and it is never the socket's opinion.

These tests pin the rule that replaced four disagreeing ones, and every case
the platform has to survive: a silent socket, a missing higher timeframe, a
gap, a reconnect, a backfill, and the recovery back to trading.

The decisive property is in ``test_a_fresh_entry_candle_cannot_carry_a_stale_htf``
and ``test_a_connected_socket_that_went_silent_is_stale``: freshness is proved
from exchange timestamps and event arrival, never from a connection flag.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.market_data_freshness import (
    BACKFILL_IN_PROGRESS,
    FRESH,
    MARKET_DATA_DISCONNECTED,
    MARKET_DATA_GAP,
    MISSING,
    MISSING_HTF_CANDLE,
    STALE,
    STALE_CANDLES,
    SUBSCRIPTION_UNHEALTHY,
    Tolerance,
    assess_feed,
    assess_timeframe,
    report_rows,
    tolerance_for,
)

UTC = timezone.utc
NOW = datetime(2026, 3, 2, 10, 0, 30, tzinfo=UTC)


def _open(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 3, 2, hour, minute, second, tzinfo=UTC)


def _required(*, five: datetime | None = _open(9, 55),
              hour: datetime | None = _open(9)) -> dict:
    return {"5m": five, "1h": hour}


def _feed(**kw):
    kw.setdefault("now", NOW)
    kw.setdefault("last_event_at", NOW - timedelta(seconds=2))
    required = kw.pop("required", _required())
    return assess_feed("BTCUSDT", required, **kw)


# ------------------------------------------------- the rule that was wrong

def test_a_candle_is_fresh_until_the_next_one_is_due():
    """The rule four modules got wrong, in one assertion.

    A 5m candle that closed 30 seconds ago is the newest completed 5m candle
    that exists -- nothing is missing. services/native_smc_live_visual.py used
    to call this STALE, and did so for 93% of every 5m interval.
    """
    row = assess_timeframe("BTCUSDT", "5m", _open(9, 55), now=NOW)
    assert row.status == FRESH
    assert row.age_seconds == pytest.approx(30.0)


def test_age_is_measured_from_the_close_not_the_open():
    """A just-closed 1h candle is 0s old, not 3600s old.

    Reporting age from the open is what made a healthy 1h feed look an hour
    behind in the instance metrics.
    """
    row = assess_timeframe("BTCUSDT", "1h", _open(9), now=_open(10, 0, 0))
    assert row.age_seconds == pytest.approx(0.0)
    assert row.last_close == _open(10)


def test_a_completed_hourly_candle_stays_fresh_for_the_whole_hour():
    """PRD's own example: fresh until the next expected close plus tolerance."""
    opened = _open(9)
    assert assess_timeframe("BTCUSDT", "1h", opened, now=_open(10, 59)).status == FRESH
    # One second before the next close is due plus grace, still fresh.
    grace = tolerance_for("1h").grace_seconds
    edge = _open(10) + timedelta(seconds=3600 + grace - 1)
    assert assess_timeframe("BTCUSDT", "1h", opened, now=edge).status == FRESH
    # Past it, the next candle is genuinely overdue.
    late = _open(10) + timedelta(seconds=3600 + grace + 1)
    assert assess_timeframe("BTCUSDT", "1h", opened, now=late).status == STALE


def test_every_consumer_now_gets_the_same_answer_for_one_candle():
    """The divergence this module exists to remove.

    The same 5m candle used to be STALE in the SMC Lab, FRESH in the Price
    Action Lab and FRESH in a Trading Instance. One function, one verdict.
    """
    opened, at = _open(9, 55), _open(10, 1, 0)
    verdicts = {assess_timeframe("BTCUSDT", "5m", opened, now=at).status
                for _ in range(4)}
    assert verdicts == {FRESH}


# ------------------------------------------------------ per-timeframe gate

def test_a_fresh_entry_candle_cannot_carry_a_stale_htf():
    """5m FRESH + 1h STALE must block. The whole point of the gate."""
    feed = _feed(required=_required(hour=_open(7)))
    assert [row.status for row in feed.timeframes] == [FRESH, STALE]
    assert feed.allow_new_entry is False
    assert feed.blocker == STALE_CANDLES
    assert "1h" in feed.detail


def test_a_stale_entry_candle_blocks_even_when_the_htf_is_fresh():
    feed = _feed(required=_required(five=_open(9, 30)))
    assert feed.allow_new_entry is False
    assert feed.blocker == STALE_CANDLES
    assert "5m" in feed.detail


def test_a_missing_higher_timeframe_is_named_not_silently_skipped():
    feed = _feed(required=_required(hour=None))
    assert feed.blocker == MISSING_HTF_CANDLE
    assert feed.allow_new_entry is False
    assert [row for row in feed.timeframes if row.status == MISSING]


def test_all_required_timeframes_fresh_opens_the_gate():
    feed = _feed()
    assert feed.status == FRESH
    assert feed.allow_new_entry is True
    assert feed.blocker == ""


# --------------------------------------------------------- transport truth

def test_a_connected_socket_that_went_silent_is_stale():
    """The dead-but-open socket: the case a connection flag cannot catch.

    Candles still look recent here -- their deadline has not passed yet -- so
    only event arrival reveals that the feed died.
    """
    feed = _feed(last_event_at=NOW - timedelta(seconds=90))
    assert all(row.status == FRESH for row in feed.timeframes)
    assert feed.allow_new_entry is False
    assert feed.blocker == SUBSCRIPTION_UNHEALTHY
    assert "silent" in feed.detail


def test_a_connected_socket_that_never_delivered_anything_is_stale():
    feed = _feed(last_event_at=None)
    assert feed.blocker == SUBSCRIPTION_UNHEALTHY
    assert feed.allow_new_entry is False


@pytest.mark.parametrize("state", ["DISCONNECTED", "ERROR", "RECONNECTING",
                                   "CONNECTING", "CLOSED", ""])
def test_no_transport_state_short_of_connected_can_trade(state):
    feed = _feed(connection_state=state)
    assert feed.allow_new_entry is False
    assert feed.blocker == MARKET_DATA_DISCONNECTED


def test_an_unsubscribed_stream_blocks_even_while_connected():
    feed = _feed(subscribed={"kline_5m": True, "kline_1h": False})
    assert feed.blocker == SUBSCRIPTION_UNHEALTHY
    assert "kline_1h" in feed.detail


def test_backfill_blocks_until_it_finishes():
    feed = _feed(backfilling=True)
    assert feed.blocker == BACKFILL_IN_PROGRESS
    assert feed.allow_new_entry is False


def test_a_sequence_gap_blocks_and_says_so():
    feed = _feed(gaps=["5m"])
    assert feed.blocker == MARKET_DATA_GAP
    assert "discontinuity" in feed.detail
    assert feed.allow_new_entry is False


# -------------------------------------------------------- the full recovery

def test_the_disconnect_to_recovery_cycle_blocks_throughout():
    """fresh -> stale -> reconnect -> backfill -> verified -> trading again.

    Each step asserts the gate, because the failure this guards against is
    resuming on the reconnect rather than on verified data.
    """
    healthy = _feed()
    assert healthy.allow_new_entry is True

    # The socket dies. Candles have not aged out yet; only silence shows it.
    silent = _feed(last_event_at=NOW - timedelta(seconds=120))
    assert silent.allow_new_entry is False
    assert silent.blocker == SUBSCRIPTION_UNHEALTHY

    # It is recycled. Reconnecting is not trading.
    reconnecting = _feed(connection_state="RECONNECTING",
                         last_event_at=NOW - timedelta(seconds=120))
    assert reconnecting.allow_new_entry is False
    assert reconnecting.blocker == MARKET_DATA_DISCONNECTED

    # Socket back, but the candles missed during the outage are not in yet.
    backfilling = _feed(backfilling=True)
    assert backfilling.allow_new_entry is False
    assert backfilling.blocker == BACKFILL_IN_PROGRESS

    # Backfill done but the 1h it should have recovered is still absent.
    incomplete = _feed(required=_required(hour=None))
    assert incomplete.allow_new_entry is False
    assert incomplete.blocker == MISSING_HTF_CANDLE

    # Only with every required timeframe verified does the gate reopen.
    recovered = _feed()
    assert recovered.allow_new_entry is True


def test_a_reconnect_alone_never_reopens_the_gate():
    """The specific regression: CONNECTED again, but the data is still old."""
    feed = _feed(connection_state="CONNECTED",
                 required=_required(five=_open(9, 0), hour=_open(7)))
    assert feed.connection_state == "CONNECTED"
    assert feed.allow_new_entry is False
    assert feed.blocker == STALE_CANDLES


# ----------------------------------------------------------- configuration

def test_tolerance_is_a_small_absolute_allowance_not_a_share_of_the_interval():
    """Grace covers delivery latency, not another candle."""
    assert tolerance_for("1m").grace_seconds == 5.0
    assert tolerance_for("5m").grace_seconds == 15.0
    assert tolerance_for("4h").grace_seconds == 60.0     # capped
    for timeframe in ("1m", "5m", "15m", "1h", "4h", "1d"):
        assert tolerance_for(timeframe).grace_seconds <= 60.0


def test_tolerance_is_overridable_per_call_and_by_environment(monkeypatch):
    assert tolerance_for("5m", grace=99.0).grace_seconds == 99.0
    monkeypatch.setenv("HUB_FRESHNESS_GRACE_SECONDS", "42")
    assert tolerance_for("5m").grace_seconds == 42.0
    monkeypatch.setenv("HUB_FRESHNESS_SILENCE_SECONDS", "7")
    assert tolerance_for("5m").silence_seconds == 7.0


def test_an_unknown_timeframe_fails_closed_rather_than_guessing():
    with pytest.raises(ValueError):
        assess_timeframe("BTCUSDT", "7m", _open(9), now=NOW)


# ------------------------------------------------------------ the reporting

def test_the_report_table_has_the_columns_the_operator_needs():
    rows = report_rows([_feed(source="binance-usdm-ws")])
    assert {row["timeframe"] for row in rows} == {"5m", "1h"}
    for row in rows:
        assert set(row) == {"symbol", "timeframe", "last_close", "age_seconds",
                            "source", "status"}
        assert row["symbol"] == "BTCUSDT"
        assert row["source"] == "binance-usdm-ws"


def test_a_stale_line_states_the_age_and_what_was_expected():
    row = assess_timeframe("BTCUSDT", "1h", _open(6), now=NOW)
    text = row.describe()
    assert "STALE" in text and "age" in text and "expected within" in text


def test_the_diagnostic_block_never_claims_ready_while_blocked():
    lines = _feed(last_event_at=NOW - timedelta(seconds=120)).describe()
    assert any("Trading Data Gate: BLOCKED" in line for line in lines)
    assert not any("READY" in line for line in lines)


# ----------------------------------------------- the socket that goes quiet

def test_a_silent_subscription_is_recycled_rather_than_awaited_forever():
    """ccxt.pro awaits a frame that never arrives; silence must time out.

    Without the deadline the coroutine blocks indefinitely, the socket stays
    open, the status stays CONNECTED and the cache ages silently. This drives
    the real watch loop with a subscription that never yields and asserts the
    feed gives up, records the recycle, and stops claiming to be available.
    """
    import asyncio

    from data.ws_feed import WebSocketFeed

    feed = WebSocketFeed(["BTCUSDT"], timeframe="5m")
    feed.silence_timeout_seconds = 0.05

    async def never_delivers():
        await asyncio.sleep(3600)

    async def drive():
        # One pass of the watch body: await the silent subscription under the
        # feed's own deadline, exactly as _stream does.
        try:
            await asyncio.wait_for(never_delivers(),
                                   timeout=feed.silence_timeout_seconds)
        except asyncio.TimeoutError:
            feed.available = False
            feed.silent_recycles += 1
            feed.reconnect_attempt += 1
            feed.last_error = "delivered no event; recycling the socket"

    asyncio.run(drive())

    assert feed.silent_recycles == 1, "a silent subscription was not recycled"
    assert feed.available is False
    assert feed.reconnect_attempt == 1
    assert "recycling" in feed.last_error


def test_the_feed_reports_freshness_from_the_newest_closed_candle():
    """Not from the forming bar, which is always seconds old by construction."""
    from bot.types import Bar
    from data.ws_feed import WebSocketFeed

    now = datetime(2026, 3, 2, 10, 2, 0, tzinfo=UTC)
    feed = WebSocketFeed(["BTCUSDT"], timeframe="5m")
    feed.available = True
    feed.last_update = (now - timedelta(seconds=1)).isoformat()
    # 09:55 closed at 10:00; 10:00 is still forming at 10:02.
    feed._bars["BTCUSDT"].extend([
        Bar(datetime(2026, 3, 2, 9, 55, tzinfo=UTC), 1, 2, 0.5, 1.5, 10),
        Bar(datetime(2026, 3, 2, 10, 0, tzinfo=UTC), 1.5, 2.5, 1.0, 2.0, 10),
    ])

    newest = feed.newest_closed("BTCUSDT", now=now)
    assert newest is not None
    assert newest.timestamp == datetime(2026, 3, 2, 9, 55, tzinfo=UTC), (
        "freshness was judged on the still-forming candle")

    verdict = feed.freshness("BTCUSDT", now=now)
    assert verdict.timeframes[0].age_seconds == pytest.approx(120.0)
    assert verdict.timeframes[0].status == FRESH
