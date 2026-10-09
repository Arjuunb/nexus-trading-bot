"""A closed-journal observer must revisit older open rows, not trade them."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import sqlite3

import pytest

from services.smc_agent_journal import SMCAgentJournal
from services.guardian_smc_journal_read_model import smc_journal_page
from tradexa.guardian.smc_journal_history import (
    GuardianSMCJournalHistory, INITIAL_CURSOR, SCOPE, smc_journal_history_view,
)
from tradexa.guardian.store import GuardianStore

KEY = "independent-journal-observer-key-123456"
URL = "http://app:8000/guardian/smc-journal"


@pytest.fixture
def journal(tmp_path):
    instance = SMCAgentJournal(tmp_path / "agent.db")
    yield instance
    instance.close()


def opened(journal, index=0):
    return journal.open_trade(
        decision_id=f"decision-{index}", symbol="BTCUSDT", timeframe="5m", direction="long",
        entry=100, stop=90, target=120, planned_rr=2, size=.01,
        order_id=f"order-{index}", setup_id=f"setup-{index}", proposal_id=f"proposal-{index}",
        strategy_fingerprint="frozen-fixture", why="fixture, never real trading",
        opened_at=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat())


def closed(journal, trade):
    journal.close_trade(trade, exit_price=120, realised_r=2, result="WIN",
                        close_reason="target", closed_at=datetime.now(timezone.utc).isoformat())


def envelope(journal, cursor):
    return {"schema_version": 1, "scope": SCOPE, "execution_integrity_verified": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "page": smc_journal_page(journal.path, cursor=cursor)}


def collector(journal, store, **kwargs):
    return GuardianSMCJournalHistory(store, URL, KEY,
                                    fetch=lambda cursor: envelope(journal, cursor), **kwargs)


def test_late_close_of_old_row_is_found_after_restart_and_deduplicated(journal, tmp_path):
    trades = [opened(journal, n) for n in range(70)]
    closed(journal, trades[-1])
    path = tmp_path / "guardian.db"
    store = GuardianStore(path)
    observer = collector(journal, store)
    assert observer.poll() == 0  # open rows still advance a durable scan checkpoint
    assert store.observer_scan_cursor(observer.component)["after"] == 32
    closed(journal, trades[0])  # older than the cursor: found on the NEXT pass
    restarted = collector(journal, GuardianStore(path))
    assert [restarted.poll() for _ in range(5)] == [0, 1, 1, 0, 0]
    assert store.count() == 2
    assert store.observer_scan_cursor(observer.component)["cycle"] == 2
    for _ in range(100):
        assert restarted.poll() == 0
    events = smc_journal_history_view(store)["events"]
    assert {e["evidence"]["trade"]["id"] for e in events} == {trades[0], trades[-1]}
    assert len({e["event_id"] for e in events}) == 2
    assert all(e["execution_id"] is None and e["position_id"] is None for e in events)
    assert all(e["evidence"]["broker_execution_verified"] is False for e in events)


@pytest.mark.parametrize("boundary", ["event", "checkpoint", "after_commit"])
def test_scan_events_and_checkpoint_commit_atomically(journal, tmp_path, monkeypatch, boundary):
    closed(journal, opened(journal))
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    if boundary == "after_commit":
        original = store.record_heartbeat
        def fail(component, state, **kwargs):
            if state == "HEALTHY":
                raise sqlite3.OperationalError("fixture crash after durable import")
            return original(component, state, **kwargs)
        monkeypatch.setattr(store, "record_heartbeat", fail)
    else:
        table = "events" if boundary == "event" else "observer_scan_state"
        with sqlite3.connect(store.path) as db:
            db.execute(f"CREATE TRIGGER injected BEFORE INSERT ON {table} "
                       "BEGIN SELECT RAISE(ABORT, 'fixture disk full'); END")
    with pytest.raises(sqlite3.Error):
        observer.poll()
    committed = boundary == "after_commit"
    assert store.count() == int(committed)
    assert store.observer_scan_cursor(observer.component)["cycle"] == int(committed)
    assert smc_journal_history_view(store)["history_state"] == "UNKNOWN"
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER IF EXISTS injected")
    assert collector(journal, GuardianStore(store.path)).poll() == int(not committed)
    assert store.count() == 1


def test_new_rows_cannot_starve_repeat_scan_of_older_open_trades(journal, tmp_path):
    trades = [opened(journal, n) for n in range(40)]
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert observer.poll() == 0
    assert store.observer_scan_cursor(observer.component)["upper"] == 40
    for n in range(40, 80):
        opened(journal, n)
    closed(journal, trades[0])
    assert observer.poll() == 0
    assert store.observer_scan_cursor(observer.component)["cycle"] == 1
    assert observer.poll() == 1
    assert store.count() == 1


def test_refresh_never_mutates_journal_or_creates_source_schema(journal, tmp_path):
    closed(journal, opened(journal))
    before = list(journal._db.iterdump())
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    for _ in range(100):
        observer.poll()
        smc_journal_history_view(store)
    assert store.count() == 1
    assert list(journal._db.iterdump()) == before
    assert "why" not in str(store.recent(1))
    assert "conditions_json" not in str(store.recent(1))
    assert "market_json" not in str(store.recent(1))


def test_wal_writer_read_missing_file_and_empty_journal(journal, tmp_path):
    trade = opened(journal)
    with sqlite3.connect(journal.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE agent_trades SET closed_at='2026-01-01T00:00:00Z' WHERE id=?", (trade,))
        page = smc_journal_page(journal.path, cursor=INITIAL_CURSOR)
        assert page["trades"][0]["closed_at"] is None
        writer.rollback()
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.Error):
        smc_journal_page(missing, cursor=INITIAL_CURSOR)
    assert not missing.exists()


def test_empty_source_and_later_first_close_are_observed_without_fake_execution(journal, tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert observer.poll() == 0
    assert store.observer_scan_cursor(observer.component)["cycle"] == 1
    closed(journal, opened(journal))
    assert observer.poll() == 1
    assert observer.poll() == 0
    assert store.count() == 1


def test_closing_predecessor_and_upper_rows_does_not_break_scan_anchor(journal, tmp_path):
    trades = [opened(journal, n) for n in range(40)]
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert observer.poll() == 0
    closed(journal, trades[31])
    closed(journal, trades[-1])
    assert observer.poll() == 1
    assert observer.poll() == 1  # predecessor is revisited on the next finite pass
    assert observer.poll() == 0
    assert store.count() == 2


def test_paged_read_of_imported_history_and_different_decisions(journal, tmp_path):
    for n in range(65):
        closed(journal, opened(journal, n))
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert [observer.poll() for _ in range(3)] == [32, 32, 1]
    after, events = 0, []
    while True:
        page = smc_journal_history_view(store, after=after)
        assert len(page["events"]) <= 32
        events += page["events"]
        after = page["next_after"]
        if not page["has_more"]:
            break
    assert len(events) == len({e["event_id"] for e in events}) == 65
    assert len({e["order_id"] for e in events}) == 65
    assert len({e["correlation_id"] for e in events}) == 65
    assert page["history_state"] == "PASS_COMPLETED_AT_LAST_POLL"
    assert page["whole_scan_atomic"] is page["net_pnl_verified"] is False
    assert page["currency_verified"] is page["execution_integrity_verified"] is False


@pytest.mark.parametrize("change", ["stale", "future", "schema", "scope", "verified", "non_atomic",
    "missing_origin", "missing_upper", "wrong_origin", "oversized", "duplicate", "unordered",
    "bad_sequence", "nan", "long_text", "secret_text", "future_close", "next_cycle", "skip_tail"])
def test_malformed_source_evidence_cannot_change_checkpoint(journal, tmp_path, change):
    closed(journal, opened(journal))
    closed(journal, opened(journal, 1))
    value = envelope(journal, INITIAL_CURSOR)
    page = value["page"]
    if change in {"stale", "future"}:
        value["observed_at"] = (datetime.now(timezone.utc) + timedelta(seconds=-100 if change == "stale" else 100)).isoformat()
    elif change == "schema":
        value["schema_version"] = True
    elif change == "scope":
        value["scope"] = "ALL_ACCOUNT_EXECUTION"
    elif change == "verified":
        value["execution_integrity_verified"] = True
    elif change == "non_atomic":
        page["atomic_snapshot"] = False
    elif change == "missing_origin":
        page["first_trade"] = None
    elif change == "missing_upper":
        page["upper_trade"] = None
    elif change == "wrong_origin":
        page["origin"] = "f" * 64
    elif change == "oversized":
        page["trades"] *= 17
    elif change == "duplicate":
        page["trades"] *= 2
    elif change == "unordered":
        page["trades"].reverse()
    elif change == "bad_sequence":
        page["trades"][0]["source_sequence"] = True
    elif change == "nan":
        page["trades"][1]["entry"] = float("nan")
    elif change in {"long_text", "secret_text"}:
        page["trades"][1]["close_reason"] = "x" * 257 if change == "long_text" else "Bearer fixture-secret"
    elif change == "future_close":
        page["trades"][1]["closed_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    elif change == "next_cycle":
        page["next_cursor"]["cycle"] = 2
    else:
        page["trades"].pop()
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCJournalHistory(store, URL, KEY, fetch=lambda c: deepcopy(value))
    with pytest.raises(ValueError):
        observer.poll()
    assert store.count() == 0
    assert store.observer_scan_cursor(observer.component) == INITIAL_CURSOR
    assert smc_journal_history_view(store)["history_state"] == "UNKNOWN"


@pytest.mark.parametrize("field,value", [("size", float("inf")), ("direction", "buy"),
    ("symbol", "x" * 10000), ("requested_size", -1), ("risk_amount", -1), ("size_capped", 2),
    ("closed_at", "1900-01-01T00:00:00Z"), ("opened_at", "not-a-date")])
def test_source_projection_rejects_corruption_before_export(journal, field, value):
    trade = opened(journal)
    # Fault injection in a disposable fixture only, never deployed evidence.
    with journal._db:
        journal._db.execute("DROP TRIGGER agent_trades_plan_is_fixed")
        journal._db.execute(f"UPDATE agent_trades SET {field}=? WHERE id=?", (value, trade))
    with pytest.raises(ValueError):
        smc_journal_page(journal.path, cursor=INITIAL_CURSOR)


def test_changed_source_origin_and_missing_predecessor_block_without_discarding_history(journal, tmp_path):
    trades = [opened(journal, n) for n in range(40)]
    closed(journal, trades[1])
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert observer.poll() == 1
    checkpoint = store.observer_scan_cursor(observer.component)
    with journal._db:
        journal._db.execute("UPDATE agent_trades SET id='replaced-origin' WHERE id=?", (trades[0],))
    with pytest.raises(ValueError):
        observer.poll()
    assert store.observer_scan_cursor(observer.component) == checkpoint
    assert store.count() == 1
    with journal._db:
        journal._db.execute("UPDATE agent_trades SET id=? WHERE id='replaced-origin'", (trades[0],))
        journal._db.execute("DROP TRIGGER agent_trades_no_delete")
        journal._db.execute("DELETE FROM agent_trades WHERE id=?", (trades[31],))
    with pytest.raises(ValueError):
        observer.poll()
    assert store.observer_scan_cursor(observer.component) == checkpoint
    assert store.count() == 1


def test_changed_closed_record_is_a_collision_not_silent_overwrite(journal, tmp_path):
    trade = opened(journal)
    closed(journal, trade)
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    assert observer.poll() == 1
    checkpoint = store.observer_scan_cursor(observer.component)
    with journal._db:
        journal._db.execute("DROP TRIGGER agent_trades_close_once")
        journal._db.execute("UPDATE agent_trades SET exit_price=119 WHERE id=?", (trade,))
    with pytest.raises(ValueError, match="collision"):
        observer.poll()
    assert store.count() == 1
    assert store.observer_scan_cursor(observer.component) == checkpoint
    assert store.recent(1)[0]["evidence"]["trade"]["exit_price"] == 120


def test_concurrent_checkpoint_update_is_compare_and_swap(journal, tmp_path, monkeypatch):
    closed(journal, opened(journal))
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(journal, store)
    original = store.append_observed_scan_page
    def raced(*args, **kwargs):
        assert original(*args, **kwargs) == 1
        return original(*args, **kwargs)
    monkeypatch.setattr(store, "append_observed_scan_page", raced)
    with pytest.raises(ValueError, match="concurrently"):
        observer.poll()
    assert store.count() == 1
    assert store.observer_scan_cursor(observer.component)["cycle"] == 1
    assert collector(journal, GuardianStore(store.path)).poll() == 0


def test_source_route_auth_lock_and_retry_without_runtime_work(journal, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module
    from config import settings
    closed(journal, opened(journal))
    journal.close()
    with sqlite3.connect(journal.path) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    monkeypatch.setattr(settings, "smc_agent_journal_db", str(journal.path))
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    client, route = TestClient(app_module.app), "/guardian/smc-journal"
    headers = {"X-Guardian-Observer-Key": KEY}
    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-Guardian-Observer-Key": settings.admin_key}).status_code == 401
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
        assert response.json()["detail"] == {"state": "PERSISTENCE_BLOCKED", "code": "SMC_JOURNAL_HISTORY_UNAVAILABLE"}
        assert str(journal.path) not in response.text
        writer.rollback()
    assert client.get(route, headers=headers).status_code == 200


@pytest.mark.parametrize("url", ["https://app:8000/guardian/smc-journal", "http://outside:8000/guardian/smc-journal",
    "http://app:8000/guardian/smc-execution", "http://app:8000/guardian/smc-journal?after=0",
    "http://user:pass@app:8000/guardian/smc-journal", "http://app:8001/guardian/smc-journal"])
def test_collector_rejects_untrusted_source_urls(tmp_path, url):
    with pytest.raises(ValueError):
        GuardianSMCJournalHistory(GuardianStore(tmp_path / "guardian.db"), url, KEY)


def test_fetch_bound_and_no_redirect(monkeypatch, tmp_path):
    import tradexa.guardian.smc_journal_history as module
    from tradexa.guardian.lab_execution_observer import _NoRedirect
    observer = GuardianSMCJournalHistory(GuardianStore(tmp_path / "guardian.db"), URL, KEY)
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
        def open(self, req, timeout):
            assert timeout == 3
            assert req.get_header("X-guardian-observer-key") == KEY
            return Response()
    def build(handler):
        assert isinstance(handler, _NoRedirect)
        return Opener()
    monkeypatch.setattr(module, "build_opener", build)
    with pytest.raises(ValueError, match="bound"):
        observer.poll()
    assert observer.store.observer_scan_cursor(observer.component) == INITIAL_CURSOR
    assert smc_journal_history_view(observer.store)["history_state"] == "UNKNOWN"
