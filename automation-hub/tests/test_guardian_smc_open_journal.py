"""Open Agent journal coverage stays independent of execution/strategy authority."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import sqlite3

import pytest

from execution.paper_broker_v2 import PaperBrokerV2
from services.guardian_execution_read_model import smc_execution_integrity_snapshot
from services.smc_agent_journal import SMCAgentJournal
from tradexa.guardian.smc_execution_observer import GuardianSMCExecutionObserver
from tradexa.guardian.incidents import GuardianIncidentEngine
from tradexa.guardian.store import GuardianStore

KEY = "guardian-smc-open-journal-test-key-123456"
URL = "http://app:8000/guardian/smc-execution"


@pytest.fixture
def sources(tmp_path):
    journal_path, broker_path = tmp_path / "agent.db", tmp_path / "smc.db"
    journal = SMCAgentJournal(journal_path)
    broker = PaperBrokerV2(broker_path, account_type="SMC_LAB", execution_engine="SMC_LAB")
    yield journal, broker, journal_path, broker_path
    journal.close()
    broker._c.close()


def intent(sources, key="decision-1", **changes):
    values = dict(execution_key=key, decision_id=key, session_id="session-1",
                  symbol="BTCUSDT", timeframe="5m")
    values.update(changes)
    sources[0].create_execution_intent(**values)
    sources[0].transition_execution(key, "EXECUTION_PENDING")


def finalized(sources, key="decision-1", **trade_changes):
    journal, broker, *_ = sources
    intent(sources, key)
    order = broker.submit(symbol="BTCUSDT", side="buy", order_type="limit", quantity=.01,
                          limit_price=100, protection_stop_loss=90, protection_take_profit=120,
                          strategy="SMC_M1_SWEEP_REVERSAL", strategy_version="1.0",
                          timeframe="5m", candle_id=key)
    broker.process_candle("BTCUSDT", {"open": 100, "high": 101, "low": 99,
                          "close": 100, "volume": 100,
                          "timestamp": datetime.now(timezone.utc).isoformat()})
    journal.transition_execution(key, "EXECUTED", broker_order_id=order["id"])
    values = dict(decision_id=key, symbol="BTCUSDT", timeframe="5m", direction="long",
                  entry=100, stop=90, target=120, planned_rr=2, size=.01,
                  order_id=order["id"], why="paper fixture")
    values.update(trade_changes)
    trade_id = journal.open_trade(**values)
    journal.transition_execution(key, "COMPLETE", trade_id=trade_id)
    return order, trade_id


def snapshot(sources):
    return smc_execution_integrity_snapshot(sources[2], sources[3])


def envelope(sources):
    return {**snapshot(sources), "schema_version": 2,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "feed_health_verified": False, "execution_integrity_verified": False}


def open_unlinked(sources, key="legacy-1"):
    return sources[0].open_trade(decision_id=key, symbol="BTCUSDT", timeframe="5m",
                                 direction="long", entry=100, stop=90, target=120,
                                 planned_rr=2, size=.01, why="legacy fixture", order_id="historical-order")


def test_old_complete_intent_with_open_trade_is_not_lost_outside_recent_eight(sources):
    journal = sources[0]
    _, trade_id = finalized(sources)
    # Only reorder terminal fixture timestamps; no production record is touched.
    earlier = datetime.now(timezone.utc) - timedelta(days=10)
    journal._db.execute("UPDATE execution_intents SET updated_at=? WHERE execution_key=?",
                        (earlier.isoformat(), "decision-1"))
    journal._db.commit()
    for number in range(8):
        key = f"later-{number}"
        intent(sources, key)
        journal.transition_execution(key, "EXECUTED")
        journal.transition_execution(key, "COMPLETE")
    view = snapshot(sources)
    rows = {row["execution_key"]: row for row in view["executions"]}
    assert "decision-1" in rows
    assert rows["decision-1"]["trade_id"] == trade_id
    assert rows["decision-1"]["integrity_code"] == "CONSISTENT"
    assert view["open_journal_trade_count"] == 1
    assert view["extra_open_intent_count"] == 1


@pytest.mark.parametrize("changes", [
    {"symbol": "ETHUSDT"}, {"timeframe": "15m"}, {"direction": "short"},
])
def test_matching_order_id_does_not_hide_wrong_trade_market_or_direction(sources, changes):
    finalized(sources, **changes)
    [row] = snapshot(sources)["executions"]
    assert row["integrity_code"] == "TRADE_IDENTITY_MISMATCH"


def test_closed_trade_with_same_order_still_open_is_not_consistent(sources):
    order, trade_id = finalized(sources)
    sources[0].close_trade(trade_id, exit_price=120, realised_r=2, result="WIN",
                           close_reason="injected journal-only close")
    [row] = snapshot(sources)["executions"]
    assert row["integrity_code"] == "CLOSED_TRADE_POSITION_STILL_OPEN"
    assert row["open_position_matches_order"] is True
    assert sources[1].positions()[0]["entry_order_id"] == order["id"]


def test_unlinked_legacy_trade_has_no_invented_execution_or_no_order_claim(sources, tmp_path):
    trade_id = open_unlinked(sources)
    view = snapshot(sources)
    assert view["executions"] == []
    [link] = view["open_journal_trades"]
    assert link["trade_id"] == trade_id
    assert link["execution_keys"] == []
    assert link["integrity_code"] == "JOURNAL_INTENT_NOT_FOUND"
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCExecutionObserver(store, URL, KEY, fetch=lambda: envelope(sources))
    assert observer.poll() == 1
    [event] = store.recent()
    assert event["execution_id"] is None
    assert event["order_id"] == "historical-order"
    engine = GuardianIncidentEngine(store)
    engine.scan()
    [finding] = engine.list()
    assert finding["confidence"] == "POSSIBLE"
    assert finding["severity"] == "WARNING"
    assert trade_id in finding["fingerprint"]
    assert sources[1].orders() == sources[1].positions() == []


def test_multiple_intents_cannot_claim_the_same_open_journal_trade(sources, tmp_path):
    _, trade_id = finalized(sources)
    intent(sources, "second-key")
    sources[0].transition_execution("second-key", "EXECUTED")
    sources[0].transition_execution("second-key", "COMPLETE", trade_id=trade_id)
    view = snapshot(sources)
    [link] = view["open_journal_trades"]
    assert link["matching_intent_count"] == 2
    assert link["integrity_code"] == "MULTIPLE_INTENTS_FOR_TRADE"
    assert all(row["integrity_code"] == "MULTIPLE_INTENTS_FOR_TRADE" for row in view["executions"])
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCExecutionObserver(store, URL, KEY, fetch=lambda: envelope(sources))
    assert observer.poll() == 3
    engine = GuardianIncidentEngine(store)
    engine.scan()
    assert len(engine.list()) == 3  # two executions and the reverse trade link
    assert all(row["severity"] == "HIGH" and row["confidence"] == "POSSIBLE"
               for row in engine.list())


def test_one_open_trade_in_recent_terminal_is_not_selected_twice(sources):
    finalized(sources)
    view = snapshot(sources)
    assert len(view["executions"]) == view["terminal_sample_count"] == 1
    assert view["extra_open_intent_count"] == 0
    assert view["open_journal_trade_count"] == 1


def test_restarts_and_one_hundred_refreshes_do_not_write_sources_or_duplicate_evidence(sources, tmp_path):
    finalized(sources)
    journal, broker, *_ = sources
    before = list(journal._db.iterdump()), list(broker._c.iterdump())
    path = tmp_path / "guardian.db"
    observer = GuardianSMCExecutionObserver(GuardianStore(path), URL, KEY, fetch=lambda: envelope(sources))
    assert observer.poll() == 2
    for _ in range(100):
        assert observer.poll() == 0
    restarted = GuardianSMCExecutionObserver(GuardianStore(path), URL, KEY,
                                            fetch=lambda: envelope(sources))
    assert restarted.poll() == 0
    assert GuardianStore(path).count() == 2
    assert (list(journal._db.iterdump()), list(broker._c.iterdump())) == before
    assert len(broker.orders()) == len(broker.positions()) == len(journal.trades()) == 1


def test_open_journal_coverage_bound_does_not_silently_truncate(sources):
    for number in range(64):
        open_unlinked(sources, f"legacy-{number}")
    assert snapshot(sources)["open_journal_trade_count"] == 64
    open_unlinked(sources, "overflow")
    with pytest.raises(ValueError, match="open journal.*coverage"):
        snapshot(sources)


@pytest.mark.parametrize("direction", ["long", "bullish", "buy"])
def test_planned_entry_not_actual_fill_and_equivalent_direction_is_not_identity_error(sources, direction):
    # Journal records the immutable approved plan, not the slippage-adjusted fill.
    finalized(sources, direction=direction, entry=100.1, stop=90.1, target=120.1)
    [row] = snapshot(sources)["executions"]
    assert row["integrity_code"] == "CONSISTENT"


def test_order_timeframe_must_match_intent_even_if_id_matches(sources):
    order, _ = finalized(sources)
    with sources[1]._c:
        sources[1]._c.execute("UPDATE v2_orders SET timeframe='15m' WHERE id=?", (order["id"],))
    [row] = snapshot(sources)["executions"]
    assert row["integrity_code"] == "ORDER_IDENTITY_MISMATCH"


def test_no_trade_means_no_journal_incident(sources, tmp_path):
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCExecutionObserver(store, URL, KEY, fetch=lambda: envelope(sources))
    assert observer.poll() == 0
    engine = GuardianIncidentEngine(store)
    engine.scan()
    assert engine.list() == []


@pytest.mark.parametrize("state", ["DECISION_APPROVED", "EXECUTION_PENDING"])
def test_pending_intent_with_unassigned_decision_or_session_is_retained(sources, tmp_path, state):
    sources[0].create_execution_intent(execution_key="early-intent", symbol="BTCUSDT", timeframe="5m")
    if state == "EXECUTION_PENDING":
        sources[0].transition_execution("early-intent", state)
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCExecutionObserver(store, URL, KEY, fetch=lambda: envelope(sources))
    assert observer.poll() == 1
    [event] = store.recent()
    assert event["execution_id"] == "early-intent"
    assert event["correlation_id"] is None
    assert event["session_id"] == ""
    assert event["reason"] == "PENDING"
    assert event["evidence"]["execution_integrity_verified"] is False
    assert sources[1].orders() == sources[1].positions() == []


@pytest.mark.parametrize("damage", [
    "old_contract", "count", "missing_link", "wrong_backref", "duplicate_link",
    "missing_key", "false_link_finding", "nan_size", "bool_count", "future_open",
    "hidden_open_trade", "wrong_outstanding_count", "disagreeing_size",
])
def test_invalid_source_contract_is_unknown_not_healthy_or_partially_imported(sources, tmp_path, damage):
    finalized(sources)
    view = envelope(sources)
    if damage == "old_contract":
        view["schema_version"] = 1
    elif damage == "count":
        view["extra_open_intent_count"] = 1
    elif damage == "missing_link":
        view["open_journal_trades"] = []
    elif damage == "wrong_backref":
        view["executions"][0]["trade_id"] = "different-trade"
    elif damage == "duplicate_link":
        view["open_journal_trades"] *= 2
        view["open_journal_trade_count"] = 2
    elif damage == "missing_key":
        view["open_journal_trades"][0]["execution_keys"] = ["not-observed"]
    elif damage == "false_link_finding":
        view["open_journal_trades"][0]["integrity_code"] = "JOURNAL_INTENT_NOT_FOUND"
    elif damage == "nan_size":
        view["open_journal_trades"][0]["journal_trade_size"] = float("nan")
    elif damage == "bool_count":
        view["open_journal_trade_count"] = True
    elif damage == "future_open":
        view["open_journal_trades"][0]["opened_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    elif damage == "hidden_open_trade":
        view["open_journal_trades"] = []
        view["open_journal_trade_count"] = 0
    elif damage == "wrong_outstanding_count":
        view["outstanding_count"], view["terminal_sample_count"] = 1, 0
    elif damage == "disagreeing_size":
        view["open_journal_trades"][0]["journal_trade_size"] = .02
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCExecutionObserver(store, URL, KEY, fetch=lambda: view)
    with pytest.raises(ValueError):
        observer.poll()
    assert store.count() == 0
    assert store.heartbeats()["guardian_smc_execution_probe"]["state"] == "FAILED"


@pytest.mark.parametrize("journal_mode", ["WAL", "DELETE"])
def test_write_lock_preserves_wal_reads_or_returns_structured_blocker_and_retry(sources, monkeypatch, journal_mode):
    from fastapi.testclient import TestClient
    import app as app_module
    from config import settings
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "smc_agent_journal_db", str(sources[2]))
    monkeypatch.setattr(settings, "smc_paper_db", str(sources[3]))
    client = TestClient(app_module.app)
    # Change only this temporary test fixture, not any production DB pragma.
    assert sources[0]._db.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0].upper() == journal_mode
    with sqlite3.connect(sources[2]) as lock:
        lock.execute("BEGIN EXCLUSIVE")
        reply = client.get("/guardian/smc-execution", headers={"X-Guardian-Observer-Key": KEY})
        assert reply.status_code == (200 if journal_mode == "WAL" else 503)
        if journal_mode == "DELETE":
            assert reply.json()["detail"]["state"] == "PERSISTENCE_BLOCKED"
        lock.rollback()
    assert client.get("/guardian/smc-execution", headers={"X-Guardian-Observer-Key": KEY}).status_code == 200


def test_identity_projection_does_not_read_full_secret_payload(sources):
    intent(sources)
    # Runtime payload is intentionally irrelevant to the observer's projection.
    # Use a freshly created second intent since its payload is immutable.
    intent(sources, "large-payload", payload={"password": "private", "window": "x" * 2_000_000})
    view = snapshot(sources)
    assert "private" not in repr(view)
    assert "window" not in repr(view)
    assert len(repr(view)) < 6000


def test_account_identity_and_market_identity_are_part_of_event_dedup(sources, tmp_path):
    finalized(sources)
    first = envelope(sources)
    second = deepcopy(first)
    second["broker_account_id"] = "a-different-paper-account"
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCExecutionObserver(store, URL, KEY, fetch=lambda: first)
    assert observer.poll() == 2
    observer.fetch = lambda: second
    assert observer.poll() == 2
    observer.fetch = lambda: first
    assert observer.poll() == 0
    # A changed identity with the same updated time must not conflict with a
    # previously immutable event ID, nor be silently deduplicated as that event.
    second["executions"][0]["session_id"] = "different-session"
    assert GuardianSMCExecutionObserver(store, URL, KEY, fetch=lambda: second).poll() == 1
    assert store.count() == 5


def test_partial_guardian_import_failure_replays_without_duplicate_trading_records(sources, tmp_path, monkeypatch):
    finalized(sources)
    store = GuardianStore(tmp_path / "guardian.db")
    original = store.append
    calls = []

    def fail_second(event):
        calls.append(event)
        if len(calls) == 2:
            raise sqlite3.OperationalError("injected Guardian write failure")
        return original(event)

    monkeypatch.setattr(store, "append", fail_second)
    observer = GuardianSMCExecutionObserver(store, URL, KEY, fetch=lambda: envelope(sources))
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        observer.poll()
    assert store.count() == 1
    assert store.heartbeats()["guardian_smc_execution_probe"]["state"] == "FAILED"
    restarted = GuardianSMCExecutionObserver(GuardianStore(store.path), URL, KEY,
                                            fetch=lambda: envelope(sources))
    assert restarted.poll() == 1
    assert restarted.poll() == 0
    assert store.count() == 2
    assert len(sources[1].orders()) == len(sources[1].positions()) == len(sources[0].trades()) == 1


def test_sql_deadline_and_query_only_are_installed_without_source_mutation(sources, monkeypatch):
    from contextlib import closing
    from services import guardian_execution_read_model as model
    ticks = iter([0.0])
    monkeypatch.setattr(model, "monotonic", lambda: next(ticks, 1.0))
    with closing(model._read_connection(sources[2])) as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 250
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM agent_trades")
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            db.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<10000) "
                       "SELECT sum(x) FROM n").fetchone()


def test_transport_rejects_redirect_and_oversized_response(sources, tmp_path, monkeypatch):
    from tradexa.guardian import smc_execution_observer as module
    from tradexa.guardian.lab_execution_observer import _NoRedirect
    observer = GuardianSMCExecutionObserver(GuardianStore(tmp_path / "guardian.db"), URL, KEY)
    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 302, "redirect", {}, "http://elsewhere/") is None
    requested = []

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, bound):
            assert bound == 1048577
            return b"x" * bound

    class Opener:
        def open(self, request, timeout):
            requested.append((request.full_url, timeout))
            assert request.get_header("X-guardian-observer-key") == KEY
            return Response()

    def build(handler):
        assert isinstance(handler, _NoRedirect)
        return Opener()

    monkeypatch.setattr(module, "build_opener", build)
    with pytest.raises(ValueError, match="size limit"):
        observer.poll()
    assert requested == [(URL, 3.0)]
    assert observer.store.count() == 0


def test_adding_a_missing_link_is_recovering_not_certified_execution(sources, tmp_path):
    trade_id = open_unlinked(sources)
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCExecutionObserver(store, URL, KEY, fetch=lambda: envelope(sources))
    assert observer.poll() == 1
    engine = GuardianIncidentEngine(store)
    engine.scan()
    [finding] = engine.list()
    intent(sources, "legacy-1")
    sources[0].transition_execution("legacy-1", "EXECUTED", broker_order_id="historical-order")
    sources[0].transition_execution("legacy-1", "COMPLETE", trade_id=trade_id)
    assert observer.poll() == 2
    engine.scan()
    assert engine.get(finding["incident_id"])["state"] == "RECOVERING"
    assert engine.get(finding["incident_id"])["confidence"] == "POSSIBLE"
    assert any(row["root_component"] == "smc_agent" and row["severity"] == "HIGH"
               for row in engine.list())  # the historical order remains unverified


def test_closed_trade_does_not_claim_new_same_symbol_position_is_its_own(sources):
    _, old_trade = finalized(sources)
    sources[1].process_candle("BTCUSDT", {"open": 120, "high": 121, "low": 119,
                            "close": 120, "volume": 100,
                            "timestamp": datetime.now(timezone.utc).isoformat()})
    assert sources[1].positions() == []
    sources[0].close_trade(old_trade, exit_price=120, realised_r=2, result="WIN", close_reason="target")
    new_order, _ = finalized(sources, key="new-decision")
    rows = {row["execution_key"]: row for row in snapshot(sources)["executions"]}
    assert rows["decision-1"]["open_position_matches_order"] is False
    assert rows["decision-1"]["integrity_code"] == "CONSISTENT"
    assert rows["new-decision"]["open_position_matches_order"] is True
    assert sources[1].positions()[0]["entry_order_id"] == new_order["id"]
