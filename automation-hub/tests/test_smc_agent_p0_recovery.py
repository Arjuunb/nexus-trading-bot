"""Failure injection uses real SQLite brokers/journals, never strategy changes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import threading
import subprocess
import sys

import pytest

from execution.paper_broker_v2 import PaperBrokerV2
from services.smc_agent import SMCAgent
from services.smc_agent_journal import SMCAgentJournal
from tests.test_smc_agent_crash_boundaries import (
    CANDLE, SESSION, _agent, _counts, _evaluation, _quote, _submitter,
)
from tests.test_smc_agent_live_wiring import build


class Crash(BaseException):
    """Bypass normal exception handlers, like process death."""


def evidence(broker, key):
    order = next((row for row in broker.orders() if row["candle_id"] == key), None)
    return {"order": order, "positions": broker.positions()} if order else None


@pytest.mark.parametrize("boundary,fill,expected_state,counts", [
    ("before_intent", False, None, (0, 0, 0)),
    ("after_intent", False, "DECISION_APPROVED", (0, 0, 0)),
    ("after_pending", False, "EXECUTION_PENDING", (0, 0, 0)),
    ("after_commit", False, "EXECUTION_PENDING", (1, 0, 0)),
    ("after_fill", True, "EXECUTION_PENDING", (1, 1, 0)),
    ("during_finalize", True, "EXECUTED", (1, 1, 0)),
    ("after_finalize", True, "COMPLETE", (1, 1, 1)),
])
def test_crash_matrix_reopens_persistent_state(
        tmp_path, boundary, fill, expected_state, counts):
    class FaultJournal(SMCAgentJournal):
        def create_execution_intent(self, **kwargs):
            if boundary == "before_intent":
                raise Crash()
            result = super().create_execution_intent(**kwargs)
            if boundary == "after_intent":
                raise Crash()
            return result

        def transition_execution(self, key, state, **kwargs):
            result = super().transition_execution(key, state, **kwargs)
            if boundary == "after_pending" and state == "EXECUTION_PENDING":
                raise Crash()
            return result

        def open_trade(self, **kwargs):
            if boundary == "during_finalize":
                raise Crash()
            return super().open_trade(**kwargs)

    class FaultAgent(SMCAgent):
        def _finalize_intent(self, *args, **kwargs):
            result = super()._finalize_intent(*args, **kwargs)
            if boundary == "after_finalize":
                raise Crash()
            return result

    broker_path, journal_path = tmp_path / "broker.db", tmp_path / "journal.db"
    broker = PaperBrokerV2(broker_path, starting_balance=100)
    journal = FaultJournal(journal_path)
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)
    submit = _submitter(broker, evaluation, key, fill=fill)

    def execute(size):
        result = submit(size)
        if boundary in {"after_commit", "after_fill"}:
            raise Crash()
        return result

    with pytest.raises(Crash):
        FaultAgent(journal, equity=100).observe(
            evaluation, session_id=SESSION, candle_time=CANDLE, executor=execute)
    assert _counts(broker, journal) == counts
    intents = journal.execution_intents()
    assert len(intents) == (0 if expected_state is None else 1)
    if intents:
        assert intents[0]["state"] == expected_state
        assert intents[0]["decision_id"] == key
        assert intents[0]["execution_key"] == key
    original_orders, original_positions = broker.orders(), broker.positions()
    journal.close()
    broker._c.close()

    for restart in range(3):
        broker = PaperBrokerV2(broker_path, starting_balance=100)
        journal = SMCAgentJournal(journal_path)
        agent = _agent(journal)
        recovered = agent.reconcile_execution_intents(lambda identity: evidence(broker, identity))
        if expected_state:
            intent = journal.execution_intents()[0]
            assert intent["state"] == ("COMPLETE" if counts[0] else "EXECUTION_FAILED")
            assert intent["execution_key"] == key
            assert intent["decision_id"] == key
            duplicate = agent.observe(evaluation, session_id=SESSION, candle_time=CANDLE,
                                      executor=lambda _: pytest.fail("duplicate submission"))
            assert duplicate["outcome"] == "ALREADY_DECIDED"
            assert duplicate["execution_state"] == intent["state"]
        assert broker.orders() == original_orders
        assert broker.positions() == original_positions
        assert _counts(broker, journal) == (counts[0], counts[1], int(bool(counts[0])))
        assert all(row["outcome"] != "MISSED" for row in journal.decisions())
        assert "No order was placed" not in str(recovered)
        if restart:
            assert recovered == []
        journal.close()
        broker._c.close()


def test_partial_fill_recovery_does_not_resize_cancel_or_fill_the_order(tmp_path):
    class Broken(SMCAgentJournal):
        def open_trade(self, **kwargs):
            raise RuntimeError("journal finalization unavailable")

    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100)
    journal = Broken(tmp_path / "journal.db")
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)
    submit = _submitter(broker, evaluation, key)

    def partial(size):
        result = submit(size)
        # Real broker participation-volume path, below the requested 0.5 BTC.
        broker.process_candle("BTCUSDT", {
            "open": 100, "high": 100.1, "low": 99.9, "close": 100,
            "volume": 0.1 / broker.participation_rate,
            "timestamp": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),
        })
        return result

    result = _agent(journal).observe(evaluation, session_id=SESSION, executor=partial)
    assert result["execution_state"] == "EXECUTION_UNCERTAIN"
    order, position = broker.orders()[0], broker.positions()[0]
    assert order["status"] == "partially_filled"
    assert position["size"] == pytest.approx(0.1)
    assert _counts(broker, journal) == (1, 1, 0)
    journal.close()
    recovered = SMCAgentJournal(tmp_path / "journal.db")
    for _ in range(3):
        _agent(recovered).reconcile_execution_intents(lambda identity: evidence(broker, identity))
        assert _counts(broker, recovered) == (1, 1, 1)
        assert broker.orders()[0] == order
        assert broker.positions()[0] == position
    assert recovered.trades()[0]["size"] == pytest.approx(order["quantity"])
    assert recovered.trades()[0]["order_id"] == order["id"]


def test_intent_and_event_are_one_transaction(tmp_path):
    journal = SMCAgentJournal(tmp_path / "journal.db")
    journal._db.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON execution_intent_events
                           BEGIN SELECT RAISE(ABORT, 'event write fault'); END;""")
    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100)
    result = _agent(journal).observe(_evaluation(), session_id=SESSION,
                                    executor=lambda _: pytest.fail("broker called"))
    assert result["execution_state"] == "EXECUTION_FAILED"
    assert journal.execution_intents() == []
    assert _counts(broker, journal) == (0, 0, 0)


def test_two_workers_share_one_execution_identity(tmp_path):
    path = tmp_path / "journal.db"
    journals = [SMCAgentJournal(path), SMCAgentJournal(path)]
    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100)
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)
    submit = _submitter(broker, evaluation, key, fill=True)
    start = threading.Barrier(2)
    calls = []

    def run(journal):
        start.wait(timeout=5)
        def execute(size):
            calls.append(key)
            return submit(size)
        return _agent(journal).observe(evaluation, session_id=SESSION, executor=execute)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, journals))
    assert len(calls) == 1
    assert {row["outcome"] for row in results} == {"TAKEN", "ALREADY_DECIDED"}
    assert _counts(broker, journals[0]) == (1, 1, 1)
    assert len(journals[0].execution_intents()) == 1


def test_distinct_sessions_do_not_incorrectly_deduplicate(tmp_path):
    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100)
    journal = SMCAgentJournal(tmp_path / "journal.db")
    for session in ("session-a", "session-b"):
        evaluation = _evaluation()
        key = SMCAgent.execution_key_for(evaluation, CANDLE, session)
        result = _agent(journal).observe(evaluation, session_id=session,
                                         executor=_submitter(broker, evaluation, key))
        assert result["outcome"] == "TAKEN"
    assert _counts(broker, journal) == (2, 0, 2)
    assert len({row["decision_id"] for row in journal.execution_intents()}) == 2


def test_runtime_recovers_broker_commit_before_account_metadata(tmp_path, monkeypatch):
    journal_path = tmp_path / "journal.db"
    lab = build(tmp_path, monkeypatch, journal_path=journal_path)
    submit = lab.account.broker.submit

    def crash_after_commit(**kwargs):
        submit(**kwargs)
        raise Crash()

    monkeypatch.setattr(lab.account.broker, "submit", crash_after_commit)
    with pytest.raises(Crash):
        lab.runtime.tick()
    assert len(lab.account.broker.orders()) == 1
    assert lab.account.broker.positions() == []
    assert lab.strategy_orders() == []
    assert lab.journal.trades() == []
    intent = lab.journal.execution_intents()[0]
    assert intent["state"] == "EXECUTION_PENDING"
    lab.journal.close()
    restarted = build(tmp_path, monkeypatch, journal_path=journal_path)
    restarted.runtime._reconcile_agent()
    assert restarted.runtime.reconciliation["state"] == "COMPLETE"
    assert _counts(restarted.account.broker, restarted.journal) == (1, 0, 1)
    assert len(restarted.strategy_orders()) == 1
    assert restarted.candidate_status() == "ORDER_CREATED"
    assert restarted.journal.execution_intents()[0]["execution_key"] == intent["execution_key"]
    for _ in range(3):
        restarted.runtime._reconcile_agent()
    assert _counts(restarted.account.broker, restarted.journal) == (1, 0, 1)


def test_persistent_journal_failure_blocks_new_entries_and_status_is_truthful(tmp_path, monkeypatch):
    class Broken(SMCAgentJournal):
        def open_trade(self, **kwargs):
            raise RuntimeError("persistent journal outage")

    path = tmp_path / "journal.db"
    lab = build(tmp_path, monkeypatch, journal=Broken(path))
    first = lab.runtime.tick()
    assert first["agent"]["outcome"] == "EXECUTION_UNCERTAIN"
    assert _counts(lab.account.broker, lab.journal) == (1, 0, 0)
    original_order = lab.account.broker.orders()[0]["id"]
    for _ in range(3):
        lab.runtime._reconcile_agent()
        status = lab.runtime.bot_status()
        assert status["execution_state"] == "BLOCKED"
        assert any("PERSISTENCE_BLOCKED" in row for row in status["blockers"])
        assert status["agent"]["reconciliation"]["state"] == "BLOCKED"
        assert _counts(lab.account.broker, lab.journal) == (1, 0, 0)
        with pytest.raises(RuntimeError, match="PERSISTENCE_BLOCKED"):
            lab.account.submit_order()
    # Recovery opens the same persistent files, does not reset the account.
    lab.journal.close()
    restarted = build(tmp_path, monkeypatch, journal_path=path)
    restarted.runtime._reconcile_agent()
    assert restarted.runtime.reconciliation["state"] == "COMPLETE"
    assert _counts(restarted.account.broker, restarted.journal) == (1, 0, 1)
    assert restarted.journal.trades()[0]["order_id"] == original_order
    assert restarted.runtime.persistence_blocker == ""


def test_unavailable_broker_remains_uncertain_across_restarts(tmp_path):
    path = tmp_path / "journal.db"
    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100)
    journal = SMCAgentJournal(path)
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)

    def response_lost(size):
        _submitter(broker, evaluation, key, fill=True)(size)
        raise RuntimeError("response lost")

    _agent(journal).observe(evaluation, session_id=SESSION, executor=response_lost)
    journal.close()
    for _ in range(3):
        journal = SMCAgentJournal(path)
        def unavailable(_):
            raise sqlite3.OperationalError("broker database temporarily unavailable")
        result = _agent(journal).reconcile_execution_intents(unavailable)
        assert result[0]["state"] == "EXECUTION_UNCERTAIN"
        assert _counts(broker, journal) == (1, 1, 0)
        assert _agent(journal).observe(
            _evaluation(proposal_id="another"), session_id=SESSION,
            executor=lambda _: pytest.fail("new entry during uncertainty"))["outcome"] == "EXECUTION_UNCERTAIN"
        journal.close()
    journal = SMCAgentJournal(path)
    _agent(journal).reconcile_execution_intents(lambda identity: evidence(broker, identity))
    assert _counts(broker, journal) == (1, 1, 1)


def _hard_crash_worker(directory):
    import os
    from pathlib import Path
    root = Path(directory)
    broker = PaperBrokerV2(root / "broker.db", starting_balance=100)
    journal = SMCAgentJournal(root / "journal.db")
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)
    def execute(size):
        _submitter(broker, evaluation, key, fill=True)(size)
        os._exit(73)  # No finally blocks, rollback handlers, or connection.close().
    _agent(journal).observe(evaluation, session_id=SESSION, executor=execute)


def test_real_process_death_after_fill_releases_guard_and_recovers(tmp_path):
    child = subprocess.run(
        [sys.executable, "-c",
         "from tests.test_smc_agent_p0_recovery import _hard_crash_worker; "
         "import sys; _hard_crash_worker(sys.argv[1])", str(tmp_path)],
        timeout=30, capture_output=True, text=True)
    assert child.returncode == 73, child.stderr
    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100)
    journal = SMCAgentJournal(tmp_path / "journal.db")
    assert _counts(broker, journal) == (1, 1, 0)
    before = broker.positions()[0]
    intent = journal.execution_intents()[0]
    assert intent["state"] == "EXECUTION_PENDING"
    result = _agent(journal).reconcile_execution_intents(lambda key: evidence(broker, key))
    assert result[0]["state"] == "COMPLETE"
    assert _counts(broker, journal) == (1, 1, 1)
    assert broker.positions()[0] == before
    trade = journal.trades()[0]
    assert trade["decision_id"] == intent["decision_id"] == intent["execution_key"]
    assert trade["market"]["execution"]["broker_evidence"]["positions"][0]["position_id"] == before["position_id"]
    assert before["entry_order_id"] == trade["order_id"]


def test_runtime_api_preserves_uncertainty_and_reconciliation(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    from routers.native_smc import router

    class Broken(SMCAgentJournal):
        def open_trade(self, **kwargs):
            raise RuntimeError("finalization failed")

    lab = build(tmp_path, monkeypatch, journal=Broken(tmp_path / "journal.db"))
    lab.runtime.tick()
    monkeypatch.setattr(webhook_api, "smc_runtime", lab.runtime)
    monkeypatch.setattr(webhook_api, "smc_paper", lab.account)
    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/research/smc/bot-status").json()
    assert body["execution_state"] == "BLOCKED"
    assert body["agent"]["last_result"]["outcome"] == "EXECUTION_UNCERTAIN"
    assert body["agent"]["last_result"]["order_id"] == lab.account.broker.orders()[0]["id"]
    assert body["agent"]["reconciliation"]["state"] == "BLOCKED"
    assert body["paper_only"] is True
    assert body["real_execution_allowed"] is False
    assert "No order was placed" not in str(body)


def test_completed_identity_and_events_cannot_be_rewritten(tmp_path):
    journal = SMCAgentJournal(tmp_path / "journal.db")
    result = _agent(journal).observe(_evaluation(), session_id=SESSION)
    key = result["execution_key"]
    with pytest.raises(ValueError, match="illegal execution transition"):
        journal.transition_execution(key, "EXECUTION_PENDING")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        journal._db.execute("UPDATE execution_intents SET symbol='ETHUSDT'")
    journal._db.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        journal._db.execute("DELETE FROM execution_intent_events")
    journal._db.rollback()


@pytest.mark.parametrize("committed", [False, True])
def test_broker_commit_failure_is_reconciled_from_durable_state(tmp_path, monkeypatch, committed):
    lab = build(tmp_path, monkeypatch, journal_path=tmp_path / "journal.db")
    connection = lab.account.broker._c

    class FaultConnection:
        def __getattr__(self, name):
            return getattr(connection, name)

        def commit(self):
            # Only inject at entry submission, not account metrics.
            pending = connection.execute("SELECT COUNT(*) FROM v2_orders").fetchone()[0]
            if pending:
                if committed:
                    connection.commit()
                raise sqlite3.OperationalError("commit response failed")
            connection.commit()

    monkeypatch.setattr(lab.account.broker, "_c", FaultConnection())
    result = lab.runtime.tick()
    assert connection.in_transaction is False
    assert _counts(lab.account.broker, lab.journal) == ((1, 0, 1) if committed else (0, 0, 0))
    intent = lab.journal.execution_intents()[0]
    assert intent["state"] == ("COMPLETE" if committed else "EXECUTION_FAILED")
    assert result["agent"]["outcome"] == ("TAKEN" if committed else "EXECUTION_FAILED")
    # A separate connection sees the same committed truth.
    with sqlite3.connect(lab.account.broker.path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM v2_orders").fetchone()[0] == int(committed)


def test_repeated_journal_outage_does_not_append_heartbeat_revisions(tmp_path):
    class Broken(SMCAgentJournal):
        def open_trade(self, **kwargs):
            raise RuntimeError("persistent finalization fault")
    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100)
    journal = Broken(tmp_path / "journal.db")
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)
    agent = _agent(journal)
    agent.observe(evaluation, session_id=SESSION, executor=_submitter(broker, evaluation, key))
    agent.reconcile_execution_intents(lambda identity: evidence(broker, identity))
    before = journal._db.execute("SELECT COUNT(*) FROM execution_intent_events").fetchone()[0]
    for _ in range(100):
        result = agent.reconcile_execution_intents(lambda identity: evidence(broker, identity))
        assert result[0]["state"] == "EXECUTION_UNCERTAIN"
    assert journal._db.execute("SELECT COUNT(*) FROM execution_intent_events").fetchone()[0] == before
    assert _counts(broker, journal) == (1, 0, 0)


def test_whole_journal_write_outage_after_fill_preserves_pending_truth(tmp_path, monkeypatch):
    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100)
    journal = SMCAgentJournal(tmp_path / "journal.db")
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)
    def execute(size):
        result = _submitter(broker, evaluation, key, fill=True)(size)
        journal._db.execute("PRAGMA query_only=ON")
        return result
    result = _agent(journal).observe(evaluation, session_id=SESSION, executor=execute)
    assert result["outcome"] == "EXECUTION_UNCERTAIN"
    assert result["executed"] is True
    assert "persistence blocked" in result["error"]
    assert journal.execution_intents()[0]["state"] == "EXECUTION_PENDING"
    assert _counts(broker, journal) == (1, 1, 0)
    assert _agent(journal).observe(
        _evaluation(proposal_id="new-decision"), session_id=SESSION,
        executor=lambda _: pytest.fail("new entry during outage"))["outcome"] == "EXECUTION_UNCERTAIN"
    journal.close()
    journal = SMCAgentJournal(tmp_path / "journal.db")
    _agent(journal).reconcile_execution_intents(lambda identity: evidence(broker, identity))
    assert _counts(broker, journal) == (1, 1, 1)


def test_legacy_intent_keeps_original_identity_after_reconciliation(tmp_path):
    class LegacyAgent(SMCAgent):
        def _intent_payload(self, *args, **kwargs):
            payload = super()._intent_payload(*args, **kwargs)
            del payload["candidate_id"]
            return payload

    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100)
    journal = SMCAgentJournal(tmp_path / "journal.db")
    evaluation = _evaluation()
    original_key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)
    LegacyAgent(journal, equity=100).observe(
        evaluation, session_id=SESSION, executor=_submitter(broker, evaluation, original_key))
    # An older safety build used the current display candle as part of its key.
    # The new build must resolve its stored proposal, not assign another identity.
    replay = _agent(journal).observe(
        evaluation, session_id=SESSION, candle_time="2026-09-20T12:05:00+00:00",
        executor=lambda _: pytest.fail("legacy decision resubmitted"))
    assert replay["outcome"] == "ALREADY_DECIDED"
    assert replay["execution_key"] == original_key
    assert _counts(broker, journal) == (1, 0, 1)


def test_journal_commit_succeeds_then_response_fails_does_not_duplicate(tmp_path, monkeypatch):
    lab = build(tmp_path, monkeypatch, journal_path=tmp_path / "journal.db")
    connection = lab.journal._db
    class FaultConnection:
        def __getattr__(self, name):
            return getattr(connection, name)
        def commit(self):
            completed = connection.execute(
                "SELECT COUNT(*) FROM execution_intents WHERE state='COMPLETE'").fetchone()[0]
            connection.commit()
            if completed:
                raise sqlite3.OperationalError("journal commit response lost")
    monkeypatch.setattr(lab.journal, "_db", FaultConnection())
    result = lab.runtime.tick()
    assert _counts(lab.account.broker, lab.journal) == (1, 0, 1)
    assert lab.journal.execution_intents()[0]["state"] == "COMPLETE"
    assert result["agent"]["outcome"] == "EXECUTION_UNCERTAIN"
    assert result["agent"]["executed"] is True
    monkeypatch.setattr(lab.journal, "_db", connection)
    lab.runtime._reconcile_agent()
    assert lab.runtime.reconciliation["state"] == "COMPLETE"
    duplicate = lab.runtime.tick()
    assert duplicate["agent"]["outcome"] == "ALREADY_DECIDED"
    assert _counts(lab.account.broker, lab.journal)[::2] == (1, 1)


@pytest.mark.parametrize("transport", ["quote", "partial_candle"])
@pytest.mark.parametrize("boundary", ["position_write", "fill_write", "protection_write"])
def test_inflight_fill_failure_rolls_back_the_whole_broker_event(
        tmp_path, monkeypatch, transport, boundary):
    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100)
    journal = SMCAgentJournal(tmp_path / "journal.db")
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)
    _agent(journal).observe(evaluation, session_id=SESSION,
                            executor=_submitter(broker, evaluation, key))
    # Crash inside the fill transaction, not merely after its commit.
    method_name = {"position_write": "_apply_position", "fill_write": "_fill",
                   "protection_write": "_resolved_protection"}[boundary]
    real = getattr(broker, method_name)
    def fault(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("in-flight broker write failure")
    monkeypatch.setattr(broker, method_name, fault)
    quote = _quote()
    candle = {"open": 100, "high": 100.1, "low": 99.9, "close": 100,
              "volume": 0.1 / broker.participation_rate,
              "timestamp": quote["received_at"]}
    def advance():
        if transport == "quote":
            return broker.process_tick("BTCUSDT", quote)
        return broker.process_candle("BTCUSDT", candle)
    with pytest.raises(RuntimeError, match="in-flight broker"):
        advance()
    assert broker._c.in_transaction is False
    assert _counts(broker, journal) == (1, 0, 1)
    assert broker.fills() == []
    assert broker.orders()[0]["filled"] == 0
    assert broker._c.execute("SELECT COUNT(*) FROM v2_quote_cursor").fetchone()[0] == 0
    assert broker.account()["balance"] == 100
    monkeypatch.setattr(broker, method_name, real)
    advance()
    assert _counts(broker, journal) == (1, 1, 1)
    assert len(broker.fills()) == 1
    assert broker.positions()[0]["size"] == pytest.approx(0.5 if transport == "quote" else 0.1)
    assert broker.positions()[0]["stop_loss"] == 99
    assert broker.positions()[0]["entry_order_id"] == broker.orders()[0]["id"]
