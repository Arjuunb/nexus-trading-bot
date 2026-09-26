"""Public representations shared by the /v1 API and outbound webhooks, so a
webhook's ``data`` and the API's response for the same object are identical."""
from __future__ import annotations


def decision(d: dict) -> dict:
    return {
        "id": f"dec_{d['id']}", "ts": d.get("ts"), "symbol": d.get("symbol"),
        "timeframe": d.get("timeframe"), "strategy": d.get("strategy"), "side": d.get("side"),
        "regime": d.get("regime"), "verdict": d.get("decision"),
        "quality_score": d.get("setup_quality_score"),
        "blocked_by": d.get("blocker") or d.get("gate_stage") or None,
        "reason": d.get("reason"), "rules_passed": d.get("passed_rules") or [],
        "rules_failed": d.get("failed_rules") or [], "executed": bool(d.get("executed")),
        "instance_id": d.get("instance_id") or None, "components": d.get("components") or {},
    }
