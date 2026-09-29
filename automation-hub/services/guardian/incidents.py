"""Incident intelligence (PRD §10, §11, §22, §29, §36; Phase 3).

One outage is one incident, however many components it touches. Grouping
follows the dependency graph Guardian already resolves: every unhealthy
component is traced to the component whose own failure explains it (its
*root*), and all components with the same root belong to one incident. When
two or more independent live feeds fail together the root is Binance USD-M
itself, so a Binance outage that stalls three instances and both labs is one
incident with five affected components, not five alerts.

Engine lifecycle events that happen between Guardian's cycles -- a candle
going stale for twenty seconds, a worker crashing and restarting -- are
attached to the same incident key the component's health would use, so the
event and the health state can never open two incidents for one fault.

Every incident carries a root-cause statement with one of the PRD's five
confidence levels, derived from what was observed and never stronger than
the evidence:

* CONFIRMED -- the component reported the failure about itself (a worker
  that is not running, a status monitor that confirmed an outage twice);
* HIGH CONFIDENCE -- independent observations agree (two or more feeds
  failed together; other consumers still receive data, so the fault is
  local to the one that does not);
* PROBABLE / POSSIBLE -- one observation that fits more than one cause;
* UNKNOWN -- Guardian cannot see enough to say.

Incidents are never erased (§36): state changes are appended to
``guardian_incident_log``, which refuses UPDATE and DELETE. Recovery is not
closure: an incident closes only after its components have stayed healthy
for a verification period; failing again before that reopens the same
incident instead of starting a new one.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Optional

from services.guardian import health as h
from services.guardian.schema import InvalidEvent, make_event, utcnow

CONFIDENCE = ("CONFIRMED", "HIGH CONFIDENCE", "PROBABLE", "POSSIBLE", "UNKNOWN")
OPEN, RECOVERED, CLOSED = "OPEN", "RECOVERED", "CLOSED"

#: PRD §10: what kind of problem this is.
DATA, INFRA, STRATEGY, EXECUTION, RISK = ("data_failure", "infrastructure_failure",
                                          "strategy_failure", "execution_failure", "risk_rejection")

#: Engine events that open or clear a signal on an incident, by the key the
#: component's health would use. (open types, clearing types, key builder)
_SIGNALS: tuple[tuple[frozenset, frozenset, Callable[[dict], Optional[str]]], ...] = (
    (frozenset({"stale_candle", "websocket_disconnected"}),
     frozenset({"websocket_connected", "websocket_reconnected"}),
     lambda e: f"feed:{e['source_component']}" if str(e["source_component"]).startswith("instance:") else None),
    (frozenset({"worker_crashed"}), frozenset({"worker_started", "websocket_connected"}),
     lambda e: e["source_component"] if str(e["source_component"]).startswith("instance:") else None),
    (frozenset({"missing_htf_candle", "stale_htf_candle"}), frozenset({"htf_candle_recovered"}),
     lambda e: f"htf:{e['source_component']}"),
    (frozenset({"collector_failed"}), frozenset({"collector_recovered"}),
     lambda e: f"collector:{e['source_component']}"),
)
_WATCHED = frozenset().union(*(o | c for o, c, _ in _SIGNALS))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guardian_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    severity TEXT NOT NULL,
    root_component TEXT NOT NULL,
    diagnosis TEXT NOT NULL,
    affected TEXT NOT NULL,
    signals TEXT NOT NULL,
    related TEXT NOT NULL,
    started_at TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    recovered_at TEXT, verified_at TEXT, closed_at TEXT,
    updates INTEGER NOT NULL,
    healthy_cycles INTEGER NOT NULL,
    last_update_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gi_key ON guardian_incidents(key, state);
CREATE INDEX IF NOT EXISTS idx_gi_started ON guardian_incidents(started_at);
CREATE TABLE IF NOT EXISTS guardian_incident_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    entry TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_gil_incident ON guardian_incident_log(incident_id, seq);
CREATE TRIGGER IF NOT EXISTS trg_gil_no_update BEFORE UPDATE ON guardian_incident_log
BEGIN SELECT RAISE(ABORT, 'the incident log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_gil_no_delete BEFORE DELETE ON guardian_incident_log
BEGIN SELECT RAISE(ABORT, 'the incident log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_gi_no_delete BEFORE DELETE ON guardian_incidents
BEGIN SELECT RAISE(ABORT, 'incidents are never erased'); END;
"""


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _load(value: Optional[str]) -> Any:
    return None if value is None else json.loads(value)


# ------------------------------------------------------------------ roots
def roots(nodes: dict[str, h.Component]) -> dict[str, set[str]]:
    """root key -> the unhealthy components it explains (itself included)."""
    def root_of(cid: str, trail: tuple = ()) -> set[str]:
        node = nodes.get(cid)
        if node is None or cid in trail:
            return {cid}
        if node.effective == h.BLOCKED and node.blocked_by:
            out: set[str] = set()
            for upstream in node.blocked_by:
                out |= root_of(upstream, trail + (cid,))
            return out
        return {cid}

    upstream = nodes.get("binance_usdm")
    binance_down = upstream is not None and upstream.effective == h.FAILED
    groups: dict[str, set[str]] = {}
    for cid, node in nodes.items():
        if cid == "binance_usdm" or node.effective not in (h.FAILED, h.BLOCKED, h.DEGRADED):
            continue
        for root in root_of(cid):
            root_node = nodes.get(root)
            if binance_down and root_node is not None and root_node.kind == "feed" \
                    and root_node.raw == h.FAILED and root_node.facts.get("live", True):
                root = "binance_usdm"
            groups.setdefault(root, set()).add(cid)
    if binance_down:
        groups.setdefault("binance_usdm", set()).add("binance_usdm")
    return groups


def signal_key(event: dict, nodes: dict[str, h.Component]) -> Optional[tuple[str, bool]]:
    """(incident key, opens?) for an engine event, or None."""
    for opens, clears, key_of in _SIGNALS:
        kind = event.get("event_type")
        if kind in opens or kind in clears:
            key = key_of(event)
            if key is None:
                return None
            upstream = nodes.get("binance_usdm")
            if key.startswith("feed:") and upstream is not None and upstream.effective == h.FAILED:
                key = "binance_usdm"
            return key, kind in opens
    return None


# -------------------------------------------------------------- diagnosis
def _live_feeds(nodes: dict[str, h.Component]) -> list[h.Component]:
    return [n for n in nodes.values() if n.kind == "feed" and n.facts.get("live", True)]


def diagnose(key: str, nodes: dict[str, h.Component], signals: dict[str, dict]) -> dict:
    """Symptom -> affected -> upstream -> root-cause candidate -> evidence ->
    confidence -> recommended action (PRD §11). Never stronger than the
    evidence; the recommended action is advice, and Guardian takes none."""
    node = nodes.get(key)
    evidence: list[str] = []
    if key == "binance_usdm":
        feeds = _live_feeds(nodes)
        down = [f for f in feeds if f.raw == h.FAILED]
        evidence = [f"{f.label}: {f.detail}" for f in down]
        return {"kind": DATA, "symptom": f"{len(down)} of {len(feeds)} live market-data consumers receive no data",
                "root_cause": "Binance USD-M public market data is not reaching this server "
                              "(Binance itself, or the network path to it)",
                "confidence": "HIGH CONFIDENCE",
                "why_this_confidence": "independent consumers failed together; Guardian cannot see "
                                       "Binance's side, so it is not CONFIRMED",
                "evidence": evidence,
                "recommended_action": "Check the server's network path to Binance USD-M and Binance's "
                                      "status. Trading already stands down on stale data; nothing needs forcing."}
    if key.startswith("feed:"):
        feeds = _live_feeds(nodes)
        others_ok = [f for f in feeds if f.id != key and f.raw == h.HEALTHY]
        detail = node.detail if node else "; ".join(s.get("reason") or "" for s in signals.values())
        evidence = [f"{node.label}: {node.detail}"] if node else []
        evidence += [f"{s['event_type']} at {s['timestamp']}: {s.get('reason') or ''}" for s in signals.values()]
        if node is None or node.raw != h.FAILED:
            return {"kind": DATA, "symptom": "the engine reported stale or disconnected market data",
                    "root_cause": "a brief market-data interruption for this consumer",
                    "confidence": "PROBABLE",
                    "why_this_confidence": "reported by the engine between Guardian's observations",
                    "evidence": evidence, "recommended_action": "None while it recovers on its own."}
        if others_ok:
            return {"kind": DATA, "symptom": detail,
                    "root_cause": "this consumer's own market-data connection or subscription",
                    "confidence": "HIGH CONFIDENCE",
                    "why_this_confidence": f"{len(others_ok)} other live consumer(s) receive data at the same "
                                           "time, so Binance is reachable",
                    "evidence": evidence + [f"{f.label}: receiving data" for f in others_ok[:5]],
                    "recommended_action": "Restarting only this consumer's feed would be the operational fix "
                                          "(an owner action until Phase 7)."}
        return {"kind": DATA, "symptom": detail,
                "root_cause": "this consumer's connection, or Binance -- one consumer cannot tell them apart",
                "confidence": "POSSIBLE",
                "why_this_confidence": "no other live consumer to compare against",
                "evidence": evidence,
                "recommended_action": "Check the consumer's feed and the server's network path to Binance."}
    if key.startswith("htf:"):
        evidence = [f"{s['event_type']} at {s['timestamp']}: {s.get('reason') or ''}" for s in signals.values()]
        return {"kind": DATA, "symptom": "a mandatory higher-timeframe series is missing or stale",
                "root_cause": "the higher-timeframe candles the strategy requires are not available and current",
                "confidence": "CONFIRMED",
                "why_this_confidence": "reported by the engine from its own higher-timeframe check",
                "evidence": evidence,
                "recommended_action": "None needed to stay safe: the instance does not enter without it."}
    if key.startswith("collector:"):
        evidence = [f"{s.get('reason') or ''}" for s in signals.values()]
        return {"kind": INFRA, "symptom": "Guardian cannot read part of the platform",
                "root_cause": "a Guardian collector is failing; the components it reads are not observed",
                "confidence": "UNKNOWN",
                "why_this_confidence": "Guardian cannot see the state it would need to judge",
                "evidence": evidence, "recommended_action": "Check the collector's error in the evidence."}
    if node is None and key.startswith("instance:"):
        evidence = [f"{s['event_type']} at {s['timestamp']}: {s.get('reason') or ''}" for s in signals.values()]
        return {"kind": INFRA, "symptom": "the worker reported a crash",
                "root_cause": "; ".join(s.get("reason") or "" for s in signals.values()) or "not recorded",
                "confidence": "CONFIRMED", "why_this_confidence": "the worker reported it about itself",
                "evidence": evidence, "recommended_action": "The supervisor restarts it; check the error."}
    if node is None:
        return {"kind": INFRA, "symptom": "a component is unhealthy", "root_cause": "not observed",
                "confidence": "UNKNOWN", "why_this_confidence": "the component is not observed",
                "evidence": [], "recommended_action": "—"}
    evidence = [f"{node.label}: {node.detail}"]
    if node.kind == "instance":
        error = str(node.facts.get("last_error") or "")
        kind = STRATEGY if "StrategyExecutionError" in error else INFRA
        return {"kind": kind, "symptom": node.detail,
                "root_cause": error or "the worker's error was not recorded",
                "confidence": "CONFIRMED" if error else "UNKNOWN",
                "why_this_confidence": ("the worker recorded the error itself" if error else
                                        "the worker stopped without recording why"),
                "evidence": evidence + ([f"last error: {error}"] if error else []),
                "recommended_action": "Read the error; the instance does not trade while stopped."}
    if node.kind == "lab":
        return {"kind": INFRA, "symptom": node.detail, "root_cause": node.detail,
                "confidence": "CONFIRMED", "why_this_confidence": "observed directly (worker thread state)",
                "evidence": evidence, "recommended_action": "Restart the lab from its page."}
    if node.kind == "database":
        return {"kind": INFRA, "symptom": node.detail, "root_cause": node.detail or "ledger database outage",
                "confidence": "CONFIRMED" if node.raw == h.FAILED else "PROBABLE",
                "why_this_confidence": "the status monitor confirms a change only after two samples",
                "evidence": evidence, "recommended_action": "Check the ledger database."}
    if node.kind == "journal":
        return {"kind": INFRA, "symptom": node.detail, "root_cause": node.detail,
                "confidence": "CONFIRMED", "why_this_confidence": "the journal recorder's own report",
                "evidence": evidence, "recommended_action": "See the journal recorder's report."}
    if node.kind == "guardian":
        return {"kind": INFRA, "symptom": node.detail, "root_cause": node.detail,
                "confidence": "CONFIRMED", "why_this_confidence": "Guardian's own counters",
                "evidence": evidence, "recommended_action": "See Guardian itself in the Command Center."}
    return {"kind": INFRA, "symptom": node.detail, "root_cause": node.detail,
            "confidence": "PROBABLE", "why_this_confidence": "a single observation",
            "evidence": evidence, "recommended_action": "—"}


def _title(key: str, nodes: dict[str, h.Component], diagnosis: dict) -> str:
    node = nodes.get(key)
    if key == "binance_usdm":
        return "Binance USD-M market data not reaching the platform"
    if key.startswith("htf:"):
        return f"Higher timeframe missing · {key[4:]}"
    if key.startswith("collector:"):
        return f"Guardian cannot read {key[len('collector:'):]}"
    label = node.label if node else key
    return f"{label}: {diagnosis['symptom']}"[:200]


_SEVERITY = {h.FAILED: "HIGH", h.BLOCKED: "WARNING", h.DEGRADED: "WATCH"}


class IncidentEngine:
    """Runs inside Guardian's cycle. Holds nothing that can change the
    platform: it reads nodes and events and writes only Guardian's store."""

    def __init__(self, store, *, verify_s: float = 60.0, degraded_grace_s: float = 300.0,
                 clock: Optional[Callable[[], float]] = None):
        self.store = store
        self.verify_s = float(verify_s)
        self.degraded_grace_s = float(degraded_grace_s)
        self.clock = clock
        self._degraded_since: dict[str, float] = {}
        with store._lock:
            store._c.executescript(_SCHEMA)
            store._c.commit()

    # ------------------------------------------------------------- store
    def _rows(self, sql: str, args: Iterable = ()) -> list[dict]:
        with self.store._lock:
            return [dict(r) for r in self.store._c.execute(sql, tuple(args))]

    def _log(self, incident_id: int, entry: str, detail: str, evidence: Any = None) -> None:
        self.store._c.execute(
            "INSERT INTO guardian_incident_log(incident_id,at,entry,detail,evidence) VALUES (?,?,?,?,?)",
            (incident_id, utcnow(), entry, detail, None if evidence is None else _dump(evidence)))

    def _active(self) -> dict[str, dict]:
        return {r["key"]: r for r in self._rows(
            "SELECT * FROM guardian_incidents WHERE state IN (?,?)", (OPEN, RECOVERED))}

    # ------------------------------------------------------------- cycle
    def cycle(self, nodes: dict[str, h.Component], *, now: float,
              publish: Callable[..., None]) -> dict:
        """Group what is unhealthy now and what the engines reported since
        the last cycle into incidents. Returns counts for the heartbeat."""
        groups = self._groups(nodes, now)
        events = self._new_events()
        # Opening events go to the key the component's health would use now;
        # a clearing event clears that component's signal of the same family
        # wherever it was filed (a stale feed filed under a Binance outage is
        # cleared by its own reconnect after Binance is back).
        signals: dict[str, dict[str, dict]] = {}
        clears: list[tuple[str, frozenset]] = []
        for event in events:
            found = signal_key(event, nodes)
            if found is None:
                continue
            key, opens = found
            if opens:
                signals.setdefault(key, {})[event["source_component"]] = event
            else:
                family = next(o for o, c, _ in _SIGNALS if event["event_type"] in c)
                clears.append((event["source_component"], family))
                for pending in signals.values():             # an open then a clear in one batch
                    if (pending.get(event["source_component"]) or {}).get("event_type") in family:
                        pending.pop(event["source_component"], None)

        stamp = utcnow()
        opened = changed = 0
        with self.store._lock:
            active = self._active()
            for key in sorted(set(groups) | set(signals) | set(active)):
                row = active.get(key)
                members = groups.get(key, set())
                current = dict(_load(row["signals"]) or {}) if row else {}
                for component, family in clears:
                    if (current.get(component) or {}).get("event_type") in family:
                        current.pop(component, None)
                for component, event in (signals.get(key) or {}).items():
                    current[component] = {k: event.get(k) for k in (
                        "event_id", "event_type", "timestamp", "reason", "severity")}
                bad = bool(members or current)
                if row is None:
                    if not bad:
                        continue
                    self._open(key, nodes, members, current, stamp, publish)
                    opened += 1
                    continue
                changed += self._advance(row, key, nodes, members, current, bad, stamp, now, publish)
            self.store._c.commit()
        self._relate()
        return {"opened": opened, "changed": changed, "open": len(self._active())}

    def _groups(self, nodes: dict[str, h.Component], now: float) -> dict[str, set[str]]:
        """Unhealthy groups, with a component that is only DEGRADED counted
        once it has stayed so for the grace period (starting up is not an
        incident)."""
        for cid, node in nodes.items():
            if node.effective == h.DEGRADED:
                self._degraded_since.setdefault(cid, now)
            else:
                self._degraded_since.pop(cid, None)
        out: dict[str, set[str]] = {}
        for key, members in roots(nodes).items():
            kept = {cid for cid in members
                    if nodes[cid].effective != h.DEGRADED
                    or now - self._degraded_since.get(cid, now) >= self.degraded_grace_s}
            root = nodes.get(key)
            if root is not None and root.effective == h.DEGRADED and key not in kept:
                continue
            if kept:
                out[key] = kept
        return out

    def _new_events(self) -> list[dict]:
        """The watched engine events stored since the last cycle, oldest
        first. The upper bound is fixed before reading, so an event stored
        meanwhile is read next cycle rather than skipped."""
        mark = int(self.store.meta("incidents.after_seq") or 0)
        top = int(self._rows("SELECT COALESCE(MAX(seq),0) m FROM guardian_events")[0]["m"])
        if top <= mark:
            return []
        kinds = sorted(_WATCHED)
        rows = self._rows(
            "SELECT seq,event_id,timestamp,source_component,event_type,severity,reason FROM guardian_events "
            f"WHERE seq>? AND seq<=? AND event_type IN ({','.join('?' for _ in kinds)}) ORDER BY seq",
            (mark, top, *kinds))
        self.store.set_meta("incidents.after_seq", top)
        return rows

    def _open(self, key, nodes, members, signals, stamp, publish) -> None:
        diagnosis = diagnose(key, nodes, signals)
        started = min([s["timestamp"] for s in signals.values() if s.get("timestamp")]
                      + self._first_bad(members) + [stamp])
        worst = h.worst(nodes[c].effective for c in members if c in nodes) if members else h.DEGRADED
        severity = _SEVERITY.get(worst, "WARNING")
        if any((s.get("severity") or "") == "HIGH" for s in signals.values()):
            severity = "HIGH"
        title = _title(key, nodes, diagnosis)
        cur = self.store._c.execute(
            "INSERT INTO guardian_incidents(key,title,kind,state,severity,root_component,diagnosis,"
            "affected,signals,related,started_at,detected_at,updates,healthy_cycles,last_update_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)",
            (key, title, diagnosis["kind"], OPEN, severity, key, _dump(diagnosis),
             _dump(sorted(members)), _dump(signals), _dump([]), started, stamp, stamp))
        incident_id = int(cur.lastrowid)
        self._log(incident_id, "opened", f"{title} ({diagnosis['confidence']})",
                  {"affected": sorted(members), "signals": signals, "diagnosis": diagnosis})
        publish("incident_opened", source_component=f"incident:{incident_id}", severity=severity,
                state_after=OPEN, reason=title, evidence={"incident_id": incident_id, "key": key,
                                                          "confidence": diagnosis["confidence"],
                                                          "affected": sorted(members)})

    def _first_bad(self, members: set[str]) -> list[str]:
        """When each component's trouble began, from its health_changed
        evidence -- which can precede the cycle that opened the incident."""
        out = []
        for cid in members:
            rows = self.store.events(source_component=cid, event_type="health_changed", limit=1)
            if rows and rows[0].get("state_after") in (h.FAILED, h.BLOCKED, h.DEGRADED):
                out.append(rows[0]["timestamp"])
        return out

    def _advance(self, row, key, nodes, members, signals, bad, stamp, now, publish) -> int:
        incident_id = int(row["id"])
        affected = set(_load(row["affected"]) or [])
        new = sorted(members - affected)
        updates = int(row["updates"])
        fields: dict[str, Any] = {"signals": _dump(signals)}
        changed = 0
        if bad:
            diagnosis = diagnose(key, nodes, signals)
            old = _load(row["diagnosis"]) or {}
            if new:
                affected |= set(new)
                fields["affected"] = _dump(sorted(affected))
                self._log(incident_id, "affected", f"now also affects {', '.join(new)}", {"added": new})
                updates += 1
                changed = 1
            if (old.get("confidence"), old.get("root_cause")) != (diagnosis["confidence"], diagnosis["root_cause"]):
                fields["diagnosis"] = _dump(diagnosis)
                self._log(incident_id, "diagnosis", f"{diagnosis['root_cause']} ({diagnosis['confidence']})",
                          {"before": old, "after": diagnosis})
                updates += 1
                changed = 1
            if row["state"] == RECOVERED:
                fields.update(state=OPEN, recovered_at=None, healthy_cycles=0)
                self._log(incident_id, "reopened", "failed again before recovery was verified",
                          {"members": sorted(members), "signals": signals})
                updates += 1
                changed = 1
                publish("incident_updated", source_component=f"incident:{incident_id}", severity=row["severity"],
                        state_before=RECOVERED, state_after=OPEN, reason=f"reopened: {row['title']}",
                        evidence={"incident_id": incident_id})
            elif changed:
                publish("incident_updated", source_component=f"incident:{incident_id}", severity=row["severity"],
                        state_before=OPEN, state_after=OPEN, reason=row["title"],
                        evidence={"incident_id": incident_id, "added": new})
        elif row["state"] == OPEN:
            fields.update(state=RECOVERED, recovered_at=stamp, healthy_cycles=1)
            self._log(incident_id, "recovered", "every affected component is healthy again",
                      {"affected": sorted(affected)})
            updates += 1
            changed = 1
            publish("incident_recovered", source_component=f"incident:{incident_id}", severity="INFO",
                    state_before=OPEN, state_after=RECOVERED, reason=row["title"],
                    evidence={"incident_id": incident_id})
        else:                                    # RECOVERED and still healthy
            healthy = int(row["healthy_cycles"]) + 1
            fields["healthy_cycles"] = healthy
            recovered_at = row["recovered_at"]
            from datetime import datetime
            age = (datetime.fromisoformat(stamp) - datetime.fromisoformat(recovered_at)).total_seconds()
            if age >= self.verify_s:
                fields.update(state=CLOSED, verified_at=stamp, closed_at=stamp)
                self._log(incident_id, "verified", f"stayed healthy for {int(age)}s ({healthy} observations)",
                          {"healthy_observations": healthy})
                self._log(incident_id, "closed", "recovery verified; the incident is kept as history")
                updates += 1
                changed = 1
                publish("incident_closed", source_component=f"incident:{incident_id}", severity="INFO",
                        state_before=RECOVERED, state_after=CLOSED, reason=row["title"],
                        evidence={"incident_id": incident_id, "verified_after_s": int(age)})
        fields["updates"] = updates
        if changed:
            fields["last_update_at"] = stamp
        sets = ", ".join(f"{k}=?" for k in fields)
        self.store._c.execute(f"UPDATE guardian_incidents SET {sets} WHERE id=?", (*fields.values(), incident_id))
        return changed

    # ------------------------------------------------------- correlation
    def _relate(self) -> None:
        """PRD §22: open incidents that overlap in time are related. Sharing
        an instance or a symbol makes the link PROBABLE; time alone POSSIBLE."""
        active = list(self._active().values())

        def scope(row) -> set[str]:
            parts = {row["key"].split(":", 1)[-1]}
            parts |= {c.split(":", 1)[-1] for c in _load(row["affected"]) or []}
            parts |= {c.split(":", 1)[-1] for c in (_load(row["signals"]) or {})}
            return {p.replace("instance:", "") for p in parts}

        with self.store._lock:
            for row in active:
                related = []
                for other in active:
                    if other["id"] == row["id"]:
                        continue
                    shared = scope(row) & scope(other)
                    related.append({"incident_id": other["id"], "title": other["title"],
                                    "confidence": "PROBABLE" if shared else "POSSIBLE",
                                    "why": (f"both involve {', '.join(sorted(shared))}" if shared
                                            else "open at the same time; no shared component")})
                if _dump(related) != row["related"]:
                    self.store._c.execute("UPDATE guardian_incidents SET related=? WHERE id=?",
                                          (_dump(related), row["id"]))
            self.store._c.commit()

    # -------------------------------------------------------------- read
    def list(self, *, state: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM guardian_incidents"
        args: list = []
        if state == "active":
            sql += " WHERE state IN (?,?)"
            args += [OPEN, RECOVERED]
        elif state:
            sql += " WHERE state=?"
            args.append(state)
        sql += " ORDER BY CASE state WHEN 'OPEN' THEN 0 WHEN 'RECOVERED' THEN 1 ELSE 2 END, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return [self._decode(r) for r in self._rows(sql, args)]

    def get(self, incident_id: int) -> Optional[dict]:
        rows = self._rows("SELECT * FROM guardian_incidents WHERE id=?", (int(incident_id),))
        if not rows:
            return None
        incident = self._decode(rows[0])
        incident["log"] = [{**r, "evidence": _load(r["evidence"])} for r in self._rows(
            "SELECT * FROM guardian_incident_log WHERE incident_id=? ORDER BY seq", (int(incident_id),))]
        incident["timeline"] = self.timeline(incident)
        return incident

    def counts(self) -> dict:
        rows = self._rows("SELECT state, COUNT(*) n FROM guardian_incidents GROUP BY state")
        return {r["state"]: int(r["n"]) for r in rows}

    @staticmethod
    def _decode(row: dict) -> dict:
        out = dict(row)
        for name in ("diagnosis", "affected", "signals", "related"):
            out[name] = _load(out[name])
        return out

    def timeline(self, incident: dict, *, lead_s: int = 600, limit: int = 300) -> list[dict]:
        """PRD §10: the event chain -- what the affected components and their
        upstreams reported from shortly before the start until the close,
        with Guardian's own incident entries in between."""
        from datetime import datetime, timedelta
        start = (datetime.fromisoformat(incident["started_at"]) - timedelta(seconds=lead_s)).isoformat()
        end = incident.get("closed_at") or utcnow()
        components = set(incident["affected"]) | set(incident["signals"] or {}) | {incident["root_component"]}
        for cid in list(components):
            if cid.startswith("instance:"):
                components.add(f"feed:{cid}")
            if cid.startswith("feed:instance:"):
                components.add(cid[len("feed:"):])
        if incident["root_component"].startswith(("htf:", "collector:")):
            components.add(incident["root_component"].split(":", 1)[1])
        marks = ",".join("?" for _ in components)
        rows = self._rows(
            f"SELECT seq,event_id,timestamp,source_component,event_type,severity,state_before,state_after,"
            f"reason FROM guardian_events WHERE source_component IN ({marks}) AND category!='strategy' "
            f"AND timestamp>=? AND timestamp<=? ORDER BY timestamp, seq LIMIT ?",
            (*sorted(components), start, end, int(limit)))
        items = [{"at": r["timestamp"], "source": r["source_component"], "what": r["event_type"],
                  "severity": r["severity"], "state": (f"{r['state_before'] or '—'} → {r['state_after'] or '—'}"
                                                       if r["state_before"] or r["state_after"] else None),
                  "detail": r["reason"], "event_id": r["event_id"]} for r in rows]
        items += [{"at": r["at"], "source": f"incident:{incident['id']}", "what": f"incident {r['entry']}",
                   "severity": "INFO", "state": None, "detail": r["detail"], "event_id": None}
                  for r in incident.get("log") or []]
        return sorted(items, key=lambda i: i["at"])


def publisher(bus) -> Callable[..., None]:
    def publish(event_type: str, **fields) -> None:
        try:
            bus.publish(make_event(event_type, source_service="guardian", **fields))
        except InvalidEvent:
            bus.reject()
    return publish
