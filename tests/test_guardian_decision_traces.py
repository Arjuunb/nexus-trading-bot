"""Decision traces are bounded observations, not strategy-performance proof."""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

from tradexa.guardian.decision_traces import decision_traces
from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.service import GuardianService
from tradexa.guardian.store import GuardianStore


def _event(event_id: str, *, source: str = "guardian_lab_probe",
           decision: str = "WATCHING", conditions=None, missing=None,
           correlation: str = "decision-1", lab: str = "SMC") -> GuardianEvent:
    return GuardianEvent(
        event_id=event_id, source_service=source,
        source_component="smc_lab" if lab == "SMC" else "pa_lab",
        event_type="lab_evaluation_observed" if source == "guardian_lab_probe"
                   else "lab_evaluation_backfilled",
        timestamp=datetime(2026, 9, 29, 12, tzinfo=timezone.utc),
        lab_id=lab, session_id="session-1", correlation_id=correlation,
        strategy_id="SMC_SOURCE_V1", strategy_version="1.0",
        symbol="BTCUSDT", timeframe="5m", decision=decision, severity="INFO",
        reason="WAITING_REJECTION", evidence={
            "conditions": conditions if conditions is not None else [
                {"key": "htf", "status": "PASS"},
                {"key": "rejection", "status": "MISSING"}],
            "missing_conditions": missing if missing is not None else ["rejection"],
            "condition_trace_available": True,
            "feed_health_verified": False, "execution_integrity_verified": False,
        }, metadata={"paper_only": True})


def _get(app: GuardianService, key: str = "reader-key-12345678901234567890", query: str = ""):
    environment = {"REQUEST_METHOD": "GET", "PATH_INFO": "/v1/decision-traces",
                   "QUERY_STRING": query, "HTTP_X_GUARDIAN_KEY": key,
                   "wsgi.input": io.BytesIO(b"")}
    status = []
    body = b"".join(app(environment, lambda code, _headers: status.append(code)))
    return int(status[0].split()[0]), json.loads(body)


def test_latest_material_snapshot_deduplicates_backfill_and_observer(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    store.append(_event("backfill-1", source="guardian_lab_backfill"))
    store.append(_event("observation-1"))
    result = decision_traces(store)
    assert len(result["traces"]) == 1
    assert result["traces"][0]["event_id"] == "observation-1"
    assert result["traces"][0]["near_valid_candidate"] is True
    assert result["traces"][0]["outcome_verified"] is False
    assert result["lifecycle_history_complete"] is False
    store.append(_event("observation-2", decision="SIGNAL_FOUND", missing=[]))
    assert decision_traces(store)["traces"][0]["near_valid_candidate"] is False
    assert store.count() == 3  # Derived reads never mutate raw evidence.


def test_incomplete_or_ambiguous_conditions_are_not_called_almost_trades(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    store.append(_event("one-condition", conditions=[{"key": "only", "status": "MISSING"}],
                        correlation="one"))
    store.append(_event("unknown-condition", conditions=[
        {"key": "htf", "status": "UNKNOWN"},
        {"key": "rejection", "status": "MISSING"}], correlation="unknown"))
    store.append(_event("two-missing", conditions=[
        {"key": "htf", "status": "MISSING"},
        {"key": "rejection", "status": "MISSING"}],
        missing=["htf", "rejection"], correlation="two"))
    assert all(not row["near_valid_candidate"] for row in decision_traces(store)["traces"])


def test_decision_trace_api_requires_read_key_and_advertises_limited_coverage(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    store.append(_event("decision-1"))
    app = GuardianService(store, source_keys={"smc_lab": "source-key-12345678901234567890"},
                          read_key="reader-key-12345678901234567890",
                          required_components=("guardian", "smc_lab"))
    assert _get(app, key="")[0] == 401
    assert _get(app, query="lab=INVALID")[0] == 400
    assert _get(app, query="limit=101")[0] == 400
    status, body = _get(app, query="lab=SMC&limit=10")
    assert status == 200
    assert body["coverage"] == "BOUNDED_RECEIVED_SNAPSHOTS"
    assert body["strategy_performance_verified"] is False
    assert len(body["traces"]) == 1
    assert _get(app, query="lab=PRICE_ACTION")[1]["traces"] == []
    assert store.count() == 1
