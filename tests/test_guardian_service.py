"""Exercise the Guardian HTTP contract without binding a network socket."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event

import pytest

from tradexa.guardian.emitter import GuardianEmitter
from tradexa.guardian.events import GuardianEvent
from tradexa.guardian.service import GuardianService
from tradexa.guardian.store import GuardianStore

SOURCE_KEY = "producer-only-key-with-enough-entropy"
READ_KEY = "separate-reader-key-with-enough-entropy"


def _event(**changes):
    return replace(GuardianEvent(
        source_service="smc_lab", source_component="agent", event_type="decision_observed",
        event_id="smc_decision_123456", timestamp=datetime(2026, 9, 29, tzinfo=timezone.utc),
        evidence={"blocker": "NO_SETUP"}), **changes)


def _request(app, method, path, *, payload=None, key="", query=""):
    encoded = b"" if payload is None else json.dumps(payload).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
        "HTTP_X_GUARDIAN_KEY": key, "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(encoded)), "wsgi.input": io.BytesIO(encoded),
    }
    recorded = {}

    def start_response(status, headers):
        recorded["status"] = int(status.split()[0])
        recorded["headers"] = dict(headers)

    body = b"".join(app(environ, start_response))
    content_type = recorded["headers"].get("Content-Type", "")
    decoded = json.loads(body) if content_type.startswith("application/json") else body.decode("utf-8")
    return recorded["status"], decoded, recorded["headers"]


@pytest.fixture
def app(tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    return GuardianService(store, source_keys={"smc_lab": SOURCE_KEY},
                           read_key=READ_KEY, required_components=("guardian", "smc_lab"))


def test_source_scoped_ingestion_idempotency_and_read_separation(app):
    payload = json.loads(_event().canonical_json())
    assert _request(app, "POST", "/v1/events", payload=payload)[0] == 401
    assert _request(app, "POST", "/v1/events", payload=payload, key=READ_KEY)[0] == 401
    assert _request(app, "POST", "/v1/events", payload=payload, key=SOURCE_KEY)[:2] == (
        201, {"event_id": payload["event_id"], "result": "APPENDED"})
    assert _request(app, "POST", "/v1/events", payload=payload, key=SOURCE_KEY)[0] == 200
    assert _request(app, "GET", "/v1/events", key=SOURCE_KEY)[0] == 401
    status, result, headers = _request(app, "GET", "/v1/events", key=READ_KEY)
    assert status == 200 and len(result["events"]) == 1
    assert result["events"][0]["received_at"]
    assert headers["Cache-Control"] == "no-store"


def test_spoofed_source_and_conflicting_replay_cannot_change_evidence(app):
    payload = json.loads(_event().canonical_json())
    spoofed = {**payload, "source_service": "instance_worker"}
    assert _request(app, "POST", "/v1/events", payload=spoofed, key=SOURCE_KEY)[0] == 403
    assert _request(app, "POST", "/v1/events", payload=payload, key=SOURCE_KEY)[0] == 201
    changed = {**payload, "reason": "different"}
    assert _request(app, "POST", "/v1/events", payload=changed, key=SOURCE_KEY)[0] == 422
    assert app.store.count() == 1


def test_invalid_schema_unknown_fields_and_secret_text_are_rejected(app):
    payload = json.loads(_event().canonical_json())
    for changed in (
        {**payload, "schema_version": 2},
        {**payload, "received_at": "producer-claimed"},
        {**payload, "reason": "Bearer secret-value"},
    ):
        assert _request(app, "POST", "/v1/events", payload=changed, key=SOURCE_KEY)[0] == 422
    assert app.store.count() == 0


def test_health_is_unknown_without_evidence_then_source_bound_heartbeat(app):
    status, self_health, _ = _request(app, "GET", "/healthz")
    assert status == 200 and self_health["self_state"] == "HEALTHY"
    status, health, _ = _request(app, "GET", "/v1/health", key=READ_KEY)
    assert status == 200 and health["state"] == "UNKNOWN"
    assert health["components"]["smc_lab"]["state"] == "UNKNOWN"
    payload = {"component": "smc_lab", "state": "BLOCKED", "reason": "NO_SETUP",
               "observed_at": datetime.now(timezone.utc).isoformat()}
    assert _request(app, "POST", "/v1/heartbeats", payload=payload, key=SOURCE_KEY)[0] == 200
    assert _request(app, "POST", "/v1/heartbeats", payload={**payload, "component": "pa_lab"},
                    key=SOURCE_KEY)[0] == 403
    _, health, _ = _request(app, "GET", "/v1/health", key=READ_KEY)
    assert health["state"] == "BLOCKED"
    assert health["evidence_complete"] is True


def test_open_incident_prevents_green_overall_health_but_does_not_claim_trading_block(app):
    assert _request(app, "GET", "/healthz")[0] == 200
    heartbeat = {"component": "smc_lab", "state": "HEALTHY", "reason": "SOURCE_OBSERVED",
                 "observed_at": datetime.now(timezone.utc).isoformat()}
    assert _request(app, "POST", "/v1/heartbeats", payload=heartbeat,
                    key=SOURCE_KEY)[0] == 200
    assert _request(app, "GET", "/v1/health", key=READ_KEY)[1]["state"] == "HEALTHY"

    crashed = json.loads(_event(event_type="worker_crashed").canonical_json())
    assert _request(app, "POST", "/v1/events", payload=crashed,
                    key=SOURCE_KEY)[0] == 201
    assert app.incidents.scan() == 1
    _, health, _ = _request(app, "GET", "/v1/health", key=READ_KEY)
    assert health["state"] == "DEGRADED"
    assert health["state_reason"] == "ACTIVE_INCIDENTS"
    assert health["active_incidents"] == {
        "total": 1, "warning_or_higher": 1, "high_or_critical": 1}
    assert health["components"]["smc_lab"]["state"] == "HEALTHY"
    assert health["evidence_complete"] is True

    verified = json.loads(_event(
        event_id="worker_verified_123456", event_type="worker_heartbeat",
        evidence={"worker_operational_verified": True}).canonical_json())
    assert _request(app, "POST", "/v1/events", payload=verified,
                    key=SOURCE_KEY)[0] == 201
    assert app.incidents.scan() == 1
    _, recovered, _ = _request(app, "GET", "/v1/health", key=READ_KEY)
    assert recovered["state"] == "HEALTHY"
    assert recovered["active_incidents"]["total"] == 0


def test_persistence_outage_returns_structured_unavailable(app, monkeypatch):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database locked")

    monkeypatch.setattr(app.store, "append", unavailable)
    status, data, _ = _request(app, "POST", "/v1/events",
                               payload=json.loads(_event().canonical_json()), key=SOURCE_KEY)
    assert status == 503 and data == {"error": "PERSISTENCE_UNAVAILABLE"}


@pytest.mark.parametrize("path", ["/v1/reports", "/v1/notifications", "/v1/system-map", "/v1/anomalies"])
def test_reports_and_notices_are_read_only_and_read_key_only(app, path):
    for key in ("", SOURCE_KEY):
        assert _request(app, "GET", path, key=key)[0] == 401
    assert _request(app, "GET", path, key=READ_KEY)[0] == 200
    assert _request(app, "POST", path, payload={}, key=READ_KEY)[0] == 405
    assert app.store.count() == 0


def test_authenticated_investigation_is_bounded_read_only_and_truthful(app):
    app.store.append(_event(event_type="worker_crashed"))
    app.incidents.scan()
    [incident] = app.incidents.list()
    path = f"/v1/incidents/{incident['incident_id']}/investigation"
    assert _request(app, "GET", path, key=SOURCE_KEY)[0] == 401
    status, result, _ = _request(app, "GET", path, key=READ_KEY)
    assert status == 200 and len(result["timeline"]) == 1
    assert result["causal_chain_verified"] is False and result["automatic_action_allowed"] is False
    assert _request(app, "GET", f"/v1/incidents/{'0' * 32}/investigation", key=READ_KEY)[0] == 404
    assert _request(app, "POST", path, payload={}, key=READ_KEY)[0] in (404, 405)
    assert app.store.count() == 1


@pytest.mark.parametrize("path", ["/v1/system-map", "/v1/anomalies"])
def test_analysis_persistence_failure_is_structured_then_retry_works(app, path, monkeypatch):
    original = app.store._connect
    def locked():
        raise sqlite3.OperationalError("database locked")
    monkeypatch.setattr(app.store, "_connect", locked)
    assert _request(app, "GET", path, key=READ_KEY)[:2] == (503, {"error": "PERSISTENCE_UNAVAILABLE"})
    monkeypatch.setattr(app.store, "_connect", original)
    assert _request(app, "GET", path, key=READ_KEY)[0] == 200
    assert app.store.count() == 0


def test_research_disabled_by_default_and_authorities_do_not_cross(app):
    from tests.test_guardian_research import proposal
    app.store.append(_event(event_id="research_evidence_001"))
    research_key, admin_key = "research-authority-independent-key", "owner-authority-independent-key"
    scoped = GuardianService(app.store, source_keys={"smc_lab": SOURCE_KEY}, read_key=READ_KEY,
                              required_components=("guardian",), research_key=research_key, admin_key=admin_key)
    base = "/v1/research/hypotheses"
    assert _request(app, "POST", base, payload=proposal(), key=READ_KEY)[0] == 403
    for key in (READ_KEY, SOURCE_KEY, admin_key):
        assert _request(scoped, "POST", base, payload=proposal(), key=key)[0] == 403
    status, hypothesis, _ = _request(scoped, "POST", base, payload=proposal(), key=research_key)
    assert status == 201
    review = f"/v1/research/{hypothesis['hypothesis_id']}/review"
    payload = {"decision": "SEND_TO_BACKTEST", "expected_digest": hypothesis["evidence_digest"]}
    for key in (READ_KEY, SOURCE_KEY, research_key):
        assert _request(scoped, "POST", review, payload=payload, key=key)[0] == 403
    assert _request(scoped, "POST", review, payload=payload, key=admin_key)[1]["status"] == "HISTORICAL_BACKTEST_PENDING"
    assert _request(scoped, "GET", base, key=READ_KEY)[0] == 200
    assert _request(scoped, "GET", base, key=admin_key)[0] == 401
    assert _request(scoped, "POST", "/v1/start", payload={}, key=admin_key)[0] == 404


@pytest.mark.parametrize("research_key,admin_key", [(READ_KEY, None), (SOURCE_KEY, None),
                                                       ("same-long-key-123456789012345", "same-long-key-123456789012345"),
                                                       ("short", None)])
def test_research_admin_and_read_credentials_must_be_independent(app, research_key, admin_key):
    with pytest.raises(ValueError, match="independently scoped"):
        GuardianService(app.store, source_keys={"smc_lab": SOURCE_KEY}, read_key=READ_KEY,
                        required_components=("guardian",), research_key=research_key, admin_key=admin_key)


def test_paper_ledger_view_is_read_only_and_unknown_without_observation(app):
    assert _request(app, "GET", "/v1/instance-ledger")[0] == 401
    assert _request(app, "GET", "/v1/instance-ledger", key=SOURCE_KEY)[0] == 401
    status, result, _ = _request(app, "GET", "/v1/instance-ledger", key=READ_KEY)
    assert status == 200
    assert result["observation_state"] == "UNKNOWN"
    assert result["instances"] == []
    assert result["global_risk_amount"] is None
    assert result["live_exposure_verified"] is False
    assert _request(app, "POST", "/v1/instance-ledger", key=READ_KEY)[0] == 405


def test_emitter_is_nonblocking_and_reports_backpressure():
    entered = Event()
    release = Event()

    def slow_sender(_payload):
        entered.set()
        assert release.wait(3)
        return 201

    emitter = GuardianEmitter(source_service="smc_lab", endpoint="http://127.0.0.1:8765/v1/events",
                              key=SOURCE_KEY, capacity=1, sender=slow_sender)
    try:
        assert emitter.emit(_event()) is True
        assert entered.wait(1)
        assert emitter.emit(_event(event_id="smc_decision_123457")) is True
        assert emitter.emit(_event(event_id="smc_decision_123458")) is False
        assert emitter.counters()["backpressure_dropped"] == 1
    finally:
        release.set()
        assert emitter.close()
    assert emitter.counters()["delivered"] == 2


def test_emitter_sender_failure_and_invalid_event_do_not_raise():
    def broken_sender(_payload):
        raise RuntimeError("Guardian is down")

    emitter = GuardianEmitter(source_service="smc_lab", endpoint="http://localhost:8765/v1/events",
                              key=SOURCE_KEY, sender=broken_sender)
    assert emitter.emit(_event(source_service="pa_lab")) is False
    assert emitter.emit(_event()) is True
    assert emitter.close()
    assert emitter.counters()["invalid"] == 1
    assert emitter.counters()["delivery_failed"] == 1


def test_insecure_non_loopback_http_and_shared_keys_fail_fast(app):
    with pytest.raises(ValueError, match="loopback-only"):
        GuardianEmitter(source_service="smc_lab", endpoint="http://example.com/v1/events",
                        key=SOURCE_KEY)
    with pytest.raises(ValueError, match="distinct"):
        GuardianService(app.store, source_keys={"smc_lab": SOURCE_KEY},
                        read_key=SOURCE_KEY, required_components=("guardian",))


def test_command_center_shell_and_assets_are_read_only(app):
    status, html, headers = _request(app, "GET", "/")
    assert status == 200
    assert "Command Center" in html
    assert "UNKNOWN" in html
    assert "No trading authority" in html
    assert SOURCE_KEY not in html and READ_KEY not in html
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    for path, mime in (("/assets/command-center.css", "text/css"),
                       ("/assets/command-center.js", "text/javascript")):
        asset_status, body, asset_headers = _request(app, "GET", path)
        assert asset_status == 200 and body
        assert asset_headers["Content-Type"].startswith(mime)
        assert asset_headers["X-Content-Type-Options"] == "nosniff"
    assert _request(app, "POST", "/")[0] == 405
    assert app.store.count() == 0


def test_command_center_never_embeds_keys_or_event_html():
    root = Path(__file__).resolve().parents[1] / "tradexa" / "guardian" / "assets"
    html = (root / "command_center.html").read_text()
    script = (root / "command_center.js").read_text()
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "innerHTML" not in script and "outerHTML" not in script
    assert "textContent" in script
    assert "X-Guardian-Key" in script
    assert "/v1/incidents?limit=50" in script
    assert "incident-count" in html
    assert "health.active_incidents" in script
    assert "All unresolved derived incidents" in html
    assert "timeline" in script
    assert 'id="decision-traces"' in html
    assert "Bounded received snapshots" in html
    assert "/v1/decision-traces?limit=50" in script
    assert "near_valid_candidate" in script
    assert "<script src=\"/assets/command-center.js\" defer>" in html


def test_command_center_assets_are_declared_for_packaging():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert '[tool.setuptools.package-data]' in pyproject
    assert '"tradexa.guardian" = ["assets/*.html", "assets/*.css", "assets/*.js"]' in pyproject


def test_incidents_and_timeline_require_read_key_and_preserve_evidence(app):
    payload = json.loads(_event(event_type="worker_crashed").canonical_json())
    assert _request(app, "POST", "/v1/events", payload=payload, key=SOURCE_KEY)[0] == 201
    assert app.incidents.scan() == 1
    assert _request(app, "GET", "/v1/incidents")[0] == 401
    status, data, _ = _request(app, "GET", "/v1/incidents", key=READ_KEY)
    assert status == 200 and len(data["incidents"]) == 1
    incident = data["incidents"][0]
    assert incident["state"] == "OPEN"
    incident_id = incident["incident_id"]
    assert _request(app, "GET", f"/v1/incidents/{incident_id}/timeline")[0] == 401
    status, timeline, _ = _request(app, "GET", f"/v1/incidents/{incident_id}/timeline",
                                   key=READ_KEY)
    assert status == 200
    assert timeline["updates"][0]["event_id"] == payload["event_id"]
    assert _request(app, "GET", "/v1/incidents", key=READ_KEY, query="state=INVALID")[0] == 400
    assert _request(app, "GET", "/v1/incidents/not-an-id/timeline", key=READ_KEY)[0] == 404
