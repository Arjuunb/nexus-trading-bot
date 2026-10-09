"""Read the latest received paper-pair evidence with explicit freshness."""
from __future__ import annotations

from datetime import datetime

from .health import component_health
from .store import GuardianStore


def instance_ledger_view(store: GuardianStore, *, now: datetime | None = None) -> dict:
    event = store.observed_snapshot("instance_ledger")
    health = component_health(store.heartbeats(),
                              ("guardian_instance_ledger_probe",), now=now)
    probe = health["components"]["guardian_instance_ledger_probe"]
    current = bool(event) and probe["state"] == "HEALTHY"
    evidence = (event or {}).get("evidence") or {}
    return {
        "scope": "INSTANCE_ATTRIBUTED_PAPER_LEDGER_ONLY",
        "observation_state": "CURRENT" if current else "UNKNOWN",
        "observation_age_seconds": probe.get("age_seconds"),
        "probe_state": probe["state"],
        "probe_reason": probe["reason"],
        "event_id": (event or {}).get("event_id"),
        "material_evidence_at": (event or {}).get("timestamp"),
        "snapshot_atomic": evidence.get("atomic_snapshot") is True,
        "source_coverage_verified": evidence.get("source_coverage_verified") is True,
        "broker_fills_verified": False,
        "live_exposure_verified": False,
        "currency_verified": False,
        "global_risk_amount": None,
        # Keep cached source rows visible for investigation, explicitly marked
        # historical. They never become a current or global risk assertion.
        "instances": [
            {**row, "risk_amount": row.get("risk_amount") if current else None,
             "risk_complete": current and row.get("risk_complete") is True}
            for row in evidence.get("instances", [])
        ],
        "findings": evidence.get("findings", []),
    }
