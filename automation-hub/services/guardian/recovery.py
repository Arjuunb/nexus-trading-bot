"""Controlled recovery (PRD §39, §40; Phase 7).

Guardian may take a small, fixed set of operational actions -- never a
trading one. Each action:

* is on an allow-list and does one operational thing;
* runs automatically only when the owner has enabled its policy
  (``HUB_GUARDIAN_RECOVERY``, comma-separated; nothing is enabled by default);
* needs evidence: an open incident of the matching kind whose diagnosis is
  CONFIRMED or HIGH CONFIDENCE;
* is rate-limited and has a cooldown per target;
* is recorded in ``guardian_actions`` (append-only) with its reason, policy,
  result and evidence -- including when it was *not* taken and why.

What recovery can never do (PRD §4): change a strategy, a risk limit, an
order, a position or the paper/live mode. The actions are handed to Guardian
as callables by the app; Guardian holds nothing else. A restart goes through
the instance manager's own staged reboot, whose validation and fail-closed
checks stay authoritative (§40).
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from services.redaction import redact

#: action -> what it does, and whether it is ever automatic
ACTIONS = {
    "GATHER_DIAGNOSTICS": {"auto": True, "effect": "none: reads and records the evidence Guardian has"},
    "RESTART_INSTANCE_WORKER": {"auto": False, "effect": "the instance manager's staged reboot of one "
                                                         "instance its owner wants running"},
}
_TRUSTED = ("CONFIRMED", "HIGH CONFIDENCE")


class RecoveryController:
    def __init__(self, store, *, restart_instance: Optional[Callable[[str], object]] = None,
                 enabled: Optional[set[str]] = None, max_per_hour: int = 3, cooldown_s: float = 900.0,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.handlers: dict[str, Callable[..., object]] = {"GATHER_DIAGNOSTICS": self._diagnostics}
        if restart_instance is not None:
            self.handlers["RESTART_INSTANCE_WORKER"] = restart_instance
        self.enabled = {a for a in (enabled or set()) if a in ACTIONS} | {"GATHER_DIAGNOSTICS"}
        self.max_per_hour = int(max_per_hour)
        self.cooldown_s = float(cooldown_s)
        self.clock = clock

    # ------------------------------------------------------------ helpers
    def _recent(self, action: str, target: str) -> tuple[int, Optional[float]]:
        state = self.store.meta("recovery.history") or []
        now = self.clock()
        hour = [h for h in state if h["action"] == action and now - h["at"] < 3600]
        last = max((h["at"] for h in state if h["action"] == action and h["target"] == target), default=None)
        return len(hour), last

    def _remember(self, action: str, target: str) -> None:
        now = self.clock()
        state = [h for h in (self.store.meta("recovery.history") or []) if now - h["at"] < 86_400]
        state.append({"action": action, "target": target, "at": now})
        self.store.set_meta("recovery.history", state)

    def _diagnostics(self, incident: dict) -> dict:
        events = self.store.events(limit=50, min_severity="WARNING")
        return {"incident": {k: incident.get(k) for k in ("id", "key", "title", "state", "diagnosis",
                                                          "affected", "signals")},
                "components": {cid: {k: c.get(k) for k in ("effective", "raw", "detail", "blocked_by")}
                               for cid, c in self.store.components().items()},
                "recent_warnings": [{k: e.get(k) for k in ("timestamp", "event_type", "source_component",
                                                           "severity", "reason")} for e in events]}

    # ------------------------------------------------------------- decide
    def plan(self, incident: dict, nodes: dict) -> list[tuple[str, str]]:
        """(action, target) that would address this incident, from its
        evidence alone. Upstream problems outside the platform get none."""
        key = incident["key"]
        confidence = (incident.get("diagnosis") or {}).get("confidence")
        steps = [("GATHER_DIAGNOSTICS", key)] if incident.get("severity") in ("HIGH", "CRITICAL") else []
        if confidence not in _TRUSTED:
            return steps
        node = nodes.get(key)
        if key.startswith("instance:") and node is not None and node.facts.get("alive") is False:
            steps.append(("RESTART_INSTANCE_WORKER", key.split(":", 1)[1]))
        return steps

    def execute(self, action: str, target: str, *, reason: str, policy: str, evidence: dict,
                incident: Optional[dict] = None, publish: Optional[Callable[..., None]] = None) -> dict:
        """Run one allow-listed action and record it, whatever happens."""
        if action not in ACTIONS or action not in self.handlers:
            result, detail = "REFUSED", "not an allow-listed action Guardian has been given"
        else:
            count, last = self._recent(action, target)
            if count >= self.max_per_hour and action != "GATHER_DIAGNOSTICS":
                result, detail = "SKIPPED_RATE_LIMIT", f"{count} {action} in the last hour"
            elif last is not None and self.clock() - last < self.cooldown_s and action != "GATHER_DIAGNOSTICS":
                result, detail = "SKIPPED_COOLDOWN", f"last {action} on {target} {int(self.clock() - last)}s ago"
            else:
                try:
                    out = self.handlers[action](incident if action == "GATHER_DIAGNOSTICS" else target)
                    result, detail = "SUCCESS", out if isinstance(out, dict) else {"returned": str(out)[:200]}
                    self._remember(action, target)
                except Exception as exc:  # noqa: BLE001 -- a failed action is recorded, never raised
                    result, detail = "FAILED", f"{type(exc).__name__}: {exc}"[:300]
        row = self.store.record_action(action, reason=reason, policy=policy, result=result,
                                       evidence=redact({**evidence, "target": target, "detail": detail}))
        if publish is not None and action == "RESTART_INSTANCE_WORKER" and result == "SUCCESS":
            publish("worker_restarted", source_component=f"instance:{target}", severity="INFO",
                    instance_id=target, reason=f"Guardian requested a staged reboot ({policy})",
                    evidence={"action_id": row["action_id"]})
        return row

    def cycle(self, incidents: list[dict], nodes: dict, *, publish: Callable[..., None]) -> list[dict]:
        """For each open incident: gather diagnostics once, and take a
        recovery action only where its policy is enabled. A recovery the
        evidence supports but the owner has not enabled is recorded as
        recommended, once, and not taken."""
        taken = []
        considered = list(self.store.meta("recovery.considered") or [])
        done = set(considered)
        for incident in incidents:
            if incident["state"] != "OPEN":
                continue
            for action, target in self.plan(incident, nodes):
                mark = f"{incident['id']}:{action}:{target}"
                if mark in done:
                    continue
                done.add(mark)
                considered.append(mark)
                evidence = {"incident_id": incident["id"], "diagnosis": incident.get("diagnosis")}
                reason = f"incident #{incident['id']}: {incident['title']}"
                if action in self.enabled:
                    taken.append(self.execute(action, target, reason=reason, evidence=evidence,
                                              policy=f"AUTO_{action}", incident=incident, publish=publish))
                else:
                    taken.append(self.store.record_action(
                        action, reason=reason, policy=f"AUTO_{action}", result="NOT_TAKEN_POLICY_DISABLED",
                        evidence={**evidence, "target": target,
                                  "detail": "the evidence supports this action but its policy is not enabled"}))
        self.store.set_meta("recovery.considered", considered[-500:])   # oldest dropped first
        return taken

    def status(self) -> dict:
        return {"actions": {name: {**spec, "enabled": name in self.enabled, "available": name in self.handlers}
                            for name, spec in ACTIONS.items()},
                "max_per_hour": self.max_per_hour, "cooldown_s": self.cooldown_s,
                "never": ["strategy rules or parameters", "risk limits or leverage", "orders or positions",
                          "paper to live", "credentials", "history, journals or incidents", "deployments"]}
