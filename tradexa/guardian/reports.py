"""Closed-UTC-window reports of *received evidence*, not invented trading P&L.

Reports have immutable, content-addressed revisions. Late source evidence can
create a new revision without rewriting the earlier report. No trading source
or model is invoked. A bounded read finishes before the short write transaction.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .decision_traces import _near_valid
from .events import GuardianEvent
from .store import GuardianStore

_LAB_SOURCES = {"guardian_lab_probe", "guardian_lab_backfill", "guardian_lab_lifecycle"}
_LAB_TYPES = {"lab_evaluation_observed", "lab_evaluation_backfilled", "lab_lifecycle_observed"}
_INSTANCE_SOURCE = "guardian_instance_decisions"
_SELF_SOURCES = ("guardian_reports", "guardian_notifications")
_SCAN_LIMIT = 10000
_SCAN_BYTES = 8 * 1024 * 1024


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("report clock must include a timezone")
    return value.astimezone(timezone.utc)


def _window(kind: str, start: datetime, now: datetime) -> tuple[datetime, datetime]:
    start, now = _utc(start), _utc(now)
    if kind not in ("DAILY", "WEEKLY") or start != start.replace(
            hour=0, minute=0, second=0, microsecond=0):
        raise ValueError("report requires a UTC-midnight daily or weekly window")
    if kind == "WEEKLY" and start.weekday() != 0:
        raise ValueError("weekly report must start on Monday UTC")
    end = start + timedelta(days=1 if kind == "DAILY" else 7)
    if end > now:
        raise ValueError("a forming report window cannot be finalized")
    return start, end


def _decision(event: dict) -> tuple[tuple, tuple] | None:
    """Source identity and rank; polling/backfill do not double-count decisions."""
    source, evidence = event.get("source_service"), event.get("evidence") or {}
    if source in _LAB_SOURCES and event.get("event_type") in _LAB_TYPES:
        identity = (event.get("lab_id"), event.get("session_id"), event.get("correlation_id"))
        if identity[0] not in ("PRICE_ACTION", "SMC") or not all(identity[1:]):
            return None
        durable = source == "guardian_lab_lifecycle"
        sequence = evidence.get("source_sequence") if durable else -1
        if durable and type(sequence) is not int:
            return None
        return identity, (durable, sequence, event["timestamp"], event["_sequence"])
    if source == _INSTANCE_SOURCE and event.get("event_type") == "instance_decision_observed":
        owner, identity = event.get("instance_id"), evidence.get("decision_identity")
        row, sequence = evidence.get("decision_id"), evidence.get("source_sequence")
        if not owner or not isinstance(identity, str) or type(row) is not int or type(sequence) is not int:
            return None
        return ("INSTANCE", owner, identity or f"source-row:{row}"), (
            True, sequence, event["timestamp"], event["_sequence"])
    return None


def _summarize(events: list[dict], *, truncated: bool) -> dict:
    sources, kinds, severity = Counter(), Counter(), Counter()
    decisions: dict[tuple, tuple[tuple, dict]] = {}
    for event in events:
        sources[event["source_service"]] += 1
        kinds[event["event_type"]] += 1
        severity[event["severity"]] += 1
        selected = _decision(event)
        if selected:
            identity, rank = selected
            if identity not in decisions or rank > decisions[identity][0]:
                decisions[identity] = (rank, event)
    groups: dict[tuple, dict] = {}
    for _, event in decisions.values():
        metadata, evidence = event.get("metadata") or {}, event.get("evidence") or {}
        # Never pool different versions, owners, markets or source configurations.
        key = (event.get("lab_id") or "INSTANCE", event.get("instance_id") or event.get("session_id"),
               event.get("strategy_id"), event.get("strategy_version"), event.get("symbol"),
               event.get("timeframe"), metadata.get("code_commit"), metadata.get("config_hash"),
               metadata.get("saved_config_hash"), metadata.get("saved_config_scope"))
        group = groups.setdefault(key, {
            "scope": key[0], "owner_id": key[1], "strategy_id": key[2], "strategy_version": key[3],
            "symbol": key[4], "timeframe": key[5], "code_commit": key[6], "config_hash": key[7],
            "saved_config_hash": key[8], "saved_config_scope": key[9],
            "exact_version_verified": bool(key[3] and key[6] and key[7]),
            "observed_decisions": 0, "unproven_near_valid_candidates": 0,
            "decisions_by_state": Counter(), "blockers": Counter(),
            "evidence_ids": [], "performance_verified": False,
        })
        group["observed_decisions"] += 1
        group["decisions_by_state"][event.get("decision") or "UNKNOWN"] += 1
        blocker = evidence.get("blocker") or event.get("reason")
        if blocker:
            group["blockers"][blocker] += 1
        if _near_valid(event):
            group["unproven_near_valid_candidates"] += 1
        if len(group["evidence_ids"]) < 20:
            group["evidence_ids"].append(event["event_id"])
    return {
        "coverage": {"scope": "RECEIVED_SOURCE_EVIDENCE_ONLY", "all_evaluations_verified": False,
                     "scan_truncated": truncated, "scan_limit": _SCAN_LIMIT,
                     "scan_byte_limit": _SCAN_BYTES,
                     "observed_events": len(events), "late_evidence_may_revise_report": True},
        "events_by_source": dict(sources), "events_by_type": dict(kinds),
        "events_by_severity": dict(severity),
        "strategies": sorted(groups.values(), key=lambda item: json.dumps(item, sort_keys=True)),
        "unavailable_metrics": {name: None for name in (
            "platform_uptime", "trades", "wins", "losses", "net_pnl", "average_r",
            "expectancy", "profit_factor", "drawdown", "mae", "mfe", "global_risk",
            "unjournalled_trades", "successful_recoveries")},
        "conclusions": ["Counts describe evidence received, not complete trading activity.",
                        "Near-valid setups are unproven research candidates, not missed profits.",
                        "Trade outcomes, currency, global exposure and uptime are not verified."],
    }


class GuardianReports:
    def __init__(self, store: GuardianStore):
        self.store = store
        with closing(store._connect()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS guardian_reports (
                    report_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    window_start TEXT NOT NULL, window_end TEXT NOT NULL,
                    generated_at TEXT NOT NULL, evidence_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(kind,window_start,window_end,evidence_digest)
                );
                CREATE INDEX IF NOT EXISTS guardian_report_windows
                  ON guardian_reports(window_end DESC,generated_at DESC);
                CREATE TRIGGER IF NOT EXISTS guardian_reports_no_update
                  BEFORE UPDATE ON guardian_reports BEGIN SELECT RAISE(ABORT,'Guardian report is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS guardian_reports_no_delete
                  BEFORE DELETE ON guardian_reports BEGIN SELECT RAISE(ABORT,'Guardian report is immutable'); END;
            """)
            conn.commit()

    def generate(self, kind: str, start: datetime, *, now: datetime | None = None) -> dict:
        now = _utc(now or datetime.now(timezone.utc))
        start, end = _window(kind, start, now)
        with closing(self.store._connect()) as conn:
            # One read snapshot; no database write lock during aggregation.
            conn.execute("BEGIN")
            parameters = (start.isoformat(), end.isoformat(), *_SELF_SOURCES)
            available = conn.execute(
                "SELECT COUNT(*),COALESCE(MAX(sequence),0) FROM events WHERE timestamp>=? "
                "AND timestamp<? AND source_service NOT IN (?,?)", parameters).fetchone()
            cursor = conn.execute(
                "SELECT sequence,payload_json FROM events WHERE timestamp>=? AND timestamp<? "
                "AND source_service NOT IN (?,?) ORDER BY sequence LIMIT ?",
                (*parameters, _SCAN_LIMIT))
            rows, size = [], 0
            for row in cursor:
                size += len(row["payload_json"].encode())
                if size > _SCAN_BYTES:
                    break
                rows.append(row)
            conn.commit()
        truncated = available[0] > len(rows)
        events = [{**json.loads(row["payload_json"]), "_sequence": row["sequence"]}
                  for row in rows]
        # Late evidence past a bound changes the coverage revision too.
        evidence_digest = hashlib.sha256(json.dumps(
            {"schema": 1, "events": [json.loads(row["payload_json"])["event_id"] for row in rows],
             "truncated": truncated, "available": available[0], "highwater": available[1],
             "scan_limit": _SCAN_LIMIT, "scan_bytes": _SCAN_BYTES}, sort_keys=True).encode()).hexdigest()
        report_id = hashlib.sha256(
            f"v1:{kind}:{start.isoformat()}:{end.isoformat()}:{evidence_digest}".encode()).hexdigest()
        report = {"schema_version": 1, "report_id": report_id, "kind": kind,
                  "window_start": start.isoformat(), "window_end": end.isoformat(),
                  "generated_at": now.isoformat(), "evidence_digest": evidence_digest,
                  **_summarize(events, truncated=truncated)}
        report["coverage"]["received_events_in_window"] = available[0]
        encoded = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)
        audit = GuardianEvent(source_service="guardian_reports", source_component="reports",
            event_type="report_created", event_id="report_" + report_id, timestamp=now,
            evidence={"report_id": report_id, "kind": kind, "window_start": start.isoformat(),
                      "window_end": end.isoformat(), "observed_events": len(events),
                      "scan_truncated": truncated, "evidence_digest": evidence_digest})
        with closing(self.store._connect()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT payload_json FROM guardian_reports WHERE report_id=?",
                                        (report_id,)).fetchone()
                if existing:
                    conn.commit()
                    return json.loads(existing["payload_json"])
                conn.execute("INSERT INTO guardian_reports VALUES(?,?,?,?,?,?,?)", (
                    report_id, kind, start.isoformat(), end.isoformat(), now.isoformat(),
                    evidence_digest, encoded))
                self.store._append_in_transaction(conn, audit, now.isoformat())
                conn.commit()
                return report
            except Exception:
                conn.rollback()
                raise

    def generate_due(self, *, now: datetime | None = None) -> list[dict]:
        now = _utc(now or datetime.now(timezone.utc))
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        monday = midnight - timedelta(days=midnight.weekday())
        return [self.generate("DAILY", midnight - timedelta(days=1), now=now),
                self.generate("WEEKLY", monday - timedelta(days=7), now=now)]

    def list(self, *, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid report limit")
        with closing(self.store._connect()) as conn:
            rows = conn.execute("SELECT payload_json FROM guardian_reports "
                                "ORDER BY window_end DESC,generated_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]
