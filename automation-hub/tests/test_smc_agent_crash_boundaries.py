"""Crash-boundary proof for the SMC agent/broker/journal seam.

These tests deliberately keep the source SMC decision fixed and inject faults
only around the downstream execution lifecycle.  The broker and journal use
separate SQLite files, so the tests exercise the same partial-commit boundary
that a process crash can leave in production.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from execution.paper_broker_v2 import PaperBrokerV2
from services.smc_agent import (ALREADY_DECIDED, ExecutionFailed, SMCAgent,
                                Sizing)
from services.smc_agent_journal import (EXECUTION_COMPLETE, EXECUTION_FAILED,
                                        EXECUTION_UNCERTAIN, TAKEN,
                                        SMCAgentJournal)


SESSION = "smc-session-crash-proof"
CANDLE = "2026-09-20T12:00:00+00:00"


def _evaluation(*, symbol: str = "BTCUSDT", timeframe: str = "5m",
                proposal_id: str = "proposal-crash", setup_id: str = "setup-crash",
                version: str = "1.0") -> dict:
    return {
        "strategy_id": "SMC_SOURCE_V1", "version": version,
        "state": "ENTRY_READY",
        "data_identity": {"symbol": symbol, "timeframe": timeframe,
                           "selected_candle": CANDLE},
        "ordered_condition_results": [{"label": "SMC complete", "status": "PASS"}],
        "missing_conditions": [],
        "trade_plan": {"entry": 100.0, "stop": 99.0, "target_1": 102.0,
                        "target_2": 103.0, "risk_percent": 0.5},
        "proposal": {"id": proposal_id, "setup_id": setup_id,
                      "symbol": symbol, "direction": "bullish"},
        "proposal_id": proposal_id, "setup_id": setup_id,
    }


def _quote() -> dict:
    now = datetime.now(timezone.utc) + timedelta(seconds=5)
    stamp = now.isoformat()
    return {"bid": 100.0, "ask": 100.01, "mark": 100.0,
            "received_at": stamp, "event_timestamp": stamp,
            "sequence": 1, "quote_event_id": "crash-proof-quote-1"}


def _submitter(broker: PaperBrokerV2, evaluation: dict, key: str, *,
               fill: bool = False):
    plan = evaluation["trade_plan"]
    identity = evaluation["data_identity"]

    def submit(sizing: Sizing):
        order = broker.submit(
            symbol=identity["symbol"], side="buy", order_type="market",
            quantity=sizing.executed, protection_stop_loss=plan["stop"],
            protection_take_profit=plan["target_2"],
            protection_target_r=3.0, protection_tick_size=0.1,
            strategy=evaluation["strategy_id"], strategy_version=evaluation["version"],
            timeframe=identity["timeframe"], candle_id=key,
            signal_timestamp=CANDLE, decision_timestamp=CANDLE,
            signal_price=plan["entry"], requested_price=plan["entry"])
        if fill:
            broker.process_tick(identity["symbol"], _quote())
        return {"order": order}

    return submit


def _agent(journal: SMCAgentJournal) -> SMCAgent:
    return SMCAgent(journal, equity=100.0)


def _counts(broker: PaperBrokerV2, journal: SMCAgentJournal) -> tuple[int, int, int]:
    return len(broker.orders()), len(broker.positions()), len(journal.trades())


def test_before_intent_persistence_calls_no_broker_and_stays_fail_closed(tmp_path):
    class BrokenIntent(SMCAgentJournal):
        def create_execution_intent(self, **kwargs):
            raise RuntimeError("intent persistence unavailable")

    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100.0)
    journal = BrokenIntent(":memory:")
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)

    first = _agent(journal).observe(
        evaluation, candle_time=CANDLE, session_id=SESSION,
        executor=_submitter(broker, evaluation, key))
    second = _agent(journal).observe(
        evaluation, candle_time=CANDLE, session_id=SESSION,
        executor=_submitter(broker, evaluation, key))

    assert first["execution_state"] == EXECUTION_FAILED
    assert second["outcome"] == ALREADY_DECIDED
    assert _counts(broker, journal) == (0, 0, 0)
    assert journal.execution_intents() == []


def test_after_intent_before_submission_is_failed_and_not_retried(tmp_path):
    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100.0)
    journal = SMCAgentJournal(tmp_path / "journal.db")
    evaluation = _evaluation()

    def fail_before_submit(sizing):
        raise ExecutionFailed("rejected before broker commit")

    agent = _agent(journal)
    first = agent.observe(evaluation, candle_time=CANDLE, session_id=SESSION,
                          executor=fail_before_submit)
    second = agent.observe(evaluation, candle_time=CANDLE, session_id=SESSION,
                           executor=lambda _: pytest.fail("duplicate submit"))

    intent = journal.execution_intents()[0]
    assert first["execution_state"] == EXECUTION_FAILED
    assert second["outcome"] == ALREADY_DECIDED
    assert intent["execution_key"] == SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)
    assert _counts(broker, journal) == (0, 0, 0)
    journal.close()


def test_submission_commit_then_response_crash_is_uncertain_and_reconciles(tmp_path):
    broker_path = tmp_path / "broker.db"
    journal_path = tmp_path / "journal.db"
    broker = PaperBrokerV2(broker_path, starting_balance=100.0)
    journal = SMCAgentJournal(journal_path)
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)
    real_submit = _submitter(broker, evaluation, key)

    def commit_then_crash(sizing):
        real_submit(sizing)
        raise RuntimeError("connection lost after broker commit")

    first = _agent(journal).observe(evaluation, candle_time=CANDLE,
                                    session_id=SESSION, executor=commit_then_crash)
    assert first["execution_state"] == EXECUTION_UNCERTAIN
    assert _counts(broker, journal) == (1, 0, 0)
    assert journal.execution_intents()[0]["state"] == EXECUTION_UNCERTAIN

    journal.close()
    broker._c.close()
    recovered_broker = PaperBrokerV2(broker_path, starting_balance=100.0)
    recovered_journal = SMCAgentJournal(journal_path)
    recovered = _agent(recovered_journal).reconcile_execution_intents(
        lambda lookup_key: next(
            ({"order": row, "positions": recovered_broker.positions()}
             for row in recovered_broker.orders()
             if row.get("candle_id") == lookup_key), None))

    assert recovered and recovered[0]["state"] == EXECUTION_COMPLETE
    assert _counts(recovered_broker, recovered_journal) == (1, 0, 1)
    assert recovered_journal.execution_intents()[0]["state"] == EXECUTION_COMPLETE
    assert recovered_journal.trades()[0]["order_id"] == recovered_broker.orders()[0]["id"]
    recovered_journal.close()
    recovered_broker._c.close()


def test_order_commit_before_fill_remains_one_order_and_can_be_reconciled(tmp_path):
    broker = PaperBrokerV2(tmp_path / "broker.db", starting_balance=100.0)
    journal = SMCAgentJournal(tmp_path / "journal.db")
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)

    result = _agent(journal).observe(
        evaluation, candle_time=CANDLE, session_id=SESSION,
        executor=_submitter(broker, evaluation, key, fill=False))

    assert result["outcome"] == TAKEN
    assert result["execution_state"] == EXECUTION_COMPLETE
    assert _counts(broker, journal) == (1, 0, 1)
    assert broker.orders()[0]["status"] == "open"
    journal.close()


def test_fill_and_position_before_journal_failure_survive_restart(tmp_path):
    broker_path = tmp_path / "broker.db"
    journal_path = tmp_path / "journal.db"
    broker = PaperBrokerV2(broker_path, starting_balance=100.0)
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)

    class JournalCrash(SMCAgentJournal):
        def open_trade(self, **kwargs):
            raise RuntimeError("crash during journal finalization")

    broken = JournalCrash(journal_path)
    first = _agent(broken).observe(
        evaluation, candle_time=CANDLE, session_id=SESSION,
        executor=_submitter(broker, evaluation, key, fill=True))
    assert first["execution_state"] == EXECUTION_UNCERTAIN
    assert _counts(broker, broken) == (1, 1, 0)
    assert broker.orders()[0]["status"] == "filled"
    assert broker.positions()[0]["size"] > 0
    assert broken.execution_intents()[0]["execution_key"] == key
    broken.close()
    broker._c.close()

    recovered_broker = PaperBrokerV2(broker_path, starting_balance=100.0)
    recovered_journal = SMCAgentJournal(journal_path)
    agent = _agent(recovered_journal)
    evidence = lambda lookup_key: next(
        ({"order": row, "positions": recovered_broker.positions()}
         for row in recovered_broker.orders()
         if row.get("candle_id") == lookup_key), None)
    first_reconcile = agent.reconcile_execution_intents(evidence)
    second_reconcile = agent.reconcile_execution_intents(evidence)

    assert first_reconcile[0]["state"] == EXECUTION_COMPLETE
    assert first_reconcile[0]["position_discovered"] is True
    assert first_reconcile[0]["position_count"] == 1
    assert second_reconcile == []
    assert _counts(recovered_broker, recovered_journal) == (1, 1, 1)
    intent = recovered_journal.execution_intents()[0]
    trade = recovered_journal.trades()[0]
    position = recovered_broker.positions()[0]
    order = recovered_broker.orders()[0]
    assert intent["state"] == EXECUTION_COMPLETE
    assert intent["execution_key"] == key
    assert intent["broker_order_id"] == order["id"] == trade["order_id"]
    assert trade["size"] == pytest.approx(position["size"])
    # The journal keeps the approved plan entry; the broker keeps the actual
    # spread/slippage-adjusted fill entry. Both remain linked by order_id.
    assert trade["entry"] == pytest.approx(evaluation["trade_plan"]["entry"])
    assert order["average_price"] == pytest.approx(position["entry_price"])
    assert order["requested_price"] == pytest.approx(trade["entry"])
    assert trade["stop"] == pytest.approx(evaluation["trade_plan"]["stop"])
    assert trade["target"] == pytest.approx(evaluation["trade_plan"]["target_2"])
    assert trade["planned_rr"] == pytest.approx(3.0)
    assert all(row["outcome"] != "MISSED" for row in recovered_journal.decisions())
    assert "No order was placed" not in str(first_reconcile)
    recovered_journal.close()
    recovered_broker._c.close()


def test_repeated_restart_reconciliation_and_duplicate_decision_are_idempotent(tmp_path):
    broker_path = tmp_path / "broker.db"
    journal_path = tmp_path / "journal.db"
    broker = PaperBrokerV2(broker_path, starting_balance=100.0)
    evaluation = _evaluation()
    key = SMCAgent.execution_key_for(evaluation, CANDLE, SESSION)

    class JournalCrash(SMCAgentJournal):
        def open_trade(self, **kwargs):
            raise RuntimeError("journal unavailable")

    broken = JournalCrash(journal_path)
    _agent(broken).observe(evaluation, candle_time=CANDLE, session_id=SESSION,
                           executor=_submitter(broker, evaluation, key, fill=True))
    broken.close()
    broker._c.close()

    for _ in range(3):
        broker = PaperBrokerV2(broker_path, starting_balance=100.0)
        journal = SMCAgentJournal(journal_path)
        recovered = _agent(journal).reconcile_execution_intents(
            lambda lookup_key: next(
                ({"order": row, "positions": broker.positions()}
                 for row in broker.orders()
                 if row.get("candle_id") == lookup_key), None))
        assert recovered in ([],) or recovered[0]["state"] == EXECUTION_COMPLETE
        journal.close()
        broker._c.close()

    broker = PaperBrokerV2(broker_path, starting_balance=100.0)
    journal = SMCAgentJournal(journal_path)
    duplicate = _agent(journal).observe(
        evaluation, candle_time=CANDLE, session_id=SESSION,
        executor=lambda _: pytest.fail("same decision submitted twice"))
    assert duplicate["outcome"] == ALREADY_DECIDED
    assert _counts(broker, journal) == (1, 1, 1)
    journal.close()
    broker._c.close()


def test_execution_keys_are_distinct_by_session_setup_market_and_candle():
    base = _evaluation()
    key = SMCAgent.execution_key_for(base, CANDLE, "session-a")
    assert key == SMCAgent.execution_key_for(base, CANDLE, "session-a")
    variants = [
        _evaluation(symbol="ETHUSDT"),
        _evaluation(timeframe="15m"),
        _evaluation(proposal_id="proposal-other"),
        _evaluation(setup_id="setup-other"),
        _evaluation(version="2.0"),
    ]
    assert all(SMCAgent.execution_key_for(item, CANDLE, "session-a") != key
               for item in variants)
    assert SMCAgent.execution_key_for(base, "2026-09-20T12:05:00+00:00", "session-a") != key
    assert SMCAgent.execution_key_for(base, CANDLE, "session-b") != key
