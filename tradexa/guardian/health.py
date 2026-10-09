"""Evidence-backed component health; unknown is never displayed as healthy."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Sequence


def component_health(heartbeats: Mapping[str, dict], required: Sequence[str], *,
                     now: datetime | None = None, stale_after_s: float = 90) -> dict:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None or stale_after_s <= 0:
        raise ValueError("health requires an aware clock and positive staleness bound")
    moment = moment.astimezone(timezone.utc)
    states = {}
    for name in required:
        row = heartbeats.get(name)
        if row is None:
            states[name] = {"state": "UNKNOWN", "reason": "no heartbeat observed"}
            continue
        try:
            observed = datetime.fromisoformat(row["observed_at"])
            if observed.tzinfo is None or observed.utcoffset() is None:
                raise ValueError("heartbeat has no timezone")
            age = (moment - observed.astimezone(timezone.utc)).total_seconds()
            if age < -5 or age > stale_after_s:
                states[name] = {"state": "UNKNOWN", "reason": "heartbeat is stale or ahead of clock"}
                continue
            state = str(row["state"])
            if state not in ("HEALTHY", "DEGRADED", "BLOCKED", "FAILED", "UNKNOWN"):
                raise ValueError("unknown health state")
            states[name] = {"state": state, "reason": str(row.get("reason") or ""),
                            "age_seconds": round(max(0, age), 2)}
        except (KeyError, TypeError, ValueError):
            states[name] = {"state": "UNKNOWN", "reason": "invalid heartbeat evidence"}
    priority = ("FAILED", "BLOCKED", "DEGRADED", "UNKNOWN", "HEALTHY")
    overall = next((state for state in priority if any(
        row["state"] == state for row in states.values())), "UNKNOWN")
    return {"state": overall, "components": states,
            "observed_at": moment.isoformat(), "evidence_complete": bool(states) and
            all(row["state"] != "UNKNOWN" for row in states.values())}
