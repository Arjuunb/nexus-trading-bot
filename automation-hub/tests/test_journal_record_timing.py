"""A trade record's times and latencies are the ones that actually happened.

Every case runs real code: the 3-Candle Rejection strategy through
AutoStrategyEngine, the signal pipeline and the forward paper engine, or the
frozen SMC strategy placed and filled by the SMC lab's own broker.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from bot.types import Bar
from data.decision_store import DecisionStore
from data.ledger import SqliteLedger
from data.trade_record_store import TradeRecordStore
from execution.paper_engine import ForwardPaperExecutionEngine
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.journal_labs import SMCLabProjector
from services.journal_recorder import JournalRecorder, LedgerSource
from services.signal_pipeline import SignalPipeline
from services.strategy_factory import make_builtin_strategy
from services.trading_instances import InstanceLedger
from tests.test_journal_integrity import _real_smc_trade
from tests.test_three_candle_rejection import _history, _long_pattern

TF = timedelta(minutes=5)


def _forward_trade(tmp_path):
    """One real forward-paper long, filled on the next quote. Returns the
    record, the ledger and the signal candle."""
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    decisions = DecisionStore(str(tmp_path / "decisions.db"))
    scoped = InstanceLedger(ledger, "inst-1", "sess-1")
    paper = ForwardPaperExecutionEngine(scoped, 10_000)
    pipe = SignalPipeline(scoped, paper, TradingControl(), equity=10_000,
                          risk_per_trade_pct=0.01, exposure_limit_pct=0.05)
    pipe.journal_context = {"instance_id": "inst-1", "simulation_session_id": "sess-1",
                            "market_data_mode": "forward_paper"}
    strategy = make_builtin_strategy("three_candle_rejection", "BTCUSDT")
    engine = AutoStrategyEngine(pipe, paper, scoped, symbols=["BTCUSDT"], timeframe="5m", live=True,
                                strategy_factory=lambda _s: strategy,
                                fetcher=lambda *a, **k: ([], "live (test)"), entry_mode="market",
                                instance_id="inst-1")
    engine.decisions, engine.quality_gate_bypass = decisions, (lambda: True)
    rows, i = _history()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    series = rows + _long_pattern(i)
    bars = [Bar(now - TF * (len(series) - k), r.open, r.high, r.low, r.close, r.volume)
            for k, r in enumerate(series)]
    strategy.bars.extend(bars[:-3])
    for bar in bars[-3:]:
        engine._process_bar("BTCUSDT", bar, strategy)
    assert paper.process_quote({"bid": 102.2, "ask": 102.22, "mark": 102.21,
                                "received_at": datetime.now(timezone.utc).isoformat()})
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    recorder = JournalRecorder(store)
    recorder.add_ledger(LedgerSource("MAIN", ledger, decision_store=decisions))
    recorder.reconcile()
    [row] = store.query_trades()
    return store.get(row["journal_record_id"]), ledger, bars[-1]


def _fill_event(ledger, record) -> dict:
    [row] = ledger._c.execute(
        "SELECT payload_json FROM webhook_events WHERE alert_id LIKE ?",
        (f"{record['intent_id']}:fill:%",)).fetchall()
    return json.loads(row[0])


# ---------------------------------------------------------------- D16
def test_a_forward_paper_order_has_no_acknowledgement_time(tmp_path):
    """The paper engine parks the intent as the order and nothing acknowledges
    it. The record keeps that one time and does not copy it into an ack."""
    record, ledger, _ = _forward_trade(tmp_path)
    fill = _fill_event(ledger, record)
    assert record["intent_id"] == record["order_id"]
    assert record["intent_created_at"] == record["order_submitted_at"]
    assert record["order_submitted_at"][:26] == fill["order_timestamp"][:26]
    assert record["order_acknowledged_at"] is None
    assert record["entry_filled_at"] is not None


def test_a_lab_order_has_no_acknowledgement_time(tmp_path):
    """The SMC lab's broker writes its order row and fills it later; it sends
    no acknowledgement either."""
    account, _ = _real_smc_trade(tmp_path)
    store = TradeRecordStore()
    SMCLabProjector(account).project(store)
    [row] = store.query_trades()
    record = store.get(row["journal_record_id"])
    [order] = [o for o in account.broker.orders() if o["id"] == record["order_id"]]
    assert record["order_submitted_at"][:19] == order["created_at"][:19]
    assert record["order_acknowledged_at"] is None
