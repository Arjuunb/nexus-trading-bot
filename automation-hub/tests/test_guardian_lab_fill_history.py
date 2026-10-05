"""Retained-fill observation cannot place, repair, delete or relabel trades."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as app_module
from config import settings
from services.price_action_lab import PriceActionPaperAccount
from services.smc_strategy_lab import SMCPaperAccount
from services.guardian_lab_fill_read_model import lab_fill_page
from tradexa.guardian.lab_fill_history import GuardianLabFillHistory, SCOPE, lab_fill_history_view
from tradexa.guardian.store import GuardianStore

KEY = "guardian-history-observer-123456789"
URL = "http://app:8000/guardian/lab-fills"


@pytest.fixture(params=["PRICE_ACTION", "SMC"])
def source(tmp_path, request):
    lab = request.param
    path = tmp_path / (lab + ".db")
    account = (PriceActionPaperAccount if lab == "PRICE_ACTION" else SMCPaperAccount)(path)
    yield lab, path, account
    account._db.close()
    account.broker._c.close()
    if lab == "PRICE_ACTION":
        account.journal._db.close()


def enter(source, key="entry", **changes):
    args = dict(symbol="BTCUSDT", side="buy", order_type="limit", quantity=.01,
                limit_price=100, protection_stop_loss=90, protection_take_profit=120,
                strategy="fixture", strategy_version="1", timeframe="5m", candle_id=key)
    args.update(changes)
    return source[2].broker.submit(**args)


def candle(source, volume=100, price=100):
    return source[2].broker.process_candle("BTCUSDT", {
        "open": price, "high": price + 1, "low": price - 1, "close": price,
        "volume": volume, "timestamp": datetime.now(timezone.utc).isoformat()})


def envelope(source, after=0, anchor=""):
    return {"schema_version": 1, "scope": SCOPE, "execution_integrity_verified": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "page": lab_fill_page(source[1], source[0], after=after, anchor=anchor)}


def collector(source, store, **kwargs):
    return GuardianLabFillHistory(store, URL, KEY, source[0],
                                  fetch=lambda a, b: envelope(source, a, b), **kwargs)


def duplicate_fixture_fills(source, count):
    db = source[2].broker._c
    row = dict(db.execute("SELECT * FROM v2_fills LIMIT 1").fetchone())
    # Synthetic retained-history volume; ordinary order/fill tests below use
    # actual broker execution. This never runs against a real account database.
    with db:
        for index in range(count):
            values = {**row, "id": f"retained-{index}", "fill_key": None}
            db.execute(f"INSERT INTO v2_fills ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                       tuple(values.values()))


def test_full_retained_window_restart_and_late_timestamp_import(source, tmp_path):
    enter(source)
    candle(source)
    duplicate_fixture_fills(source, 136)  # exceeds the old 128-fill snapshot
    db = source[2].broker._c
    before = list(db.iterdump())
    path = tmp_path / "guardian.db"
    store = GuardianStore(path)
    observer = collector(source, store)
    assert observer.poll() == 32
    assert lab_fill_history_view(store, source[0])["history_state"] == "IMPORTING"
    # Cursor and facts must resume in a separately constructed Guardian instance.
    observer = collector(source, GuardianStore(path))
    assert [observer.poll() for _ in range(4)] == [32, 32, 32, 9]
    assert store.count() == 137
    cursor = store.observer_cursor(observer.component)
    assert cursor[0] == 137
    for _ in range(100):
        assert observer.poll() == 0
    assert list(db.iterdump()) == before  # no source edits, indexes or orders
    assert store.count() == 137
    assert lab_fill_history_view(store, source[0])["history_state"] == "CAUGHT_UP_AT_LAST_POLL"
    after, events = 0, []
    while True:
        page = lab_fill_history_view(store, source[0], after=after)
        events += page["events"]
        after = page["next_after"]
        if not page["has_more"]:
            break
    assert len(events) == len({e["event_id"] for e in events}) == 137
    assert len({e["order_id"] for e in events}) == 1
    assert all(e["evidence"]["execution_integrity_verified"] is False for e in events)
    # An appended fill with an older source timestamp is not lost by paging.
    enter(source, key="later", side="sell", order_type="market", reduce_only=True)
    candle(source)
    with db:
        db.execute("UPDATE v2_fills SET timestamp='2001-01-01T00:00:00+00:00' WHERE rowid=138")
    assert collector(source, GuardianStore(path)).poll() == 1
    assert store.count() == 138
    assert store.recent(1)[0]["timestamp"].startswith("2001-")


def test_partial_full_reduce_and_protective_exit_preserve_fill_truth(source, tmp_path):
    entry = enter(source)
    candle(source, volume=.2)
    candle(source)
    reduce = enter(source, key="reduce", side="sell", order_type="market", quantity=.004,
                   reduce_only=True, protection_stop_loss=None, protection_take_profit=None)
    candle(source)
    candle(source, price=80)
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(source, store)
    assert observer.poll() == 4
    data = lab_fill_history_view(store, source[0])
    fills = [e["evidence"]["fill"] for e in data["events"]]
    assert [f["order_id"] for f in fills[:3]] == [entry["id"], entry["id"], reduce["id"]]
    assert fills[3]["order_id"].startswith("protective-")
    assert sum(f["quantity"] for f in fills[:2]) == pytest.approx(.01)
    assert fills[3]["quantity"] == pytest.approx(.006)
    assert fills[3]["realized_pnl"] < 0
    assert len(source[2].broker.orders()) == 2
    assert source[2].broker.positions() == []
    assert data["full_lifecycle_verified"] is data["net_pnl_verified"] is False
    assert data["currency_verified"] is False
    assert observer.poll() == 0
    assert store.count() == 4


@pytest.mark.parametrize("boundary", ["event", "checkpoint", "after_commit"])
def test_atomic_import_faults_do_not_lose_or_duplicate_fills(source, tmp_path, monkeypatch, boundary):
    enter(source)
    candle(source)
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(source, store)
    if boundary != "after_commit":
        table = "events" if boundary == "event" else "observer_cursors"
        with sqlite3.connect(store.path) as db:
            db.execute(f"CREATE TRIGGER injected BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'fixture full disk'); END")
    else:
        original = store.record_heartbeat
        def heartbeat(component, state, **kwargs):
            if state == "HEALTHY":
                raise sqlite3.OperationalError("fixture crash after commit")
            return original(component, state, **kwargs)
        monkeypatch.setattr(store, "record_heartbeat", heartbeat)
    with pytest.raises(sqlite3.Error):
        observer.poll()
    committed = boundary == "after_commit"
    assert store.count() == int(committed)
    assert store.observer_cursor(observer.component)[0] == int(committed)
    assert lab_fill_history_view(store, source[0])["history_state"] == "UNKNOWN"
    assert len(source[2].broker.orders()) == len(source[2].broker.positions()) == 1
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER IF EXISTS injected")
    restarted = collector(source, GuardianStore(store.path))
    assert restarted.poll() == int(not committed)
    assert restarted.poll() == 0
    assert store.count() == 1


@pytest.mark.parametrize("change", ["first", "last", "deleted_anchor", "account", "reset"])
def test_changed_source_cursor_blocks_without_discarding_history(source, tmp_path, change):
    enter(source)
    candle(source)
    duplicate_fixture_fills(source, 2)
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(source, store)
    assert observer.poll() == 3
    checkpoint = store.observer_cursor(observer.component)
    with source[2].broker._c as db:
        if change == "first":
            db.execute("UPDATE v2_fills SET price=999 WHERE rowid=1")
        elif change == "last":
            db.execute("UPDATE v2_fills SET price=999 WHERE rowid=3")
        elif change == "deleted_anchor":
            db.execute("DELETE FROM v2_fills WHERE rowid=3")
        elif change == "account":
            db.execute("UPDATE v2_account SET account_id='replaced-account'")
        else:
            db.execute("DELETE FROM v2_fills")
    with pytest.raises(ValueError):
        collector(source, GuardianStore(store.path)).poll()
    assert store.count() == 3
    assert store.observer_cursor(observer.component) == checkpoint
    data = lab_fill_history_view(store, source[0])
    assert data["history_state"] == "UNKNOWN"
    assert len(data["events"]) == 3


def test_wal_read_and_missing_source_are_read_only(source, tmp_path):
    enter(source)
    candle(source)
    with sqlite3.connect(source[1]) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE v2_fills SET price=999")
        assert lab_fill_page(source[1], source[0])["fills"][0]["price"] != 999
        writer.rollback()
    absent = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        lab_fill_page(absent, source[0])
    assert not absent.exists()


def test_source_auth_unavailable_lock_and_retry(source, monkeypatch):
    enter(source)
    candle(source)
    account = source[2]
    account._db.close()
    account.broker._c.close()
    if source[0] == "PRICE_ACTION":
        account.journal._db.close()
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    setting = "price_action_paper_db" if source[0] == "PRICE_ACTION" else "smc_paper_db"
    monkeypatch.setattr(settings, setting, str(source[1]))
    client = TestClient(app_module.app)
    route = "/guardian/lab-fills?lab=" + source[0]
    headers = {"X-Guardian-Observer-Key": KEY}
    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-Webhook-Secret": KEY}).status_code == 401
    assert client.post(route, headers=headers).status_code != 200
    with sqlite3.connect(source[1]) as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        for _ in range(2):
            response = client.get(route, headers=headers)
            assert response.status_code == 503
            assert response.json()["detail"] == {"state": "PERSISTENCE_BLOCKED", "code": "LAB_FILL_HISTORY_UNAVAILABLE"}
            assert str(source[1]) not in response.text
        writer.rollback()
        response = client.get(route, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert len(response.json()["page"]["fills"]) == 1
    assert client.get(route + "&after=9999999999999999999999", headers=headers).status_code == 422
    assert client.get(route + "&after=1&anchor=wrong", headers=headers).status_code == 503
    monkeypatch.setattr(settings, "guardian_observer_key", "")
    # The app's outer auth middleware denies an unconfigured observer before
    # the route's own 404 guard is reached. Do not widen that bypass.
    assert client.get(route, headers=headers).status_code == 401


@pytest.mark.parametrize("change", ["stale", "future", "foreign_lab", "foreign_fill", "nan", "oversized",
                                     "duplicate", "next_cursor", "no_origin", "bad_sequence", "future_fill"])
def test_bad_page_cannot_advance_cursor(source, tmp_path, change):
    enter(source)
    candle(source)
    value = envelope(source)
    page = value["page"]
    if change in {"stale", "future"}:
        value["observed_at"] = (datetime.now(timezone.utc) + timedelta(seconds=-100 if change == "stale" else 100)).isoformat()
    elif change == "foreign_lab":
        page["lab"] = "OTHER"
    elif change == "foreign_fill":
        page["fills"][0]["account_id"] = "OTHER"
    elif change == "nan":
        page["fills"][0]["quantity"] = float("nan")
    elif change == "oversized":
        page["fills"] *= 33
    elif change == "duplicate":
        page["fills"] *= 2
    elif change == "next_cursor":
        page["next_after"] = 2
    elif change == "no_origin":
        page["first_fill"] = None
    elif change == "future_fill":
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        page["fills"][0]["timestamp"] = page["first_fill"]["timestamp"] = future
    else:
        page["fills"][0]["source_sequence"] = True
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianLabFillHistory(store, URL, KEY, source[0], fetch=lambda *args: value)
    with pytest.raises(ValueError):
        observer.poll()
    assert store.count() == 0
    assert store.observer_cursor(observer.component) == (0, "")


def test_empty_history_is_caught_up_not_proof_of_trading_integrity(source, tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(source, store)
    assert observer.poll() == 0
    result = lab_fill_history_view(store, source[0])
    assert result["history_state"] == "CAUGHT_UP_AT_LAST_POLL"
    assert result["events"] == []
    assert result["execution_integrity_verified"] is False
    future = datetime.now(timezone.utc) + timedelta(seconds=100)
    assert lab_fill_history_view(store, source[0], now=future)["history_state"] == "UNKNOWN"


def test_source_query_bound_and_oversized_string(source, monkeypatch):
    enter(source)
    candle(source)
    duplicate_fixture_fills(source, 40)
    assert len(lab_fill_page(source[1], source[0])["fills"]) == 32
    with source[2].broker._c as db:
        db.execute("UPDATE v2_fills SET strategy=? WHERE rowid=1", ("x" * 10000,))
    with pytest.raises(ValueError):
        lab_fill_page(source[1], source[0])
    with source[2].broker._c as db:
        db.execute("UPDATE v2_fills SET strategy='fixture' WHERE rowid=1")
    import services.guardian_lab_fill_read_model as module
    moments = iter([0., 10.])
    monkeypatch.setattr(module, "monotonic", lambda: next(moments, 10.))
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        lab_fill_page(source[1], source[0])


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+23:00", "9999-12-31T23:59:59-23:00"])
def test_unrepresentable_utc_timestamp_is_structured_failure_not_500(source, monkeypatch, stamp):
    enter(source)
    candle(source)
    with source[2].broker._c as db:
        db.execute("UPDATE v2_fills SET timestamp=?", (stamp,))
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    setting = "price_action_paper_db" if source[0] == "PRICE_ACTION" else "smc_paper_db"
    monkeypatch.setattr(settings, setting, str(source[1]))
    client = TestClient(app_module.app)
    response = client.get("/guardian/lab-fills?lab=" + source[0],
                          headers={"X-Guardian-Observer-Key": KEY})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "LAB_FILL_HISTORY_UNAVAILABLE"
    assert stamp not in response.text
    with source[2].broker._c as db:
        db.execute("UPDATE v2_fills SET timestamp=?", (datetime.now(timezone.utc).isoformat(),))
    assert client.get("/guardian/lab-fills?lab=" + source[0],
                      headers={"X-Guardian-Observer-Key": KEY}).status_code == 200


def test_stable_fill_identity_is_isolated_by_lab_and_account(source, tmp_path):
    enter(source)
    candle(source)
    store = GuardianStore(tmp_path / "guardian.db")
    collector(source, store).poll()
    original = store.recent(1)[0]["event_id"]
    value = deepcopy(envelope(source))
    other = "SMC" if source[0] == "PRICE_ACTION" else "PRICE_ACTION"
    from tradexa.guardian.lab_fill_history import LABS, cursor_anchor
    page = value["page"]
    page["lab"], page["account_type"] = other, LABS[other]
    for row in [page["first_fill"], *page["fills"]]:
        row["execution_engine"] = LABS[other]
    page["next_anchor"] = cursor_anchor(other, page["account_id"], page["first_fill"], page["fills"][-1])
    GuardianLabFillHistory(store, URL, KEY, other, fetch=lambda *args: value).poll()
    assert store.count() == 2
    assert store.recent(1)[0]["event_id"] != original
    assert len(lab_fill_history_view(store, source[0])["events"]) == 1
    # Same fill ID on a distinct account cannot collide, even within one lab.
    alternate = GuardianStore(tmp_path / "other-account.db")
    page["account_id"] = "other-isolated-account"
    for row in [page["first_fill"], *page["fills"]]:
        row["account_id"] = page["account_id"]
    page["next_anchor"] = cursor_anchor(other, page["account_id"], page["first_fill"], page["fills"][-1])
    GuardianLabFillHistory(alternate, URL, KEY, other, fetch=lambda *args: value).poll()
    assert alternate.recent(1)[0]["event_id"] != store.recent(1)[0]["event_id"]


def test_failure_halfway_through_page_rolls_back_every_event_and_checkpoint(source, tmp_path):
    enter(source)
    candle(source)
    duplicate_fixture_fills(source, 3)
    store = GuardianStore(tmp_path / "guardian.db")
    observer = collector(source, store)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER injected BEFORE INSERT ON events WHEN (SELECT COUNT(*) FROM events)=2 "
                   "BEGIN SELECT RAISE(ABORT, 'fixture full disk'); END")
    with pytest.raises(sqlite3.IntegrityError):
        observer.poll()
    assert store.count() == 0
    assert store.observer_cursor(observer.component) == (0, "")
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER injected")
    assert collector(source, GuardianStore(store.path)).poll() == 4
    assert store.count() == 4


def test_concurrent_collectors_cannot_duplicate_a_page(source, tmp_path):
    enter(source)
    candle(source)
    store = GuardianStore(tmp_path / "guardian.db")
    first = collector(source, store)
    second = collector(source, GuardianStore(store.path))
    def intervening_poll(after, anchor):
        stale_page = envelope(source, after, anchor)
        first.poll()
        return stale_page
    second.fetch = intervening_poll
    with pytest.raises(ValueError, match="concurrently"):
        second.poll()
    assert store.count() == 1
    assert collector(source, GuardianStore(store.path)).poll() == 0
    assert lab_fill_history_view(store, source[0])["history_state"] == "CAUGHT_UP_AT_LAST_POLL"


@pytest.mark.parametrize("url", ["https://example.com/guardian/lab-fills", "http://app:8000/api/v1/start",
                                 "http://app:8000/guardian/lab-fills?lab=SMC", "http://user:pass@app:8000/guardian/lab-fills"])
def test_external_control_or_credential_urls_are_rejected(tmp_path, url):
    with pytest.raises(ValueError, match="internal read"):
        GuardianLabFillHistory(GuardianStore(tmp_path / "guardian.db"), url, KEY, "SMC")


def test_collector_transport_is_bounded_and_never_redirects(source, tmp_path, monkeypatch):
    import tradexa.guardian.lab_fill_history as module
    from tradexa.guardian.lab_execution_observer import _NoRedirect
    enter(source)
    candle(source)
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianLabFillHistory(store, URL, KEY, source[0])
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def read(self, size):
            assert size == 262145
            return b"x" * size
    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith(URL + "?lab=" + source[0])
            assert request.get_header("X-guardian-observer-key") == KEY
            assert timeout == 3
            return Response()
    def opener(handler):
        assert isinstance(handler, _NoRedirect)
        return Opener()
    monkeypatch.setattr(module, "build_opener", opener)
    with pytest.raises(ValueError, match="bound"):
        observer.poll()
    assert store.count() == 0
    assert store.heartbeats()[observer.probe]["state"] == "FAILED"


def test_pa_outage_does_not_suppress_smc_collector():
    from threading import Event
    from tradexa.guardian.service import _lab_execution_monitor
    stopped, calls = Event(), []
    class PA:
        def poll(self):
            calls.append("PA")
            raise sqlite3.OperationalError("fixture unavailable")
    class SMC:
        def poll(self):
            calls.append("SMC")
            stopped.set()
    _lab_execution_monitor((PA(), SMC()), stopped)
    assert calls == ["PA", "SMC"]
