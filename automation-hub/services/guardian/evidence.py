"""The evidence pack: what Guardian's reasoning layer is allowed to see
(PRD §6 Phase 6, §27, §43).

The model never gets raw production access -- no database, no ledger, no
credentials. It gets this pack: Guardian's own structured findings, each
with the id it can be cited by, stripped of secrets before it is built. The
pack says what it left out, so an answer can say what it could not see.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from services.redaction import redact

LIMITS = {"incidents": 20, "events": 40, "almost_trades": 10, "hypotheses": 20, "findings": 20,
          "strategies": 20, "actions": 10}


def _cap(name: str, rows: list, omitted: dict) -> list:
    limit = LIMITS[name]
    if len(rows) > limit:
        omitted[name] = len(rows) - limit
    return rows[:limit]


def build(service, *, research=None) -> dict:
    """A bounded, secret-free snapshot of Guardian's findings, with ids."""
    omitted: dict[str, int] = {}
    snap = service.snapshot()
    incidents = service.incidents.list(limit=100)
    strategies = service.strategies(days=7)
    events = service.store.events(min_severity="WARNING", limit=200)
    integrity = getattr(service.integrity, "last", None) or {}
    pack: dict[str, Any] = {
        "generated_at": snap["generated_at"],
        "platform": {"state": snap["summary"]["state"],
                     "components": [{"id": c["id"], "state": c["effective"], "detail": c["detail"],
                                     "blocked_by": c["blocked_by"]} for c in snap["components"]]},
        "incidents": _cap("incidents", [
            {"id": f"incident:{i['id']}", "title": i["title"], "state": i["state"], "severity": i["severity"],
             "kind": i["kind"], "started_at": i["started_at"], "closed_at": i["closed_at"],
             "diagnosis": i["diagnosis"], "affected": i["affected"]} for i in incidents], omitted),
        "anomalies": [{"id": f"anomaly:{a['key']}", **{k: a.get(k) for k in ("detector", "scope", "detail",
                                                                             "baseline", "note")}}
                      for a in snap.get("anomalies") or []],
        "integrity": {"checked_at": integrity.get("at"),
                      "findings": _cap("findings", [
                          {"id": f"integrity:{f['source']}:{f['rule']}:{f['item']}", **{
                              k: f[k] for k in ("rule", "source", "severity", "detail")}}
                          for f in integrity.get("findings", [])], omitted),
                      "paper_open_risk": ((integrity.get("exposure") or {}).get("paper") or {}).get("risk"),
                      "live_positions": ((integrity.get("exposure") or {}).get("live") or {}).get("positions")},
        "strategies": _cap("strategies", [
            {"id": f"strategy:{c['scope']}:{c['strategy_id']}:{c['symbol']}", **{
                k: c[k] for k in ("scope", "strategy_id", "strategy_version", "symbol", "timeframe",
                                  "evaluations", "setups", "entries", "refused", "almost_trades",
                                  "top_rejection_reasons")},
             "performance": c["performance"]} for c in strategies["strategies"]], omitted),
        "almost_trades": _cap("almost_trades", [
            {"id": f"almost:{a['identity']}", **{k: a[k] for k in (
                "strategy_id", "symbol", "timeframe", "direction", "kind", "passed", "evaluated",
                "prevented_by", "sightings", "last_seen")}, "note": a["note"]}
            for a in service.store.almost_trades(limit=50)], omitted),
        "hypotheses": _cap("hypotheses", [
            {"id": f"hypothesis:{h['id']}", **{k: h[k] for k in ("strategy_id", "strategy_version",
                                                                 "hypothesis", "observation", "status", "stage")}}
            for h in (research.list() if research else [])], omitted),
        "recent_warnings": _cap("events", [
            {"id": f"event:{e['event_id']}", **{k: e[k] for k in ("timestamp", "event_type", "severity",
                                                                  "source_component", "reason")}}
            for e in events], omitted),
        "guardian_actions": _cap("actions", [
            {"id": f"action:{a['action_id']}", **{k: a[k] for k in ("at", "action", "policy", "result",
                                                                    "reason")}}
            for a in service.store.actions(50)], omitted),
        "omitted": omitted,
        "boundary": snap["boundary"],
    }
    return redact(pack)


def ids(pack: Any) -> set[str]:
    """Every citable id in the pack."""
    out: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("id"), str):
                out.add(node["id"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(pack)
    return out


def digest(pack: dict) -> str:
    return hashlib.sha256(json.dumps(pack, sort_keys=True, default=str).encode()).hexdigest()
