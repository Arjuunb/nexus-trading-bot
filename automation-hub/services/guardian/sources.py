"""Read-only adapters from live platform objects to plain rows.

Guardian itself never holds a trading object: the app builds these callables
over its instance managers and lab runtimes and hands Guardian only the
callables (PRD §4, §43). Each reads in-memory state -- no remote database
call -- so asking every few seconds costs the platform nothing, including
where the ledger is Supabase.
"""
from __future__ import annotations

from typing import Any, Callable, Optional


def instance_rows(manager, *, lab_id: Optional[str] = None) -> list[dict]:
    """Every instance its owner wants running, with its worker and feed state."""
    rows = []
    runtimes = getattr(manager, "_runtime", {}) or {}
    for inst in list((getattr(manager, "_instances", {}) or {}).values()):
        if not getattr(inst, "desired_running", False):
            continue
        runtime = runtimes.get(inst.id)
        engine = runtime[0] if runtime else None
        try:
            alive = bool(manager.worker_alive(inst.id))
        except Exception:  # noqa: BLE001 -- unknown liveness is reported as such
            alive = None
        rows.append({
            "id": inst.id, "lab_id": lab_id,
            "symbol": getattr(inst, "symbol", None), "timeframe": getattr(inst, "timeframe", None),
            "strategy_id": getattr(inst, "strategy_key", None),
            "strategy_version": getattr(inst, "strategy_version", None),
            "strategy_label": getattr(inst, "strategy_label", None),
            "mode": getattr(inst, "mode", None),
            "live_feed": getattr(inst, "mode", None) == "trading",
            "state": getattr(inst, "state", None),
            "paused": getattr(inst, "state", None) == "paused",
            "alive": alive,
            "lifecycle_state": getattr(engine, "lifecycle_state", None),
            "market_data_status": getattr(engine, "market_data_status", None),
            "last_heartbeat": getattr(engine, "last_heartbeat", None),
            "last_error": getattr(engine, "last_error", None) or getattr(inst, "last_error", None),
        })
    return rows


def lab_row(lab_id: str, label: str, runtime, *, session: Optional[Callable[[], Any]] = None) -> dict:
    """One lab: is its worker thread alive, is a session active, and what does
    its market-data stream say about itself."""
    thread = getattr(runtime, "_thread", None) or getattr(runtime, "_supervisor_thread", None)
    try:
        active = bool(session()) if session is not None else None
    except Exception:  # noqa: BLE001
        active = None
    stream = getattr(runtime, "stream", None)
    status = None
    if stream is not None and callable(getattr(stream, "status", None)):
        try:
            status = stream.status()
        except Exception as exc:  # noqa: BLE001 -- a stream that cannot report is itself evidence
            status = {"state": "ERROR", "health_reason": f"status unavailable: {type(exc).__name__}"}
    return {
        "id": lab_id, "label": label,
        "thread_alive": thread.is_alive() if thread is not None else None,
        "session_active": active,
        "stream": {k: (status or {}).get(k) for k in (
            "state", "transport_state", "health_reason", "reliable", "failing_dependency",
            "symbol", "timeframe", "last_closed_update", "candle_age_seconds")} if status else None,
    }


def journal_row(recorder) -> dict:
    """The journal recorder's own account of its last pass, including any
    ledger it had to skip (a skipped ledger is a ledger whose trades are not
    being journalled)."""
    status = recorder.status()
    report = getattr(recorder, "last_report", None) or {}
    skipped = [f"{row.get('source')}: {row.get('skipped')}"
               for row in report.get("ledgers", []) if isinstance(row, dict) and row.get("skipped")]
    return {"running": bool(status.get("running")), "passes": int(status.get("passes") or 0),
            "last_error": status.get("last_error"), "last_pass_at": report.get("at"),
            "skipped": skipped}
