"""Execution evidence observes committed accounting without changing economics."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from data.ledger import SqliteLedger
from execution.paper_engine import ForwardPaperExecutionEngine, PaperExecutionEngine
from services.fill_model import RealisticFill
from services.trading_instances import InstanceLedger


def _model():
    return RealisticFill(spread_pct=0, slippage_pct=0, latency_pct=0,
                         taker_fee_pct=0.001, maker_fee_pct=0.0005)


def _open(engine, **kwargs):
    return engine.open(symbol="XRPUSDT", side="BUY", size=10, entry=1.0,
                       stop=0.9, target=1.2, alert_id="entry-1", **kwargs)


def _quote(at):
    return {"symbol": "XRPUSDT", "bid": 1.0, "ask": 1.01, "mark": 1.005,
            "received_at": at.isoformat()}


def test_open_receipt_has_committed_ids_and_actual_initial_risk():
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, fill_model=_model())
    observed = []

    def observer(fill):
        assert ledger.get_positions("open")[0]["id"] == fill.position_id
        assert ledger.get_paper_trades()[0]["id"] == fill.trade_id
        observed.append(fill)

    paper.evidence_listener = observer
    before = datetime.now(timezone.utc)
    fill = _open(paper, sizing_context={"evidence_context": {"decision_id": "d1"}})
    assert observed[0] == fill
    assert fill.receipt["initial_risk_amount"] == pytest.approx(1.0)
    assert fill.receipt["booked_fees"] == 0
    assert fill.receipt["funding"] is None
    assert fill.receipt["funding_coverage"] == "not_modeled"
    assert fill.receipt["sizing_context"]["evidence_context"]["decision_id"] == "d1"
    assert before <= datetime.fromisoformat(fill.executed_at) <= datetime.now(timezone.utc)


def test_reduce_and_close_receipts_reference_true_parent_and_remainder_ids():
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, fill_model=_model())
    opened = _open(paper)
    paper.update_stop("XRPUSDT", 1.0)  # current stop must not redefine initial risk
    reduced = paper.reduce(symbol="XRPUSDT", exit_price=1.1, fraction=0.4,
                           execution_id="reduce-1")
    remainder = ledger.get_positions("open")[0]
    remaining_trade = next(t for t in ledger.get_paper_trades() if t["status"] == "open")
    assert reduced.trade_id == opened.trade_id
    assert reduced.parent_trade_id == opened.trade_id
    assert reduced.remainder_position_id == remainder["id"]
    assert reduced.remainder_trade_id == remaining_trade["id"]
    assert reduced.receipt["remainder_size"] == remaining_trade["size"]
    assert reduced.receipt["initial_risk_amount"] == pytest.approx(1.0)
    assert reduced.receipt["gross_pnl"] == pytest.approx(0.4)
    assert reduced.receipt["booked_fees"] == pytest.approx(0.0084)
    closed = paper.close(symbol="XRPUSDT", exit_price=1.2, execution_id="close-1")
    assert closed.trade_id == reduced.remainder_trade_id
    assert closed.receipt["initial_risk_amount"] == pytest.approx(1.0)
    for fill in (reduced, closed):
        trade = next(t for t in ledger.get_paper_trades() if t["id"] == fill.trade_id)
        assert fill.receipt["net_pnl"] == trade["pnl"] == fill.pnl
        assert fill.receipt["booked_fees"] == trade["fees"] == fill.fee
        assert fill.receipt["gross_pnl"] == pytest.approx(fill.pnl + fill.fee)
    assert paper.balance() == pytest.approx(10_000 + reduced.pnl + closed.pnl)


@pytest.mark.parametrize("observer_mode", ["absent", "success", "failure", "mutating"])
def test_observer_cannot_change_financial_rows_or_returned_economics(observer_mode):
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, fill_model=_model())
    seen = []

    def observer(fill):
        seen.append(fill.action)
        if observer_mode == "failure":
            raise RuntimeError("observational write failed")
        if observer_mode == "mutating":
            fill.pnl = 100_000
            fill.receipt["net_pnl"] = 100_000

    if observer_mode != "absent":
        paper.evidence_listener = observer
    _open(paper)
    reduced = paper.reduce(symbol="XRPUSDT", exit_price=1.1, fraction=0.4)
    closed = paper.close(symbol="XRPUSDT", exit_price=1.2)
    assert reduced.pnl == pytest.approx(0.3916)
    assert closed.pnl == pytest.approx(1.1868)
    assert paper.realized_pnl() == pytest.approx(1.5784)
    assert paper.fees_paid() == pytest.approx(0.0216)
    if observer_mode != "absent":
        assert seen == ["opened", "reduced", "closed"]


def test_failed_commit_does_not_publish_evidence():
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger)
    observed = []
    paper.evidence_listener = observed.append
    ledger._c.execute("CREATE TRIGGER fail BEFORE INSERT ON paper_executions "
                      "BEGIN SELECT RAISE(ABORT, 'injected execution failure'); END")
    with pytest.raises(Exception, match="injected execution failure"):
        _open(paper)
    assert observed == []
    assert ledger.get_positions() == []


def test_deferred_receipt_uses_actual_quote_and_keeps_durable_evidence_context():
    ledger = SqliteLedger(":memory:")
    engine = ForwardPaperExecutionEngine(InstanceLedger(ledger, "instance-1"),
                                        fill_model=_model())
    decided = datetime.now(timezone.utc)
    quote_at = decided + timedelta(seconds=1)
    context = {"decision_timestamp": decided.isoformat(),
               "strategy_version": "1.0.0", "strategy_config_hash": "hash-1",
               "evidence_context": {"decision_id": "d1"}}
    observed = []
    engine.evidence_listener = observed.append
    assert _open(engine, sizing_context=context).action == "intent"
    assert observed == []
    fill, = engine.process_quote(_quote(quote_at))
    assert fill.executed_at == quote_at.isoformat()
    assert len(observed) == 1
    assert observed[0].receipt["sizing_context"]["evidence_context"] == {"decision_id": "d1"}
    event = ledger.get_webhook_events()[0]
    payload = event["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["trade_id"] == fill.trade_id
    assert payload["position_id"] == fill.position_id
    assert payload["execution_id"] == fill.execution_id
    assert payload["strategy_config_hash"] == "hash-1"
    assert payload["funding_coverage"] == "not_modeled"
    assert engine.process_quote(_quote(quote_at + timedelta(seconds=1))) == []
    assert len(observed) == 1


def test_restart_recovers_existing_fill_without_new_accounting_or_quote_economics():
    ledger = SqliteLedger(":memory:")
    scoped = InstanceLedger(ledger, "instance-1")
    decided = datetime.now(timezone.utc)
    first = ForwardPaperExecutionEngine(scoped, fill_model=_model())
    _open(first, sizing_context={"decision_timestamp": decided.isoformat(),
                                "evidence_context": {"decision_id": "d1"}})
    persisted_intents = first.pending_intents()
    opened, = first.process_quote(_quote(decided + timedelta(seconds=1)))
    restored = ForwardPaperExecutionEngine(scoped, fill_model=_model(),
                                           initial_intents=persisted_intents)
    observed = []
    restored.evidence_listener = observed.append
    assert restored.process_quote(_quote(decided + timedelta(seconds=2))) == []
    recovery, = observed
    assert recovery.action == "recovered"
    assert recovery.trade_id == opened.trade_id
    assert recovery.position_id == opened.position_id
    assert recovery.receipt["recovery_coverage"] == "persisted_open"
    assert recovery.receipt["net_pnl"] is None
    assert "fill_bid" not in recovery.receipt["sizing_context"]
    assert recovery.executed_at == ledger.get_paper_trades()[0]["opened_at"]
    assert restored.pending_intents() == {}
    assert len(ledger.get_paper_trades()) == 1
    assert restored.process_quote(_quote(decided + timedelta(seconds=3))) == []
    assert len(observed) == 1


def test_recovery_does_not_attribute_unrelated_open_position_to_pending_decision():
    ledger = SqliteLedger(":memory:")
    scoped = InstanceLedger(ledger, "instance-1")
    engine = ForwardPaperExecutionEngine(scoped)
    decided = datetime.now(timezone.utc)
    _open(engine, sizing_context={"decision_timestamp": decided.isoformat()})
    paper = PaperExecutionEngine(scoped)
    paper.open(symbol="XRPUSDT", side="BUY", size=1, entry=1.0, stop=0.9,
               alert_id="different-decision")
    observed = []
    engine.evidence_listener = observed.append
    assert engine.process_quote(_quote(decided + timedelta(seconds=1))) == []
    assert observed == []


def test_historical_missing_risk_is_unknown_in_exit_receipt():
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger)
    _open(paper)
    ledger._c.execute("UPDATE paper_trades SET risk_amount_at_entry=NULL")
    ledger._c.commit()
    closed = paper.close(symbol="XRPUSDT", exit_price=1.1)
    assert closed.receipt["initial_risk_amount"] is None


@pytest.mark.parametrize("action", ["reduced", "closed"])
def test_exit_prepare_receipt_precedes_commit_and_matches_committed_economics(action):
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, fill_model=_model())
    opened = _open(paper)
    events = []

    def prepare(prepared_action, execution_id, receipt):
        assert ledger.get_paper_trades()[0]["status"] == "open"
        assert ledger.get_positions("open")[0]["id"] == opened.position_id
        assert receipt["trade_id"] == opened.trade_id
        assert receipt["position_id"] == opened.position_id
        assert receipt["position_size"] == 10
        assert "executed_at" not in receipt
        events.append((prepared_action, execution_id, receipt))

    paper.evidence_prepare_listener = prepare
    paper.evidence_listener = lambda fill: events.append(fill)
    if action == "reduced":
        fill = paper.reduce(symbol="XRPUSDT", exit_price=1.1, fraction=0.4,
                            execution_id="exit-prepare-1")
    else:
        fill = paper.close(symbol="XRPUSDT", exit_price=1.1,
                           execution_id="exit-prepare-1")
    prepared_action, execution_id, receipt = events[0]
    assert prepared_action == action
    assert execution_id == fill.execution_id
    assert events[1] == fill
    assert receipt["net_pnl"] == fill.pnl
    assert receipt["gross_pnl"] == fill.pnl + fill.fee
    assert receipt["booked_fees"] == fill.fee
    assert receipt["closed_size"] == fill.size
    assert receipt["remainder_size"] == (6 if action == "reduced" else 0)


@pytest.mark.parametrize("failure_mode", ["failure", "mutation"])
def test_exit_prepare_observer_cannot_change_accounting_or_result(failure_mode):
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, fill_model=_model())
    _open(paper)
    prepared = []

    def prepare(action, execution_id, receipt):
        prepared.append(action)
        if failure_mode == "failure":
            raise RuntimeError("prepare store unavailable")
        receipt["net_pnl"] = 1_000_000
        receipt["booked_fees"] = 1_000_000

    paper.evidence_prepare_listener = prepare
    reduced = paper.reduce(symbol="XRPUSDT", exit_price=1.1, fraction=0.4)
    closed = paper.close(symbol="XRPUSDT", exit_price=1.2)
    assert prepared == ["reduced", "closed"]
    assert reduced.pnl == reduced.receipt["net_pnl"] == pytest.approx(0.3916)
    assert closed.pnl == closed.receipt["net_pnl"] == pytest.approx(1.1868)
    assert paper.realized_pnl() == pytest.approx(1.5784)


def test_prepare_receipt_survives_failed_commit_and_is_stable_for_same_id_retry():
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, fill_model=_model())
    _open(paper)
    prepared, observed = [], []
    paper.evidence_prepare_listener = lambda action, execution_id, receipt: prepared.append(
        (action, execution_id, receipt))
    paper.evidence_listener = observed.append
    ledger._c.execute("CREATE TRIGGER fail BEFORE INSERT ON paper_executions "
                      "WHEN NEW.action='REDUCE' "
                      "BEGIN SELECT RAISE(ABORT, 'injected execution failure'); END")
    with pytest.raises(Exception, match="injected execution failure"):
        paper.reduce(symbol="XRPUSDT", exit_price=1.1, fraction=0.4,
                     execution_id="retry-reduce-1")
    assert len(prepared) == 1
    assert observed == []
    assert len(ledger.get_paper_trades()) == 1
    assert ledger.get_paper_trades()[0]["status"] == "open"
    ledger._c.execute("DROP TRIGGER fail")
    result = paper.reduce(symbol="XRPUSDT", exit_price=1.1, fraction=0.4,
                          execution_id="retry-reduce-1")
    assert prepared[0] == prepared[1]
    assert observed == [result]
