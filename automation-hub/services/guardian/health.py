"""Component health, derived through the dependency graph (PRD §19).

Each component reports only what it knows about itself (its ``raw`` state).
Guardian then resolves the graph: a component whose upstream is FAILED or
BLOCKED is BLOCKED, and names what blocks it -- so a lab whose feed has gone
stale reads "BLOCKED by its feed", never "the lab is broken". Only a
component's own failure makes it FAILED.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

HEALTHY, DEGRADED, BLOCKED, FAILED, UNKNOWN = "HEALTHY", "DEGRADED", "BLOCKED", "FAILED", "UNKNOWN"
#: Headline order: the worst state anywhere is the platform's state.
_WORST_FIRST = (FAILED, BLOCKED, DEGRADED, UNKNOWN, HEALTHY)
_RANK = {state: rank for rank, state in enumerate(reversed(_WORST_FIRST))}


@dataclass
class Component:
    id: str
    label: str
    kind: str                       # upstream | feed | instance | lab | database | journal | guardian
    raw: str = UNKNOWN
    detail: str = ""
    depends_on: tuple[str, ...] = ()
    observed_at: Optional[str] = None
    facts: dict = field(default_factory=dict)       # what the state was read from
    effective: str = UNKNOWN
    blocked_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["depends_on"] = list(self.depends_on)
        return data


def worst(states: Iterable[str]) -> str:
    states = list(states)
    for state in _WORST_FIRST:
        if state in states:
            return state
    return UNKNOWN


def derive(components: dict[str, Component]) -> dict[str, Component]:
    """Resolve effective states in dependency order. A missing upstream is
    treated as UNKNOWN, and a cycle cannot loop: each node resolves once."""
    resolved: dict[str, str] = {}

    def resolve(cid: str, trail: tuple[str, ...] = ()) -> str:
        if cid in resolved:
            return resolved[cid]
        node = components.get(cid)
        if node is None or cid in trail:
            return UNKNOWN
        upstream = {dep: resolve(dep, trail + (cid,)) for dep in node.depends_on}
        blockers = sorted(dep for dep, state in upstream.items() if state in (FAILED, BLOCKED))
        if node.raw == FAILED:
            state = FAILED                  # its own failure outranks anything upstream
        elif blockers:
            state = BLOCKED
        else:
            state = node.raw
        node.effective = state
        node.blocked_by = blockers if state == BLOCKED else []
        resolved[cid] = state
        return state

    for cid in components:
        resolve(cid)
    return components


def summarize(components: dict[str, Component]) -> dict:
    """The Command Center headline: the platform's state and each group's."""
    groups: dict[str, list[Component]] = {}
    for node in components.values():
        groups.setdefault(node.kind, []).append(node)

    def group(kind: str) -> Optional[dict]:
        nodes = groups.get(kind) or []
        if not nodes:
            return None
        return {"state": worst(n.effective for n in nodes), "total": len(nodes),
                "healthy": sum(1 for n in nodes if n.effective == HEALTHY)}

    return {
        "state": worst(n.effective for n in components.values()) if components else UNKNOWN,
        "groups": {kind: group(kind) for kind in
                   ("upstream", "feed", "instance", "lab", "database", "journal", "guardian")},
        "counts": {state: sum(1 for n in components.values() if n.effective == state)
                   for state in _WORST_FIRST},
    }


# --------------------------------------------------------------- mappings
# Each maps a source's own vocabulary onto the five states. Anything a source
# reports that is not listed here is UNKNOWN: Guardian does not guess.

def instance_feed_state(market_data_status: str) -> tuple[str, str]:
    status = (market_data_status or "").lower()
    return {
        "healthy": (HEALTHY, "closed candles arriving on time"),
        "warming_up": (DEGRADED, "warming up; candles not yet verified current"),
        "stale": (FAILED, "candles are late; the worker stands down until they catch up"),
        "error": (FAILED, "the market data connection reported an error"),
        "disconnected": (FAILED, "the market data connection is down"),
        "replay": (HEALTHY, "replaying recorded candles (not the live feed)"),
    }.get(status, (UNKNOWN, f"unrecognised market data status {market_data_status!r}"))


def instance_worker_state(alive: bool, lifecycle_state: str, paused: bool) -> tuple[str, str]:
    lifecycle = (lifecycle_state or "").lower()
    if not alive:
        return FAILED, "the worker is not running though the instance should be"
    if lifecycle == "error":
        return FAILED, "the worker stopped on an error"
    if paused:
        return HEALTHY, "paused by the owner; new entries closed, positions still managed"
    if lifecycle in ("running", "ready"):
        return HEALTHY, "running"
    if lifecycle in ("starting", "bootstrapping", "warming", "syncing"):
        return DEGRADED, f"starting ({lifecycle})"
    if lifecycle in ("data_stale", "recovering"):
        # The worker itself is fine; its feed is not, and the feed node says so.
        return HEALTHY, f"waiting on market data ({lifecycle})"
    return UNKNOWN, f"unrecognised lifecycle state {lifecycle_state!r}"


def lab_stream_state(stream_state: str) -> tuple[str, str]:
    state = (stream_state or "").upper()
    if state == "SYNCHRONIZED":
        return HEALTHY, "candles, quotes and mark price reconciled and fresh"
    if state in ("CONNECTING", "LOADING_HISTORY", "RECONCILING"):
        return DEGRADED, f"starting ({state.lower()})"
    if state in ("STALE_QUOTE", "STALE_MARK", "QUOTE_MISMATCH"):
        return DEGRADED, f"candles fresh but {state.lower().replace('_', ' ')}"
    if state in ("STALE_CANDLES", "DISCONNECTED", "ERROR", "RECONNECTING"):
        return FAILED, state.lower().replace("_", " ")
    return UNKNOWN, f"unrecognised stream state {stream_state!r}"


def status_monitor_state(state: str) -> str:
    return {"operational": HEALTHY, "degraded": DEGRADED,
            "outage": FAILED}.get((state or "").lower(), UNKNOWN)


def upstream_from_feeds(feeds: list[Component]) -> tuple[str, str]:
    """Binance USD-M as seen by every live consumer. It is FAILED only when at
    least two independent consumers are all without data: one dead socket is
    that consumer's problem, and a single consumer cannot tell the two apart."""
    live = [f for f in feeds if f.facts.get("live", True)]
    if not live:
        return UNKNOWN, "no live consumer is reading Binance USD-M"
    failed = sum(1 for f in live if f.raw == FAILED)
    healthy = sum(1 for f in live if f.raw == HEALTHY)
    if failed == len(live) and len(live) >= 2:
        return FAILED, f"none of the {len(live)} live consumers is receiving data"
    if failed == len(live):
        return DEGRADED, ("the only live consumer is not receiving data; "
                          "not enough evidence to tell Binance from that consumer's connection")
    if healthy == len(live):
        return HEALTHY, f"all {len(live)} live consumers receiving data"
    return DEGRADED, f"{healthy} of {len(live)} live consumers receiving data"
