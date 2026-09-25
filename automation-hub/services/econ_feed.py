"""Economic-calendar feed for the event guard (services/econ_guard.py).

The guard's protection policy (halt new entries inside the blackout, halve
size and widen stops in the caution window) only works if it knows when the
releases are. Until now it relied on someone typing dates in by hand. This
feed fills the calendar automatically from a public weekly export of
scheduled macro releases, keeps only the ones the calendar itself rates
high-impact for the configured currencies, and records when it last
succeeded, so the dashboard can say "fetched 12:03, 7 events" or say why not.

It never invents a date: a failed fetch keeps the last good events (while
they are still upcoming) and reports the error; with no successful fetch and
no manual events, the guard says the calendar is not connected.

Configuration (environment):
    HUB_ECON_FEED            "forexfactory" (default) or "off"
    HUB_ECON_FEED_URL        override the export URL
    HUB_ECON_FEED_COUNTRIES  comma list, default "USD" (crypto reacts most to US data)
    HUB_ECON_FEED_INTERVAL_S refresh interval, default 21600 (6 h)
"""
from __future__ import annotations

import json
import os
import random
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

SOURCE = "forexfactory"
DEFAULT_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
DEFAULT_COUNTRIES = ("USD",)
DEFAULT_INTERVAL_S = 6 * 3600
MAX_BYTES = 2_000_000


def _parse_time(value) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def parse_forexfactory(payload, *, countries=DEFAULT_COUNTRIES) -> list[dict]:
    """High-impact releases for the given currencies from the weekly export.

    Each item looks like {"title": "CPI m/m", "country": "USD",
    "date": "2026-09-24T08:30:00-04:00", "impact": "High", ...}. Anything that
    is not rated High, is for another currency, or has no usable time is
    dropped. Times are normalised to UTC."""
    wanted = {c.strip().upper() for c in (countries or ()) if c and c.strip()}
    out: dict[tuple[str, str], dict] = {}
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or str(item.get("impact", "")).strip().lower() != "high":
            continue
        country = str(item.get("country", "")).strip().upper()
        if wanted and country not in wanted:
            continue
        title = str(item.get("title", "")).strip()
        when = _parse_time(item.get("date"))
        if not title or when is None:
            continue
        event = {"name": f"{country} {title}".strip(), "time": when.isoformat(), "impact": "high",
                 "source": SOURCE, "country": country}
        out[(event["name"], event["time"])] = event
    return sorted(out.values(), key=lambda e: e["time"])


def _http_fetch(url: str, timeout: float = 15.0):
    request = urllib.request.Request(url, headers={"User-Agent": "TradeLogX-Nexus/1.0 (+economic-calendar)",
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- fixed https URL
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("calendar export is larger than expected")
    return json.loads(body.decode("utf-8"))


def feed_enabled() -> bool:
    return os.environ.get("HUB_ECON_FEED", SOURCE).strip().lower() not in ("", "off", "0", "false", "none")


class EconFeed:
    """Keeps the EconCalendar's provider events current."""

    def __init__(self, calendar, *, url: Optional[str] = None, countries=None,
                 interval_s: Optional[float] = None, fetch: Callable[[str], object] = _http_fetch,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.calendar = calendar
        self.url = url or os.environ.get("HUB_ECON_FEED_URL") or DEFAULT_URL
        env_countries = os.environ.get("HUB_ECON_FEED_COUNTRIES")
        self.countries = tuple(countries) if countries else (
            tuple(c for c in env_countries.split(",") if c.strip()) if env_countries else DEFAULT_COUNTRIES)
        self.interval_s = float(interval_s or os.environ.get("HUB_ECON_FEED_INTERVAL_S") or DEFAULT_INTERVAL_S)
        self.fetch = fetch
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_attempt: Optional[str] = None
        self.last_error: Optional[str] = None

    def sync(self) -> dict:
        """Fetch once. On success the provider events are replaced; on failure
        the previous ones are kept and the error is recorded."""
        with self._lock:
            now = self.clock()
            self.last_attempt = now.isoformat(timespec="seconds")
            try:
                events = parse_forexfactory(self.fetch(self.url), countries=self.countries)
            except Exception as exc:  # noqa: BLE001 -- a dead feed must never break the engine
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                return self.status()
            self.calendar.set_provider_events(events, fetched_at=now, source=SOURCE)
            self.last_error = None
            return self.status()

    def status(self) -> dict:
        provider = self.calendar.provider_status()
        return {"enabled": feed_enabled(), "source": SOURCE, "url": self.url, "countries": list(self.countries),
                "interval_s": self.interval_s, "last_attempt": self.last_attempt, "last_error": self.last_error,
                "last_success": provider.get("fetched_at"), "events": provider.get("count", 0)}

    def start(self) -> bool:
        if not feed_enabled() or (self._thread and self._thread.is_alive()):
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="econ-feed", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sync()
            # A failed fetch retries sooner; a little jitter keeps restarts
            # from hitting the export at the same second.
            wait = self.interval_s if self.last_error is None else min(self.interval_s, 900.0)
            self._stop.wait(wait + random.uniform(0, 60))
