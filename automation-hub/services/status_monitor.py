"""Service status, measured rather than asserted.

Once a minute the monitor asks each component whether it is working and
records the answer:

* **API** -- the app itself. If the process is down there is no sample at
  all, so a gap between samples is recorded as the API having been down
  (a deploy or a crash looks the same from outside, and both are downtime).
* **Trading workers** -- every paper-trading instance the operator has
  started: is its worker actually alive?
* **Market data** -- are those workers' Binance candles arriving on time?
* **Database** -- does the ledger that records every decision answer, and
  quickly?

From the samples it keeps a per-day tally (90 days of uptime bars) and an
incident list. An incident opens only after a component has been unhealthy
for ``confirm`` consecutive samples and closes after as many healthy ones, so
a single slow minute is not an incident and does not page anyone. Opening and
closing an incident is sent to the configured alert channels.

Everything shown publicly comes from ``public_view``: component states, the
daily tally and incident times, with plain-language details written here --
never an exception message, a hostname or a secret.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

OPERATIONAL, DEGRADED, OUTAGE, UNKNOWN = "operational", "degraded", "outage", "unknown"
_RANK = {UNKNOWN: -1, OPERATIONAL: 0, DEGRADED: 1, OUTAGE: 2}

COMPONENTS: dict[str, tuple[str, str]] = {
    "api": ("API and dashboard", "The web app and the API behind it answering requests"),
    "workers": ("Trading workers", "Every paper-trading instance the operator has started"),
    "market_data": ("Market data", "Binance USDⓈ-M candles reaching the workers on time"),
    "database": ("Database", "The ledger that records every decision and fill"),
}

Probe = Callable[[], tuple[str, str]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    day TEXT NOT NULL, component TEXT NOT NULL,
    operational INTEGER NOT NULL DEFAULT 0, degraded INTEGER NOT NULL DEFAULT 0,
    outage INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, component)
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL,
    started_at REAL NOT NULL, ended_at REAL
);
CREATE TABLE IF NOT EXISTS current (
    component TEXT PRIMARY KEY, state TEXT NOT NULL, detail TEXT NOT NULL,
    since REAL NOT NULL, streak INTEGER NOT NULL DEFAULT 0, pending TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------- probes
def api_probe() -> tuple[str, str]:
    return OPERATIONAL, "Answering requests"


def database_probe(ping: Callable[[], object], *, slow_s: float = 3.0) -> Probe:
    def probe():
        started = time.monotonic()
        try:
            ping()
        except Exception:  # noqa: BLE001 -- the reason stays private; the state is public
            return OUTAGE, "The ledger is not answering"
        if time.monotonic() - started > slow_s:
            return DEGRADED, "The ledger is answering slowly"
        return OPERATIONAL, "Recording decisions and fills"
    return probe


def workers_probe(snapshot: Callable[[], list[dict]]) -> Probe:
    def probe():
        rows = snapshot()
        if not rows:
            return OPERATIONAL, "No workers scheduled"
        alive = sum(1 for r in rows if r.get("alive"))
        if alive == len(rows):
            return OPERATIONAL, f"{alive} of {len(rows)} running"
        if alive == 0:
            return OUTAGE, f"None of the {len(rows)} scheduled workers is running"
        return DEGRADED, f"{alive} of {len(rows)} running"
    return probe


def market_data_probe(snapshot: Callable[[], list[dict]]) -> Probe:
    def probe():
        feeds = [str(r.get("market_data_status") or "") for r in snapshot() if r.get("alive")]
        if not feeds:
            return UNKNOWN, "No worker is reading the feed"
        late = sum(1 for s in feeds if s in ("stale", "error", "disconnected"))
        if late:
            return DEGRADED, f"Candles arriving late on {late} of {len(feeds)} feeds; those workers stand down"
        if any(s == "warming_up" for s in feeds):
            return OPERATIONAL, "Warming up after a start"
        return OPERATIONAL, "Candles current"
    return probe


# -------------------------------------------------------------------- store
class StatusMonitor:
    def __init__(self, path: str | Path, probes: dict[str, Probe], *,
                 notify: Optional[Callable[[dict], None]] = None,
                 interval_s: float = 60.0, confirm: int = 2,
                 clock: Callable[[], float] = time.time):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(self.path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._c.executescript(_SCHEMA)
            self._c.commit()
        self.probes = probes
        self.notify = notify
        self.interval_s = float(interval_s)
        self.confirm = max(1, int(confirm))
        self.clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cache: tuple[float, dict] | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="status-monitor", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception as exc:  # noqa: BLE001 -- monitoring must never die quietly
                print(f"[status] sample failed: {type(exc).__name__}: {exc}"[:300], flush=True)
            self._stop.wait(self.interval_s)

    # --------------------------------------------------------------- sample
    def _meta(self, key: str) -> Optional[str]:
        row = self._c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._c.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))

    def _tally(self, day: str, component: str, state: str, minutes: int = 1) -> None:
        if state not in (OPERATIONAL, DEGRADED, OUTAGE) or minutes <= 0:
            return
        self._c.execute(f"INSERT INTO daily(day,component,{state}) VALUES (?,?,?) "
                        f"ON CONFLICT(day,component) DO UPDATE SET {state}={state}+excluded.{state}",
                        (day, component, minutes))

    def _record_gap(self, last: float, now: float) -> None:
        """No samples between ``last`` and ``now``: the process was not running.
        Count the missed minutes against the API and record it as an outage."""
        missed = int((now - last) // self.interval_s) - 1
        if missed < 2:
            return
        t = last + self.interval_s
        per_day: dict[str, int] = {}
        for _ in range(min(missed, 90 * 1440)):
            per_day[_day(t)] = per_day.get(_day(t), 0) + 1
            t += self.interval_s
        for day, minutes in per_day.items():
            self._tally(day, "api", OUTAGE, minutes)
        self._c.execute("INSERT INTO incidents(component,state,detail,started_at,ended_at) VALUES (?,?,?,?,?)",
                        ("api", OUTAGE, "Not answering: the service was restarting or down", last, now))
        self._notify("api", OUTAGE, "Not answering: the service was restarting or down", opened=True,
                     started=last, ended=now)

    def sample(self) -> dict:
        now = self.clock()
        results: dict[str, tuple[str, str]] = {}
        for component, probe in self.probes.items():
            try:
                state, detail = probe()
            except Exception:  # noqa: BLE001
                state, detail = OUTAGE, "The health check itself failed"
            results[component] = (state if state in _RANK else UNKNOWN, str(detail)[:200])
        with self._lock:
            last = self._meta("last_sample_at")
            if last is None:
                self._set_meta("monitoring_since", str(now))
            else:
                self._record_gap(float(last), now)
            for component, (state, detail) in results.items():
                self._tally(_day(now), component, state)
                self._advance(component, state, detail, now)
            self._set_meta("last_sample_at", str(now))
            self._c.commit()
            self._cache = None
        return results

    def _advance(self, component: str, state: str, detail: str, now: float) -> None:
        """Hysteresis: the confirmed state changes only after ``confirm``
        consecutive samples agree on a new state."""
        row = self._c.execute("SELECT * FROM current WHERE component=?", (component,)).fetchone()
        if row is None:
            # A new component starts unmeasured and earns its first state the
            # same way as any other change -- so a component that is broken
            # from the very first sample still opens an incident.
            self._c.execute("INSERT INTO current VALUES (?,?,?,?,0,NULL)",
                            (component, UNKNOWN, "Not measured yet", now))
            row = self._c.execute("SELECT * FROM current WHERE component=?", (component,)).fetchone()
        confirmed = row["state"]
        if state == confirmed:
            self._c.execute("UPDATE current SET detail=?, streak=0, pending=NULL WHERE component=?",
                            (detail, component))
            if state in (DEGRADED, OUTAGE):
                self._escalate(component, state, detail)
            return
        was_bad, is_bad = confirmed in (DEGRADED, OUTAGE), state in (DEGRADED, OUTAGE)
        if was_bad and is_bad:
            # Degraded turning into an outage (or back) is one problem, not two:
            # the open incident records the worst state seen, immediately.
            self._escalate(component, state, detail)
        streak = (row["streak"] + 1) if row["pending"] == state else 1
        if streak < self.confirm:
            self._c.execute("UPDATE current SET streak=?, pending=? WHERE component=?",
                            (streak, state, component))
            return
        # confirmed transition; it began at the first sample of the streak
        began = now - (streak - 1) * self.interval_s
        self._c.execute("UPDATE current SET state=?, detail=?, since=?, streak=0, pending=NULL WHERE component=?",
                        (state, detail, began, component))
        if was_bad and not is_bad:
            self._close(component, began)
        elif is_bad and not was_bad:
            self._open(component, state, detail, began)

    def _open(self, component, state, detail, started):
        self._c.execute("INSERT INTO incidents(component,state,detail,started_at) VALUES (?,?,?,?)",
                        (component, state, detail, started))
        self._notify(component, state, detail, opened=True, started=started)

    def _escalate(self, component, state, detail):
        """An open incident takes the worst state and latest detail seen."""
        row = self._c.execute("SELECT id, state FROM incidents WHERE component=? AND ended_at IS NULL "
                              "ORDER BY id DESC LIMIT 1", (component,)).fetchone()
        if row and _RANK[state] > _RANK.get(row["state"], 0):
            self._c.execute("UPDATE incidents SET state=?, detail=? WHERE id=?", (state, detail, row["id"]))
            self._notify(component, state, detail, opened=True, started=None)

    def _close(self, component, ended):
        row = self._c.execute("SELECT * FROM incidents WHERE component=? AND ended_at IS NULL "
                              "ORDER BY id DESC LIMIT 1", (component,)).fetchone()
        if row:
            self._c.execute("UPDATE incidents SET ended_at=? WHERE id=?", (ended, row["id"]))
            self._notify(component, row["state"], row["detail"], opened=False,
                         started=row["started_at"], ended=ended)

    def _notify(self, component, state, detail, *, opened, started, ended=None):
        if not self.notify:
            return
        name = COMPONENTS.get(component, (component, ""))[0]
        if opened and ended is None:
            alert = {"severity": "critical" if state == OUTAGE else "warning",
                     "title": f"{name}: {'outage' if state == OUTAGE else 'degraded'}", "detail": detail}
        else:
            minutes = max(1, round(((ended or started) - started) / 60))
            alert = {"severity": "info", "title": f"{name}: recovered",
                     "detail": f"{detail} — lasted {minutes} min"}
        try:
            self.notify(alert)
        except Exception:  # noqa: BLE001 -- a failed notification never breaks monitoring
            pass

    # ------------------------------------------------------------ public view
    def public_view(self, *, days: int = 90, cache_s: float = 30.0) -> dict:
        now = self.clock()
        if self._cache and now - self._cache[0] < cache_s:
            return self._cache[1]
        with self._lock:
            since_raw = self._meta("monitoring_since")
            last_raw = self._meta("last_sample_at")
            current = {r["component"]: dict(r) for r in self._c.execute("SELECT * FROM current")}
            start_day = _day(now - (days - 1) * 86400)
            tallies = {(r["day"], r["component"]): dict(r) for r in self._c.execute(
                "SELECT * FROM daily WHERE day >= ?", (start_day,))}
            incidents = [dict(r) for r in self._c.execute(
                "SELECT * FROM incidents WHERE COALESCE(ended_at, ?) >= ? ORDER BY started_at DESC LIMIT 100",
                (now, now - days * 86400))]
        since = float(since_raw) if since_raw else None
        last = float(last_raw) if last_raw else None
        stale = last is None or now - last > 3 * self.interval_s
        day_list = [_day(now - (days - 1 - i) * 86400) for i in range(days)]
        components = []
        for key, (name, description) in COMPONENTS.items():
            if key not in self.probes:
                continue
            cur = current.get(key)
            state = cur["state"] if cur else UNKNOWN
            detail = cur["detail"] if cur else "Not measured yet"
            if stale:
                state, detail = UNKNOWN, "The monitor has not reported recently"
            bars, ok_total, measured_total = [], 0, 0
            for day in day_list:
                t = tallies.get((day, key))
                ok = t["operational"] if t else 0
                bad_d = t["degraded"] if t else 0
                bad_o = t["outage"] if t else 0
                measured = ok + bad_d + bad_o
                ok_total += ok
                measured_total += measured
                day_state = (UNKNOWN if not measured else OUTAGE if bad_o >= 5 else
                             DEGRADED if bad_d >= 5 or bad_o > 0 else OPERATIONAL)
                bars.append({"date": day, "state": day_state,
                             "uptime": round(100 * ok / measured, 2) if measured else None,
                             "samples": {"operational": ok, "degraded": bad_d, "outage": bad_o}})
            components.append({
                "id": key, "name": name, "description": description, "state": state, "detail": detail,
                "since": _iso(cur["since"]) if cur else None,
                "uptime_pct": round(100 * ok_total / measured_total, 3) if measured_total else None,
                "days": bars,
            })
        worst = max((c["state"] for c in components), key=lambda s: _RANK.get(s, -1), default=UNKNOWN)
        view = {
            "overall": worst if not stale else UNKNOWN,
            "generated_at": _iso(now),
            "last_sample_at": _iso(last),
            "monitoring_since": _iso(since),
            "interval_s": self.interval_s,
            "components": components,
            "incidents": [{
                "id": i["id"], "component": i["component"],
                "component_name": COMPONENTS.get(i["component"], (i["component"], ""))[0],
                "state": i["state"], "detail": i["detail"],
                "started_at": _iso(i["started_at"]), "ended_at": _iso(i["ended_at"]),
                "duration_min": round(((i["ended_at"] or now) - i["started_at"]) / 60),
                "ongoing": i["ended_at"] is None,
            } for i in incidents],
        }
        self._cache = (now, view)
        return view

    def close(self) -> None:
        self.stop()
        with self._lock:
            self._c.close()
