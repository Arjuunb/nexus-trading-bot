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


# ───────────────────────────── resets ─────────────────────────────
def test_a_logged_paper_reset_ends_the_open_records_it_deleted(tmp_path):
    """An initial-capital change deletes every paper trade and position. The
    journal used to keep their records OPEN forever; a missing row without
    the reset's log entry must still not end anything."""
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    paper = PaperExecutionEngine(ledger, 10_000)
    opened = paper.open(symbol="BTCUSDT", side="BUY", size=1, entry=100, stop=95)
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    recorder = JournalRecorder(store)
    recorder.add_ledger(LedgerSource("MAIN", ledger))
    assert recorder.reconcile()["errors"] == []

    with ledger._lock:                   # rows gone without a logged reset: left alone
        saved = [dict(r) for r in ledger._c.execute("SELECT * FROM paper_trades")]
        ledger._c.execute("DELETE FROM paper_trades")
        ledger._c.commit()
    recorder.reconcile()
    assert store.get(opened.trade_id)["status"] == "OPEN"

    with ledger._lock:
        for row in saved:
            ledger._c.execute(f"INSERT INTO paper_trades({','.join(row)}) "
                              f"VALUES ({','.join('?' * len(row))})", list(row.values()))
        ledger._c.commit()
    ledger.reset_paper()                 # what POST /paper/initial-capital does, then logs
    ledger.log(level="warning", stage="account",
               message="Initial capital set to 5000 — paper account reset.")
    assert recorder.reconcile()["errors"] == []
    record = store.get(opened.trade_id)
    assert (record["status"], record["outcome"], record["finalized"]) == ("CANCELLED", "CANCELLED", 1)
    assert record["exit_reason"] == "paper-reset"
    for field in ("actual_exit", "net_pnl", "realized_r"):
        assert record[field] is None, field
    assert [e["status"] for e in record["timeline"] if e["stage"] == "EXIT"] == ["NOT_REACHED"]


def test_a_lab_session_reset_ends_its_open_and_pending_records(tmp_path):
    """Restarting a lab wipes the broker's fills and orders. Its OPEN record
    and its PENDING order record used to stay open forever."""
    from services.journal_labs import PALabProjector
    pa, _now, _feed = _pa_lab(tmp_path, "LIVE_PAPER")
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    projector = PALabProjector(pa)
    projector.project(store)
    [pending] = store.query_trades(where="record_source='PA_LAB'", limit=5)
    assert pending["status"] == "PENDING"

    at = datetime.now(timezone.utc).isoformat()
    pa.process_quote("BTCUSDT", {"bid": 105.0, "ask": 105.1, "mark": 105.05, "received_at": at,
                                 "event_timestamp": at, "sequence": 1}, feed_reliable=True)
    projector.project(store)
    [opened] = store.query_trades(where="record_source='PA_LAB'", limit=5)
    assert opened["status"] == "OPEN" and pa.broker.positions()

    pa.reset()
    assert not pa.broker.positions()
    projector.project(store)
    record = store.get(opened["journal_record_id"])
    assert (record["status"], record["outcome"], record["finalized"]) == ("CANCELLED", "CANCELLED", 1)
    assert record["exit_reason"] == "lab-session-reset"
    assert record["net_pnl"] is None and record["actual_exit"] is None
    projector.project(store)             # nothing left to end; the new session's records stay
    assert store.unfinished(("PA_LAB",)) == []


def test_a_lab_session_reset_ends_an_unfilled_order_record(tmp_path):
    from services.journal_labs import PALabProjector
    pa, _now, _feed = _pa_lab(tmp_path, "LIVE_PAPER")
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    projector = PALabProjector(pa)
    projector.project(store)
    [pending] = store.query_trades(where="record_source='PA_LAB'", limit=5)
    assert pending["status"] == "PENDING"
    pa.reset()
    projector.project(store)
    record = store.get(pending["journal_record_id"])
    assert (record["status"], record["outcome"]) == ("CANCELLED", "CANCELLED")
    assert record["execution_status"] == "ORDER_REMOVED"


# ──────────────────────────── reporting ────────────────────────────
_n = iter(range(1, 10_000))


def _closed(store, **facts):
    """A finalized CLOSED record holding only the given facts (unknowns stay NULL)."""
    n = next(_n)
    rec = {"execution_key": f"GATE:{n}", "record_source": "INSTANCE",
           "record_origin": "FORWARD_PAPER", "verification": "VERIFIED", "status": "CLOSED",
           "data_completeness": "PARTIAL", "trade_id": f"gate-{n}", "symbol": "BTCUSDT",
           "side": "long", **facts}
    store.upsert_trade(rec)
    return store.get(rec["trade_id"])


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    monkeypatch.setattr(webhook_api, "trade_records", store)
    app = FastAPI()
    app.include_router(webhook_api.router)
    return store, TestClient(app, raise_server_exceptions=False)


def test_totals_never_present_unknown_values_as_known(api):
    """A loss with no recorded P&L used to read "No losses", a $0 drawdown
    and a profit factor over the trades that had one."""
    store, client = api
    _closed(store, outcome="WIN", net_pnl=10.0, realized_r=1.0,
            position_closed_at="2026-07-06T10:00:00+00:00")
    _closed(store, outcome="LOSS", net_pnl=None, realized_r=-1.0,
            position_closed_at="2026-07-06T11:00:00+00:00")
    k = client.get("/journal/records").json()["kpis"]
    assert (k["trades"], k["losses"], k["pnl_known"], k["r_known"]) == (2, 1, 1, 2)
    assert k["profit_factor"] is None
    assert k["profit_factor_note"] != "no losing trades"
    from services import journal_stats
    summary = journal_stats.summarize(store.query_trades())
    assert summary["max_drawdown"] is None            # a gap hides the true path
    assert summary["max_drawdown_r"] == pytest.approx(1.0)


def test_an_unknown_exit_leg_value_is_not_turned_into_zero():
    from services.journal_recorder import _result_fields
    out = _result_fields(entry=100.0, stop=95.0, side="long", risk_amount=5.0,
                         exit_reason="take-profit",
                         legs=[{"price": 110.0, "size": 1.0, "pnl": None, "fees": None}])
    assert out["net_pnl"] is None and out["fees"] is None and out["gross_pnl"] is None
    assert out["realized_r"] is None and out["outcome"] is None
    assert out["actual_exit"] == pytest.approx(110.0)


def test_strategy_compliance_is_unknown_when_no_rule_check_ran():
    """Any recorded setup used to make a trade COMPLIANT, even one whose
    quality gate listed blocks while it was switched off."""
    from services.journal_reviews import review_trade
    base = {"journal_record_id": "r1", "planned_stop_loss": 95.0, "realized_r": 1.0,
            "outcome": "WIN", "exit_reason": "take-profit"}
    unchecked = review_trade({**base, "setup": {"quality_blocks": ["score below minimum"]}})
    assert unchecked["strategy_compliance"] == "UNKNOWN"
    passed = review_trade({**base, "setup": {"conditions_failed": []}})
    assert passed["strategy_compliance"] == "COMPLIANT"
    failed = review_trade({**base, "setup": {"conditions_failed": ["opposing zone within 1R", None]}})
    assert failed["strategy_compliance"] == "VIOLATION"
    assert failed["rule_violations"][0]["detail"] == \
        "entered with failed conditions: opposing zone within 1R"


def test_a_decision_whose_order_never_filled_is_not_a_trade(tmp_path):
    """The forward intent's PENDING record carried the decision id, so the
    decision was shown as TRADE_OPENED / FILLED with no fill behind it."""
    from tests.test_journal_integrity import Env
    env = Env(tmp_path)
    did = env.decisions.record({
        "symbol": "BTCUSDT", "timeframe": "5m", "strategy": "3-Candle Rejection · EMA 9/33",
        "side": "long", "decision": "accepted", "reason": "limit entry parked",
        "instance_id": env.instance_id, "ts": "2026-10-07T08:00:00+00:00",
        "decision_identity": "inst-A:BTCUSDT:a1", "final_state": "PENDING_INTENT"})
    assert env.entry().accepted                       # parked, never filled
    env.recorder().reconcile()
    env.decisions.finalize(did, final_state="GATE_REJECTED", gate_stage="execution",
                           blocker="EXPIRED", reason="limit order expired unfilled")
    env.recorder().reconcile()
    [trade] = env.store.query_trades()
    assert trade["status"] == "PENDING"
    [decision] = env.store.query_decisions()
    assert decision["decision_type"] != "TRADE_OPENED" and decision["status"] != "FILLED"
    assert decision["journal_record_id"] is None
    assert decision["source_ref"]["order_record_id"] == trade["journal_record_id"]


def test_date_filters_are_london_days_and_bad_inputs_are_rejected(api):
    """The table shows London time, but the filter compared text against UTC
    stamps: a trade at 00:30 London on Monday 6 July fell under Sunday."""
    store, client = api
    for opened in ("2026-07-05T23:30:00+00:00",      # Mon 6 Jul 00:30 London
                   "2026-07-06T12:00:00+00:00",
                   "2026-07-06T22:59:59+00:00",      # Mon 23:59:59 London
                   "2026-07-06T23:30:00+00:00"):     # Tue 7 Jul 00:30 London
        _closed(store, outcome="WIN", net_pnl=1.0, position_opened_at=opened)
    got = client.get("/journal/records", params={"date_from": "2026-07-06",
                                                 "date_to": "2026-07-06"}).json()
    assert sorted(r["position_opened_at"] for r in got["records"]) == [
        "2026-07-05T23:30:00+00:00", "2026-07-06T12:00:00+00:00", "2026-07-06T22:59:59+00:00"]
    assert client.get("/journal/records", params={"date_to": "9999-12-31"}).status_code == 200
    assert client.get("/journal/records", params={"date_from": "0001-01-01"}).status_code == 200
    for params in ({"date_from": "not-a-date"}, {"date_to": "2026-13-45"}, {"origin": "garbage"}):
        assert client.get("/journal/records", params=params).status_code == 400, params
    assert client.get("/journal/records", params={"offset": "99999999999999999999"}).status_code == 200
    assert client.get("/journal/decision-records",
                      params={"offset": "99999999999999999999"}).status_code == 200


def test_decisions_default_to_forward_paper_and_name_other_origins(api):
    store, client = api
    for instance, origin in (("inst-live", "FORWARD_PAPER"), ("inst-replay", "SIMULATION")):
        store.upsert_decision({"decision_key": f"INSTANCE:decision:{instance}:1",
                               "record_source": "INSTANCE", "record_origin": origin,
                               "instance_id": instance, "symbol": "BTCUSDT",
                               "decided_at": "2026-07-06T10:00:00+00:00",
                               "decision_type": "QUALITY_BLOCKED"})
    default = client.get("/journal/decision-records").json()
    assert [d["instance_id"] for d in default["decisions"]] == ["inst-live"]
    assert default["by_type"] == {"QUALITY_BLOCKED": 1}
    assert client.get("/journal/decision-records", params={"origin": "all"}).json()["total"] == 2
    assert client.get("/journal/decision-records",
                      params={"origin": "SIMULATION"}).json()["total"] == 1


def test_nan_in_stored_evidence_reads_as_unknown(api):
    store, client = api
    record = _closed(store, outcome="WIN", net_pnl=1.0, evidence_json={"score": float("nan")})
    response = client.get(f"/journal/records/{record['journal_record_id']}")
    assert response.status_code == 200
    assert response.json()["evidence"] == {"score": None}


def test_a_lab_journal_pass_never_holds_the_broker_lock(tmp_path):
    """Fills and quotes take the broker lock. The pass read the whole broker
    history through it, holding it about 0.24 s at 10,000 trips every pass;
    it now reads a WAL snapshot on its own connection."""
    import threading
    from services.journal_labs import PALabProjector
    pa, _now, _feed = _pa_lab(tmp_path, "LIVE_PAPER")
    at = datetime.now(timezone.utc).isoformat()
    pa.process_quote("BTCUSDT", {"bid": 105.0, "ask": 105.1, "mark": 105.05, "received_at": at,
                                 "event_timestamp": at, "sequence": 1}, feed_reliable=True)
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    projector = PALabProjector(pa)
    held, done = threading.Event(), threading.Event()

    def hold():                          # a fill in progress owns the broker lock
        with pa.broker._lock:
            held.set()
            done.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    assert held.wait(5)
    import time
    started = time.monotonic()
    try:
        projector.project(store)         # used to wait here until the fill let go
        elapsed = time.monotonic() - started
    finally:
        done.set()
        worker.join(5)
    assert elapsed < 2, f"the pass waited {elapsed:.1f} s for the broker lock"
    [record] = store.query_trades(where="record_source='PA_LAB'", limit=5)
    assert record["status"] == "OPEN"


def test_an_ordinary_ledger_pass_does_not_read_the_bot_logs(tmp_path):
    """bot_logs has no index and grows with every cycle; the reset check reads
    it only when an OPEN record's trade is actually missing."""
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    paper = PaperExecutionEngine(ledger, 10_000)
    paper.open(symbol="BTCUSDT", side="BUY", size=1, entry=100, stop=95)
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    recorder = JournalRecorder(store)
    recorder.add_ledger(LedgerSource("MAIN", ledger))
    statements = []
    ledger._c.set_trace_callback(statements.append)
    try:
        recorder.reconcile()
        recorder.reconcile()
    finally:
        ledger._c.set_trace_callback(None)
    assert statements and not [s for s in statements if "bot_logs" in s]


def test_a_trade_imported_from_the_legacy_journal_first_is_not_rewritten_every_pass(tmp_path):
    """Production imported legacy journal rows while its Supabase ledger was not
    readable yet. Once the ledger mirror made the same trades projectable, the
    ledger projection wrote each one (reached by trade id, kept under its legacy
    key), then the legacy import wrote it back as UNVERIFIED legacy: every
    record twice per pass, about 20 row changes each, forever."""
    from services.journal_legacy import LegacyJournalMigration
    from tests.test_journal_integrity import Env
    env = Env(tmp_path)
    assert env.entry().accepted
    env.fill()
    assert env.close(102.0, reason="take-profit").accepted
    [trade] = env.ledger.get_paper_trades()
    legacy = JournalStore(str(tmp_path / "journal.db"))
    legacy.record_entry({"trade_id": trade["id"], "mode": "paper", "symbol": "BTCUSDT",
                         "side": "long", "strategy": "3-Candle Rejection", "timeframe": "5m",
                         "entry": 100, "stop": 99, "target": 102, "size": trade["size"],
                         "risk_amount": 1, "planned_rr": 2, "confidence": 80, "brain_score": 80,
                         "regime": "trend", "sections": {}, "instance_id": env.instance_id,
                         "execution_mode": "paper"})
    before_ledger = JournalRecorder(env.store)                 # the ledger was not readable yet
    before_ledger.legacy = LegacyJournalMigration(legacy)
    assert before_ledger.reconcile()["legacy"]["legacy_unverified"] == 1

    recorder = env.recorder()
    recorder.legacy = LegacyJournalMigration(legacy)
    first = recorder.reconcile()
    assert first["errors"] == [] and first["legacy"]["matched_to_ledger"] == 1
    for _ in range(2):
        changes = env.store._c.total_changes
        report = recorder.reconcile()
        assert report["ledgers"][0]["written"] == 0 and report["ledgers"][0]["skipped_final"] == 1
        assert env.store._c.total_changes - changes <= 1      # only the recorder's own status row
    [record] = env.store.query_trades()
    assert record["verification"] == "VERIFIED" and record["status"] == "CLOSED"
    assert record["net_pnl"] == pytest.approx(trade["pnl"])
