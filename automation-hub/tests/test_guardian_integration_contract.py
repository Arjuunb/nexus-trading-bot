"""The deployed observer and optional standalone exporter must coexist safely."""
from collections import Counter
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
import webhook_api
from config import settings
from data.decision_store import DecisionStore
from services import guardian
from services.guardian.bus import EventBus
from services.guardian.store import GuardianStore as EmbeddedStore
from services.guardian_instance_read_model import instance_decision_page
from tradexa.guardian.instance_decisions import GuardianInstanceDecisions
from tradexa.guardian.instance_decision_traces import instance_decision_traces
from tradexa.guardian.store import GuardianStore as StandaloneStore

KEY = "independent-integration-observer-key-12345"


def test_guardian_read_routes_have_distinct_paths_and_preserve_owner_actions():
    def operations_for(router, prefix=""):
        # FastAPI can retain included routers lazily or flatten them; inspect
        # both forms without relying on a test-only OpenAPI deduplication.
        for route in router.routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from operations_for(included, prefix + route.include_context.prefix)
            else:
                path = prefix + getattr(route, "path", "")
                for method in getattr(route, "methods", ()):
                    yield path, method

    operations = Counter(
        (path, method)
        for path, method in operations_for(app_module.app)
        if path.startswith("/guardian/")
    )
    assert operations
    assert all(count == 1 for count in operations.values())
    assert {path for path, method in operations if method not in {"GET", "HEAD"}} == {
        "/guardian/research/{hypothesis_id}/action", "/guardian/reasoning/ask"}
    assert ("/guardian/events", "GET") in operations
    assert ("/guardian/instance-decisions", "GET") in operations


def test_observer_key_cannot_access_embedded_api_or_execution_control(tmp_path, monkeypatch):
    embedded = EmbeddedStore(str(tmp_path / "embedded.db"))
    source = DecisionStore(str(tmp_path / "decisions.db"))
    monkeypatch.setattr(webhook_api, "guardian_store", embedded)
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "decisions_db", source._path)
    client = TestClient(app_module.app)
    observer = {"X-Guardian-Observer-Key": KEY}
    control = {"X-Webhook-Secret": settings.admin_key}
    assert client.get("/guardian/events", headers=control).status_code == 200
    assert client.get("/guardian/events", headers=observer).status_code == 401
    for path in ("/guardian/research/1/action", "/guardian/reasoning/ask"):
        assert client.post(path, json={}, headers=observer).status_code == 401
    assert client.get("/guardian/instance-decisions", headers=control).status_code == 401
    response = client.get("/guardian/instance-decisions", headers=observer)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["page"]["transitions"] == []
    for path in ("/api/v1/start", "/instances", "/research/smc/sessions/current/configuration"):
        assert client.post(path, json={}, headers=observer).status_code == 401
    assert source.count() == 0
    assert embedded.events() == []


@pytest.mark.parametrize("embedded_enabled", [False, True])
def test_one_paper_fill_keeps_both_observers_on_the_same_decision(
        tmp_path, monkeypatch, embedded_enabled):
    # These exercise the unchanged real strategy, engine, pipeline and paper
    # broker fixtures from the deployed branch, not a mocked execution result.
    from tests.test_guardian_strategy import _instance, _run
    from tests.test_three_candle_rejection import _history, _long_pattern

    monkeypatch.setenv("GIT_COMMIT", "a" * 40)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    _, engine, paper = _instance(tmp_path)
    source = DecisionStore(str(tmp_path / "decisions.db"))
    engine.decisions = source
    engine.strategy_version, engine.config_revision = "integration-fixture", 7
    embedded = EmbeddedStore(str(tmp_path / "embedded.db"))
    bus = EventBus(embedded)
    previous_bus = guardian.installed()
    guardian.install(bus if embedded_enabled else None)
    try:
        rows, index = _history()
        _run(engine, rows + _long_pattern(index))
        bus.flush()
    finally:
        guardian.install(previous_bus)

    assert len(paper.positions()) == 1
    assert engine.stats["trades"] == 1
    assert source.count() == 1
    [decision] = source.list()
    identity = decision["decision_identity"]
    assert identity and decision["executed"] is True
    transitions = instance_decision_page(source._path)["transitions"]
    assert transitions[-1]["final_state"] == "FILLED"
    assert all(row["decision_identity"] == identity for row in transitions)
    assert transitions[-1]["instance_provenance"]["saved_config"]["config_revision"] == 7

    entries = embedded.events(event_type="setup_detected")
    assert len(entries) == int(embedded_enabled)
    if entries:
        assert entries[0]["decision"] == "ENTERED"
        assert entries[0]["correlation_id"] == identity

    standalone = StandaloneStore(tmp_path / "standalone.db")

    def fetch(after, anchor):
        return {"schema_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(),
                "scope": "POST_INSTALL_INSTANCE_DECISION_LIFECYCLE",
                "feed_health_verified": False, "execution_integrity_verified": False,
                "page": instance_decision_page(source._path, after=after, anchor=anchor)}

    collector = GuardianInstanceDecisions(
        standalone, "http://app:8000/guardian/instance-decisions", KEY, fetch=fetch)
    assert collector.poll() == len(transitions)
    assert collector.poll() == 0
    assert standalone.count() == len(transitions)
    [trace] = instance_decision_traces(standalone)["traces"]
    assert trace["decision_identity"] == identity
    assert trace["final_state"] == "FILLED"
    assert len(paper.positions()) == 1 and engine.stats["trades"] == 1
    assert source.count() == 1
