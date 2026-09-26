"""Measured service status (services/status_monitor.py) and /status/public."""
import pytest

from services import status_monitor as sm
from services.status_monitor import DEGRADED, OPERATIONAL, OUTAGE, UNKNOWN, StatusMonitor

T0 = 1_780_000_000.0  # a fixed UTC instant


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, seconds=60):
        self.t += seconds


class Switch:
    """A probe whose answer the test sets."""

    def __init__(self, state=OPERATIONAL, detail="fine"):
        self.state, self.detail = state, detail

    def __call__(self):
        return self.state, self.detail


@pytest.fixture()
def rig(tmp_path):
    clock, db, alerts = Clock(), Switch(), []
    mon = StatusMonitor(tmp_path / "status.db", {"api": sm.api_probe, "database": db},
                        notify=alerts.append, interval_s=60, confirm=2, clock=clock)
    return mon, clock, db, alerts


def run(mon, clock, n):
    for _ in range(n):
        mon.sample()
        clock.tick()


def test_healthy_components_report_operational_with_full_uptime(rig):
    mon, clock, _, alerts = rig
    run(mon, clock, 3)
    view = mon.public_view(cache_s=0)
    assert view["overall"] == OPERATIONAL and alerts == [] and view["incidents"] == []
    db = next(c for c in view["components"] if c["id"] == "database")
    assert db["state"] == OPERATIONAL and db["uptime_pct"] == 100.0
    assert len(db["days"]) == 90 and db["days"][-1]["state"] == OPERATIONAL
    assert db["days"][0]["state"] == UNKNOWN  # before monitoring began: no data, not "up"


def test_one_bad_minute_is_not_an_incident(rig):
    mon, clock, db, alerts = rig
    run(mon, clock, 2)
    db.state = OUTAGE
    run(mon, clock, 1)
    db.state = OPERATIONAL
    run(mon, clock, 3)
    assert mon.public_view(cache_s=0)["incidents"] == [] and alerts == []


def test_a_confirmed_outage_opens_notifies_and_closes_with_its_true_start(rig):
    mon, clock, db, alerts = rig
    run(mon, clock, 2)
    first_bad = clock.t
    db.state, db.detail = OUTAGE, "The ledger is not answering"
    run(mon, clock, 3)
    view = mon.public_view(cache_s=0)
    assert view["overall"] == OUTAGE
    [inc] = view["incidents"]
    assert inc["ongoing"] and inc["component"] == "database" and inc["state"] == OUTAGE
    assert inc["started_at"] == sm._iso(first_bad)
    assert alerts[-1]["severity"] == "critical" and "Database" in alerts[-1]["title"]

    db.state = OPERATIONAL
    recovered_at = clock.t
    run(mon, clock, 2)
    [inc] = mon.public_view(cache_s=0)["incidents"]
    assert not inc["ongoing"] and inc["ended_at"] == sm._iso(recovered_at)
    assert alerts[-1]["title"] == "Database: recovered"


def test_a_component_broken_from_the_first_sample_still_opens_an_incident(rig):
    mon, clock, db, alerts = rig
    db.state = OUTAGE
    run(mon, clock, 2)
    assert [i["component"] for i in mon.public_view(cache_s=0)["incidents"]] == ["database"]
    assert len(alerts) == 1


def test_an_open_incident_takes_the_worst_state_seen(rig):
    mon, clock, db, _ = rig
    run(mon, clock, 2)
    db.state = DEGRADED
    run(mon, clock, 2)
    db.state = OUTAGE
    run(mon, clock, 1)
    [inc] = mon.public_view(cache_s=0)["incidents"]
    assert inc["state"] == OUTAGE


def test_a_gap_in_samples_is_counted_as_api_downtime(rig):
    mon, clock, _, alerts = rig
    run(mon, clock, 2)
    clock.tick(10 * 60)  # the process was down for ten minutes
    run(mon, clock, 1)
    view = mon.public_view(cache_s=0)
    api = next(c for c in view["components"] if c["id"] == "api")
    assert api["days"][-1]["samples"]["outage"] >= 9
    assert api["uptime_pct"] < 100
    [inc] = view["incidents"]
    assert inc["component"] == "api" and not inc["ongoing"] and inc["duration_min"] >= 10
    assert alerts[-1]["title"] == "API and dashboard: recovered"


def test_probe_errors_are_outages_and_never_leak_their_text(tmp_path):
    def broken():
        raise RuntimeError("postgres://user:hunter2@db.internal:5432 refused")
    clock = Clock()
    mon = StatusMonitor(tmp_path / "s.db", {"database": broken}, interval_s=60, confirm=1, clock=clock)
    run(mon, clock, 2)
    view = mon.public_view(cache_s=0)
    assert view["components"][0]["state"] == OUTAGE
    assert "hunter2" not in str(view) and "db.internal" not in str(view)


def test_a_silent_monitor_reports_unknown_rather_than_the_last_good_state(rig):
    mon, clock, _, _ = rig
    run(mon, clock, 3)
    clock.tick(10 * 60)
    view = mon.public_view(cache_s=0)
    assert view["overall"] == UNKNOWN
    assert all(c["state"] == UNKNOWN for c in view["components"])


def test_worker_and_market_data_probes():
    rows = []
    workers, feed = sm.workers_probe(lambda: rows), sm.market_data_probe(lambda: rows)
    assert workers() == (OPERATIONAL, "No workers scheduled") and feed()[0] == UNKNOWN
    rows[:] = [{"alive": True, "market_data_status": "healthy"},
               {"alive": True, "market_data_status": "healthy"}]
    assert workers() == (OPERATIONAL, "2 of 2 running") and feed() == (OPERATIONAL, "Candles current")
    rows[1] = {"alive": False, "market_data_status": ""}
    assert workers() == (DEGRADED, "1 of 2 running")
    rows[0]["market_data_status"] = "stale"
    assert feed()[0] == DEGRADED
    rows[0]["alive"] = False
    assert workers()[0] == OUTAGE


def test_database_probe_marks_slow_answers_degraded(monkeypatch):
    ticks = iter([0.0, 5.0])
    monkeypatch.setattr(sm.time, "monotonic", lambda: next(ticks))
    assert sm.database_probe(lambda: None, slow_s=3.0)()[0] == DEGRADED


def test_public_status_endpoint_needs_no_sign_in(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import app as hub_app
    import webhook_api
    clock = Clock()
    mon = StatusMonitor(tmp_path / "s.db", {"api": sm.api_probe}, interval_s=60, confirm=1, clock=clock)
    run(mon, clock, 2)
    monkeypatch.setattr(webhook_api, "status_monitor", mon)
    r = TestClient(hub_app.app).get("/status/public")
    assert r.status_code == 200
    assert r.json()["components"][0]["id"] == "api"
    assert "max-age=30" in r.headers["cache-control"]


def test_degraded_turning_into_an_outage_is_one_incident(rig):
    mon, clock, db, alerts = rig
    run(mon, clock, 2)
    db.state = DEGRADED
    run(mon, clock, 3)
    db.state = OUTAGE
    run(mon, clock, 3)
    db.state = OPERATIONAL
    run(mon, clock, 2)
    [inc] = mon.public_view(cache_s=0)["incidents"]
    assert inc["state"] == OUTAGE and not inc["ongoing"]
    assert [a["title"] for a in alerts] == ["Database: degraded", "Database: outage", "Database: recovered"]


# ─────────────────────── private "why" on the owner's alerts ───────────────────────
def test_an_outage_alert_says_why_but_the_public_page_does_not(tmp_path):
    clock, db, alerts = Clock(), Switch(), []
    mon = StatusMonitor(tmp_path / "s.db", {"database": db}, notify=alerts.append, confirm=2, clock=clock,
                        explain={"database": lambda: "sqlite3.OperationalError: disk I/O error"})
    run(mon, clock, 2)
    db.state, db.detail = OUTAGE, "The ledger is not answering"
    run(mon, clock, 3)
    assert alerts[-1]["detail"] == "The ledger is not answering. sqlite3.OperationalError: disk I/O error"
    view = mon.public_view(cache_s=0)
    assert "OperationalError" not in str(view)          # error text never goes public
    [inc] = view["incidents"]
    assert inc["detail"] == "The ledger is not answering"


def test_a_broken_explanation_never_stops_the_alert(tmp_path):
    clock, db, alerts = Clock(), Switch(), []

    def broken():
        raise RuntimeError("no")
    mon = StatusMonitor(tmp_path / "s.db", {"database": db}, notify=alerts.append, confirm=2, clock=clock,
                        explain={"database": broken})
    run(mon, clock, 2)
    db.state, db.detail = OUTAGE, "The ledger is not answering"
    run(mon, clock, 3)
    assert alerts[-1]["detail"] == "The ledger is not answering"


def test_a_recovery_reads_as_a_recovery(rig):
    mon, clock, db, alerts = rig
    run(mon, clock, 2)
    db.state, db.detail = OUTAGE, "The ledger is not answering"
    run(mon, clock, 3)
    db.state = OPERATIONAL
    run(mon, clock, 2)
    assert alerts[-1]["title"] == "Database: recovered"
    assert alerts[-1]["detail"].startswith("Back to normal after ")
    assert alerts[-1]["detail"].endswith("(was: The ledger is not answering)")


class _Inst:
    def __init__(self, iid, symbol, state, last_error="", desired=True, mode="trading", tf="5m"):
        self.id, self.symbol, self.state, self.last_error = iid, symbol, state, last_error
        self.desired_running, self.mode, self.timeframe = desired, mode, tf


class _Manager:
    def __init__(self, instances, alive=()):
        self._instances = {i.id: i for i in instances}
        self._alive = set(alive)

    def worker_alive(self, iid):
        return iid in self._alive


class _Supervisor:
    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


def test_the_workers_explanation_names_each_stopped_worker_and_the_supervisors_plan():
    manager = _Manager([
        _Inst("a", "BTCUSDT", "error", "Binance USD-M market-data hub failed to start"),
        _Inst("b", "ETHUSDT", "blocked", "reconciliation mismatch"),
        _Inst("c", "SOLUSDT", "running"),                    # alive: not mentioned
        _Inst("d", "XRPUSDT", "stopped", desired=False),     # not scheduled: not mentioned
    ], alive={"c"})
    supervisor = _Supervisor({"running": True, "last_error": None,
                              "backoff": {"a": {"consecutive_failures": 4, "retry_in_s": 240.4}},
                              "last_report": [{"instance_id": "b", "action": "blocked"}]})
    text = sm.workers_explainer(manager, supervisor)()
    assert "BTCUSDT 5m: error (Binance USD-M market-data hub failed to start)" in text
    assert "restart attempt 4 failed, next in 240s" in text
    assert "ETHUSDT 5m: blocked (reconciliation mismatch); blocked, needs you" in text
    assert "SOLUSDT" not in text and "XRPUSDT" not in text

    stopped = sm.workers_explainer(manager, _Supervisor({"running": False}))()
    assert stopped.startswith("the instance supervisor is not running")
    assert sm.workers_explainer(manager, None)()          # works without a supervisor


def test_the_market_data_explanation_lists_the_late_feeds():
    rows = [{"symbol": "BTCUSDT", "alive": True, "market_data_status": "stale"},
            {"symbol": "ETHUSDT", "alive": True, "market_data_status": "live"},
            {"symbol": "SOLUSDT", "alive": False, "market_data_status": "stale"}]
    assert sm.market_data_explainer(lambda: rows)() == "BTCUSDT: stale"


def test_the_server_wires_the_explanations_in():
    pytest.importorskip("fastapi")
    import webhook_api
    assert set(webhook_api.status_monitor.explain) == {"workers", "market_data"}
