"""Economic-calendar feed (services/econ_feed.py) and the guard's use of it.

The landing and Features page promise event blackouts around high-impact
releases "so time-based discipline does not depend on someone being awake".
Before this feed the calendar only held hand-typed dates."""
from datetime import datetime, timedelta, timezone

import pytest

from services.econ_feed import EconFeed, parse_forexfactory
from services.econ_guard import EconCalendar, evaluate, is_high_impact

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _item(title, when, *, country="USD", impact="High"):
    return {"title": title, "country": country, "date": when, "impact": impact, "forecast": "", "previous": ""}


EXPORT = [
    _item("CPI m/m", "2026-09-24T08:30:00-04:00"),                 # 12:30 UTC
    _item("Non-Farm Employment Change", "2026-09-25T08:30:00-04:00"),
    _item("Federal Funds Rate", "2026-09-23T14:00:00-04:00"),
    _item("Retail Sales m/m", "2026-09-22T08:30:00-04:00", impact="Medium"),   # not high
    _item("Main Refinancing Rate", "2026-09-24T08:15:00-04:00", country="EUR"),  # other currency
    _item("Bank Holiday", "2026-09-22T00:00:00-04:00", impact="Holiday"),
    _item("CPI m/m", "not a date"),                                 # unusable time
    _item("CPI m/m", "2026-09-24T08:30:00-04:00"),                  # duplicate
    "junk",
]


def test_the_export_is_reduced_to_high_impact_releases_for_the_chosen_currencies():
    events = parse_forexfactory(EXPORT, countries=("USD",))
    assert [e["name"] for e in events] == ["USD Federal Funds Rate", "USD CPI m/m", "USD Non-Farm Employment Change"]
    assert events[1]["time"] == "2026-09-24T12:30:00+00:00"        # normalised to UTC
    assert all(e["impact"] == "high" and e["source"] == "forexfactory" for e in events)
    both = parse_forexfactory(EXPORT, countries=("USD", "EUR"))
    assert "EUR Main Refinancing Rate" in [e["name"] for e in both]
    assert parse_forexfactory({"not": "a list"}) == []


def test_provider_rated_events_count_and_hand_entries_still_need_a_known_name():
    assert is_high_impact({"name": "USD Advance GDP q/q", "impact": "high", "source": "forexfactory"})
    assert is_high_impact({"name": "USD Non-Farm Employment Change", "impact": "high"})   # known name
    assert is_high_impact({"name": "Federal Funds Rate", "impact": "high"})
    assert not is_high_impact({"name": "Some minor PMI", "impact": "high"})             # hand-typed, unknown


def test_a_feed_release_triggers_the_blackout():
    cal_events = parse_forexfactory(EXPORT)
    before = datetime(2026, 9, 24, 12, 10, tzinfo=timezone.utc)      # 20 min before CPI
    result = evaluate(cal_events, now=before)
    assert result["mode"] == "blackout" and result["halt_new_entries"]
    assert result["next_event"]["name"] == "USD CPI m/m"


def test_sync_stores_provider_events_beside_hand_entered_ones(tmp_path):
    cal = EconCalendar(str(tmp_path / "ev.json"))
    cal.set_events([{"name": "FOMC", "time": "2026-09-30T18:00:00+00:00"}])
    feed = EconFeed(cal, fetch=lambda url: EXPORT, clock=lambda: NOW, countries=("USD",))
    status = feed.sync()
    assert status["last_error"] is None and status["events"] == 3 and status["last_success"].startswith("2026-09-21")
    assert len(cal.manual_events()) == 1 and len(cal.provider_events()) == 3
    assert len(cal.events()) == 4
    # setting hand events again leaves the feed's events alone
    cal.set_events([])
    assert len(cal.provider_events()) == 3


def test_a_failed_fetch_keeps_the_last_good_events_and_says_why(tmp_path):
    cal = EconCalendar(str(tmp_path / "ev.json"))
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("network unreachable")
        return EXPORT

    feed = EconFeed(cal, fetch=flaky, clock=lambda: NOW)
    feed.sync()
    status = feed.sync()
    assert status["last_error"] == "OSError: network unreachable"
    assert len(cal.provider_events()) == 3                           # not wiped


@pytest.mark.parametrize("payload, fragment", [
    ({"error": "rate limited"}, "expected a list of releases, got dict"),
    ("<html>blocked</html>", "expected a list of releases, got str"),
    ([{"name": "CPI", "when": "2026-09-24"}], "releases have no impact/date fields"),
])
def test_an_export_in_another_shape_is_an_error_not_a_quiet_week(tmp_path, payload, fragment):
    """It used to parse to zero events and read as "nothing high-impact this week"."""
    cal = EconCalendar(str(tmp_path / "ev.json"))
    feed = EconFeed(cal, fetch=lambda url: EXPORT, clock=lambda: NOW)
    feed.sync()
    feed.fetch = lambda url: payload
    status = feed.sync()
    assert fragment in status["last_error"]
    assert status["events"] == 3                                     # the last good week is kept


def test_status_says_how_many_releases_the_export_held(tmp_path):
    cal = EconCalendar(str(tmp_path / "ev.json"))
    quiet = [_item("Retail Sales m/m", "2026-09-22T08:30:00-04:00", impact="Medium")]
    status = EconFeed(cal, fetch=lambda url: quiet, clock=lambda: NOW).sync()
    assert status["last_error"] is None and status["events"] == 0 and status["rows_seen"] == 1


def test_the_sites_own_high_impact_wording_counts():
    rows = [_item("CPI m/m", "2026-09-24T08:30:00-04:00", impact="High Impact Expected"),
            {"title": "GDP q/q", "currency": "USD", "date": "2026-09-24T08:30:00-04:00", "impact": "High"}]
    assert [e["name"] for e in parse_forexfactory(rows)] == ["USD CPI m/m", "USD GDP q/q"]


def test_connected_means_real_events_not_a_configured_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ECON_CALENDAR_KEY", "something")
    cal = EconCalendar(str(tmp_path / "ev.json"))
    assert cal.connected is False                                    # a key alone fetched nothing
    EconFeed(cal, fetch=lambda url: EXPORT, clock=lambda: datetime.now(timezone.utc)).sync()
    assert cal.connected is True
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    EconFeed(cal, fetch=lambda url: EXPORT, clock=lambda: stale).sync()
    assert cal.connected is False                                    # a feed that stopped succeeding


def test_the_feed_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_ECON_FEED", "off")
    feed = EconFeed(EconCalendar(str(tmp_path / "ev.json")), fetch=lambda url: EXPORT)
    assert feed.start() is False and feed.status()["enabled"] is False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    cal = EconCalendar(str(tmp_path / "ev.json"))
    monkeypatch.setattr(webhook_api, "econ_calendar", cal)
    monkeypatch.setattr(webhook_api, "econ_feed",
                        EconFeed(cal, fetch=lambda url: EXPORT, clock=lambda: datetime.now(timezone.utc)))
    app = FastAPI()
    app.include_router(webhook_api.router)
    return TestClient(app)


def test_the_api_reports_the_feed_and_fetches_on_demand(client):
    before = client.get("/econ/protection").json()
    assert before["connected"] is False and before["feed"]["last_success"] is None
    assert "has not fetched yet" in before["note"]
    assert client.post("/econ/feed/sync").status_code == 401
    synced = client.post("/econ/feed/sync", headers={"X-Webhook-Secret": "dev-control-key"}).json()
    assert synced["events"] == 3 and synced["last_error"] is None
    after = client.get("/econ/protection").json()
    assert after["connected"] is True and after["feed"]["events"] == 3
