"""Bounded, causal operational latency baselines over received evidence only.

No trading imports/writes, fitted strategy parameters or invented metrics.
Deviations are WATCH observations, not failures or permissions to trade.
"""
from __future__ import annotations

import hashlib
import json
import math
from contextlib import closing
from datetime import datetime, timedelta, timezone
from statistics import median

from .store import GuardianStore

_SCAN_LIMIT = 5000
_SCAN_BYTES = 4 * 1024 * 1024
_MIN_BASELINE = 20
_MIN_CURRENT = 5
_MAX_LATENCY_MS = 7 * 24 * 60 * 60 * 1000
_LATENCY_KINDS = {"candle_processing", "strategy_evaluation", "paper_fill", "api_response", "worker_task"}
_GROUP_FIELDS = ("source_service", "source_component", "event_type", "lab_id", "instance_id",
                 "session_id", "strategy_id", "strategy_version", "symbol", "timeframe")


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("analysis requires an aware clock")
    return value.astimezone(timezone.utc)


def bounded_events(cursor, *, limit: int, byte_limit: int) -> tuple[list[dict], bool]:
    """Read at most limit/bytes; caller requests limit+1 to detect truncation."""
    rows, size, truncated = [], 0, False
    for row in cursor:
        size += len(row["payload_json"].encode("utf-8"))
        if len(rows) >= limit or size > byte_limit:
            truncated = True
            break
        rows.append({**json.loads(row["payload_json"]), "received_at": row["received_at"],
                     "_sequence": row["sequence"]})
    return rows, truncated


def latency_anomalies(store: GuardianStore, *, now: datetime | None = None) -> dict:
    """Compare a closed five-minute window to the preceding 24 hours.

    Both source timestamp AND store receipt must predate the decision cutoff.
    Forming-minute/future/late-import evidence cannot enter an earlier window.
    Baselines never mix source/version/config/venue/session/owner or metric kind.
    """
    cutoff = utc(now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    current_start = cutoff - timedelta(minutes=5)
    baseline_start = current_start - timedelta(hours=24)
    with closing(store._connect()) as conn:
        conn.execute("BEGIN")
        cursor = conn.execute(
            "SELECT sequence,received_at,payload_json FROM events WHERE timestamp>=? "
            "AND timestamp<? AND received_at<? ORDER BY timestamp,sequence LIMIT ?",
            (baseline_start.isoformat(), cutoff.isoformat(), cutoff.isoformat(), _SCAN_LIMIT + 1))
        events, truncated = bounded_events(cursor, limit=_SCAN_LIMIT, byte_limit=_SCAN_BYTES)
        conn.commit()
    groups = {}
    excluded = 0
    for event in events:
        metadata = event.get("metadata") or {}
        value = event.get("latency_ms")
        kind = metadata.get("latency_kind")
        if (not isinstance(kind, str) or kind not in _LATENCY_KINDS or type(value) not in (int, float) or
                not math.isfinite(value) or not 0 <= value <= _MAX_LATENCY_MS):
            excluded += 1
            continue
        # Every event stores normalized UTC timestamps; compare aware instants.
        stamp = utc(datetime.fromisoformat(event["timestamp"]))
        if stamp > utc(datetime.fromisoformat(event["received_at"])) + timedelta(seconds=5):
            excluded += 1
            continue
        if any(metadata.get(field) is not None and not isinstance(metadata[field], str)
               for field in ("venue", "scope", "code_commit", "config_hash")):
            excluded += 1
            continue
        key = tuple(event.get(field) for field in _GROUP_FIELDS) + tuple(
            metadata.get(field) for field in ("venue", "scope", "code_commit", "config_hash", "latency_kind"))
        group = groups.setdefault(key, {"baseline": [], "current": [], "identity": {
            **{field: event.get(field) for field in _GROUP_FIELDS},
            **{field: metadata.get(field) for field in
               ("venue", "scope", "code_commit", "config_hash", "latency_kind")}}})
        group["baseline" if stamp < current_start else "current"].append((value, event["event_id"]))
    rows = []
    for group in groups.values():
        identity = group["identity"]
        # A deployment/config identity is required even for non-strategy metrics.
        provenance = (isinstance(identity["code_commit"], str) and
                      len(identity["code_commit"]) == 40 and
                      all(c in "0123456789abcdef" for c in identity["code_commit"]) and
                      isinstance(identity["config_hash"], str) and
                      len(identity["config_hash"]) == 64 and
                      all(c in "0123456789abcdef" for c in identity["config_hash"]) and
                      (not identity["strategy_id"] or bool(identity["strategy_version"])))
        baseline = sorted(value for value, _ in group["baseline"])
        current = [value for value, _ in group["current"]]
        reasons = (["BOUNDED_SCAN_TRUNCATED"] if truncated else [])
        if not provenance:
            reasons.append("SOURCE_VERSION_OR_CONFIGURATION_UNVERIFIED")
        if len(baseline) < _MIN_BASELINE:
            reasons.append("INSUFFICIENT_BASELINE_SAMPLES")
        if len(current) < _MIN_CURRENT:
            reasons.append("INSUFFICIENT_CURRENT_SAMPLES")
        threshold = None
        if not reasons:
            center = median(baseline)
            mad = median(abs(value - center) for value in baseline)
            p95 = baseline[math.ceil(.95 * len(baseline)) - 1]
            # Fixed operational heuristic, NOT a fitted alpha/trading threshold.
            threshold = max(50.0, 3 * p95, center + 6 * mad)
        state = ("INSUFFICIENT_EVIDENCE" if reasons else "LATENCY_DEVIATION"
                 if median(current) > threshold else "NO_DEVIATION_OBSERVED")
        digest = hashlib.sha256(json.dumps({"identity": identity,
            "cutoff": cutoff.isoformat(), "baseline_ids": [eid for _, eid in group["baseline"]],
            "current_ids": [eid for _, eid in group["current"]]}, sort_keys=True).encode()).hexdigest()
        rows.append({"analysis_id": digest, "identity": identity, "state": state,
                     "severity": "WATCH" if state == "LATENCY_DEVIATION" else "INFO",
                     "reasons": reasons, "baseline_samples": len(baseline),
                     "current_samples": len(current), "threshold_ms": threshold,
                     "baseline_median_ms": median(baseline) if baseline else None,
                     "current_median_ms": median(current) if current else None,
                     "current_evidence_ids": [eid for _, eid in group["current"][-20:]],
                     "failure_verified": False, "financial_risk_verified": False})
    rows.sort(key=lambda row: (row["state"] != "LATENCY_DEVIATION", row["analysis_id"]))
    return {"schema_version": 1, "scope": "RECEIVED_TYPED_LATENCY_EVIDENCE_ONLY",
            "cutoff": cutoff.isoformat(), "baseline_start": baseline_start.isoformat(),
            "current_start": current_start.isoformat(), "window_closed": True,
            "anomalies": rows[:50], "groups_truncated": len(rows) > 50,
            "scan_truncated": truncated, "scan_limit": _SCAN_LIMIT, "scan_byte_limit": _SCAN_BYTES,
            "observed_events": len(events), "excluded_untyped_events": excluded,
            "coverage_state": "INSUFFICIENT_EVIDENCE" if truncated or not rows else "PARTIAL",
            "all_metrics_observed": False, "automatic_action_allowed": False,
            "unavailable_metrics": ["signal_frequency", "rejection_distribution", "resource_usage",
                                    "complete_evaluation_frequency", "platform_uptime"],
            "limitations": ["A latency deviation is not a failure or proof of trading danger.",
                            "No metric samples means insufficient evidence, not normal operation.",
                            "No new evidence or revision is written by dashboard refreshes."]}
