"""Economic Event Protection (#7)."""
from datetime import datetime, timedelta, timezone

import pytest

from services.econ_guard import evaluate, EconCalendar


NOW = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def _ev(name, mins):
    return {"name": name, "impact": "high", "time": (NOW + timedelta(minutes=mins)).isoformat()}


def test_blackout_inside_window_halts_entries():
    r = evaluate([_ev("CPI", 15)], now=NOW, blackout_min=30, caution_min=120)
    assert r["mode"] == "blackout" and r["halt_new_entries"] is True
    assert r["risk_multiplier"] == 0.0 and r["actions"]


def test_caution_reduces_size_and_widens_stops():
    r = evaluate([_ev("FOMC", 90)], now=NOW, blackout_min=30, caution_min=120)
    assert r["mode"] == "caution" and r["halt_new_entries"] is False
    assert r["risk_multiplier"] == 0.5 and r["stop_multiplier"] == 1.5


def test_normal_when_event_far_away():
    r = evaluate([_ev("NFP", 600)], now=NOW)
    assert r["mode"] == "normal" and r["risk_multiplier"] == 1.0


def test_only_high_impact_and_future_events_count():
    events = [_ev("Some minor PMI", 10),                 # not high-impact
              {"name": "CPI", "impact": "high", "time": (NOW - timedelta(minutes=10)).isoformat()}]  # past
    r = evaluate(events, now=NOW)
    assert r["mode"] == "normal" and r["next_event"] is None


def test_picks_the_nearest_event():
    r = evaluate([_ev("FOMC", 200), _ev("CPI", 20)], now=NOW)
    assert r["next_event"]["name"] == "CPI" and r["mode"] == "blackout"


def test_calendar_store_roundtrip(tmp_path):
    cal = EconCalendar(str(tmp_path / "ev.json"))
    assert cal.connected is False and cal.events() == []
    cal.set_events([{"name": "CPI", "time": NOW.isoformat()}, {"name": "", "time": "x"}])
    assert len(cal.events()) == 1 and cal.connected is True   # blanks dropped


# ───────────────────────── endpoints ─────────────────────────
@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    from services.econ_guard import EconCalendar
    # monkeypatch, not assignment: a replaced module global outlives the test
    # and its events would leak into every later reader of the calendar.
    monkeypatch.setattr(webhook_api, "econ_calendar", EconCalendar(str(tmp_path / "ev.json")))
    app = FastAPI(); app.include_router(webhook_api.router)
    return TestClient(app)


SECRET = "dev-control-key"


def test_econ_endpoints(client):
    p = client.get("/econ/protection").json()
    assert p["connected"] is False and "tracked_event_types" in p
    assert client.post("/econ/events", json={"events": []}).status_code == 401
    far = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    saved = client.post("/econ/events", json={"events": [{"name": "FOMC", "time": far}]},
                        headers={"X-Webhook-Secret": SECRET}).json()
    assert len(saved["events"]) == 1
    assert client.get("/econ/protection").json()["connected"] is True
