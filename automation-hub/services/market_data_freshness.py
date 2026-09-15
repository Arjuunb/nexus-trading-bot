"""One definition of fresh market data, for every consumer on the platform.

Before this module there were four, and they disagreed about the same candle.
How much lateness each allowed a 5m candle beyond its own interval:

    services/native_smc_live_visual.py    20s   (tf + 20, measured from OPEN)
    services/price_action_stream.py       75s   (tf + max(15, tf*.25), from close)
    services/auto_engine.py              150s   (tf * 1.5, from close)
    data/ws_feed.py                      300s   (2 * tf, from OPEN, forming bar)

Two distinct defects, not one:

  * ``native_smc_live_visual`` compared the candle's OPEN timestamp against
    ``tf + 20``, so a 5m candle was declared STALE twenty seconds after it
    CLOSED and stayed STALE for the remaining 280 seconds -- STALE_CANDLES on
    a perfectly current feed 93% of the time, and 99.4% of the time on 1h.
    That false red is the one users actually saw.

  * ``price_action_stream`` and ``auto_engine`` measured correctly from the
    close but allowed wildly different lateness, so the same candle could be
    tradeable in an instance and stale in a lab.

``ws_feed.fresh()`` measured from the open against two intervals and read
``bars[-1]``, which may be the still-forming candle -- so it answered "fresh"
almost unconditionally and was never a real gate.

The rule here is the one a trader would state:

    A closed candle is fresh until the NEXT candle of that timeframe is due,
    plus a small tolerance for delivery latency and clock skew.

        fresh  while  now <= close + interval + grace

A 1h candle that closed at 01:00 is therefore fresh for the whole hour until
02:00 (+grace), which is correct: it is the most recent completed 1h candle
that exists. Nothing newer is missing.

Two things this module refuses to do:

  * treat a connected socket as evidence of fresh data. A socket that is open
    and silent is stale, and says so via SUBSCRIPTION_UNHEALTHY.
  * report an aggregate FRESH when any single required timeframe is not. Every
    timeframe is judged on its own and the worst one decides the gate, so a
    fresh 5m can never carry a stale 1h into a trade.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Optional, Sequence

# Candle durations come from bot.data.resample -- the repository's designated
# single definition. services/mtf_policy.py carries a deliberately narrower
# table (only the six timeframes the native MTF policy covers), and the visual
# labs legitimately run on 3m, 30m and 1w as well, so keying off the policy
# table would raise on timeframes the platform genuinely supports.
from bot.data.resample import TF_SECONDS as TIMEFRAME_SECONDS

__all__ = [
    "FRESH", "STALE", "MISSING",
    "STALE_CANDLES", "MISSING_HTF_CANDLE", "MARKET_DATA_DISCONNECTED",
    "MARKET_DATA_GAP", "BACKFILL_IN_PROGRESS", "SUBSCRIPTION_UNHEALTHY",
    "Tolerance", "TimeframeFreshness", "FeedFreshness",
    "tolerance_for", "grace_for_interval", "assess_timeframe", "assess_feed",
    "expected_next_close",
]

FRESH = "FRESH"
STALE = "STALE"
MISSING = "MISSING"

#: Operator-facing blockers. These are the strings the API, the labs and the
#: dashboard all report, so a blocker means the same thing everywhere.
STALE_CANDLES = "STALE_CANDLES"
MISSING_HTF_CANDLE = "MISSING_HTF_CANDLE"
MARKET_DATA_DISCONNECTED = "MARKET_DATA_DISCONNECTED"
MARKET_DATA_GAP = "MARKET_DATA_GAP"
BACKFILL_IN_PROGRESS = "BACKFILL_IN_PROGRESS"
SUBSCRIPTION_UNHEALTHY = "SUBSCRIPTION_UNHEALTHY"

#: Connection states that mean "there is no live feed", whatever the candles say.
_DEAD_STATES = frozenset({"DISCONNECTED", "ERROR", "RECONNECTING", "CONNECTING",
                          "CLOSED", "STOPPED", "UNKNOWN", ""})

_ENV_GRACE = "HUB_FRESHNESS_GRACE_SECONDS"
_ENV_SILENCE = "HUB_FRESHNESS_SILENCE_SECONDS"

#: How long a feed may deliver nothing at all before it is presumed dead. The
#: Binance USDⓈ-M kline stream pushes an update every second or two for the
#: forming candle -- it does not go quiet between closes -- so silence on any
#: timeframe is a transport fault, not a slow market.
DEFAULT_SILENCE_SECONDS = 30.0


def _env_float(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def interval_seconds(timeframe: str) -> int:
    try:
        return int(TIMEFRAME_SECONDS[timeframe])
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe '{timeframe}'") from exc


@dataclass(frozen=True)
class Tolerance:
    """How much lateness is allowed before data counts as stale.

    ``grace_seconds`` covers delivery latency and clock skew only. The candle
    interval itself is NOT part of it -- that is already in the freshness rule
    -- which is why the grace is small and absolute rather than a fraction of
    a 4h candle.
    """
    grace_seconds: float
    silence_seconds: float = DEFAULT_SILENCE_SECONDS


def grace_for_interval(seconds: float) -> float:
    """The delivery grace for a candle interval, in seconds.

    Takes the interval rather than a timeframe name so that callers which
    already hold one -- and which must not raise, such as a status read on a
    stream that has not been configured yet -- can share this single formula
    instead of keeping a second one.
    """
    override = _env_float(_ENV_GRACE, None)
    if override is not None:
        return float(override)
    # 5% of the interval, floored at 5s and capped at 60s: enough for a slow
    # delivery on any timeframe, never enough to hide a missed candle.
    return max(5.0, min(float(seconds) * 0.05, 60.0))


def tolerance_for(timeframe: str, *, grace: Optional[float] = None,
                  silence: Optional[float] = None) -> Tolerance:
    """The tolerance for one timeframe, overridable per call or by environment.

    Raises on an unknown timeframe: a trading decision must never proceed on a
    guessed interval. Callers on a display path that may legitimately hold no
    timeframe yet should use ``grace_for_interval`` with their own default.
    """
    seconds = interval_seconds(timeframe)
    if grace is None:
        grace = grace_for_interval(seconds)
    if silence is None:
        silence = _env_float(_ENV_SILENCE, DEFAULT_SILENCE_SECONDS)
    return Tolerance(float(grace), float(silence))


def expected_next_close(last_candle_open: datetime, timeframe: str) -> datetime:
    """When the next candle of this timeframe is due to close.

    ``last_candle_open`` is the provider's timestamp for the most recent CLOSED
    candle -- providers stamp a candle at its open, which is the convention the
    whole codebase stores.
    """
    seconds = interval_seconds(timeframe)
    return _utc(last_candle_open) + timedelta(seconds=seconds * 2)


def assess_timeframe(symbol: str, timeframe: str,
                     last_candle_open: Optional[datetime], *,
                     now: Optional[datetime] = None,
                     tolerance: Optional[Tolerance] = None,
                     gap: bool = False) -> "TimeframeFreshness":
    """Judge one timeframe on its own.

    ``last_candle_open`` is the open timestamp of the newest CLOSED candle, or
    None when none is held. Ages are reported from the candle's CLOSE, because
    that is the instant the data became complete and the only age a trader can
    reason about.
    """
    now = _utc(now or datetime.now(timezone.utc))
    tolerance = tolerance or tolerance_for(timeframe)
    seconds = interval_seconds(timeframe)

    if last_candle_open is None:
        return TimeframeFreshness(
            symbol=symbol, timeframe=timeframe, last_close=None,
            age_seconds=None, interval_seconds=seconds,
            allowed_age_seconds=float(seconds) + tolerance.grace_seconds,
            status=MISSING, blocker=MISSING_HTF_CANDLE)

    opened = _utc(last_candle_open)
    close = opened + timedelta(seconds=seconds)
    age = (now - close).total_seconds()
    allowed = float(seconds) + tolerance.grace_seconds

    if gap:
        status, blocker = STALE, MARKET_DATA_GAP
    elif age > allowed:
        status, blocker = STALE, STALE_CANDLES
    else:
        status, blocker = FRESH, ""
    return TimeframeFreshness(
        symbol=symbol, timeframe=timeframe, last_close=close,
        age_seconds=age, interval_seconds=seconds, allowed_age_seconds=allowed,
        status=status, blocker=blocker)


@dataclass(frozen=True)
class TimeframeFreshness:
    """One timeframe's verdict, with every number the judgement used."""
    symbol: str
    timeframe: str
    last_close: Optional[datetime]
    age_seconds: Optional[float]
    interval_seconds: int
    allowed_age_seconds: float
    status: str
    blocker: str = ""

    @property
    def fresh(self) -> bool:
        return self.status == FRESH

    def describe(self) -> str:
        """The one-line form the dashboard and diagnostics print."""
        if self.last_close is None:
            return f"{self.timeframe} Candle: MISSING · no closed candle held"
        age = f"{self.age_seconds:.0f}s"
        if self.fresh:
            return (f"{self.timeframe} Candle: FRESH · age {age} · "
                    f"last close {self.last_close:%H:%M:%S}")
        return (f"{self.timeframe} Candle: STALE · age {age} · "
                f"expected within {self.allowed_age_seconds:.0f}s · "
                f"last close {self.last_close:%H:%M:%S}")

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "timeframe": self.timeframe,
            "last_close": self.last_close.isoformat() if self.last_close else None,
            "age_seconds": (round(self.age_seconds, 3)
                            if self.age_seconds is not None else None),
            "interval_seconds": self.interval_seconds,
            "allowed_age_seconds": round(self.allowed_age_seconds, 3),
            "status": self.status, "blocker": self.blocker,
        }


@dataclass(frozen=True)
class FeedFreshness:
    """The whole feed's verdict: every required timeframe plus the transport."""
    symbol: str
    timeframes: tuple[TimeframeFreshness, ...]
    connection_state: str
    last_event_age_seconds: Optional[float]
    status: str
    blocker: str
    source: str = ""
    reconnects: int = 0
    detail: str = ""

    @property
    def allow_new_entry(self) -> bool:
        """The gate. Fail-closed: anything unproven blocks a new entry."""
        return self.status == FRESH and not self.blocker

    def stale_timeframes(self) -> tuple[TimeframeFreshness, ...]:
        return tuple(row for row in self.timeframes if not row.fresh)

    def describe(self) -> list[str]:
        """The operator diagnostic block."""
        lines = [f"Market Feed: {self.connection_state}"]
        lines += [row.describe() for row in self.timeframes]
        if self.last_event_age_seconds is not None:
            lines.append(f"Last Event: {self.last_event_age_seconds:.0f}s ago")
        else:
            lines.append("Last Event: never")
        lines.append(f"Reconnects: {self.reconnects}")
        lines.append(
            "Trading Data Gate: READY" if self.allow_new_entry
            else f"Trading Data Gate: BLOCKED · {self.blocker}"
            + (f" · {self.detail}" if self.detail else ""))
        return lines

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "blocker": self.blocker,
            "detail": self.detail,
            "allow_new_entry": self.allow_new_entry,
            "connection_state": self.connection_state,
            "last_event_age_seconds": (round(self.last_event_age_seconds, 3)
                                       if self.last_event_age_seconds is not None else None),
            "source": self.source,
            "reconnects": self.reconnects,
            "timeframes": [row.to_dict() for row in self.timeframes],
        }


def _reason(row: "TimeframeFreshness", code: str) -> str:
    """Say why THIS timeframe failed, in the terms of the blocker that caught it."""
    if code == MISSING_HTF_CANDLE:
        return f"{row.timeframe} absent"
    if code == MARKET_DATA_GAP:
        # A gap is a hole in the sequence; the newest candle's age is not the
        # complaint and printing it reads as a contradiction.
        return f"{row.timeframe} has a discontinuity in its candle sequence"
    return (f"{row.timeframe} age {row.age_seconds:.0f}s "
            f"> {row.allowed_age_seconds:.0f}s")


def assess_feed(symbol: str,
                required: Mapping[str, Optional[datetime]], *,
                now: Optional[datetime] = None,
                connection_state: str = "CONNECTED",
                last_event_at: Optional[datetime] = None,
                backfilling: bool = False,
                gaps: Iterable[str] = (),
                subscribed: Optional[Mapping[str, bool]] = None,
                tolerance: Optional[Tolerance] = None,
                source: str = "",
                reconnects: int = 0) -> FeedFreshness:
    """Judge a symbol's feed across every timeframe a strategy requires.

    ``required`` maps timeframe -> open timestamp of its newest CLOSED candle
    (None when none is held). Every entry is evaluated; the worst decides.

    The transport is judged before the candles, because a disconnected or
    silent feed makes the candle arithmetic irrelevant: the newest candle may
    look recent purely because the clock has not yet passed its deadline.
    """
    now = _utc(now or datetime.now(timezone.utc))
    gap_set = {str(tf) for tf in gaps}
    rows = tuple(
        assess_timeframe(symbol, timeframe, opened, now=now,
                         tolerance=tolerance or tolerance_for(timeframe),
                         gap=timeframe in gap_set)
        for timeframe, opened in required.items()
    )

    event_age = None
    if last_event_at is not None:
        event_age = max(0.0, (now - _utc(last_event_at)).total_seconds())

    silence_limit = (tolerance.silence_seconds if tolerance
                     else _env_float(_ENV_SILENCE, DEFAULT_SILENCE_SECONDS))

    state = (connection_state or "").upper()
    blocker, detail = "", ""

    # 1. Transport first. A connected socket proves nothing on its own.
    if state in _DEAD_STATES:
        blocker = MARKET_DATA_DISCONNECTED
        detail = f"transport is {state or 'UNKNOWN'}"
    elif backfilling:
        blocker = BACKFILL_IN_PROGRESS
        detail = "waiting for REST backfill to complete"
    elif subscribed is not None and not all(subscribed.values()):
        missing = sorted(name for name, ok in subscribed.items() if not ok)
        blocker = SUBSCRIPTION_UNHEALTHY
        detail = f"not subscribed: {', '.join(missing)}"
    elif last_event_at is None:
        blocker = SUBSCRIPTION_UNHEALTHY
        detail = "connected but no market event has ever arrived"
    elif event_age is not None and silence_limit and event_age > silence_limit:
        # The dead-but-open socket. This is the case a connection-state check
        # can never catch, and the reason "CONNECTED" is not evidence.
        blocker = SUBSCRIPTION_UNHEALTHY
        detail = (f"connected but silent for {event_age:.0f}s "
                  f"(limit {silence_limit:.0f}s)")

    # 2. Then the candles, worst first: a gap is more specific than lateness,
    #    and a missing timeframe more specific still.
    if not blocker:
        for code in (MISSING_HTF_CANDLE, MARKET_DATA_GAP, STALE_CANDLES):
            hit = [row for row in rows if row.blocker == code]
            if hit:
                blocker = code
                detail = ", ".join(_reason(row, code) for row in hit)
                break

    if not rows and not blocker:
        blocker, detail = MISSING_HTF_CANDLE, "no timeframe was evaluated"

    return FeedFreshness(
        symbol=symbol, timeframes=rows, connection_state=state or "UNKNOWN",
        last_event_age_seconds=event_age,
        status=FRESH if not blocker else STALE,
        blocker=blocker, detail=detail, source=source, reconnects=reconnects)


def report_rows(feeds: Sequence[FeedFreshness]) -> list[dict]:
    """The flat `symbol | timeframe | last close | age | source | status` table."""
    out: list[dict] = []
    for feed in feeds:
        for row in feed.timeframes:
            out.append({
                "symbol": feed.symbol,
                "timeframe": row.timeframe,
                "last_close": row.last_close.isoformat() if row.last_close else None,
                "age_seconds": (round(row.age_seconds, 1)
                                if row.age_seconds is not None else None),
                "source": feed.source,
                "status": row.status,
            })
    return out
