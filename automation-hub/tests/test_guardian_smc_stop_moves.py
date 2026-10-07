"""Retained stop history must not become trading/protection authority."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import sqlite3

import pytest
from services.smc_agent_journal import SMCAgentJournal
from services.guardian_smc_stop_read_model import smc_stop_move_page
from tradexa.guardian.smc_stop_moves import (
    COMPONENT, PROBE, SCOPE, MAX_RESPONSE_BYTES, GuardianSMCStopMoves, smc_stop_moves_view)
from tradexa.guardian.service import GuardianService
from tradexa.guardian.store import GuardianStore

URL = "http://app:8000/guardian/smc-stop-moves"
KEY = "independent-stop-observer-key-123456789"
READ_KEY = "independent-stop-reader-key-123456789"


@pytest.fixture
def sources(tmp_path):
    journal = SMCAgentJournal(tmp_path / "agent.db")
    yield journal, GuardianStore(tmp_path / "guardian.db")
    journal.close()


def move(journal, **changes):
    values = dict(trade_id="trade-1", symbol="BTCUSDT", from_price=90, to_price=100,
        reason_code="BREAKEVEN", reason="private detail not exported", progress_r=1,
        applied=True, candle_time="2026-01-01T00:00:00Z")
    values.update(changes)
    return journal.record_stop_move(**values)


def envelope(journal, after=0, anchor=""):
    return dict(schema_version=1, scope=SCOPE, observed_at=datetime.now(timezone.utc).isoformat(),
        execution_integrity_verified=False, page=smc_stop_move_page(journal.path, after=after, anchor=anchor))


def collector(sources, *, store=None, fetch=None, **kwargs):
    journal, original = sources
    return GuardianSMCStopMoves(store or original, URL, KEY,
        fetch=fetch or (lambda after, anchor: envelope(journal, after, anchor)), **kwargs)


def request(store, *, query="", key=READ_KEY, method="GET"):
    app = getattr(store, "_fixture_app", None)
    if app is None:
        app = GuardianService(store, source_keys={"guardian_probe": KEY},
            read_key=READ_KEY, required_components=("guardian", PROBE))
        store._fixture_app = app
    result = {}
    def respond(status, headers):
        result.update(status=int(status.split()[0]), headers=dict(headers))
    raw = b"".join(app(dict(REQUEST_METHOD=method, PATH_INFO="/v1/smc-stop-moves",
        QUERY_STRING=query, HTTP_X_GUARDIAN_KEY=key, CONTENT_LENGTH="0",
        **{"wsgi.input": io.BytesIO()}), respond))
    return result["status"], json.loads(raw), result["headers"]


def test_applied_failed_and_orphan_records_are_not_broker_truth(sources):
    journal, store = sources
    ids = [move(journal), move(journal, to_price=105, reason_code="TRAIL", applied=False,
                              error="Bearer private error never exported")]
    before = list(journal._db.iterdump())
    assert collector(sources).poll() == 2
    view = smc_stop_moves_view(store)
    rows = [e["evidence"]["move"] for e in view["events"]]
    assert [r["id"] for r in rows] == ids
    assert [r["applied"] for r in rows] == [True, False]
    assert [r["error_recorded"] for r in rows] == [False, True]
    assert all(r["reason_recorded"] for r in rows)
    assert view["history_state"] == "CAUGHT_UP_AT_LAST_POLL"
    assert all(view[k] is False for k in view if k.endswith("_verified"))
    assert all(e["execution_id"] is e["order_id"] is e["position_id"] is e["session_id"] is None for e in view["events"])
    assert "private" not in str(view) and "Bearer" not in str(view)
    assert "MISSED" not in str(view) and "No order was placed" not in str(view)
    assert list(journal._db.iterdump()) == before


def test_restart_sequence_paging_and_100_refreshes(sources):
    journal, store = sources
    for n in range(70):
        move(journal, trade_id=f"trade-{n}", symbol="ETHUSDT" if n % 2 else "BTCUSDT",
             at="2000-01-01T00:00:00Z" if n == 69 else None)
    before = list(journal._db.iterdump())
    assert collector(sources).poll() == 32
    assert smc_stop_moves_view(store)["history_state"] == "IMPORTING"
    restarted = GuardianStore(store.path)
    assert collector(sources, store=restarted).poll() == 32
    assert collector(sources, store=restarted).poll() == 6
    cursor = store.observer_cursor(COMPONENT)
    for _ in range(100):
        assert collector(sources, store=restarted).poll() == 0
        assert request(restarted)[0] == 200
    after, events = 0, []
    while True:
        page = smc_stop_moves_view(store, after=after)
        assert len(page["events"]) <= 32
        events.extend(page["events"])
        after = page["next_after"]
        if not page["has_more"]: break
    assert len(events) == len({e["event_id"] for e in events}) == store.count() == 70
    assert events[-1]["timestamp"].startswith("2000-")
    assert store.observer_cursor(COMPONENT) == cursor
    assert list(journal._db.iterdump()) == before


@pytest.mark.parametrize("boundary", ["event", "cursor", "after_commit"])
def test_atomic_failure_and_restart(sources, boundary):
    journal, store = sources
    move(journal)
    before = list(journal._db.iterdump())
    with closing(sqlite3.connect(store.path)) as db:
        table = {"event": "events", "cursor": "observer_cursors", "after_commit": "heartbeats"}[boundary]
        condition = " WHEN NEW.state!='FAILED'" if boundary == "after_commit" else ""
        db.execute(f"CREATE TRIGGER injected BEFORE INSERT ON {table}{condition} BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.Error): collector(sources).poll()
    count = int(boundary == "after_commit")
    assert store.count() == store.observer_cursor(COMPONENT)[0] == count
    assert smc_stop_moves_view(store)["history_state"] == "UNKNOWN"
    with closing(sqlite3.connect(store.path)) as db: db.execute("DROP TRIGGER injected")
    assert collector(sources, store=GuardianStore(store.path)).poll() == 1-count
    assert store.count() == 1 and list(journal._db.iterdump()) == before


@pytest.mark.parametrize("change", [
    "schema", "scope", "integrity", "stale", "future", "naive", "atomic", "after", "anchor",
    "has_more", "empty_more", "too_many", "duplicate", "sequence_bool", "sequence_overflow",
    "first", "previous", "checkpoint", "next_anchor", "origin", "time", "price_bool",
    "price_nan", "price_zero", "price_overflow", "progress_nan", "progress_bool", "applied_int",
    "id_blank", "unicode", "secret", "reason_text", "error_text", "extra_root", "extra_page", "extra_row", "response_bound"])
def test_invalid_page_is_fail_closed(sources, change):
    journal, store = sources
    move(journal)
    value = envelope(journal)
    p, r = value["page"], value["page"]["moves"][0]
    now = datetime.now(timezone.utc)
    if change == "schema": value["schema_version"] = True
    elif change == "scope": value["scope"] = "OTHER"
    elif change == "integrity": value["execution_integrity_verified"] = True
    elif change == "stale": value["observed_at"] = (now-timedelta(seconds=100)).isoformat()
    elif change == "future": value["observed_at"] = (now+timedelta(seconds=10)).isoformat()
    elif change == "naive": value["observed_at"] = now.replace(tzinfo=None).isoformat()
    elif change == "atomic": p["atomic_snapshot"] = False
    elif change == "after": p["after"] = True
    elif change == "anchor": p["anchor"] = "a"*64
    elif change == "has_more": p["has_more"] = 1
    elif change == "empty_more": p["moves"], p["has_more"] = [], True
    elif change == "too_many": p["moves"] *= 33
    elif change == "duplicate": p["moves"].append(deepcopy(r))
    elif change == "sequence_bool": r["source_sequence"] = True
    elif change == "sequence_overflow": r["source_sequence"] = 2**63
    elif change == "first": p["first_move"]["id"] = "wrong"
    elif change == "previous": p["previous_move"] = deepcopy(r)
    elif change == "checkpoint": p["next_after"] = 2
    elif change == "next_anchor": p["next_anchor"] = "a"*64
    elif change == "origin": p["origin"] = "a"*64
    elif change == "time": r["at"] = (now+timedelta(seconds=10)).isoformat()
    elif change == "price_bool": r["to_price"] = True
    elif change == "price_nan": r["to_price"] = float("nan")
    elif change == "price_zero": r["to_price"] = 0
    elif change == "price_overflow": r["to_price"] = 10**500
    elif change == "progress_nan": r["progress_r"] = float("nan")
    elif change == "progress_bool": r["progress_r"] = True
    elif change == "applied_int": r["applied"] = 1
    elif change == "id_blank": r["id"] = ""
    elif change == "unicode": r["id"] = "\ud800"
    elif change == "secret": r["reason_code"] = "Bearer private-value"
    elif change == "reason_text": r["reason"] = "private detail"
    elif change == "error_text": r["error"] = "private error"
    elif change == "extra_root": value["unexpected"] = True
    elif change == "extra_page": p["unexpected"] = True
    elif change == "extra_row": r["unexpected"] = True
    elif change == "response_bound": value["padding"] = "x"*MAX_RESPONSE_BYTES
    with pytest.raises((ValueError, TypeError, OverflowError, UnicodeError)):
        collector(sources, fetch=lambda *args: value).poll()
    assert store.count() == 0 and store.observer_cursor(COMPONENT) == (0, "")
    assert smc_stop_moves_view(store)["history_state"] == "UNKNOWN"


def test_source_auth_wal_reads_lock_retry_and_no_source_mutation(sources, monkeypatch):
    from fastapi.testclient import TestClient
    from config import settings
    import app as source_app
    journal, _ = sources
    move(journal)
    before = list(journal._db.iterdump())
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "smc_agent_journal_db", journal.path)
    client, route = TestClient(source_app.app), "/guardian/smc-stop-moves"
    headers = {"X-Guardian-Observer-Key": KEY}
    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-Webhook-Secret": settings.admin_key}).status_code == 401
    assert client.get(route, headers={"X-Guardian-Observer-Key": settings.admin_key}).status_code == 401
    assert client.post(route, headers={"X-Webhook-Secret": settings.admin_key}).status_code == 405
    for query in ("after=-1", "after=1&after=2", "anchor=a&anchor=b", "limit=999", "lab=PRICE_ACTION"):
        assert client.get(route+"?"+query, headers=headers).status_code == 422
    with closing(sqlite3.connect(journal.path)) as db:
        db.execute("BEGIN IMMEDIATE")
        for _ in range(10): assert client.get(route, headers=headers).status_code == 200
        db.rollback()
    journal.close()
    with closing(sqlite3.connect(journal.path)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN EXCLUSIVE")
        for _ in range(2):
            r = client.get(route, headers=headers)
            assert r.status_code == 503
            assert r.json()["detail"] == dict(state="PERSISTENCE_BLOCKED", code="SMC_STOP_HISTORY_UNAVAILABLE")
            assert str(journal.path) not in r.text and KEY not in r.text
        db.rollback()
    for _ in range(100):
        r = client.get(route, headers=headers)
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    with closing(sqlite3.connect(journal.path)) as db: assert list(db.iterdump()) == before


@pytest.mark.parametrize("corruption", ["claim", "zero_flag", "missing_header", "order_identity"])
def test_standalone_auth_staleness_query_bounds_lock_and_corrupt_evidence(sources, corruption):
    journal, store = sources
    move(journal)
    assert collector(sources).poll() == 1
    assert request(store)[0] == 200
    assert request(store, key=KEY)[0] == request(store, key="")[0] == 401
    assert request(store, method="POST")[0] == 405
    for query in ("after=-1", "after=1&after=2", "after=no", "after=9223372036854775808", "lab=SMC"):
        assert request(store, query=query)[0] == 400
    store.record_heartbeat(PROBE, "HEALTHY", observed_at=datetime.now(timezone.utc)-timedelta(seconds=100))
    assert request(store)[1]["history_state"] == "UNKNOWN" and len(request(store)[1]["events"]) == 1
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN EXCLUSIVE")
        assert request(store)[:2] == (503, {"error": "PERSISTENCE_UNAVAILABLE"})
        db.rollback()
        db.execute("DROP TRIGGER events_no_update")
        payload = json.loads(db.execute("SELECT payload_json FROM events").fetchone()[0])
        if corruption == "claim": payload["metadata"]["current_protection_verified"] = True
        elif corruption == "zero_flag": payload["metadata"]["current_protection_verified"] = 0
        elif corruption == "missing_header": payload.pop("position_id")
        elif corruption == "order_identity": payload["order_id"] = "invented-order"
        db.execute("UPDATE events SET payload_json=?", (json.dumps(payload),))
        db.commit()
    # Full canonical record validation, not just trusting a JSON field.
    assert request(store)[:2] == (503, {"error": "STOP_HISTORY_EVIDENCE_UNAVAILABLE"})


def test_empty_source_missing_table_and_replacement_fail_closed(sources, tmp_path):
    journal, store = sources
    assert collector(sources).poll() == 0
    move(journal)
    assert collector(sources).poll() == 1
    other = SMCAgentJournal(tmp_path / "other.db")
    try:
        move(other)
        with pytest.raises(ValueError):
            collector(sources, fetch=lambda after, anchor: envelope(other, after, anchor)).poll()
    finally: other.close()
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.Error): smc_stop_move_page(missing)
    assert not missing.exists()
    legacy = tmp_path / "legacy.db"
    with closing(sqlite3.connect(legacy)) as db: db.execute("CREATE TABLE legacy(id)")
    with pytest.raises(sqlite3.Error): smc_stop_move_page(legacy)
    with closing(sqlite3.connect(legacy)) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("legacy",)]


def test_concurrent_collectors_cannot_advance_same_page_twice(sources):
    journal, store = sources
    move(journal)
    value = envelope(journal)
    def racing(*args):
        assert collector(sources).poll() == 1
        return value
    with pytest.raises(ValueError): collector(sources, fetch=racing).poll()
    assert store.count() == 1

@pytest.mark.parametrize("url", [
    "https://app:8000/guardian/smc-stop-moves", "http://outside:8000/guardian/smc-stop-moves",
    "http://app:80/guardian/smc-stop-moves", "http://a:b@app:8000/guardian/smc-stop-moves",
    URL+"?after=0", URL+"#unsafe", "http://app:8000/api/v1/start"])
def test_internal_read_url_only(sources, url):
    with pytest.raises(ValueError): GuardianSMCStopMoves(sources[1], url, KEY)


@pytest.mark.parametrize("key", [None, "", "short", 100])
def test_long_independent_key_required(sources, key):
    with pytest.raises(ValueError): GuardianSMCStopMoves(sources[1], URL, key)


@pytest.mark.parametrize("fault", ["oversized", "non_200", "timeout", "duplicate_json"])
def test_bounded_transport_header_and_no_redirect(sources, monkeypatch, fault):
    import tradexa.guardian.smc_stop_moves as module
    journal, store = sources
    move(journal)
    class Response:
        status = 503 if fault == "non_200" else 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, bound):
            assert bound == MAX_RESPONSE_BYTES+1
            if fault == "duplicate_json":
                return ('{"scope":"OTHER",'+json.dumps(envelope(journal))[1:]).encode()
            return b"x"*bound
    class Opener:
        def open(self, req, timeout):
            assert timeout == 3
            assert dict(req.header_items()) == {"X-guardian-observer-key": KEY}
            assert req.full_url == URL+"?after=0&anchor="
            if fault == "timeout": raise TimeoutError("private source URL")
            return Response()
    def build(handler):
        assert handler.redirect_request(None, None, 302, "", {}, "http://outside") is None
        return Opener()
    monkeypatch.setattr(module, "build_opener", build)
    with pytest.raises((ValueError, TimeoutError)):
        GuardianSMCStopMoves(store, URL, KEY).poll()
    assert store.count() == 0 and store.observer_cursor(COMPONENT) == (0, "")
    assert "private" not in str(store.heartbeats())


def test_source_and_guardian_reads_during_wal_writes_use_indexes_and_write_nothing(sources):
    journal, store = sources
    move(journal)
    assert collector(sources).poll() == 1
    assert request(store)[0] == 200
    before = list(journal._db.iterdump())
    own = None
    with closing(sqlite3.connect(store.path)) as db:
        own = list(db.iterdump())
        plan = db.execute("EXPLAIN QUERY PLAN SELECT sequence FROM events INDEXED BY smc_stop_history "
            "WHERE source_service='guardian_smc_stop_moves' AND event_type='smc_stop_move_observed' "
            "AND sequence>? ORDER BY sequence LIMIT 33", (0,)).fetchall()
        assert "SEARCH" in str(plan) and "smc_stop_history" in str(plan)
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE heartbeats SET reason='uncommitted' WHERE component=?", (PROBE,))
        for _ in range(100): assert request(store)[0] == 200
        db.rollback()
        assert list(db.iterdump()) == own
    assert list(journal._db.iterdump()) == before


def test_import_cannot_apply_a_reported_stop_or_change_orders_positions_and_plan(sources, tmp_path):
    from execution.paper_broker_v2 import PaperBrokerV2
    journal, store = sources
    broker = PaperBrokerV2(tmp_path / "smc.db", account_type="SMC_LAB", execution_engine="SMC_LAB",
        leverage=5, fee_rate=0, spread_bps=0, slippage_bps=0, participation_rate=1)
    try:
        order = broker.submit(symbol="BTCUSDT", side="buy", order_type="market",
            quantity=1, timeframe="5m", candle_id="execution-1")
        broker.process_candle("BTCUSDT", dict(open=100, high=101, low=99, close=100, volume=100))
        broker.set_protection("BTCUSDT", stop_loss=90, take_profit=120)
        decision = journal.record_decision(smc_state="ENTRY_READY", outcome="TAKEN",
            symbol="BTCUSDT", timeframe="5m", reason_code="FIXTURE", reason="fixture")
        trade = journal.open_trade(decision_id=decision, symbol="BTCUSDT", timeframe="5m",
            direction="bullish", entry=100, stop=90, target=120, planned_rr=2, size=1,
            order_id=order["id"], why="fixture")
        move(journal, trade_id=trade)  # recorded as applied, but actual fixture broker stop is still 90
        before = list(broker._c.iterdump())
        plan = journal.trade(trade)
        assert collector(sources).poll() == 1
        assert smc_stop_moves_view(store)["broker_stop_application_verified"] is False
        assert len(broker.orders()) == len(broker.positions()) == len(broker.fills()) == 1
        assert broker.positions()[0]["stop_loss"] == 90
        assert journal.trade(trade) == plan and plan["stop"] == 90
        assert list(broker._c.iterdump()) == before
    finally: broker._c.close()


def test_identical_legitimate_attempts_keep_distinct_source_ids(sources):
    journal, store = sources
    ids = [move(journal), move(journal)]
    assert collector(sources).poll() == 2
    events = smc_stop_moves_view(store)["events"]
    assert {e["evidence"]["move"]["id"] for e in events} == set(ids)
    assert len({e["event_id"] for e in events}) == 2
    assert collector(sources).poll() == 0


def test_valid_changed_replay_is_detected_by_existing_event_identity(sources):
    journal, store = sources
    move(journal)
    move(journal)
    original = envelope(journal)
    assert collector(sources).poll() == 2
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DELETE FROM observer_cursors WHERE component=?", (COMPONENT,))
        db.commit()
    changed = deepcopy(original)
    changed["page"]["moves"][1]["to_price"] = 105
    from tradexa.guardian.smc_stop_moves import cursor_anchor
    changed["page"]["next_anchor"] = cursor_anchor(changed["page"]["first_move"], changed["page"]["moves"][-1])
    with pytest.raises(ValueError):
        collector(sources, fetch=lambda *args: changed).poll()
    assert store.count() == 2 and store.observer_cursor(COMPONENT) == (0, "")


def test_standalone_import_has_no_trading_dependencies():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c", "import sys; import tradexa.guardian.service; "
        "assert not any(k.startswith(('execution.', 'services.', 'bot.')) for k in sys.modules)"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
