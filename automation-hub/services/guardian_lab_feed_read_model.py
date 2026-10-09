"""Bounded source-authoritative lab feed probe for the independent Guardian.

Only a saved session row and the stream's local status snapshot are read. No
strategy, provider, broker, account state, or journal operation runs here.
"""
from __future__ import annotations

import sqlite3
import math
from contextlib import closing
from pathlib import Path

_TABLES = {"PRICE_ACTION": "pa_sessions", "SMC": "smc_sessions"}


def lab_feed_snapshot(path: str | Path, lab: str, stream, *,
                      reconciled: dict | None = None) -> dict:
    if lab not in _TABLES:
        raise ValueError("invalid Guardian lab feed")
    source = Path(path)
    if not source.is_file():
        raise sqlite3.OperationalError("lab evidence database is unavailable")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro",
                                 uri=True, timeout=0.25)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=250")
        connection.execute("BEGIN")
        session = connection.execute(
            f"SELECT id,symbol,timeframe,mode,operating_mode FROM {_TABLES[lab]} "
            "WHERE status='active' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    if session is None:
        return {"lab": lab, "component_state": "UNKNOWN", "reason": "NO_ACTIVE_SESSION",
                "session_id": None, "feed_state": "UNKNOWN", "reliable": False,
                "paper_only": True, "execution_health_verified": False}
    base = {"lab": lab, "session_id": session["id"],
            "symbol": session["symbol"], "timeframe": session["timeframe"],
            "operating_mode": session["operating_mode"], "mode": session["mode"],
            "paper_only": True, "execution_health_verified": False}
    if session["mode"] != "LIVE_PAPER":
        return {**base, "component_state": "UNKNOWN", "reason": "SESSION_NOT_LIVE_PAPER",
                "feed_state": "UNKNOWN", "reliable": False}
    if stream is None:
        return {**base, "component_state": "UNKNOWN", "reason": "STREAM_NOT_ATTACHED",
                "feed_state": "UNKNOWN", "reliable": False}
    status = stream.status()
    if not isinstance(status, dict) or not isinstance(status.get("state"), str):
        raise ValueError("stream status contract is invalid")
    same_market = (status.get("symbol"), status.get("timeframe")) == (
        session["symbol"], session["timeframe"])
    closed_age = status.get("closed_candle_age_seconds")
    thresholds = status.get("freshness_thresholds_seconds")
    closed_limit = thresholds.get("completed_candle") if isinstance(thresholds, dict) else None
    closed_fresh = (type(closed_age) in (int, float) and
                    type(closed_limit) in (int, float) and
                    math.isfinite(closed_age) and math.isfinite(closed_limit) and
                    0 <= closed_age <= closed_limit and closed_limit > 0)
    synchronized = (same_market and
                    status.get("state") == "SYNCHRONIZED" and
                    status.get("reliable") is True and
                    closed_fresh and
                    status.get("reconciliation_complete") is True and
                    status.get("unresolved_missing_candles") == 0 and
                    isinstance(status.get("last_closed_update"), str) and
                    bool(status["last_closed_update"]))
    if lab == "SMC":
        # The SMC chart must also agree with the stream's completed-candle
        # history. A transport-only green status cannot prove that identity.
        synchronized = synchronized and isinstance(reconciled, dict) and \
            reconciled.get("reliable") is True and \
            reconciled.get("state") == "SYNCHRONIZED" and \
            reconciled.get("last_closed_update") == status.get("last_closed_update")
    reason = ("FEED_RECONCILED" if synchronized else
              "STREAM_IDENTITY_MISMATCH" if not same_market else
              "CHART_RECONCILIATION_UNVERIFIED" if lab == "SMC" and
              status.get("state") == "SYNCHRONIZED" and status.get("reliable") is True else
              "FEED_NOT_SYNCHRONIZED")
    return {
        **base, "component_state": "HEALTHY" if synchronized else "BLOCKED",
        "reason": reason, "feed_state": status["state"],
        "reliable": bool(synchronized),
        "last_closed_update": status.get("last_closed_update"),
        "last_update": status.get("last_update"),
        "closed_candle_age_seconds": closed_age if type(closed_age) in (int, float)
            and math.isfinite(closed_age) else None,
        "closed_candle_freshness_limit_seconds": closed_limit if
            type(closed_limit) in (int, float) and math.isfinite(closed_limit) else None,
        "failing_dependency": status.get("failing_dependency")
            if isinstance(status.get("failing_dependency"), str) else None,
    }
