"""Retained intent events are evidence, never Guardian execution authority."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import sqlite3

import pytest

from services.smc_agent_journal import SMCAgentJournal
from services.guardian_smc_intent_read_model import smc_intent_event_page
from tradexa.guardian.smc_intent_history import GuardianSMCIntentHistory, SCOPE, smc_intent_history_view
from tradexa.guardian.store import GuardianStore

KEY = "independent-intent-history-key-123456789"
URL = "http://app:8000/guardian/smc-intent-events"


@pytest.fixture
def journal(tmp_path):
    instance = SMCAgentJournal(tmp_path / "agent.db")
    yield instance
    instance.close()


def lifecycle(journal, key="decision-1", **identity):
    values = dict(execution_key=key, session_id="session-1", symbol="BTCUSDT", timeframe="5m",
                  candle_time="2026-01-01T00:00:00Z", proposal_id=key,
                  payload={"private_fixture": "do not export"})
    values.update(identity)
    journal.create_execution_intent(**values)
    journal.transition_execution(key, "EXECUTION_PENDING")
    journal.prepare_order_request(key, {"symbol": values["symbol"], "quantity": .01})
    journal.transition_execution(key, "EXECUTED", broker_order_id="order-" + key,
                                 decision_id="later-decision-" + key)
    journal.transition_execution(key, "COMPLETE", trade_id="trade-" + key)


def envelope(journal, after=0, anchor=""):
    return {"schema_version": 1, "scope": SCOPE, "execution_integrity_verified": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "page": smc_intent_event_page(journal.path, after=after, anchor=anchor)}


def collector(journal, store, **kwargs):
    return GuardianSMCIntentHistory(store, URL, KEY,
                                   fetch=lambda after, anchor: envelope(journal, after, anchor), **kwargs)


def test_full_retained_history_resumes_and_preserves_original_event_links(journal, tmp_path):
    for n in range(27):
        lifecycle(journal, f"decision-{n}")
    before = list(journal._db.iterdump())
    path = tmp_path / "guardian.db"
    store = GuardianStore(path)
    assert collector(journal, store).poll() == 32
    observer = collector(journal, GuardianStore(path))
    assert [observer.poll() for _ in range(4)] == [32, 32, 32, 7]
    assert store.count() == 135
    for _ in range(100):
        assert observer.poll() == 0
    assert store.count() == 135
    assert list(journal._db.iterdump()) == before
    page = smc_intent_history_view(store)
    assert page["history_state"] == "CAUGHT_UP_AT_LAST_POLL"
    first = page["events"][:5]
    assert [e["state_after"] for e in first] == ["DECISION_APPROVED", "EXECUTION_PENDING",
                                               "EXECUTION_PENDING", "EXECUTED", "COMPLETE"]
    assert [e["order_id"] for e in first] == [None, None, None, "order-decision-0", "order-decision-0"]
    assert [e["evidence"]["transition"]["trade_id"] for e in first] == [None] * 4 + ["trade-decision-0"]
    assert {e["execution_id"] for e in first} == {"decision-0"}
    assert all(e["correlation_id"] is None for e in first)  # later decision_id is NOT historical evidence
    assert all(e["state_before"] is None for e in first)
    assert "private_fixture" not in str(store.recent(200))
    assert page["execution_integrity_verified"] is page["full_lifecycle_verified"] is False


@pytest.mark.parametrize("boundary", ["event", "checkpoint", "after_commit"])
def test_atomic_page_failure_and_restart_do_not_lose_transitions(journal, tmp_path, monkeypatch, boundary):
    lifecycle(journal)
    store = GuardianStore(tmp_path / "guardian.db")
    if boundary != "after_commit":
        table = "events" if boundary == "event" else "observer_cursors"
        with sqlite3.connect(store.path) as db:
            db.execute(f"CREATE TRIGGER injected BEFORE INSERT ON {table} "
                       "BEGIN SELECT RAISE(ABORT, 'fixture disk full'); END")
    else:
        original = store.record_heartbeat
        def fail(component, state, **kwargs):
            if state == "HEALTHY":
                raise sqlite3.OperationalError("fixture crash after commit")
            return original(component, state, **kwargs)
        monkeypatch.setattr(store, "record_heartbeat", fail)
    observer = collector(journal, store)
    with pytest.raises(sqlite3.Error):
        observer.poll()
    committed = boundary == "after_commit"
    assert store.count() == (5 if committed else 0)
    assert store.observer_cursor(observer.component)[0] == (5 if committed else 0)
    assert smc_intent_history_view(store)["history_state"] == "UNKNOWN"
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER IF EXISTS injected")
    assert collector(journal, GuardianStore(store.path)).poll() == (0 if committed else 5)
    assert store.count() == 5


def test_late_source_timestamp_is_imported_by_sequence_not_clock(journal, tmp_path):
    original = journal._db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    assert original
    journal.create_execution_intent(execution_key="late", symbol="ETHUSDT", timeframe="15m")
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert observer.poll() == 1
    journal.transition_execution("late", "EXECUTION_UNCERTAIN", error="Bearer fixture-do-not-export")
    # Simulate a retained late-arriving row with an older source clock. The
    # immutable writer stays unchanged; only this disposable fixture is altered.
    with journal._db:
        journal._db.execute("DROP TRIGGER execution_intent_events_no_update")
        journal._db.execute("UPDATE execution_intent_events SET created_at='2001-01-01T00:00:00Z' WHERE rowid=2")
    assert observer.poll() == 1
    event = store.recent(1)[0]
    assert event["timestamp"].startswith("2001-")
    assert event["state_after"] == "EXECUTION_UNCERTAIN"
    assert event["evidence"]["transition"]["error_recorded"] is True
    assert "Bearer" not in str(event)
    assert event["order_id"] is None  # not interpreted as proof of no order
    assert event["evidence"]["broker_execution_verified"] is False


def test_late_bound_decision_and_current_state_do_not_change_prior_event_anchor(journal, tmp_path):
    journal.create_execution_intent(execution_key="same", symbol="BTCUSDT", timeframe="5m")
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert observer.poll() == 1
    original = store.recent(1)[0]
    journal.transition_execution("same", "EXECUTION_PENDING")
    journal.transition_execution("same", "EXECUTED", decision_id="late-decision", broker_order_id="existing-order")
    assert collector(journal, GuardianStore(store.path)).poll() == 2
    assert observer.poll() == 0
    assert store.recent(3)[-1]["event_id"] == original["event_id"]
    assert store.count() == 3
    assert original["order_id"] is None and original["correlation_id"] is None
    assert original["session_id"] == ""  # legacy absence retained, no invented session


def test_failed_uncertain_executed_and_legacy_reconciled_are_recorded_not_certified(journal, tmp_path):
    journal.create_execution_intent(execution_key="failed", symbol="BTCUSDT", timeframe="5m")
    journal.transition_execution("failed", "EXECUTION_FAILED", error="fixture submission failure")
    journal.create_execution_intent(execution_key="uncertain", symbol="ETHUSDT", timeframe="15m", session_id="session-2")
    journal.transition_execution("uncertain", "EXECUTION_UNCERTAIN", error="fixture unknown commit")
    journal.transition_execution("uncertain", "EXECUTED", broker_order_id="existing-order")
    # Older retained journals can contain the declared RECONCILED state. Read
    # observation does not assert it was a valid current-writer transition.
    with journal._db:
        journal._db.execute("INSERT INTO execution_intent_events(id,execution_key,state,created_at) VALUES (?,?,?,?)",
                            ("legacy-reconciled", "uncertain", "RECONCILED", datetime.now(timezone.utc).isoformat()))
    store = GuardianStore(tmp_path / "guardian.db")
    assert collector(journal, store).poll() == 6
    events = smc_intent_history_view(store)["events"]
    assert {e["state_after"] for e in events} == {"DECISION_APPROVED", "EXECUTION_FAILED",
                                                 "EXECUTION_UNCERTAIN", "EXECUTED", "RECONCILED"}
    assert {e["symbol"] for e in events if e["execution_id"] == "uncertain"} == {"ETHUSDT"}
    assert all(e["evidence"]["broker_execution_verified"] is False for e in events)
    assert all(e["decision"] is None and e["position_id"] is None for e in events)


def test_empty_journal_then_new_events_and_missing_database(journal, tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert observer.poll() == 0
    assert store.observer_cursor(observer.component) == (0, "")
    lifecycle(journal)
    assert observer.poll() == 5
    assert store.count() == 5
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.Error):
        smc_intent_event_page(missing)
    assert not missing.exists()


def test_paged_read_and_multi_market_session_execution_identity(journal, tmp_path):
    for n in range(14):
        lifecycle(journal, f"decision-{n}", session_id=f"session-{n}",
                  symbol="BTCUSDT" if n % 2 else "ETHUSDT", timeframe="5m" if n % 2 else "15m")
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert [observer.poll() for _ in range(3)] == [32, 32, 6]
    after, events = 0, []
    while True:
        page = smc_intent_history_view(store, after=after)
        assert len(page["events"]) <= 32
        events += page["events"]
        after = page["next_after"]
        if not page["has_more"]:
            break
    assert len(events) == len({e["event_id"] for e in events}) == 70
    assert len({e["execution_id"] for e in events}) == len({e["session_id"] for e in events}) == 14
    assert all(e["evidence"]["transition"]["execution_key"] == e["execution_id"] for e in events)
    now = datetime.now(timezone.utc)
    assert smc_intent_history_view(store, now=now + timedelta(seconds=100))["history_state"] == "UNKNOWN"
    assert len(smc_intent_history_view(store, now=now + timedelta(seconds=100))["events"]) == 32


def test_failure_halfway_into_second_page_rolls_back_all_new_events(journal, tmp_path):
    for n in range(14):
        lifecycle(journal, f"decision-{n}")
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert observer.poll() == 32
    checkpoint = store.observer_cursor(observer.component)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER injected BEFORE INSERT ON events WHEN "
                   "(SELECT count(*) FROM events WHERE source_service='guardian_smc_intent_history')>=33 "
                   "BEGIN SELECT RAISE(ABORT,'fixture failure at second insert'); END")
    with pytest.raises(sqlite3.Error):
        observer.poll()
    assert store.count() == 32 and store.observer_cursor(observer.component) == checkpoint
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER injected")
    assert collector(journal, GuardianStore(store.path)).poll() == 32
    assert store.count() == 64


@pytest.mark.parametrize("change", ["stale", "future", "schema", "scope", "verified", "non_atomic",
    "missing_origin", "wrong_origin", "oversized", "duplicate", "unordered", "bad_sequence", "bad_state",
    "long_text", "secret_text", "future_event", "next_cursor", "missing_identity", "bad_error_flag"])
def test_bad_contract_cannot_advance_history(journal, tmp_path, change):
    lifecycle(journal)
    value = envelope(journal)
    page = value["page"]
    if change in {"stale", "future"}:
        value["observed_at"] = (datetime.now(timezone.utc) + timedelta(seconds=-100 if change == "stale" else 100)).isoformat()
    elif change == "schema":
        value["schema_version"] = True
    elif change == "scope":
        value["scope"] = "ALL_EXECUTIONS_VERIFIED"
    elif change == "verified":
        value["execution_integrity_verified"] = True
    elif change == "non_atomic":
        page["atomic_snapshot"] = False
    elif change == "missing_origin":
        page["first_transition"] = None
    elif change == "wrong_origin":
        page["origin"] = "f" * 64
    elif change == "oversized":
        page["transitions"] *= 7
    elif change == "duplicate":
        page["transitions"] *= 2
    elif change == "unordered":
        page["transitions"].reverse()
    elif change == "bad_sequence":
        page["transitions"][1]["source_sequence"] = True
    elif change == "bad_state":
        page["transitions"][1]["state"] = "MISSED"
    elif change in {"long_text", "secret_text"}:
        page["transitions"][1]["broker_order_id"] = "x" * 257 if change == "long_text" else "Bearer fixture-secret"
    elif change == "future_event":
        page["transitions"][1]["created_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    elif change == "next_cursor":
        page["next_after"] = 100
    elif change == "missing_identity":
        page["transitions"][1]["intent_id"] = None
    else:
        page["transitions"][1]["error_recorded"] = 1
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCIntentHistory(store, URL, KEY, fetch=lambda *args: deepcopy(value))
    with pytest.raises(ValueError):
        observer.poll()
    assert store.count() == 0 and store.observer_cursor(observer.component) == (0, "")
    assert smc_intent_history_view(store)["history_state"] == "UNKNOWN"


@pytest.mark.parametrize("change", ["origin", "last", "removed_anchor", "parent", "reset"])
def test_changed_source_blocks_without_resetting_evidence(journal, tmp_path, change):
    lifecycle(journal)
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert observer.poll() == 5
    checkpoint = store.observer_cursor(observer.component)
    # Corruption injection only in this test's temporary database.
    with journal._db:
        journal._db.execute("DROP TRIGGER execution_intent_events_no_update")
        journal._db.execute("DROP TRIGGER execution_intent_events_no_delete")
        if change == "origin":
            journal._db.execute("UPDATE execution_intent_events SET id='replacement-origin' WHERE rowid=1")
        elif change == "last":
            journal._db.execute("UPDATE execution_intent_events SET trade_id='wrong-trade' WHERE rowid=5")
        elif change == "removed_anchor":
            journal._db.execute("DELETE FROM execution_intent_events WHERE rowid=5")
        elif change == "parent":
            journal._db.execute("DROP TRIGGER execution_identity_is_fixed")
            journal._db.execute("UPDATE execution_intents SET symbol='FOREIGN' WHERE execution_key='decision-1'")
        else:
            journal._db.execute("DELETE FROM execution_intent_events")
    with pytest.raises(ValueError):
        observer.poll()
    assert store.count() == 5 and store.observer_cursor(observer.component) == checkpoint
    assert smc_intent_history_view(store)["history_state"] == "UNKNOWN"
    assert len(smc_intent_history_view(store)["events"]) == 5


def test_missing_parent_identity_is_unavailable_not_no_order(journal):
    with journal._db:
        journal._db.execute("INSERT INTO execution_intent_events(id,execution_key,state,created_at) VALUES (?,?,?,?)",
                            ("orphan", "missing-parent", "EXECUTION_UNCERTAIN", datetime.now(timezone.utc).isoformat()))
    with pytest.raises(ValueError):
        smc_intent_event_page(journal.path)


def test_wal_snapshot_read_during_write(journal):
    lifecycle(journal)
    with sqlite3.connect(journal.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE execution_intents SET state='EXECUTION_UNCERTAIN' WHERE execution_key='decision-1'")
        page = smc_intent_event_page(journal.path)
        assert [r["state"] for r in page["transitions"]] == ["DECISION_APPROVED", "EXECUTION_PENDING",
                                                           "EXECUTION_PENDING", "EXECUTED", "COMPLETE"]
        writer.rollback()


def test_source_auth_lock_retry_and_refresh_leave_history_unchanged(journal, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module
    from config import settings
    lifecycle(journal)
    before = list(journal._db.iterdump())
    journal.close()
    with sqlite3.connect(journal.path) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    monkeypatch.setattr(settings, "smc_agent_journal_db", str(journal.path))
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    client, route = TestClient(app_module.app), "/guardian/smc-intent-events"
    headers = {"X-Guardian-Observer-Key": KEY}
    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-Guardian-Observer-Key": settings.admin_key}).status_code == 401
    assert client.get(route, headers={"X-Webhook-Secret": settings.admin_key}).status_code == 401
    response = client.get(route, headers=headers)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.json()["execution_integrity_verified"] is False
    assert client.get(route + "?after=-1", headers=headers).status_code == 422
    assert client.post(route, headers=headers).status_code == 401
    assert client.post(route, headers={"X-Webhook-Secret": settings.admin_key}).status_code == 405
    with sqlite3.connect(journal.path) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        response = client.get(route, headers=headers)
        assert response.status_code == 503
        assert response.json()["detail"] == {"state": "PERSISTENCE_BLOCKED", "code": "SMC_INTENT_HISTORY_UNAVAILABLE"}
        assert str(journal.path) not in response.text
        writer.rollback()
    for _ in range(100):
        assert client.get(route, headers=headers).status_code == 200
    with sqlite3.connect(journal.path) as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("url", ["https://app:8000/guardian/smc-intent-events",
    "http://outside:8000/guardian/smc-intent-events", "http://app:8000/guardian/smc-journal",
    "http://app:8000/guardian/smc-intent-events?after=0", "http://user:pass@app:8000/guardian/smc-intent-events",
    "http://app:8001/guardian/smc-intent-events"])
def test_untrusted_source_urls_rejected(tmp_path, url):
    with pytest.raises(ValueError):
        GuardianSMCIntentHistory(GuardianStore(tmp_path / "guardian.db"), url, KEY)


def test_concurrent_collectors_cannot_duplicate_or_skip_transitions(journal, tmp_path):
    lifecycle(journal)
    store = GuardianStore(tmp_path / "guardian.db")
    winner = collector(journal, store)
    stale = collector(journal, GuardianStore(store.path))
    def intervening_poll(after, anchor):
        view = envelope(journal, after, anchor)
        assert winner.poll() == 5
        return view
    stale.fetch = intervening_poll
    with pytest.raises(ValueError, match="concurrently"):
        stale.poll()
    assert store.count() == 5
    assert store.observer_cursor("smc_intent_history")[0] == 5
    retry = collector(journal, GuardianStore(store.path))
    assert retry.poll() == 0
    lifecycle(journal, "different-decision")
    assert retry.poll() == 5
    assert store.count() == 10
    events = smc_intent_history_view(store)["events"]
    assert len({e["event_id"] for e in events}) == 10
    assert {e["execution_id"] for e in events} == {"decision-1", "different-decision"}
    assert smc_intent_history_view(store)["history_state"] == "CAUGHT_UP_AT_LAST_POLL"


def test_fetch_bound_no_redirect_and_failure_probe(monkeypatch, tmp_path):
    import tradexa.guardian.smc_intent_history as module
    from tradexa.guardian.lab_execution_observer import _NoRedirect
    observer = GuardianSMCIntentHistory(GuardianStore(tmp_path / "guardian.db"), URL, KEY)
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def read(self, bound):
            assert bound == 262145
            return b"x" * bound
    class Opener:
        def open(self, request, timeout):
            assert timeout == 3
            assert request.get_header("X-guardian-observer-key") == KEY
            return Response()
    def build(handler):
        assert isinstance(handler, _NoRedirect)
        return Opener()
    monkeypatch.setattr(module, "build_opener", build)
    with pytest.raises(ValueError, match="bound"):
        observer.poll()
    assert smc_intent_history_view(observer.store)["history_state"] == "UNKNOWN"
    assert observer.store.count() == 0
