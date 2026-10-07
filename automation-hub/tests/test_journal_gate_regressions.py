"""Journal release-gate regressions: each test reproduces a defect the gate
found in the canonical journal and fails on the code before its fix.

Every scenario drives the real execution components (paper engine, ledger,
Trading Instance manager, PaperBrokerV2 labs) and checks the journal against
the facts they wrote.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from data.journal_store import JournalStore
from data.ledger import SqliteLedger
from data.trade_record_store import TradeRecordStore
from execution.paper_engine import PaperExecutionEngine
from services.journal_recorder import JournalRecorder, LedgerSource
from services.trading_instances import InstanceLedger, TradingInstanceManager


def _factory(_key, symbol):
    from strategies.brain_strategy import DecisionBrain
    return DecisionBrain(symbol)


class InstanceRig:
    """One Trading Instance on a file-backed ledger, journaled by the recorder."""

    def __init__(self, tmp_path):
        self.ledger = SqliteLedger(str(tmp_path / "ledger.db"))
        self.manager = TradingInstanceManager(
            self.ledger, strategy_factory=_factory, live=False, live_poll_s=60,
            decision_journal=SimpleNamespace(store=JournalStore(str(tmp_path / "journal.db"))))
        self.instance = self.manager.create(
            symbol="BTCUSDT", strategy_key="brain", strategy_label="Decision Brain",
            strategy_version="v1", timeframe="5m", risk_per_trade_pct=0.005,
            capital_allocation=1_000)
        self.store = TradeRecordStore(str(tmp_path / "trade_records.db"))
        self.recorder = JournalRecorder(self.store)
        self.recorder.add_ledger(LedgerSource("MAIN", self.ledger))

    def paper(self) -> PaperExecutionEngine:
        instance = next(i for i in self.manager.store.list() if i.id == self.instance.id)
        return PaperExecutionEngine(
            InstanceLedger(self.ledger, instance.id, instance.simulation_session_id),
            instance.starting_equity)

    def restart(self):
        return self.manager.restart_simulation_account(self.instance.id, initiated_by="operator")

    def reconcile(self) -> dict:
        report = self.recorder.reconcile()
        assert report["errors"] == []
        return report

    def record(self, trade_id: str) -> dict:
        return next(r for r in self.store.query_trades(limit=50) if r["trade_id"] == trade_id)


@pytest.fixture
def rig(tmp_path):
    rig = InstanceRig(tmp_path)
    yield rig
    rig.manager.shutdown()


def test_account_restart_with_an_open_position_ends_its_record_and_keeps_recording(rig):
    """The restart marks the open row cancelled. Projecting that lifecycle used
    to raise IndexError on every pass, so no later trade reached the journal."""
    opened = rig.paper().open(symbol="BTCUSDT", side="BUY", size=1, entry=100, stop=95)
    rig.reconcile()
    assert rig.record(opened.trade_id)["status"] == "OPEN"

    assert rig.restart()["open_positions_cleared"] == 1
    paper = rig.paper()                      # the new simulation session
    later = paper.open(symbol="BTCUSDT", side="BUY", size=1, entry=101, stop=96)
    paper.close(symbol="BTCUSDT", exit_price=104)
    rig.reconcile()
    rig.reconcile()                          # idempotent: nothing left to fail on

    cancelled = rig.record(opened.trade_id)
    assert (cancelled["status"], cancelled["outcome"], cancelled["finalized"]) == (
        "CANCELLED", "CANCELLED", 1)
    assert cancelled["exit_reason"] == "account-restart"
    # no exit fill happened, so no exit price or result is invented
    for field in ("actual_exit", "gross_pnl", "fees", "net_pnl", "realized_r"):
        assert cancelled[field] is None, field
    assert cancelled["position_closed_at"] is not None
    row = next(r for r in rig.ledger.get_paper_trades(instance_id=rig.instance.id)
               if r["id"] == later.trade_id)
    closed = rig.record(later.trade_id)
    assert (closed["status"], closed["outcome"]) == ("CLOSED", "WIN")
    assert closed["net_pnl"] == pytest.approx(row["pnl"])


def test_account_restart_after_a_scale_out_keeps_the_realized_part(rig):
    """A restart after a partial exit: the realized leg stays a result (the
    ledger booked it), the cancelled remainder adds none, and the trade ends
    because of the restart."""
    paper = rig.paper()
    opened = paper.open(symbol="BTCUSDT", side="BUY", size=2, entry=100, stop=95)
    paper.reduce(symbol="BTCUSDT", exit_price=110, fraction=0.5)
    rig.reconcile()
    rig.restart()
    rig.reconcile()

    legs = [r for r in rig.ledger.get_paper_trades(instance_id=rig.instance.id)]
    assert sorted(r["status"] for r in legs) == ["cancelled", "closed"]
    realized = sum(r["pnl"] for r in legs if r["status"] == "closed")
    record = rig.record(opened.trade_id)
    assert (record["status"], record["finalized"]) == ("CLOSED", 1)
    assert record["exit_reason"] == "account-restart"
    assert record["net_pnl"] == pytest.approx(realized)
    assert record["actual_exit"] == pytest.approx(110)
    assert record["filled_quantity"] == pytest.approx(2)


def test_filled_quantity_after_a_scale_out_is_the_whole_entry(rig):
    """A scale-out rewrites the trade row to the size it closed; the filled
    quantity used to read that row alone (1.0 of a 2.0 entry)."""
    paper = rig.paper()
    opened = paper.open(symbol="BTCUSDT", side="BUY", size=2, entry=100, stop=95)
    paper.reduce(symbol="BTCUSDT", exit_price=106, fraction=0.5)
    rig.reconcile()
    assert rig.record(opened.trade_id)["filled_quantity"] == pytest.approx(2)
    paper.close(symbol="BTCUSDT", exit_price=103)
    rig.reconcile()
    record = rig.record(opened.trade_id)
    legs = rig.ledger.get_paper_trades(instance_id=rig.instance.id)
    assert record["status"] == "CLOSED"
    assert record["filled_quantity"] == pytest.approx(sum(r["size"] for r in legs)) == pytest.approx(2)
    assert record["net_pnl"] == pytest.approx(sum(r["pnl"] for r in legs))


# ───────────────────────── PaperBrokerV2 labs ─────────────────────────
_RULES = {"tick_size": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
          "max_quantity": 100.0, "min_notional": 5.0}


def _pa_state(entry=105, stop=100, target=117.5, valid_until_index=10_000):
    return {"research_id": "PRICE_ACTION_NATIVE_V1_RESEARCH", "strategy_version": "1.1.0",
            "symbol": "BTCUSDT", "timeframe": "5m",
            "setups": [{"id": "s-gate", "strategy_id": "PA1_SR_REJECTION", "direction": "bullish",
                        "phase": "ORDER_PENDING", "zone_id": "z-gate"}],
            "proposals": [{"id": "p-gate", "setup_id": "s-gate", "strategy_id": "PA1_SR_REJECTION",
                           "direction": "bullish", "entry": entry, "stop": stop, "target": target,
                           "valid_until_index": valid_until_index}], "metrics": {}}


def _pa_lab(tmp_path, mode: str):
    from bot.types import Bar
    from services.price_action_lab import PaperExecutionConfig, PriceActionPaperAccount
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(hours=1)
    pa = PriceActionPaperAccount(str(tmp_path / "pa.db"))
    pa.start(mode=mode, symbol="BTCUSDT", timeframe="5m",
             execution_config=PaperExecutionConfig(operating_mode="automatic",
                                                   strategy_id="PA1_SR_REJECTION"))
    feed = {"state": "HISTORICAL_REPLAY" if mode == "HISTORICAL" else "SYNCHRONIZED"}
    pa.synchronize_strategy(_pa_state(), contract_rules=_RULES, candle=Bar(now, 100, 104, 99, 103, 1000),
                            feed_reliable=True, feed_status=feed,
                            allow_candle_fills=mode == "HISTORICAL")
    return pa, now, feed


def _lab_truth(broker, start_balance):
    with broker._lock:
        funding = [dict(r) for r in broker._c.execute("SELECT * FROM v2_funding_events")]
    account = broker.account()
    return {"balance_change": account["balance"] - start_balance,
            "funding": sum(e["amount"] for e in funding), "fills": broker.fills(limit=1000)}


def test_one_entry_order_that_opens_two_positions_records_each_position(tmp_path):
    """On the candle path the bar that fills an entry's first slice can also
    stop that slice out; the order's remainder then fills from flat and opens
    a second position. Both positions used to share one record key, so the
    second one's P&L never reached the journal (net -20.66 vs broker +54.48)."""
    from bot.types import Bar
    from services.journal_labs import PALabProjector
    pa, now, feed = _pa_lab(tmp_path, "HISTORICAL")
    start = pa.broker.account()["balance"]
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    projector = PALabProjector(pa)
    for minutes, bar in ((5, (104.0, 105.5, 99.5, 100.5, 200)), (10, (104.5, 106.0, 104.0, 105.8, 500)),
                         (15, (110.0, 119.0, 109.5, 118.5, 500))):
        pa.synchronize_strategy(_pa_state(), contract_rules=_RULES,
                                candle=Bar(now + timedelta(minutes=minutes), *bar),
                                feed_reliable=True, feed_status=feed)
        projector.project(store)
    [order] = [o for o in pa.broker.orders() if not o["reduce_only"]]
    entries = sorted((f for f in pa.broker.fills(limit=1000) if f["order_id"] == order["id"]),
                     key=lambda f: f["timestamp"])
    assert len(entries) == 2 and not pa.broker.positions()      # two positions, both closed

    records = sorted(store.query_trades(where="record_source='PA_LAB'", limit=10),
                     key=lambda r: r["entry_filled_at"])
    assert [r["status"] for r in records] == ["CLOSED", "CLOSED"]
    assert [r["filled_quantity"] for r in records] == [pytest.approx(f["quantity"]) for f in entries]
    truth = _lab_truth(pa.broker, start)
    assert sum(r["net_pnl"] for r in records) == pytest.approx(truth["balance_change"])
    for record, entry in zip(records, entries):
        assert record["legs"][0]["fill_id"] == entry["id"]       # each record keeps its own legs

    # A record finalized before this rule had the second position's legs
    # written over its own; the next pass restores them.
    first = records[0]
    with store._lock:
        store._c.execute("UPDATE trade_records SET legs_json=? WHERE journal_record_id=?",
                         (json.dumps(records[1]["legs"]), first["journal_record_id"]))
        store._c.commit()
    projector.project(store)
    assert store.get(first["journal_record_id"])["legs"] == first["legs"]


def test_lab_trade_carries_the_risk_of_every_entry_fill_and_the_funding_it_paid(tmp_path):
    """SMC lab, candle path: the entry fills in three pieces around a T1
    scale-out, funding is charged while it is open, then the stop closes it.
    Risk used to be the first fill's (R inflated 5.5x) and funding was read
    from the fills, which the broker always writes as 0."""
    from bot.types import Bar
    from services.journal_labs import SMCLabProjector
    from services.smc_strategy_lab import SMCPaperAccount, SMCPaperConfig
    from services.smc_strategy_v1 import evaluate
    from tests.test_smc_strategy_ladder import seeded_engine
    smc = SMCPaperAccount(str(tmp_path / "smc.db"))
    smc.configure(config=SMCPaperConfig(operating_mode="automatic"))
    evaluation = evaluate(seeded_engine())
    smc.synchronize_candidate(evaluation, rules=_RULES,
                              reference_price=evaluation["trade_plan"]["entry"], feed_reliable=True)
    [entry_order] = smc.broker.orders()
    start = smc.broker.account()["balance"]
    t0 = evaluation["proposal"]["signal_timestamp"]
    smc.process_candle("BTCUSDT", Bar(t0 + timedelta(minutes=5), 100.9, 101.5, 100.5, 101.0, 150))
    assert smc.apply_funding_once(symbol="BTCUSDT", funding_time=t0.isoformat(), rate=0.0005,
                                  mark_price=101.0)["applied"]
    t1 = next(o for o in smc.broker.orders() if o["reduce_only"])
    smc.process_candle("BTCUSDT", Bar(t0 + timedelta(minutes=10), 110.0, t1["limit_price"] + 0.5,
                                      109.0, 113.0, 150))
    smc.process_candle("BTCUSDT", Bar(t0 + timedelta(minutes=15), 112.0, 113.0, 111.0, 112.0, 150))
    smc.process_candle("BTCUSDT", Bar(t0 + timedelta(minutes=20), 100.0, 100.5, 94.0, 95.0, 2000))
    assert not smc.broker.positions()
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    SMCLabProjector(smc).project(store)

    [record] = store.query_trades(where="record_source='SMC_LAB'", limit=10)
    truth = _lab_truth(smc.broker, start)
    entries = [f for f in truth["fills"] if f["order_id"] == entry_order["id"]]
    assert len(entries) == 3
    assert truth["funding"] > 0
    assert record["risk_amount"] == pytest.approx(sum(f["risk_amount"] for f in entries))
    assert record["filled_quantity"] == pytest.approx(sum(f["quantity"] for f in entries))
    assert record["funding"] == pytest.approx(truth["funding"])
    assert record["fees"] == pytest.approx(sum(f["fee"] for f in truth["fills"]))
    assert record["net_pnl"] == pytest.approx(truth["balance_change"])
    assert record["gross_pnl"] == pytest.approx(sum(f["realized_pnl"] for f in truth["fills"]))
    assert record["realized_r"] == pytest.approx(record["net_pnl"] / record["risk_amount"], abs=1e-4)


def test_funding_booked_while_the_exit_quote_waits_belongs_to_the_closing_trade(tmp_path):
    """PA lab, live quote path: the exit quote is received, a funding charge is
    booked on the still-open position, then the quote is processed. The
    journal's net must equal the broker balance change, funding included."""
    from services.journal_labs import PALabProjector
    pa, _now, _feed = _pa_lab(tmp_path, "LIVE_PAPER")
    start = pa.broker.account()["balance"]
    sequence = iter(range(1, 100))

    def quote(bid, ask, at=None):
        at = at or datetime.now(timezone.utc)
        return pa.process_quote("BTCUSDT", {
            "bid": bid, "ask": ask, "mark": (bid + ask) / 2, "received_at": at.isoformat(),
            "event_timestamp": at.isoformat(), "sequence": next(sequence)}, feed_reliable=True)

    quote(105.0, 105.1)                                   # the stop entry fills
    assert pa.broker.positions()
    exit_received = datetime.now(timezone.utc)
    assert pa.apply_funding_once(symbol="BTCUSDT", funding_time=exit_received.isoformat(),
                                 rate=0.0004, mark_price=118.0)["applied"]
    quote(118.05, 118.15, exit_received)                  # processed after the charge
    assert not pa.broker.positions()
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    PALabProjector(pa).project(store)
    [record] = store.query_trades(where="record_source='PA_LAB'", limit=10)
    truth = _lab_truth(pa.broker, start)
    assert truth["funding"] > 0
    assert record["status"] == "CLOSED"
    assert record["funding"] == pytest.approx(truth["funding"])
    assert record["net_pnl"] == pytest.approx(truth["balance_change"])
