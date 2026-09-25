"""News blackout for Trading Instances (opt-in, off by default).

The global paper engine has always consulted the economic calendar
(services/econ_guard.py); Trading Instances never did, so an instance could
open a trade a minute before CPI. This connects the same gate to each
instance's pipeline, but only for instances whose owner switched it on:

* blackout — no new entries from ``BLACKOUT_MIN`` before a high-impact
  release until ``AFTER_MIN`` after it (the first reaction is the violent
  part);
* caution — new entries sized at half risk in the ``CAUTION_MIN`` before.

Open positions, their stops and targets, and every strategy's entry rules
are untouched: the gate sits in the shared risk pipeline, after the strategy
has decided. An instance with the guard off gets an empty event list, which
evaluates exactly as if no calendar were connected.

The switch is kept in a JSON file beside the calendar rather than as a
column on ``trading_instances``: a new column would have to be migrated on
every Supabase deployment before any instance could be saved again.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from services.econ_guard import evaluate

BLACKOUT_MIN = 30
CAUTION_MIN = 120
AFTER_MIN = 15


class InstanceEventGuard:
    def __init__(self, path: str | None, calendar, *, after_min: int = AFTER_MIN):
        self.path = Path(path) if path else None
        self.calendar = calendar
        self.after_min = int(after_min)
        self._lock = threading.Lock()
        self._memory: dict = {}          # used when no path is configured
        self._cache: tuple[float, dict] | None = None

    # ------------------------------------------------------------ storage
    def _read(self) -> dict:
        if not self.path:
            return self._memory
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return {}
        # Read on every signal, so skip the parse while the file is unchanged.
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        try:
            data = json.loads(self.path.read_text())
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        self._cache = (mtime, data)
        return data

    def _write(self, data: dict) -> None:
        if not self.path:
            self._memory = data
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)
        self._cache = None

    # ------------------------------------------------------------ switch
    def enabled(self, instance_id: str) -> bool:
        return bool((self._read().get(instance_id) or {}).get("enabled"))

    def set(self, instance_id: str, enabled: bool, *, by: str = "") -> dict:
        with self._lock:
            data = dict(self._read())
            data[instance_id] = {"enabled": bool(enabled), "by": by,
                                 "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._write(data)
        return self.state(instance_id)

    def forget(self, instance_id: str) -> None:
        with self._lock:
            data = dict(self._read())
            if data.pop(instance_id, None) is not None:
                self._write(data)

    # ------------------------------------------------------------ pipeline
    def events_for(self, instance_id: str) -> Callable[[], list]:
        """The pipeline's ``econ_events`` source for one instance. It checks
        the switch on every call, so turning it on or off applies to the next
        signal without restarting the worker."""
        def events() -> list:
            return self.calendar.events() if self.enabled(instance_id) else []
        return events

    def entry_block(self, instance_id: str) -> str | None:
        """Why a new entry is refused right now under this switch, or None.
        For callers without a SignalPipeline (the research labs)."""
        if not self.enabled(instance_id):
            return None
        ev = evaluate(self.calendar.events(), blackout_min=BLACKOUT_MIN,
                      caution_min=CAUTION_MIN, after_min=self.after_min)
        if not ev["halt_new_entries"]:
            return None
        mins = ev["minutes_to_event"]
        when = f"released {-mins:.0f}m ago" if mins < 0 else f"in {mins:.0f}m"
        return f"News blackout: {ev['next_event']['name']} {when} — no new strategy entries"

    def state(self, instance_id: str) -> dict:
        row = self._read().get(instance_id) or {}
        on = bool(row.get("enabled"))
        ev = evaluate(self.calendar.events(), blackout_min=BLACKOUT_MIN,
                      caution_min=CAUTION_MIN, after_min=self.after_min)
        return {
            "enabled": on,
            "updated_at": row.get("updated_at"),
            "calendar_connected": bool(self.calendar.connected),
            # What the calendar says right now, whether or not this instance
            # acts on it, so the owner can see what switching on would do.
            "mode": ev["mode"],
            "halt_new_entries": on and ev["halt_new_entries"],
            "risk_multiplier": ev["risk_multiplier"] if on else 1.0,
            "next_event": ev["next_event"],
            "minutes_to_event": ev["minutes_to_event"],
            "window": {"blackout_before_min": BLACKOUT_MIN, "blackout_after_min": self.after_min,
                       "caution_before_min": CAUTION_MIN},
        }
