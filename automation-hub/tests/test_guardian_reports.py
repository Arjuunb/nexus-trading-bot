"""Guardian Phase 8: reports that say where every number came from, and
notifications only when useful.

Journal trades are the journal's own records (seeded through its store, as
the journal's tests do). Incidents are real Guardian incidents driven by the
collector row shape. The notifier is a recorder standing in for Telegram.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from data.trade_record_store import TradeRecordStore
from services.guardian.incidents import IncidentEngine
from services.guardian.reports import Reporter
from services.guardian.store import GuardianStore
from tests.test_guardian_incidents import _rows, _svc
from tests.test_guardian_research import _seed

DAY = datetime(2026, 9, 29, tzinfo=timezone.utc)          # a Tuesday: a daily report is due
MONDAY = datetime(2026, 9, 28, tzinfo=timezone.utc)       # a daily and a weekly report are due


def _observe(reporter, start: datetime, hours: float) -> None:
    """Guardian cycling once a minute for ``hours``."""
    t = start.timestamp()
    for _ in range(int(hours * 60) + 1):
        reporter.observed(t)
        t += 60


def _reporter(journal=None, **kw):
    store = GuardianStore()
    return Reporter(store, incidents=IncidentEngine(store), journal_path=journal, **kw)


def test_a_period_guardian_never_observed_gets_no_report():
    reporter = _reporter()
    assert reporter.cycle(now=(DAY + timedelta(minutes=5)).timestamp()) == []
    assert reporter.reports() == []


def test_the_daily_report_counts_forward_paper_trades_and_says_what_it_did_not_see(tmp_path):
    path = tmp_path / "records.db"
    journal = TradeRecordStore(str(path))
    yesterday = DAY - timedelta(days=1)
    _seed(journal, start=yesterday + timedelta(hours=1), n=6)
    _seed(journal, start=yesterday + timedelta(hours=1), n=6, key="bt", origin="BACKTEST")   # never counted
    _seed(journal, start=DAY - timedelta(days=3), n=4, key="old")                           # another day
    reporter = _reporter(str(path))
    _observe(reporter, yesterday, 12)                     # Guardian saw half the day
    assert reporter.cycle(now=(DAY + timedelta(minutes=5)).timestamp()) == ["daily"]
    [report] = reporter.reports("daily")
    body = report["body"]
    assert body["period"] == {"start": yesterday.isoformat(), "end": DAY.isoformat()}
    assert body["guardian_coverage"] == pytest.approx(0.5, abs=0.001)
    # the six seeded forward-paper trades: +2, -1, -1(Asia), +1.5, -1, -1(Asia)
    assert body["trades"] == {"closed": 6, "wins": 2, "losses": 4, "net_pnl": -5.0}
    assert body["integrity_checked"] is False
    text = report["text"]
    assert "Guardian observed 50.0% of this day" in text
    assert "of the time Guardian observed" in text              # never claimed for unseen hours
    assert "Unjournalled trades: not checked" in text          # never a made-up zero


def test_on_monday_the_weekly_report_says_how_much_of_the_week_guardian_saw():
    reporter = _reporter()
    _observe(reporter, MONDAY - timedelta(days=1), 24)       # only Sunday was observed
    assert reporter.cycle(now=(MONDAY + timedelta(minutes=5)).timestamp()) == ["daily", "weekly"]
    [weekly] = reporter.reports("weekly")
    assert weekly["body"]["guardian_coverage"] == pytest.approx(1 / 7, abs=0.001)
    assert "Guardian observed 14.3% of this week" in weekly["text"]


def test_without_a_journal_trades_are_not_measured_rather_than_zero():
    reporter = _reporter()
    _observe(reporter, DAY - timedelta(days=1), 24)
    reporter.cycle(now=(DAY + timedelta(minutes=5)).timestamp())
    [report] = reporter.reports("daily")
    assert report["body"]["trades"] is None
    assert "Trades: not measured" in report["text"]
    assert report["body"]["guardian_coverage"] == 1.0


def test_a_report_is_issued_once_and_kept_as_issued():
    reporter = _reporter()
    _observe(reporter, DAY - timedelta(days=1), 24)
    now = (DAY + timedelta(minutes=5)).timestamp()
    assert reporter.cycle(now=now) == ["daily"]
    assert reporter.cycle(now=now) == []
    with pytest.raises(sqlite3.DatabaseError):
        with reporter.store._lock:
            reporter.store._c.execute("UPDATE guardian_reports SET text='edited'")
    with pytest.raises(sqlite3.DatabaseError):
        with reporter.store._lock:
            reporter.store._c.execute("DELETE FROM guardian_reports")


def test_the_weekly_report_lists_unresolved_incidents_as_priorities():
    store = GuardianStore()
    rows = {"rows": _rows("healthy", "healthy")}
    svc, bus = _svc(store, rows, incident_verify_s=0)
    svc.cycle()
    crashed = _rows("healthy", "healthy")
    crashed[0].update(alive=False, lifecycle_state="error", last_error="boom")
    rows["rows"] = crashed
    for _ in range(3):
        svc.cycle()
        bus.flush()
    [incident] = svc.incidents.list(state="active")
    reporter = svc.reports
    _observe(reporter, MONDAY - timedelta(days=7), 7 * 24)
    body = reporter.weekly(MONDAY)
    assert body["guardian_coverage"] == 1.0
    assert body["unresolved_incidents"][0]["id"] == incident["id"]
    assert any(p.startswith("Unresolved: incident #") for p in body["engineering_priorities"])
    assert "(research only)" in reporter._weekly_text(body)


# ------------------------------------------------------------ notifications
def _crash_then_recover(sent):
    store = GuardianStore()
    rows = {"rows": _rows("healthy", "healthy")}
    svc, bus = _svc(store, rows, incident_verify_s=0, notify=lambda text: sent.append(text) or True)
    svc.cycle()
    crashed = _rows("healthy", "healthy")
    crashed[0].update(alive=False, lifecycle_state="error", last_error="boom")
    rows["rows"] = crashed
    for _ in range(4):
        svc.cycle()
        bus.flush()
    opened = list(sent)
    rows["rows"] = _rows("healthy", "healthy")
    for _ in range(5):
        svc.cycle()
        bus.flush()
    return svc, opened


def test_a_serious_incident_notifies_once_when_opened_and_once_when_closed():
    sent: list[str] = []
    svc, opened = _crash_then_recover(sent)
    assert len(opened) == 1 and "INCIDENT #1 (HIGH)" in opened[0] and "CONFIRMED" in opened[0]
    assert len(sent) == 2 and "CLOSED" in sent[1]                 # updates in between are not sent
    notes = [a for a in svc.store.actions(50) if a["action"] == "NOTIFY"]
    assert [a["result"] for a in notes] == ["SENT", "SENT"]


def test_a_quiet_feed_or_idle_strategy_never_notifies():
    sent: list[str] = []
    store = GuardianStore()
    rows = {"rows": _rows("healthy")}
    svc, _ = _svc(store, rows, incident_verify_s=0, notify=lambda text: sent.append(text) or True)
    for _ in range(5):
        svc.cycle()
    assert sent == []


def test_notifications_carry_no_secret_and_a_missing_channel_is_recorded():
    from config import settings
    sent: list[str] = []
    reporter = _reporter(notify=lambda text: sent.append(text) or True)
    reporter._send("k1", f"failed with {settings.admin_key}", reason="t")
    assert settings.admin_key not in sent[0]
    unconfigured = _reporter(notify=lambda text: None)
    unconfigured._send("k2", "hello", reason="t")
    assert [a["result"] for a in unconfigured.store.actions(5)] == ["NO_CHANNEL"]
    assert _reporter()._send("k3", "hello", reason="t") is False   # no notifier wired: nothing at all


def test_the_reports_and_recovery_apis_are_read_only():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    app = FastAPI()
    app.include_router(webhook_api.router)
    client = TestClient(app)
    assert "reports" in client.get("/guardian/reports").json()
    assert client.get("/guardian/reports?kind=monthly").status_code == 400
    recovery = client.get("/guardian/recovery").json()
    assert recovery["actions"]["RESTART_INSTANCE_WORKER"]["enabled"] is False
    assert client.post("/guardian/reports").status_code == 405


def test_incident_history_is_never_replayed_to_the_owners_phone():
    """Deploying notifications onto a Guardian with past incidents sends none
    of them; the next serious incident is sent."""
    store = GuardianStore()
    rows = {"rows": _rows("healthy", "healthy")}
    svc, bus = _svc(store, rows, incident_verify_s=0)          # no notifier yet
    svc.cycle()
    crashed = _rows("healthy", "healthy")
    crashed[0].update(alive=False, lifecycle_state="error", last_error="boom")
    rows["rows"] = crashed
    for _ in range(3):
        svc.cycle()
    rows["rows"] = _rows("healthy", "healthy")
    for _ in range(4):
        svc.cycle()
    assert svc.incidents.list()[0]["state"] == "CLOSED"       # history: opened and closed
    store.set_meta("notify.since", datetime.now(timezone.utc).isoformat())   # notifications begin now
    sent: list[str] = []
    reporter = Reporter(store, incidents=svc.incidents, notify=lambda text: sent.append(text) or True)
    assert reporter.notify_incidents() == 0 and sent == []
    rows["rows"] = crashed                                     # a new serious incident
    for _ in range(3):
        svc.cycle()
    assert reporter.notify_incidents() == 1 and "INCIDENT #2" in sent[0]


def test_an_issued_report_carries_no_secret():
    from config import settings
    reporter = _reporter()
    _observe(reporter, DAY - timedelta(days=1), 24)
    body = reporter.daily(DAY)
    body["guardian_actions"] = [{"reason": f"token {settings.admin_key}"}]
    reporter._issue("daily", body, f"text with {settings.admin_key}")
    [report] = reporter.reports("daily")
    assert settings.admin_key not in json.dumps(report)
