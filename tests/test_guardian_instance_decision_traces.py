"""Instance decision UI contract distinguishes strategy verdict from gate result."""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.instance_decision_traces import instance_decision_traces
from tradexa.guardian.service import GuardianService
from tradexa.guardian.store import GuardianStore


READ_KEY = "guardian-instance-trace-reader-key-12345"


def _event(event_id: str, sequence: int, state: str, *, instance_id="instance-1"):
    return GuardianEvent(
        source_service="guardian_instance_decisions", source_component="instance_decisions",
        event_type="instance_decision_observed", event_id=event_id,
        timestamp=datetime(2026, 10, 3, 12, tzinfo=timezone.utc),
        instance_id=instance_id, strategy_id="Adaptive MTF Trend Pullback",
        symbol="BTCUSDT", timeframe="5m", decision=state, state_after=state,
        reason="correlation limit" if state == "GATE_REJECTED" else "setup accepted",
        evidence={
            "source_sequence": sequence, "decision_id": 7,
            "decision_identity": "instance-1:BTCUSDT:5m:decision-7",
            "decision_time": "2026-10-03T11:55:00+00:00",
            "strategy_verdict": "accepted", "side": "long",
            "gate_stage": "correlation" if state == "GATE_REJECTED" else "",
            "blocker": "GATE_REJECTED: CORRELATION_LIMIT" if state == "GATE_REJECTED" else "",
            "executed": False,
            "passed_rules": [{"key": "setup", "status": "PASS"}],
            "failed_rules": [],
        },
    )


def _request(app, *, key=READ_KEY, query="", method="GET"):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": "/v1/instance-decision-traces",
               "QUERY_STRING": query, "HTTP_X_GUARDIAN_KEY": key,
               "wsgi.input": io.BytesIO(b"")}
    status = []
    body = b"".join(app(environ, lambda code, _headers: status.append(code)))
    return int(status[0].split()[0]), json.loads(body)


def test_latest_source_transition_wins_even_if_older_evidence_arrives_later(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    store.append(_event("event-qualified-0001", 1, "QUALIFIED"))
    store.append(_event("event-gate-rejected-0002", 2, "GATE_REJECTED"))
    store.append(_event("event-old-replayed-0003", 1, "QUALIFIED"))
    page = instance_decision_traces(store)
    assert len(page["traces"]) == 1
    trace = page["traces"][0]
    assert trace["strategy_verdict"] == "accepted"
    assert trace["final_state"] == "GATE_REJECTED"
    assert trace["blocker"] == "GATE_REJECTED: CORRELATION_LIMIT"
    assert trace["broker_fill_verified"] is False
    assert page["all_evaluations_proven"] is False
    assert store.count() == 3


def test_instance_trace_endpoint_auth_filter_and_method(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    store.append(_event("event-gate-rejected-0002", 2, "GATE_REJECTED"))
    app = GuardianService(
        store, source_keys={"source": "guardian-instance-source-key-12345"},
        read_key=READ_KEY, required_components=("guardian",))
    assert _request(app, key="")[0] == 401
    assert _request(app, method="POST")[0] == 405
    assert _request(app, query="limit=101")[0] == 400
    status, body = _request(app, query="instance_id=instance-1")
    assert status == 200
    assert body["coverage"] == "BOUNDED_POST_INSTALL_PERSISTED_DECISIONS"
    assert len(body["traces"]) == 1
    assert _request(app, query="instance_id=other-instance")[1]["traces"] == []


def test_command_center_labels_instance_gate_evidence_and_limits():
    root = Path(__file__).resolve().parents[1] / "tradexa/guardian/assets"
    html = (root / "command_center.html").read_text()
    script = (root / "command_center.js").read_text()
    assert "Observed Trading Instance decisions" in html
    assert "broker fills are not independently verified" in html
    assert "instance-decision-traces" in html
    assert "/v1/instance-decision-traces?limit=50" in script
    assert "trace.strategy_verdict" in script and "trace.final_state" in script
