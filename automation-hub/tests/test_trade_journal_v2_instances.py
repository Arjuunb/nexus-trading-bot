"""Trading Instance workers write the canonical journal through the same
pipeline and engine hooks as every other source, with instance identity."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from data.trade_journal_store import TradeJournalStore
from services.trade_journal import TradeJournalRecorder
from tests.test_instance_failure_modes import _create, _manager


def _journaled_manager(tmp_path):
    ledger, hub, manager = _manager(tmp_path)
    store = TradeJournalStore(str(tmp_path / "journal.db"))
    manager.trade_journal = TradeJournalRecorder(store)
    return ledger, manager, store


def test_instance_forward_trade_is_journaled_from_decision_to_operator_close(tmp_path):
    ledger, manager, store = _journaled_manager(tmp_path)
    instance = _create(manager, "BTCUSDT")
    manager.start(instance.id)
    engine, paper = manager._runtime[instance.id][0], manager._runtime[instance.id][1]
    engine.pipeline.symbol_rules_provider = None   # the shared harness stubs venue rules as a dict
    decided = datetime.now(timezone.utc) - timedelta(seconds=2)
    result = engine.pipeline.process({
        "alert_id": f"auto:{instance.id}:BTCUSDT:5m:{decided.isoformat()}:buy", "symbol": "BTCUSDT",
        "side": "BUY", "entry": 100.0, "stop": 95.0, "target": 115.0, "confidence": 1.0,
        "strategy": "Decision Brain", "timeframe": "5m", "timestamp": decided.isoformat(),
        "reason": "fixture decision", "regime": "Trending"})
    assert result.accepted, result.reason
    pending = store.list_trades()[0]
    assert pending["status"] == "PENDING" and pending["instance_id"] == instance.id
    assert pending["trade_source"] == "TRADING_INSTANCE" and pending["trading_mode"] == "FORWARD_PAPER"
    assert pending["exchange"] == "Binance USD-M Futures" and pending["market_type"] == "PERPETUAL_FUTURES"
    stamp = datetime.now(timezone.utc).isoformat()
    paper.process_quote({"symbol": "BTCUSDT", "last": 100.0, "bid": 99.9, "ask": 100.1, "mark": 100.0,
                         "sequence": 1, "received_at": stamp, "event_timestamp": stamp, "quote_event_id": "q1"})
    trade = store.get_trade(pending["trade_id"])
    assert trade["status"] == "OPEN" and trade["entry_filled_at"] == stamp
    assert trade["instance_name"].startswith("BTCUSDT Decision Brain 5m")
    # an operator disposes of the position before deleting the instance
    engine.last_prices["BTCUSDT"] = 104.0
    engine.last_activity = datetime.now(timezone.utc).isoformat()
    idle = threading.Event()
    engine._thread = threading.Thread(target=idle.wait, daemon=True)
    engine._thread.start()
    manager.close_open_positions(instance.id)
    idle.set()
    closed = store.get_trade(pending["trade_id"])
    assert closed["status"] == "CLOSED" and closed["exit_reason"] == "MANUAL_CLOSE"
    assert closed["exit_reason_source"] == "OPERATOR_DISPOSAL"
    ledger_net = sum(r["pnl"] for r in ledger.get_paper_trades(instance_id=instance.id) if r["status"] == "closed")
    assert abs(closed["net_pnl"] - ledger_net) < 1e-9
    assert len(store.list_trades()) == 1
    manager.shutdown()


def test_instance_reconciliation_resolves_identity_for_unjournaled_ledger_rows(tmp_path):
    ledger, manager, store = _journaled_manager(tmp_path)
    instance = _create(manager, "ETHUSDT")
    manager.start(instance.id)
    paper = manager._runtime[instance.id][1]
    paper.journal = None   # the journal was unavailable when this fill happened
    stamp = datetime.now(timezone.utc).isoformat()
    paper.open(symbol="ETHUSDT", side="BUY", size=0.01, entry=100.0, stop=95.0,
               sizing_context={"decision_timestamp": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
    paper.process_quote({"symbol": "ETHUSDT", "bid": 99.9, "ask": 100.1, "mark": 100.0, "sequence": 1,
                         "received_at": stamp, "event_timestamp": stamp, "quote_event_id": "q1"})
    summary = manager.trade_journal.reconcile_ledger(ledger, mode_resolver=manager.journal_identity, grace_s=0)
    assert summary["created"] == 1
    trade = store.list_trades()[0]
    assert trade["instance_id"] == instance.id and trade["strategy_name"] == "Decision Brain"
    assert trade["trading_mode"] == "FORWARD_PAPER" and trade["timeframe"] == "5m"
    assert trade["data_completeness"] == "RECONCILED_FROM_LEDGER"
    manager.shutdown()
