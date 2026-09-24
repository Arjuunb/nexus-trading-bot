"""The Adaptive MTF lab API: the lab's own bot, the Visual Lab's own evidence."""
import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routers.adaptive_lab as module
from tests.test_adaptive_lab import _lab
from tests.test_instance_runtime_reliability import _reset_streams  # noqa: F401

KEY = "lab-test-key"


@pytest.fixture()
def api(monkeypatch, tmp_path):
    lab = _lab(tmp_path)

    def check(secret):
        if secret != KEY:
            raise HTTPException(401, "Invalid or missing credential")

    class _Proxy:
        adaptive_lab = lab
        _check_secret = staticmethod(check)

    monkeypatch.setattr(module, "_wa", _Proxy())
    app = FastAPI()
    app.include_router(module.router)
    yield TestClient(app), lab
    lab.shutdown()


def test_a_lab_without_a_bot_says_so_instead_of_inventing_one(api):
    client, _lab_ = api
    assert client.get("/research/adaptive-lab/status").json()["bot"] is None
    response = client.get("/research/adaptive-lab/state")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "NO_BOT"


def test_status_and_evidence_come_from_the_labs_own_bot(api):
    client, lab = api
    bot = lab.ensure_started()

    status = client.get("/research/adaptive-lab/status").json()
    assert status["bot_id"] == bot.id and status["mode"] == "automatic"
    assert status["strategy"]["key"] == "adaptive_trend_pullback"
    assert status["real_execution_allowed"] is False

    state = client.get("/research/adaptive-lab/state").json()
    assert state["instance"]["instance_id"] == bot.id
    assert state["strategy"]["strategy_id"] == "adaptive_trend_pullback"
    # The strategy's own gate sequence, after the shared feed gates.
    assert {"indicators_ready", "htf_bias", "regime_gate", "pullback", "resume"} <= {
        gate["id"] for gate in state["gates"]}

    timeline = client.get("/research/adaptive-lab/timeline").json()
    assert timeline["instance_id"] == bot.id and timeline["events"] == []

    paper = client.get("/research/adaptive-lab/paper").json()
    assert paper["bot_id"] == bot.id and paper["positions"] == [] and paper["trades"] == []


def test_configuration_needs_the_control_credential(api):
    client, lab = api
    lab.ensure_started()

    refused = client.post("/research/adaptive-lab/configuration", json={"mode": "off"})
    assert refused.status_code == 401
    assert lab.status()["mode"] == "automatic"

    saved = client.post("/research/adaptive-lab/configuration", json={"mode": "signals_only"},
                        headers={"x-webhook-secret": KEY})
    assert saved.status_code == 200 and saved.json()["mode"] == "signals_only"
    assert lab.status()["mode"] == "signals_only"


def test_a_refused_change_is_a_400_with_the_reason(api):
    client, lab = api
    bot = lab.ensure_started()
    lab.ledger.open_position(symbol="XRPUSDT", side="long", size=10, entry=0.5, stop=0.49,
                             instance_id=bot.id, simulation_session_id=bot.simulation_session_id)
    response = client.post("/research/adaptive-lab/configuration", json={"symbol": "ADAUSDT"},
                           headers={"x-webhook-secret": KEY})
    assert response.status_code == 400
    assert "open paper position" in response.json()["detail"]
    assert lab.current().symbol == "XRPUSDT"


def test_the_only_write_is_the_configuration_post():
    writes = [(route.path, sorted(route.methods)) for route in module.router.routes
              if set(route.methods) - {"GET", "HEAD"}]
    assert writes == [("/research/adaptive-lab/configuration", ["POST"])]


def test_live_chart_and_journal_routes(api):
    client, lab = api
    bot = lab.ensure_started()
    chart = client.get("/research/adaptive-lab/live-chart?window=120").json()
    assert len(chart["candles"]) == 120 and chart["bot_id"] == bot.id
    journal = client.get("/research/adaptive-lab/journal").json()
    assert journal["bot_id"] == bot.id and isinstance(journal["entries"], list)
    lab.configure(mode="off")
    off = client.get("/research/adaptive-lab/live-chart")
    assert off.status_code == 503 and off.json()["detail"]["code"] == "NO_LIVE_FEED"


def test_every_read_can_show_a_mirrored_trading_instance_and_refuses_unknown_ones(api, tmp_path):
    from tests.test_adaptive_lab import _instance, _instances
    client, lab = api
    lab.ensure_started()
    instances, _cycles = _instances(tmp_path, lab.manager.market_hub)
    lab.attach_instances(instances)
    inst = _instance(instances)
    try:
        status = client.get("/research/adaptive-lab/status", params={"source": inst.id}).json()
        assert [row["id"] for row in status["sources"]] == ["lab", inst.id]
        assert status["view"]["kind"] == "instance" and status["view"]["bot_id"] == inst.id
        assert status["bot_id"] != inst.id                     # the lab's own fields stay the lab's
        for path in ("/paper", "/state", "/timeline", "/journal", "/live-chart"):
            response = client.get(f"/research/adaptive-lab{path}", params={"source": inst.id})
            assert response.status_code == 200, (path, response.text)
        # The strategy publishes no chart features, mirrored or not; nothing is invented.
        for source in ("lab", inst.id):
            response = client.get("/research/adaptive-lab/features", params={"source": source})
            assert response.status_code == 501
            assert response.json()["detail"]["code"] == "FEATURES_NOT_EXPOSED"
        assert client.get("/research/adaptive-lab/live-chart",
                          params={"source": inst.id}).json()["bot_id"] == inst.id
        for path in ("/status", "/paper", "/state", "/journal", "/live-chart", "/timeline"):
            response = client.get(f"/research/adaptive-lab{path}", params={"source": "not-an-instance"})
            assert response.status_code == 404, (path, response.status_code)
    finally:
        instances.shutdown()
