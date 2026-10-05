"""Trading-session and London-time classification for the trade journal.

Sessions are defined in each centre's own local time, so daylight-saving
mismatches (the weeks when London and New York change clocks on different
dates) are handled correctly rather than approximated with fixed UTC hours.

    ASIA               Tokyo     09:00-18:00 JST  (UTC+9, no DST)
    LONDON             London    08:00-17:00 local
    NEW_YORK           New York  08:00-17:00 local
    LONDON_NY_OVERLAP  London and New York both open
    OFF_HOURS          none of the above are open

Precedence when two sessions are open: overlap > London > New York > Asia.

The DST rules are computed directly (UK: last Sunday of March/October at
01:00 UTC; US: second Sunday of March / first Sunday of November at 02:00
local) so classification does not depend on the container shipping tzdata.
The rules are versioned in ``SESSION_MODEL`` and stored on every trade, so a
later change to the definitions never silently re-labels history.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

SESSION_MODEL = "sessions-v1:asia=tokyo09-18,london=08-17,ny=08-17,overlap=london&ny"
SESSIONS = ("ASIA", "LONDON", "NEW_YORK", "LONDON_NY_OVERLAP", "OFF_HOURS")
SESSION_LABELS = {
    "ASIA": "Asia", "LONDON": "London", "NEW_YORK": "New York",
    "LONDON_NY_OVERLAP": "London/New York overlap", "OFF_HOURS": "Outside main sessions",
}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def parse_ts(value) -> Optional[datetime]:
    """Parse an ISO timestamp (or datetime) to an aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _last_sunday(year: int, month: int) -> date:
    nxt = date(year + (month // 12), month % 12 + 1, 1)
    day = nxt - timedelta(days=1)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def london_offset(utc: datetime) -> timedelta:
    start = datetime.combine(_last_sunday(utc.year, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(utc.year, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    return timedelta(hours=1) if start <= utc < end else timedelta(0)


def new_york_offset(utc: datetime) -> timedelta:
    # 02:00 EST (UTC-5) = 07:00 UTC; 02:00 EDT (UTC-4) = 06:00 UTC.
    start = datetime.combine(_nth_sunday(utc.year, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=7)
    end = datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=6)
    return timedelta(hours=-4) if start <= utc < end else timedelta(hours=-5)


def to_london(value) -> Optional[datetime]:
    utc = parse_ts(value)
    if utc is None:
        return None
    offset = london_offset(utc)
    return (utc + offset).replace(tzinfo=timezone(offset, "BST" if offset else "GMT"))


def _minutes(stamp: datetime) -> int:
    return stamp.hour * 60 + stamp.minute


def classify_session(value) -> Optional[str]:
    utc = parse_ts(value)
    if utc is None:
        return None
    london = utc + london_offset(utc)
    new_york = utc + new_york_offset(utc)
    tokyo = utc + timedelta(hours=9)
    london_open = 8 * 60 <= _minutes(london) < 17 * 60
    ny_open = 8 * 60 <= _minutes(new_york) < 17 * 60
    asia_open = 9 * 60 <= _minutes(tokyo) < 18 * 60
    if london_open and ny_open:
        return "LONDON_NY_OVERLAP"
    if london_open:
        return "LONDON"
    if ny_open:
        return "NEW_YORK"
    if asia_open:
        return "ASIA"
    return "OFF_HOURS"


def in_preferred_window(value, start_hour_utc: Optional[int], end_hour_utc: Optional[int]) -> Optional[bool]:
    """Whether an entry fell inside the configured trading window (UTC hours).

    Returns None when no restriction is configured (a full-day window), so the
    journal never claims a preference that the operator did not set."""
    utc = parse_ts(value)
    if utc is None or start_hour_utc is None or end_hour_utc is None:
        return None
    start, end = int(start_hour_utc), int(end_hour_utc)
    if (end - start) % 24 == 0:
        return None
    start, end, hour = start % 24, end % 24, utc.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def timing_fields(entry_at, exit_at=None, *, preferred_window: Optional[tuple] = None) -> dict:
    """Every derived timing column for a trade."""
    entry_utc = parse_ts(entry_at)
    exit_utc = parse_ts(exit_at)
    out: dict = {"session_model": SESSION_MODEL}
    if entry_utc is not None:
        london = to_london(entry_utc)
        out.update({
            "entry_session": classify_session(entry_utc),
            "entry_weekday": WEEKDAYS[london.weekday()],
            "entry_hour_london": london.hour,
            "entry_at_london": london.isoformat(),
        })
        if preferred_window:
            flag = in_preferred_window(entry_utc, *preferred_window)
            out["in_preferred_session"] = None if flag is None else int(flag)
    if exit_utc is not None:
        out["exit_at_london"] = to_london(exit_utc).isoformat()
        if entry_utc is not None:
            out["duration_s"] = max(0.0, (exit_utc - entry_utc).total_seconds())
    return out


def london_display(value) -> Optional[str]:
    """``05 Oct 2026 09:42 London``"""
    london = to_london(value)
    return london.strftime("%d %b %Y %H:%M London") if london else None


def duration_display(seconds) -> Optional[str]:
    if seconds is None:
        return None
    seconds = int(round(float(seconds)))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    if not days and not hours and minutes < 5:
        parts.append(f"{secs}s")
    return " ".join(parts)


def iso_week_key(value) -> Optional[str]:
    london = to_london(value)
    if london is None:
        return None
    year, week, _ = london.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(week_key: str) -> tuple[str, str]:
    """UTC ISO bounds [start, end) of an ISO week key such as ``2026-W40``."""
    year_text, week_text = week_key.upper().split("-W")
    monday = date.fromisocalendar(int(year_text), int(week_text), 1)
    start = datetime.combine(monday, datetime.min.time(), timezone.utc)
    return start.isoformat(), (start + timedelta(days=7)).isoformat()


def session_order(names: Iterable[str]) -> list[str]:
    order = {name: i for i, name in enumerate(SESSIONS)}
    return sorted(names, key=lambda n: order.get(n, 99))
