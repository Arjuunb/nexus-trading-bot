"""Economic Event Protection (#7).

High-impact macro events (CPI, FOMC, NFP, interest-rate decisions) routinely
spike volatility and gap stops. This guard, given a list of upcoming events,
decides whether to halt new entries, reduce size or widen stops around them.

The protection POLICY is real and testable; exact event times come from a
connected economic-calendar provider or a user-set list — when none is
configured we report it honestly rather than inventing dates.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

HIGH_IMPACT = ("CPI", "FOMC", "NFP", "Interest rate decision", "Rate decision",
               "PCE", "Unemployment",
               # the same releases under the names calendars publish them as
               "Non-Farm", "Nonfarm", "Federal Funds Rate", "Fed Funds Rate")
EVENT_TYPES = [
    {"name": "CPI", "impact": "high", "desc": "US inflation print"},
    {"name": "FOMC", "impact": "high", "desc": "Fed rate decision / statement"},
    {"name": "NFP", "impact": "high", "desc": "US non-farm payrolls"},
    {"name": "Interest rate decision", "impact": "high", "desc": "Central-bank rate decision"},
]


def _parse(ts):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _is_high(name: str) -> bool:
    n = (name or "").upper()
    return any(h.upper() in n for h in HIGH_IMPACT)


def is_high_impact(event: dict) -> bool:
    """A named high-impact release, or one a calendar feed itself rated high.

    Hand-entered events count by name (so a typo'd "impact: high" on a minor
    release does not halt trading); feed events carry the provider's own
    rating, which covers releases the name list does not (GDP, retail sales,
    ISM)."""
    if _is_high(event.get("name", "")):
        return True
    return bool(event.get("source")) and str(event.get("impact", "")).lower() == "high"


def evaluate(events: list, now=None, *, blackout_min: int = 30, caution_min: int = 120) -> dict:
    """Protection decision for the nearest upcoming high-impact event.

    Within ``blackout_min`` -> halt new entries; within ``caution_min`` ->
    reduce size + widen stops; otherwise normal."""
    now = now or datetime.now(timezone.utc)
    upcoming = []
    for e in events or []:
        t = _parse(e.get("time"))
        if t and t >= now and is_high_impact(e):
            upcoming.append((t, e))
    upcoming.sort(key=lambda x: x[0])

    if not upcoming:
        return {"mode": "normal", "risk_multiplier": 1.0, "stop_multiplier": 1.0,
                "halt_new_entries": False, "next_event": None, "minutes_to_event": None,
                "actions": [], "note": "No upcoming high-impact events in range."}

    t, ev = upcoming[0]
    mins = (t - now).total_seconds() / 60.0
    if mins <= blackout_min:
        mode, risk, stop, halt = "blackout", 0.0, 1.0, True
        actions = [f"Halt new entries — {ev['name']} in {mins:.0f} min.",
                   "Let open trades run with their existing stops."]
    elif mins <= caution_min:
        mode, risk, stop, halt = "caution", 0.5, 1.5, False
        actions = [f"Reduce position size to ~50% ahead of {ev['name']}.",
                   "Widen new stops by ~1.5× to survive the volatility spike."]
    else:
        mode, risk, stop, halt = "normal", 1.0, 1.0, False
        actions = []
    return {
        "mode": mode, "risk_multiplier": risk, "stop_multiplier": stop,
        "halt_new_entries": halt,
        "next_event": {"name": ev["name"], "time": t.isoformat(), "impact": ev.get("impact", "high")},
        "minutes_to_event": round(mins, 1), "actions": actions,
        "note": f"Next high-impact event: {ev['name']} in {mins/60:.1f}h.",
    }


PROVIDER_FRESH_S = 24 * 3600   # a feed that has not succeeded in a day no longer counts as connected


class EconCalendar:
    """Upcoming events: hand-entered ones plus those a calendar feed supplied
    (services/econ_feed.py). Stored as JSON beside the provider settings."""

    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else None
        # The feed thread and the API both rewrite the file; each write is a
        # read-modify-write of the other's half, so they take turns.
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            if self.path and self.path.exists():
                data = json.loads(self.path.read_text())
                return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _write(self, data: dict) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.path)   # a reader never sees half a file

    def manual_events(self) -> list:
        return list(self._read().get("events", []))

    def provider_events(self) -> list:
        return list((self._read().get("provider") or {}).get("events", []))

    def events(self) -> list:
        """Every known event, hand-entered first; a release both sources know
        (same name and time) appears once."""
        seen, out = set(), []
        for e in self.manual_events() + self.provider_events():
            key = (e.get("name"), e.get("time"))
            if key not in seen:
                seen.add(key)
                out.append(e)
        return out

    def set_events(self, events: list) -> list:
        clean = [{"name": e.get("name", ""), "impact": e.get("impact", "high"),
                  "time": e.get("time")} for e in (events or []) if e.get("name") and e.get("time")]
        with self._lock:
            data = self._read()
            data["events"] = clean
            self._write(data)
        return clean

    def set_provider_events(self, events: list, *, fetched_at: datetime, source: str) -> None:
        with self._lock:
            data = self._read()
            data["provider"] = {"source": source, "fetched_at": fetched_at.isoformat(timespec="seconds"),
                                "events": list(events)}
            self._write(data)

    def provider_status(self) -> dict:
        provider = self._read().get("provider") or {}
        return {"source": provider.get("source"), "fetched_at": provider.get("fetched_at"),
                "count": len(provider.get("events", []))}

    @property
    def connected(self) -> bool:
        """True when there is something real to act on: hand-entered events,
        or a feed that succeeded within the last day. A provider key on its
        own no longer counts; nothing fetched with it."""
        if self.manual_events():
            return True
        fetched = _parse(self.provider_status().get("fetched_at"))
        return bool(fetched and (datetime.now(timezone.utc) - fetched).total_seconds() <= PROVIDER_FRESH_S)
