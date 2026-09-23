"""Read-side SMC snapshots. Never evaluate strategies or authorize orders here."""
from copy import deepcopy
from threading import Lock


def current_health(cached: dict, raw: dict) -> dict:
    """Live transport owns timestamps; reconciliation failures remain vetoes."""
    result = {**cached, **raw}
    dependency = cached.get("failing_dependency")
    # A connected transport alone cannot clear a failed runtime/reconciliation.
    preserve = (dependency and not dependency.startswith("BINANCE_USDM_")) or (
        cached.get("state") == "ERROR" and not dependency) or (
        cached.get("state") == "DISCONNECTED" and not cached.get("last_candle_update"))
    if raw.get("reliable") and preserve:
        for key in ("state", "health_reason", "failing_dependency"):
            result[key] = cached.get(key)
        result.update(reliable=False, new_entries_paused=True)
    result["connection_state"] = result.get("state", "DISCONNECTED")
    return result


def session_identity(session):
    return tuple(session.get(key) for key in ("id", "symbol", "timeframe", "model_id"))


class SMCLabDisplay:
    def __init__(self):
        self._lock = Lock()
        self._snapshot = None

    def publish(self, session, visual):
        snapshot = (session_identity(session), deepcopy(visual))
        with self._lock:
            self._snapshot = snapshot

    def read(self, session):
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None or snapshot[0] != session_identity(session):
            return None
        return deepcopy(snapshot[1])
