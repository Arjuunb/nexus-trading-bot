"""Declared operating dependencies, never a trading gate or strategy diagnosis.

A fresh probe heartbeat proves the probe answered, not the component it reads.
Edges are architectural declarations, not proof of a shared transport/process.
"""
from __future__ import annotations

from datetime import datetime

from .health import component_health

# Upstream -> consumer. Instance venue(s) are deliberately not attached to the
# labs' Binance venue, and PA/SMC brokers/journals are separate nodes.
DEPENDENCIES = {
    "pa_feed": (),
    "smc_feed": (),
    "instance_market_data": (),
    "pa_lab": ("pa_feed",),
    "smc_lab": ("smc_feed",),
    "trading_instances": ("instance_market_data",),
    "pa_paper_execution": ("pa_lab", "pa_paper_journal"),
    "smc_agent": ("smc_lab", "smc_agent_journal"),
    # Automatic Lab paper and independent Agent approval are distinct paths.
    # Do not declare the Agent an authority over the automatic Lab path.
    "smc_paper_execution": ("smc_lab", "smc_paper_journal"),
    "smc_agent_paper_execution": ("smc_agent", "smc_agent_journal"),
    "instance_paper_execution": ("trading_instances", "instance_ledger"),
    "pa_paper_journal": (),
    "smc_paper_journal": (),
    "smc_agent_journal": (),
    "instance_ledger": (),
    "api": (),
    "guardian": (),
}


def dependency_map(heartbeats: dict, required: tuple[str, ...], *,
                   now: datetime | None = None) -> dict:
    """Separate observed component state from inferred dependency readiness.

    BLOCKED means a declared prerequisite is unavailable; it does not assert
    the strategy is defective or that its actual trading gate was changed.
    Missing component evidence remains UNKNOWN even if its parent is healthy.
    """
    names = tuple(sorted(set(DEPENDENCIES) | set(required)))
    observed = component_health(heartbeats, names, now=now)
    nodes: dict[str, dict] = {}

    def visit(name: str, path: tuple[str, ...] = ()) -> dict:
        if name in path:
            raise ValueError("Guardian dependency declaration contains a cycle")
        if name in nodes:
            return nodes[name]
        parents = DEPENDENCIES.get(name, ())
        upstream = [visit(parent, (*path, name)) for parent in parents]
        blocked = sorted({cause for item in upstream for cause in
                          ([item["component"]] if item["observed_state"] in
                           {"FAILED", "BLOCKED"} else item["blocked_by"])})
        unknown = sorted({cause for item in upstream for cause in
                          ([item["component"]] if item["observed_state"] == "UNKNOWN"
                           else item["unknown_dependencies"])})
        own = observed["components"][name]
        if blocked:
            readiness = "BLOCKED_BY_DEPENDENCY"
        elif own["state"] == "UNKNOWN" or unknown:
            readiness = "UNKNOWN"
        elif own["state"] in {"FAILED", "BLOCKED", "DEGRADED"}:
            readiness = "OBSERVED_" + own["state"]
        elif any(item["dependency_readiness"] != "OBSERVED_AVAILABLE" for item in upstream):
            readiness = "DEPENDENCY_DEGRADED"
        else:
            readiness = "OBSERVED_AVAILABLE"
        nodes[name] = {
            "component": name, "observed_state": own["state"],
            "observed_reason": own["reason"], "age_seconds": own.get("age_seconds"),
            "dependency_readiness": readiness, "requires": list(parents),
            "blocked_by": blocked, "unknown_dependencies": unknown,
            "strategy_failure_verified": False, "trading_gate_verified": False,
            "execution_path_selected_verified": False,
        }
        return nodes[name]

    for name in names:
        visit(name)
    required_nodes = [nodes[name] for name in required]
    overall = ("BLOCKED_BY_DEPENDENCY" if any(item["blocked_by"] for item in required_nodes)
               else "UNKNOWN" if not required_nodes or any(
                   item["dependency_readiness"] == "UNKNOWN" for item in required_nodes)
               else "OBSERVED_UNAVAILABLE" if any(
                   item["dependency_readiness"] != "OBSERVED_AVAILABLE" for item in required_nodes)
               else "OBSERVED_AVAILABLE")
    return {
        "schema_version": 1, "scope": "DECLARED_DEPENDENCIES_AND_FRESH_HEARTBEATS",
        "required_dependency_readiness": overall, "observed_at": observed["observed_at"],
        "nodes": [nodes[name] for name in names],
        "edges": [{"upstream": parent, "consumer": name,
                   "basis": "DECLARED_NOT_RUNTIME_VERIFIED"}
                  for name in names for parent in DEPENDENCIES.get(name, ())],
        "runtime_topology_verified": False, "global_trading_health_verified": False,
        "limitations": ["Probe health is not inherited by trading components.",
                        "A dependency block is not proof of a strategy defect.",
                        "PA, SMC and instance venues/accounts remain isolated."],
    }
