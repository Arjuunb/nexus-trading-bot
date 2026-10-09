"""Deterministic incident grouping over persisted Guardian events.

This observer can only write Guardian-owned incident tables. It cannot call a
trading worker, broker, risk engine, recovery endpoint, or strategy constructor.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .store import GuardianStore

_SEVERITY = {"INFO": 0, "WATCH": 1, "WARNING": 2, "HIGH": 3, "CRITICAL": 4}

# The SMC probe reads the broker and agent journal in separate snapshots.
# These are investigation candidates, never proof that a broker/journal
# invariant failed at one atomic instant. In particular, an unfilled order is
# normal; a trade journal row that claims that order filled is not.
_SMC_EXECUTION_WARNINGS = frozenset({
    "AGENT_TRADE_PRECEDES_FILL", "JOURNAL_SIZE_EXCEEDS_BROKER_FILL",
    "OPEN_TRADE_POSITION_UNVERIFIED", "BROKER_ORDER_UNRECORDED",
    "ORDER_ID_UNRECORDED", "TRADE_ID_NOT_FOUND",
    "COMPLETE_INTENT_TRADE_UNRECORDED", "EXECUTION_UNCERTAIN",
    "CLOSED_TRADE_POSITION_STILL_OPEN",
})
_SMC_EXECUTION_HIGH = frozenset({
    "FILLED_ORDER_JOURNAL_PENDING", "DUPLICATE_EXECUTION_KEY",
    "ORDER_ID_NOT_FOUND", "ORDER_IDENTITY_MISMATCH",
    "TRADE_IDENTITY_MISMATCH", "ENTRY_MARKED_REDUCE_ONLY",
    "FAILED_INTENT_WITH_ORDER_ID",
    "MULTIPLE_INTENTS_FOR_TRADE", "POSITION_SIDE_MISMATCH",
})
_INSTANCE_LEDGER_HIGH = frozenset({
    "MISSING_STOP", "STOP_GEOMETRY_INVALID", "POSITION_GEOMETRY_INVALID",
    "MULTIPLE_EXECUTION_LINKS", "MULTIPLE_POSITION_LINKS",
    "EXECUTION_OWNER_MISMATCH", "TRADE_OWNER_MISMATCH", "SESSION_MISMATCH",
    "SYMBOL_SIDE_MISMATCH", "SIZE_MISMATCH", "ENTRY_MISMATCH",
    "OPEN_POSITION_TRADE_UNVERIFIED",
})
_INSTANCE_LEDGER_WARNING = frozenset({"PAPER_SOURCE_UNVERIFIED"})


@dataclass(frozen=True)
class IncidentSignal:
    fingerprint: str
    title: str
    root_component: str
    root_cause: str
    confidence: str
    severity: str
    transition: str


def _scope(event: dict, key: str, fallback: str) -> str:
    value = (event.get("metadata") or {}).get(key)
    return value if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,64}", value) else fallback


def classify_incident(event: dict) -> IncidentSignal | None:
    """Classify only explicit operational evidence; no-trade is not an incident."""
    kind = event.get("event_type")
    source = event.get("source_service") or "unknown"
    component = event.get("source_component") or "unknown"
    if kind == "execution_integrity_observed":
        # This exact read-only source is the only producer of this contract.
        # Never permit a generic event to resolve an execution incident.
        evidence = event.get("evidence") or {}
        code = evidence.get("integrity_code")
        key = event.get("execution_id")
        if (source != "guardian_smc_probe" or component != "smc_agent" or
                (event.get("metadata") or {}).get("paper_only") is not True or
                evidence.get("cross_database_atomic") is not False or
                evidence.get("execution_integrity_verified") is not False or
                not isinstance(key, str) or not key or
                evidence.get("execution_key") != key or
                code != event.get("reason")):
            return None
        account = evidence.get("broker_account_id")
        if account is not None and (not isinstance(account, str) or not 1 <= len(account) <= 256):
            return None
        fingerprint = (f"smc_execution_integrity:{quote(account, safe='')}:{quote(key, safe='')}" if account else
                       f"smc_execution_integrity:{key}")
        if code == "CONSISTENT":
            # A later non-atomic healthy-looking read cannot close an incident.
            # Source-authoritative reconciliation must do that explicitly.
            return IncidentSignal(fingerprint, "SMC paper execution integrity",
                                  "smc_agent", "SMC broker and journal appear consistent in a later read; independent verification is still required",
                                  "POSSIBLE", "WARNING", "RECOVERING")
        if code not in _SMC_EXECUTION_WARNINGS | _SMC_EXECUTION_HIGH:
            return None  # PENDING and ORDER_AWAITING_FILL are normal states.
        severity = "HIGH" if code in _SMC_EXECUTION_HIGH else "WARNING"
        return IncidentSignal(fingerprint, "SMC paper execution integrity",
                              "smc_agent", f"SMC paper execution observation: {code}; broker and journal reads are non-atomic",
                              "POSSIBLE", severity, "OPEN")
    if kind == "agent_journal_integrity_observed":
        evidence = event.get("evidence") or {}
        code, trade_id, account = (evidence.get(name) for name in
                                   ("integrity_code", "trade_id", "broker_account_id"))
        keys, count = evidence.get("execution_keys"), evidence.get("matching_intent_count")
        if (source != "guardian_smc_probe" or component != "smc_agent_journal" or
                (event.get("metadata") or {}).get("paper_only") is not True or
                evidence.get("cross_database_atomic") is not False or
                evidence.get("execution_integrity_verified") is not False or
                any(not isinstance(value, str) or not 1 <= len(value) <= 256
                    for value in (trade_id, account)) or
                not isinstance(keys, list) or type(count) is not int or not 0 <= count <= 64 or
                len(keys) != count or any(not isinstance(key, str) or not key for key in keys) or
                len(set(keys)) != count or code != event.get("reason") or
                code != ({0: "JOURNAL_INTENT_NOT_FOUND", 1: "INTENT_LINK_FOUND"}.get(
                    count, "MULTIPLE_INTENTS_FOR_TRADE"))):
            return None
        return IncidentSignal(
            f"smc_journal_link:{quote(account, safe='')}:{quote(trade_id, safe='')}", "SMC open journal execution link",
            "smc_agent_journal", f"SMC open journal observation: {code}; execution truth is not certified",
            "POSSIBLE", "HIGH" if count > 1 else "WARNING",
            "RECOVERING" if count == 1 else "OPEN")
    if kind in {"websocket_disconnected", "stale_candle", "stale_htf_candle",
                "candle_missing", "sequence_gap", "websocket_reconnected",
                "feed_synchronized"}:
        venue = _scope(event, "venue", source)
        scope = _scope(event, "scope", "shared")
        key = f"market_data:{venue}:{scope}"
        if kind == "feed_synchronized":
            verified = (event.get("evidence") or {}).get("closed_candle_continuity_verified") is True
            return IncidentSignal(key, "Market data disruption", "market_data",
                                  ("Public feed and candle continuity verified by source" if verified else
                                   "Feed reports synchronization; closed-candle continuity not verified"),
                                  "CONFIRMED" if verified else "POSSIBLE", "WARNING",
                                  "RECOVERED" if verified else "RECOVERING")
        if kind == "websocket_reconnected":
            return IncidentSignal(key, "Market data disruption", "market_data",
                                  "Transport reconnected; candle continuity is not yet proven",
                                  "CONFIRMED", "WARNING", "RECOVERING")
        confirmed = kind == "websocket_disconnected"
        return IncidentSignal(key, "Market data disruption", "market_data",
                              ("Public websocket disconnected" if confirmed else
                               "Candle/quote freshness failed; upstream cause not proven"),
                              "CONFIRMED" if confirmed else "UNKNOWN",
                              "HIGH" if kind in {"sequence_gap", "stale_htf_candle"} else "WARNING",
                              "OPEN")
    if kind in {"worker_crashed", "worker_restarted", "worker_heartbeat"}:
        key = f"worker:{source}:{component}"
        verified = (event.get("evidence") or {}).get("worker_operational_verified") is True
        transition = {"worker_crashed": "OPEN", "worker_restarted": "RECOVERING",
                      "worker_heartbeat": "RECOVERED" if verified else "RECOVERING"}[kind]
        return IncidentSignal(key, "Worker failure", component,
                              ("Worker crash reported" if kind == "worker_crashed" else
                               "Worker operational state verified" if verified else
                               "Worker restart observed; operation not verified"),
                              "CONFIRMED" if kind == "worker_crashed" or verified else "POSSIBLE",
                              "HIGH", transition)
    if kind in {"journal_failed", "journal_reconciled"}:
        scope = event.get("execution_id") or event.get("session_id") or source
        verified = (event.get("evidence") or {}).get("journal_consistency_verified") is True
        return IncidentSignal(f"journal:{scope}", "Journal persistence failure", "journal",
                              ("Journal write failed" if kind == "journal_failed" else
                               "Journal reconciliation verified by source" if verified else
                               "Journal reconciliation reported; consistency not verified"),
                              "CONFIRMED" if kind == "journal_failed" or verified else "POSSIBLE",
                              "HIGH", "OPEN" if kind == "journal_failed" else
                              "RECOVERED" if verified else "RECOVERING")
    if kind in {"execution_uncertain", "reconciliation_completed"}:
        scope = event.get("execution_id") or event.get("order_id")
        if not scope:
            return None  # Never group unrelated executions under one incident.
        verified = (event.get("evidence") or {}).get("verified") is True
        transition = ("OPEN" if kind == "execution_uncertain" else
                      "RECOVERED" if verified else "RECOVERING")
        return IncidentSignal(f"execution:{scope}", "Execution truth uncertain", "execution",
                              ("Broker outcome not proven" if kind == "execution_uncertain" else
                               "Reconciliation reported by source"),
                              "CONFIRMED" if kind == "execution_uncertain" else
                              "CONFIRMED" if verified else "POSSIBLE", "HIGH", transition)
    return None


def _instance_ledger_signals(event: dict) -> list[IncidentSignal]:
    """Group a source snapshot per instance without mixing paper accounts."""
    evidence = event.get("evidence") or {}
    if (event.get("source_service") != "guardian_instance_ledger_probe" or
            event.get("source_component") != "instance_ledger" or
            event.get("event_type") != "instance_paper_ledger_observed" or
            (event.get("metadata") or {}).get("paper_only") is not True or
            evidence.get("scope") != "INSTANCE_ATTRIBUTED_PAPER_LEDGER_ONLY" or
            evidence.get("broker_fill_verified") is not False or
            evidence.get("live_exposure_verified") is not False or
            type(evidence.get("atomic_snapshot")) is not bool or
            not isinstance(evidence.get("findings"), list) or
            len(evidence["findings"]) > 128):
        return []
    grouped: dict[str, set[str]] = {}
    for finding in evidence["findings"]:
        if not isinstance(finding, dict):
            return []
        owner, codes = finding.get("instance_id"), finding.get("codes")
        if (not isinstance(owner, str) or not 1 <= len(owner) <= 128 or
                not isinstance(codes, list) or
                any(not isinstance(code, str) for code in codes)):
            return []
        grouped.setdefault(owner, set()).update(codes)
    atomic = (evidence["atomic_snapshot"] and
              evidence.get("source_coverage_verified") is True)
    signals = []
    for owner, codes in sorted(grouped.items()):
        significant = codes & (_INSTANCE_LEDGER_HIGH | _INSTANCE_LEDGER_WARNING)
        fingerprint = f"instance_paper_ledger:{owner}"
        if significant:
            signals.append(IncidentSignal(
                fingerprint, "Trading Instance paper ledger integrity", "instance_ledger",
                f"Instance {owner} paper ledger: {', '.join(sorted(significant))}",
                "CONFIRMED" if atomic and significant <= _INSTANCE_LEDGER_HIGH else "POSSIBLE",
                "HIGH" if significant & _INSTANCE_LEDGER_HIGH else "WARNING", "OPEN"))
        elif not codes and atomic:
            # A matching current read is useful progress but cannot certify
            # that a historical execution/journal defect was repaired.
            signals.append(IncidentSignal(
                fingerprint, "Trading Instance paper ledger integrity", "instance_ledger",
                f"Instance {owner} open paper rows match in a later read; repair verification remains required",
                "POSSIBLE", "WARNING", "RECOVERING"))
    return signals


def _lab_paper_signals(event: dict) -> list[IncidentSignal]:
    if event.get("event_type") != "lab_paper_execution_observed":
        return []
    evidence = event.get("evidence") or {}
    component = {"PRICE_ACTION": "pa_paper_execution", "SMC": "smc_paper_execution"}.get(evidence.get("lab"))
    if (not component or event.get("source_component") != component or
            event.get("source_service") != "guardian_" + component + "_probe" or
            evidence.get("atomic_snapshot") is not True or
            (event.get("metadata") or {}).get("paper_only") is not True):
        return []
    account, findings = evidence.get("account_id"), evidence.get("findings")
    if (not isinstance(account, str) or not 1 <= len(account) <= 256 or
            not isinstance(findings, list) or len(findings) > 256):
        return []
    significant = {"ORDER_IDENTITY_MISMATCH", "ORDER_QUANTITY_MISMATCH", "EXIT_NOT_REDUCE_ONLY",
                   "ENTRY_MARKED_REDUCE_ONLY", "FILLED_QUANTITY_MISMATCH",
                   "FILLED_PRICE_MISMATCH", "ORDER_STATUS_QUANTITY_MISMATCH",
                   "FILL_ORDER_IDENTITY_MISMATCH", "DUPLICATE_ORDER_KEY", "POSITION_STOP_UNVERIFIED"}
    grouped = {}
    for row in findings:
        if (not isinstance(row, dict) or not isinstance(row.get("code"), str) or
                row.get("record_type") not in {"order", "position"} or
                not isinstance(row.get("record_id"), str) or not 1 <= len(row["record_id"]) <= 256 or
                row.get("confidence") not in {"CONFIRMED_RECORD_FACT", "UNVERIFIED"}):
            return []
        if row.get("code") in significant:
            grouped.setdefault((row["record_type"], row["record_id"]), []).append(row)
    return [IncidentSignal(
        f"lab_paper:{component}:{account}:{kind}:{identity}",
        "Isolated lab paper execution evidence", component,
        "Paper record observation: " + ", ".join(sorted({row["code"] for row in rows})),
        "CONFIRMED" if all(row["confidence"] == "CONFIRMED_RECORD_FACT" for row in rows) else "POSSIBLE",
        "HIGH" if any(row["confidence"] == "CONFIRMED_RECORD_FACT" for row in rows) else "WARNING", "OPEN")
        for (kind, identity), rows in sorted(grouped.items())]


class GuardianIncidentEngine:
    def __init__(self, store: GuardianStore):
        self.store = store
        with closing(store._connect()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS guardian_incidents (
                    incident_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    title TEXT NOT NULL,
                    root_component TEXT NOT NULL,
                    root_cause TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    state TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_at TEXT,
                    evidence_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS guardian_incident_active
                  ON guardian_incidents(fingerprint)
                  WHERE state IN ('OPEN', 'RECOVERING');
                CREATE TABLE IF NOT EXISTS guardian_incident_updates (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    transition TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    UNIQUE(incident_id, event_id),
                    FOREIGN KEY(incident_id) REFERENCES guardian_incidents(incident_id)
                );
                CREATE TABLE IF NOT EXISTS guardian_analysis_cursor (
                    name TEXT PRIMARY KEY,
                    last_event_sequence INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO guardian_analysis_cursor(name, last_event_sequence)
                VALUES ('incidents_v1', 0);
                CREATE INDEX IF NOT EXISTS guardian_incident_timeline
                  ON guardian_incident_updates(incident_id, sequence);
            """)
            conn.commit()

    def scan(self, *, limit: int = 500) -> int:
        """Consume raw events transactionally; restart/replay cannot duplicate updates."""
        if not 1 <= limit <= 5000:
            raise ValueError("incident scan limit must be between 1 and 5000")
        processed = 0
        with closing(self.store._connect()) as conn:
            while processed < limit:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cursor = conn.execute(
                        "SELECT last_event_sequence FROM guardian_analysis_cursor WHERE name='incidents_v1'")
                    last_sequence = int(cursor.fetchone()[0])
                    row = conn.execute(
                        "SELECT sequence, event_id, received_at, payload_json FROM events "
                        "WHERE sequence > ? ORDER BY sequence LIMIT 1", (last_sequence,)).fetchone()
                    if row is None:
                        conn.commit()
                        break
                    event = json.loads(row["payload_json"])
                    signals = _instance_ledger_signals(event) + _lab_paper_signals(event)
                    signal = classify_incident(event)
                    if signal is not None:
                        signals.append(signal)
                    for signal in signals:
                        self._apply(conn, event, row["received_at"], signal)
                    conn.execute(
                        "UPDATE guardian_analysis_cursor SET last_event_sequence=? "
                        "WHERE name='incidents_v1'", (row["sequence"],))
                    conn.commit()
                    processed += 1
                except Exception:
                    conn.rollback()
                    raise
        return processed

    @staticmethod
    def _apply(conn: sqlite3.Connection, event: dict, received_at: str,
               signal: IncidentSignal) -> None:
        existing = conn.execute(
            "SELECT * FROM guardian_incidents WHERE fingerprint=? "
            "AND state IN ('OPEN', 'RECOVERING')", (signal.fingerprint,)).fetchone()
        if existing is None and signal.transition != "OPEN":
            return  # A recovery claim without an observed failure proves no incident.
        if existing is None:
            incident_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO guardian_incidents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (incident_id, signal.fingerprint, signal.title, signal.root_component,
                 signal.root_cause, signal.confidence, signal.severity, "OPEN",
                 received_at, received_at, None))
            current_severity = signal.severity
            current_confidence = signal.confidence
        else:
            incident_id = existing["incident_id"]
            current_severity = max((existing["severity"], signal.severity),
                                   key=lambda item: _SEVERITY[item])
            current_confidence = ("CONFIRMED" if "CONFIRMED" in
                                  (existing["confidence"], signal.confidence) else
                                  signal.confidence)
        root_cause = (existing["root_cause"] if existing is not None and
                      (existing["confidence"] == "CONFIRMED" or signal.transition != "OPEN")
                      else signal.root_cause)
        state = signal.transition
        # Fresh failure evidence re-opens an in-progress recovery, never the
        # separately closed historical incident.
        if signal.transition == "OPEN":
            state = "OPEN"
        conn.execute(
            """UPDATE guardian_incidents SET last_seen_at=?, root_cause=?, confidence=?,
               severity=?, state=?, resolved_at=?, evidence_count=evidence_count+1
               WHERE incident_id=?""",
            (received_at, root_cause, current_confidence, current_severity,
             state, received_at if state == "RECOVERED" else None, incident_id))
        conn.execute(
            """INSERT INTO guardian_incident_updates
               (incident_id, event_id, observed_at, transition, summary)
               VALUES (?, ?, ?, ?, ?)""",
            (incident_id, event["event_id"], received_at, signal.transition,
             signal.root_cause))

    def list(self, *, limit: int = 50, state: str | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500 or state not in (None, "OPEN", "RECOVERING", "RECOVERED"):
            raise ValueError("invalid incident query")
        with closing(self.store._connect()) as conn:
            if state:
                rows = conn.execute(
                    "SELECT * FROM guardian_incidents WHERE state=? "
                    "ORDER BY last_seen_at DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM guardian_incidents ORDER BY last_seen_at DESC LIMIT ?",
                    (limit,)).fetchall()
        return [dict(row) for row in rows]

    def active_summary(self) -> dict[str, int]:
        """Count all unresolved incidents, not only the visible recent page."""
        with closing(self.store._connect()) as conn:
            rows = conn.execute(
                "SELECT severity, COUNT(*) AS total FROM guardian_incidents "
                "WHERE state IN ('OPEN','RECOVERING') GROUP BY severity"
            ).fetchall()
        counts = {row["severity"]: int(row["total"]) for row in rows}
        return {"total": sum(counts.values()),
                "warning_or_higher": sum(counts.get(level, 0) for level in
                                          ("WARNING", "HIGH", "CRITICAL")),
                "high_or_critical": counts.get("HIGH", 0) + counts.get("CRITICAL", 0)}

    def timeline(self, incident_id: str) -> list[dict[str, Any]]:
        with closing(self.store._connect()) as conn:
            rows = conn.execute(
                "SELECT event_id, observed_at, transition, summary "
                "FROM guardian_incident_updates WHERE incident_id=? ORDER BY sequence",
                (incident_id,)).fetchall()
        return [dict(row) for row in rows]

    def get(self, incident_id: str) -> dict[str, Any] | None:
        with closing(self.store._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM guardian_incidents WHERE incident_id=?", (incident_id,)).fetchone()
        return dict(row) if row is not None else None
