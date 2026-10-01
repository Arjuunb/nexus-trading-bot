"""Guardian observes SMC paper evidence without becoming an order authority."""
from datetime import datetime, timedelta, timezone
import sqlite3

from fastapi.testclient import TestClient

import app as app_module
from config import settings
from execution.paper_broker_v2 import PaperBrokerV2
from services.guardian_execution_read_model import smc_execution_integrity_snapshot
from services.smc_agent_journal import SMCAgentJournal
from tradexa.guardian.smc_execution_observer import GuardianSMCExecutionObserver
from tradexa.guardian.incidents import GuardianIncidentEngine
from tradexa.guardian.store import GuardianStore


KEY = "guardian-smc-execution-observer-key-12345"
URL = "http://app:8000/guardian/smc-execution"


def _sources(tmp_path):
    journal_path, broker_path = tmp_path / "agent.db", tmp_path / "smc.db"
    journal = SMCAgentJournal(journal_path)
    broker = PaperBrokerV2(broker_path, account_type="SMC_LAB",
                           execution_engine="SMC_LAB")
    journal.create_execution_intent(
        execution_key="decision-1", decision_id="decision-1",
        session_id="smc-session", symbol="BTCUSDT", timeframe="5m")
    journal.transition_execution("decision-1", "EXECUTION_PENDING")
    return journal, broker, journal_path, broker_path


def _submit(broker, key="decision-1"):
    return broker.submit(
        symbol="BTCUSDT", side="buy", order_type="limit", quantity=0.01,
        limit_price=100, protection_stop_loss=90, protection_take_profit=120,
        strategy="SMC_M1_SWEEP_REVERSAL", strategy_version="1.0",
        timeframe="5m", candle_id=key)


def test_broker_commit_before_journal_order_id_is_discovered_without_writes(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    _submit(broker)
    before = (journal.execution_intent("decision-1"), len(broker.orders()))
    snap = smc_execution_integrity_snapshot(journal_path, broker_path)
    [row] = snap["executions"]
    assert snap["broker_account_type"] == "SMC_LAB"
    assert snap["cross_database_atomic"] is False
    assert row["integrity_code"] == "BROKER_ORDER_UNRECORDED"
    assert row["broker_order_id"] is None
    assert row["discovered_broker_order_id"] == broker.orders()[0]["id"]
    assert row["matching_broker_order_count"] == 1
    assert (journal.execution_intent("decision-1"), len(broker.orders())) == before
    journal.close()
    broker._c.close()


def test_fill_before_trade_finalization_remains_observable(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    order = _submit(broker)
    broker.process_candle("BTCUSDT", {
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100,
        "timestamp": "2030-01-01T00:05:00+00:00",
    })
    assert broker.positions()[0]["entry_order_id"] == order["id"]
    journal.transition_execution("decision-1", "EXECUTED",
                                 broker_order_id=order["id"])
    [row] = smc_execution_integrity_snapshot(journal_path, broker_path)["executions"]
    assert row["integrity_code"] == "FILLED_ORDER_JOURNAL_PENDING"
    assert row["open_position_matches_order"] is True
    assert row["journal_trade_found"] is None
    assert len(broker.orders()) == len(broker.positions()) == 1
    journal.close()
    broker._c.close()


def test_finalized_trade_is_linked_to_the_same_order_and_position(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    order = _submit(broker)
    broker.process_candle("BTCUSDT", {
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100,
        "timestamp": "2030-01-01T00:05:00+00:00",
    })
    journal.transition_execution("decision-1", "EXECUTED",
                                 broker_order_id=order["id"])
    trade_id = journal.open_trade(
        decision_id="decision-1", symbol="BTCUSDT", timeframe="5m",
        direction="long", entry=100, stop=90, target=120,
        planned_rr=2, size=0.01, why="approved SMC paper decision",
        order_id=order["id"])
    journal.transition_execution("decision-1", "COMPLETE", trade_id=trade_id)
    [row] = smc_execution_integrity_snapshot(journal_path, broker_path)["executions"]
    assert row["integrity_code"] == "CONSISTENT"
    assert row["journal_trade_found"] is True
    assert row["open_position_matches_order"] is True
    assert len(broker.orders()) == len(broker.positions()) == len(journal.trades()) == 1
    journal.close()
    broker._c.close()


def test_trade_journal_does_not_prove_resting_order_filled(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    order = _submit(broker)
    journal.transition_execution("decision-1", "EXECUTED",
                                 broker_order_id=order["id"])
    trade_id = journal.open_trade(
        decision_id="decision-1", symbol="BTCUSDT", timeframe="5m",
        direction="long", entry=100, stop=90, target=120,
        planned_rr=2, size=0.01, why="approved SMC paper decision",
        order_id=order["id"])
    journal.transition_execution("decision-1", "COMPLETE", trade_id=trade_id)
    [row] = smc_execution_integrity_snapshot(journal_path, broker_path)["executions"]
    assert row["broker_filled_quantity"] == 0
    assert row["open_position_matches_order"] is None
    assert row["integrity_code"] == "AGENT_TRADE_PRECEDES_FILL"
    assert broker.positions() == []
    journal.close()
    broker._c.close()


def test_partial_fill_does_not_prove_full_journaled_size(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    order = _submit(broker)
    broker.process_candle("BTCUSDT", {
        "open": 100, "high": 101, "low": 99, "close": 100, "volume": 0.2,
        "timestamp": "2030-01-01T00:05:00+00:00",
    })
    journal.transition_execution("decision-1", "EXECUTED",
                                 broker_order_id=order["id"])
    trade_id = journal.open_trade(
        decision_id="decision-1", symbol="BTCUSDT", timeframe="5m",
        direction="long", entry=100, stop=90, target=120,
        planned_rr=2, size=0.01, why="approved SMC paper decision",
        order_id=order["id"])
    journal.transition_execution("decision-1", "COMPLETE", trade_id=trade_id)
    [row] = smc_execution_integrity_snapshot(journal_path, broker_path)["executions"]
    assert 0 < row["broker_filled_quantity"] < row["journal_trade_size"]
    assert row["integrity_code"] == "JOURNAL_SIZE_EXCEEDS_BROKER_FILL"
    assert row["open_position_matches_order"] is True
    journal.close()
    broker._c.close()


def test_broker_commit_after_intent_failed_is_not_called_no_order(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    _submit(broker)
    journal.transition_execution("decision-1", "EXECUTION_FAILED",
                                 error="journal could not prove broker result")
    [row] = smc_execution_integrity_snapshot(journal_path, broker_path)["executions"]
    assert row["integrity_code"] == "BROKER_ORDER_UNRECORDED"
    assert row["state"] == "EXECUTION_FAILED"
    assert row["discovered_broker_order_id"] == broker.orders()[0]["id"]
    journal.close()
    broker._c.close()


def test_observer_key_and_deduplicated_event_do_not_change_trading_records(
        tmp_path, monkeypatch):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    _submit(broker)
    monkeypatch.setattr(settings, "guardian_observer_key", KEY)
    monkeypatch.setattr(settings, "smc_agent_journal_db", str(journal_path))
    monkeypatch.setattr(settings, "smc_paper_db", str(broker_path))
    client = TestClient(app_module.app)
    assert client.get("/guardian/smc-execution").status_code == 401
    assert client.post("/guardian/smc-execution", headers={
        "X-Guardian-Observer-Key": KEY}).status_code == 401
    response = client.get("/guardian/smc-execution", headers={
        "X-Guardian-Observer-Key": KEY})
    assert response.status_code == 200
    assert response.json()["executions"][0]["integrity_code"] == "BROKER_ORDER_UNRECORDED"
    assert response.json()["execution_integrity_verified"] is False
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCExecutionObserver(store, URL, KEY, fetch=response.json)
    assert observer.poll() == 1
    assert observer.poll() == 0
    assert len(store.recent()) == 1
    assert len(broker.orders()) == 1
    assert journal.execution_intent("decision-1")["broker_order_id"] is None
    journal.close()
    broker._c.close()


def test_real_resting_order_with_premature_trade_opens_possible_incident(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    order = _submit(broker)
    journal.transition_execution("decision-1", "EXECUTED",
                                 broker_order_id=order["id"])
    trade_id = journal.open_trade(
        decision_id="decision-1", symbol="BTCUSDT", timeframe="5m",
        direction="long", entry=100, stop=90, target=120,
        planned_rr=2, size=0.01, why="approved SMC paper decision",
        order_id=order["id"])
    journal.transition_execution("decision-1", "COMPLETE", trade_id=trade_id)

    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCExecutionObserver(
        store, URL, KEY,
        fetch=lambda: {**smc_execution_integrity_snapshot(journal_path, broker_path),
                       "schema_version": 1,
                       "observed_at": datetime.now(timezone.utc).isoformat(),
                       "feed_health_verified": False,
                       "execution_integrity_verified": False})
    assert observer.poll() == 1
    engine = GuardianIncidentEngine(store)
    assert engine.scan() == 1
    [incident] = engine.list()
    assert incident["fingerprint"] == "smc_execution_integrity:decision-1"
    assert incident["severity"] == "WARNING"
    assert incident["confidence"] == "POSSIBLE"
    assert len(broker.orders()) == 1
    assert broker.positions() == []
    assert len(journal.trades()) == 1
    journal.close()
    broker._c.close()


def test_source_update_with_unchanged_projection_is_not_event_id_collision(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    store = GuardianStore(tmp_path / "guardian.db")
    first_update = datetime.now(timezone.utc) - timedelta(seconds=2)
    source = smc_execution_integrity_snapshot(journal_path, broker_path)
    source["executions"][0]["updated_at"] = first_update.isoformat()

    def fetch():
        return {**source, "schema_version": 1,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "feed_health_verified": False,
                "execution_integrity_verified": False}

    observer = GuardianSMCExecutionObserver(store, URL, KEY, fetch=fetch)
    assert observer.poll() == 1
    assert observer.poll() == 0
    source["executions"][0]["updated_at"] = (
        first_update + timedelta(seconds=1)).isoformat()
    assert observer.poll() == 1
    assert observer.poll() == 0
    assert store.count() == 2
    assert all(event["severity"] == "INFO" for event in store.recent())
    journal.close()
    broker._c.close()


def test_resting_order_without_trade_is_normal_guardian_info(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    order = _submit(broker)
    journal.transition_execution("decision-1", "EXECUTED",
                                 broker_order_id=order["id"])
    store = GuardianStore(tmp_path / "guardian.db")
    observer = GuardianSMCExecutionObserver(
        store, URL, KEY,
        fetch=lambda: {**smc_execution_integrity_snapshot(journal_path, broker_path),
                       "schema_version": 1,
                       "observed_at": datetime.now(timezone.utc).isoformat(),
                       "feed_health_verified": False,
                       "execution_integrity_verified": False})
    assert observer.poll() == 1
    assert store.recent()[0]["reason"] == "ORDER_AWAITING_FILL"
    assert store.recent()[0]["severity"] == "INFO"
    engine = GuardianIncidentEngine(store)
    engine.scan()
    assert engine.list() == []
    journal.close()
    broker._c.close()


def test_outstanding_coverage_limit_fails_closed(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    for number in range(64):
        journal.create_execution_intent(
            execution_key=f"decision-extra-{number}",
            decision_id=f"decision-extra-{number}",
            session_id="smc-session", symbol="BTCUSDT", timeframe="5m")
    try:
        smc_execution_integrity_snapshot(journal_path, broker_path)
        raise AssertionError("unobserved outstanding intents must fail closed")
    except ValueError:
        pass
    journal.close()
    broker._c.close()


def test_missing_or_non_smc_broker_fails_closed(tmp_path):
    journal, broker, journal_path, broker_path = _sources(tmp_path)
    broker._c.execute("UPDATE v2_account SET account_type='PAPER' WHERE id=1")
    broker._c.commit()
    try:
        smc_execution_integrity_snapshot(journal_path, broker_path)
        raise AssertionError("non-SMC account must not be observed as SMC paper")
    except ValueError:
        pass
    journal.close()
    broker._c.close()
    broker_path.unlink()
    try:
        smc_execution_integrity_snapshot(journal_path, broker_path)
        raise AssertionError("missing broker must not produce an empty healthy snapshot")
    except sqlite3.OperationalError:
        pass
