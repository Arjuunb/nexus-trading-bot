"""The Guardian service: observe every few seconds, derive health, record changes.

Isolation (PRD §3, §42):

* Guardian runs on its own daemon threads -- the bus drain and this loop. A
  trading worker dying cannot stop them, and nothing here can stop a worker:
  every exception is caught at the collector, recorded, and the loop goes on.
* Guardian holds no trading object. It is built from read-only callables
  (``services/guardian/sources.py``), so there is no method on hand through
  which it could start, stop, reconfigure or trade anything (PRD §4).
* Trading never waits on Guardian. The only thing trading code does is
  ``services.guardian.emit()``, which queues or drops and never raises.

Self-monitoring (PRD §17): Guardian judges its own health every cycle from
its bus counters and collectors, and anyone reading its state judges its
heartbeat at read time, so a Guardian that has stopped reads FAILED instead of
showing its last good answer forever.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from services.guardian import health as h
from services.guardian.schema import InvalidEvent, make_event, utcnow

SERVICE = "guardian"
_SEVERITY_FOR = {h.FAILED: "HIGH", h.BLOCKED: "WARNING", h.DEGRADED: "WATCH",
                 h.UNKNOWN: "WATCH", h.HEALTHY: "INFO"}


class GuardianService:
    def __init__(self, store, bus, *,
                 instances: Optional[dict[str, Callable[[], list[dict]]]] = None,
                 labs: Optional[dict[str, Callable[[], dict]]] = None,
                 database: Optional[Callable[[], dict]] = None,
                 journal: Optional[Callable[[], dict]] = None,
                 interval_s: float = 15.0, clock: Callable[[], float] = time.time):
        self.store, self.bus = store, bus
        self.instances = dict(instances or {})    # name -> read-only rows
        self.labs = dict(labs or {})              # lab id -> read-only row
        self.database = database
        self.journal = journal
        self.interval_s = max(1.0, float(interval_s))
        self.clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._previous: dict[str, str] = {}
        self._last_nodes: dict[str, dict[str, h.Component]] = {}
        self._collector_errors: dict[str, str] = {}
        self._dropped_seen = 0
        self.cycles = 0
        self.last_cycle_ms: Optional[float] = None

    # ------------------------------------------------------------ publish
    def _publish(self, event_type: str, **fields) -> None:
        try:
            self.bus.publish(make_event(event_type, source_service=SERVICE, **fields))
        except InvalidEvent:
            self.bus.reject()

    # ----------------------------------------------------------- collect
    def _collect(self, name: str, fn: Callable[[], dict[str, h.Component]]) -> dict[str, h.Component]:
        """Run one collector. On failure its last nodes are kept but marked
        UNKNOWN -- Guardian says it cannot see them rather than guessing."""
        try:
            nodes = fn()
        except Exception as exc:  # noqa: BLE001 -- one blind collector never blinds the rest
            error = f"{type(exc).__name__}: {exc}"[:300]
            if self._collector_errors.get(name) != error:
                self._publish("collector_failed", source_component=f"guardian.{name}",
                              severity="WARNING", reason=error)
            self._collector_errors[name] = error
            stale = {}
            for cid, node in self._last_nodes.get(name, {}).items():
                stale[cid] = h.Component(**{**node.to_dict(), "raw": h.UNKNOWN,
                                            "detail": f"not observed: collector failed ({error})",
                                            "depends_on": tuple(node.depends_on),
                                            "effective": h.UNKNOWN, "blocked_by": []})
            return stale
        if name in self._collector_errors:
            self._publish("collector_recovered", source_component=f"guardian.{name}",
                          severity="INFO", reason=self._collector_errors.pop(name))
        self._last_nodes[name] = nodes
        return nodes

    def _instance_nodes(self, source: str, rows: list[dict]) -> dict[str, h.Component]:
        now, out = utcnow(), {}
        for row in rows:
            iid = row["id"]
            feed_id, node_id = f"feed:instance:{iid}", f"instance:{iid}"
            feed_state, feed_detail = h.instance_feed_state(row.get("market_data_status") or "")
            label = f"{row.get('symbol') or '?'} {row.get('timeframe') or ''} · {row.get('strategy_label') or row.get('strategy_id') or ''}".strip()
            out[feed_id] = h.Component(
                id=feed_id, label=f"Feed · {label}", kind="feed", raw=feed_state, detail=feed_detail,
                observed_at=now, facts={"live": bool(row.get("live_feed")), "consumer": node_id,
                                        "market_data_status": row.get("market_data_status")})
            alive = row.get("alive")
            state, detail = ((h.UNKNOWN, "worker liveness could not be read") if alive is None else
                             h.instance_worker_state(bool(alive), row.get("lifecycle_state") or "",
                                                     bool(row.get("paused"))))
            out[node_id] = h.Component(
                id=node_id, label=label, kind="instance", raw=state, detail=detail,
                depends_on=(feed_id, "database"), observed_at=now,
                facts={k: row.get(k) for k in ("id", "lab_id", "symbol", "timeframe", "strategy_id",
                                               "strategy_version", "mode", "state", "alive",
                                               "lifecycle_state", "last_heartbeat", "last_error")}
                | {"source": source})
        return out

    def _lab_nodes(self, row: dict) -> dict[str, h.Component]:
        now, lab = utcnow(), row["id"]
        node_id, feed_id = f"lab:{lab}", f"feed:lab:{lab}"
        out: dict[str, h.Component] = {}
        if row.get("thread_alive") is False:
            state, detail = h.FAILED, "the lab worker thread is not running"
        elif row.get("thread_alive") is None:
            state, detail = h.UNKNOWN, "the lab worker thread could not be read"
        elif row.get("session_active") is False:
            state, detail = h.HEALTHY, "idle: no active session"
        else:
            state, detail = h.HEALTHY, "running"
        depends: tuple[str, ...] = ()
        stream = row.get("stream")
        if stream and row.get("session_active") is not False:
            feed_state, feed_detail = h.lab_stream_state(stream.get("state") or "")
            if stream.get("failing_dependency"):
                feed_detail += f" (failing: {stream['failing_dependency']})"
            out[feed_id] = h.Component(id=feed_id, label=f"Feed · {row['label']}", kind="feed",
                                       raw=feed_state, detail=feed_detail, observed_at=now,
                                       facts={"live": True, "consumer": node_id, **stream})
            depends = (feed_id,)
        out[node_id] = h.Component(id=node_id, label=row["label"], kind="lab", raw=state, detail=detail,
                                   depends_on=depends, observed_at=now,
                                   facts={"thread_alive": row.get("thread_alive"),
                                          "session_active": row.get("session_active")})
        return out

    def _database_node(self) -> dict[str, h.Component]:
        view = self.database() if self.database else None
        if not view:
            return {"database": h.Component(id="database", label="Ledger database", kind="database",
                                            raw=h.UNKNOWN, detail="not measured", observed_at=utcnow())}
        row = (view.get("components") or {}).get("database")
        last, interval = view.get("last_sample_at"), float(view.get("interval_s") or 60)
        if row is None or last is None:
            state, detail = h.UNKNOWN, "the status monitor has not measured the ledger yet"
        elif self.clock() - float(last) > 3 * interval:
            state, detail = h.UNKNOWN, f"the status monitor last sampled {int(self.clock() - float(last))}s ago"
        else:
            state, detail = h.status_monitor_state(row.get("state")), row.get("detail") or ""
        return {"database": h.Component(id="database", label="Ledger database", kind="database",
                                        raw=state, detail=detail, observed_at=utcnow(),
                                        facts={"status_monitor": row})}

    def _journal_node(self) -> dict[str, h.Component]:
        row = self.journal() if self.journal else None
        if row is None:
            return {}
        if not row.get("running"):
            state, detail = h.FAILED, "the journal recorder is not running"
        elif row.get("skipped"):
            state, detail = h.DEGRADED, "not journalling: " + "; ".join(row["skipped"])
        elif row.get("last_error"):
            state, detail = h.DEGRADED, f"last pass had errors: {row['last_error']}"
        elif not row.get("passes"):
            state, detail = h.UNKNOWN, "no reconcile pass has completed yet"
        else:
            state, detail = h.HEALTHY, f"{row['passes']} passes; last at {row.get('last_pass_at')}"
        return {"journal": h.Component(id="journal", label="Trade journal", kind="journal", raw=state,
                                       detail=detail, depends_on=("database",), observed_at=utcnow(),
                                       facts=row)}

    def observe(self) -> dict[str, h.Component]:
        nodes: dict[str, h.Component] = {}
        for name, fn in self.instances.items():
            nodes.update(self._collect(f"instances.{name}", lambda fn=fn, name=name:
                                       self._instance_nodes(name, fn())))
        for lab_id, fn in self.labs.items():
            nodes.update(self._collect(f"labs.{lab_id}", lambda fn=fn: self._lab_nodes(fn())))
        nodes.update(self._collect("database", self._database_node))
        nodes.update(self._collect("journal", self._journal_node))
        feeds = [n for n in nodes.values() if n.kind == "feed"]
        state, detail = h.upstream_from_feeds(feeds)
        nodes["binance_usdm"] = h.Component(id="binance_usdm", label="Binance USD-M public data",
                                            kind="upstream", raw=state, detail=detail, observed_at=utcnow(),
                                            facts={"feeds": len(feeds)})
        nodes["guardian"] = self._self_node()
        return h.derive(nodes)

    def _self_node(self) -> h.Component:
        stats = self.bus.stats()
        problems = []
        dropped_now = stats["dropped"] - self._dropped_seen
        self._dropped_seen = stats["dropped"]
        if dropped_now > 0:
            problems.append(f"{dropped_now} events dropped since the last cycle (queue full)")
        if stats["last_store_error"]:
            problems.append(f"cannot write evidence: {stats['last_store_error']}")
        if not stats["running"]:
            problems.append("the event bus is not draining")
        if self._collector_errors:
            problems.append(f"{len(self._collector_errors)} collector(s) failing: "
                            + ", ".join(sorted(self._collector_errors)))
        state = h.DEGRADED if problems else h.HEALTHY
        return h.Component(id="guardian", label="Guardian", kind="guardian", raw=state,
                           detail="; ".join(problems) or "observing; evidence being written",
                           observed_at=utcnow(), facts={"bus": stats})

    # ------------------------------------------------------------- cycle
    def cycle(self) -> dict[str, h.Component]:
        started = time.monotonic()
        nodes = self.observe()
        for cid, node in nodes.items():
            before = self._previous.get(cid)
            if before != node.effective:
                self._publish(
                    "health_changed", source_component=cid, severity=_SEVERITY_FOR[node.effective],
                    state_before=before, state_after=node.effective,
                    reason=(f"blocked by {', '.join(node.blocked_by)}" if node.blocked_by else node.detail),
                    instance_id=node.facts.get("id") if node.kind == "instance" else None,
                    lab_id=(cid.split(":", 1)[1] if node.kind == "lab" else node.facts.get("lab_id")),
                    symbol=node.facts.get("symbol"), timeframe=node.facts.get("timeframe"),
                    strategy_id=node.facts.get("strategy_id"),
                    evidence={"raw": node.raw, "detail": node.detail, "blocked_by": node.blocked_by,
                              "depends_on": list(node.depends_on)})
        for cid in set(self._previous) - set(nodes):
            self._publish("health_changed", source_component=cid, severity="INFO",
                          state_before=self._previous[cid], state_after=None,
                          reason="no longer observed: stopped or removed by its owner")
        self._previous = {cid: n.effective for cid, n in nodes.items()}
        self.store.save_components({cid: n.to_dict() for cid, n in nodes.items()})
        self.cycles += 1
        self.last_cycle_ms = round((time.monotonic() - started) * 1000, 1)
        self.store.set_meta("heartbeat", {"at": self.clock(), "cycles": self.cycles,
                                          "interval_s": self.interval_s,
                                          "last_cycle_ms": self.last_cycle_ms})
        return nodes

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception as exc:  # noqa: BLE001 -- the loop outlives any single cycle
                self._publish("collector_failed", source_component="guardian.cycle",
                              severity="WARNING", reason=f"{type(exc).__name__}: {exc}"[:300])
            self._stop.wait(self.interval_s)

    # --------------------------------------------------------- lifecycle
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self.bus.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="guardian", daemon=True)
        self._thread.start()
        self.store.record_action("GUARDIAN_STARTED", reason="application start",
                                 policy="GUARDIAN_LIFECYCLE", result="SUCCESS",
                                 evidence={"interval_s": self.interval_s})
        self._publish("guardian_started", source_component="guardian", severity="INFO",
                      reason="Guardian started observing (read-only)")
        return True

    def stop(self, timeout_s: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout_s)
        self._publish("guardian_stopped", source_component="guardian", severity="INFO",
                      reason="Guardian stopped")
        self.store.record_action("GUARDIAN_STOPPED", reason="application shutdown",
                                 policy="GUARDIAN_LIFECYCLE", result="SUCCESS")
        self.bus.stop(timeout_s)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------- read
    def snapshot(self) -> dict:
        """Everything the Command Center shows, judged at read time."""
        nodes = {cid: h.Component(**{**data, "depends_on": tuple(data.get("depends_on") or ())})
                 for cid, data in self.store.components().items()}
        beat = self.store.meta("heartbeat") or {}
        age = (self.clock() - float(beat["at"])) if beat.get("at") else None
        if age is None or age > 3 * self.interval_s or not self.running:
            reason = ("Guardian is not running" if not self.running else
                      "Guardian has never completed a cycle" if age is None else
                      f"no Guardian heartbeat for {int(age)}s")
            nodes["guardian"] = h.Component(id="guardian", label="Guardian", kind="guardian",
                                            raw=h.FAILED, detail=reason, observed_at=utcnow(),
                                            facts={"bus": self.bus.stats()})
            h.derive(nodes)
        since = datetime.fromtimestamp(self.clock() - 86_400, timezone.utc).isoformat()
        return {
            "generated_at": utcnow(),
            "summary": h.summarize(nodes),
            "components": [n.to_dict() for n in sorted(nodes.values(), key=lambda n: (n.kind, n.id))],
            "self": {"running": self.running, "heartbeat_age_s": round(age, 1) if age is not None else None,
                     "interval_s": self.interval_s, "cycles": beat.get("cycles", 0),
                     "last_cycle_ms": beat.get("last_cycle_ms"), "bus": self.bus.stats(),
                     "collectors_failing": dict(self._collector_errors)},
            "events_24h": {"total": self.store.count_events(since=since),
                           "warning_or_worse": self.store.count_events(since=since, min_severity="WARNING")},
            "boundary": {"mode": "read-only",
                         "may_change": [],
                         "never_changes": ["strategy rules or parameters", "risk limits or leverage",
                                           "stop-loss / take-profit / RR rules", "positions or orders",
                                           "paper to live", "API credentials",
                                           "trading history, journals or incidents"]},
        }
