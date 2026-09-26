"""Journal integrity: one execution lifecycle is exactly one canonical record.

Every scenario drives the real execution components -- SignalPipeline with the
forward-paper engine and an instance-scoped ledger, PaperBrokerV2 inside the
real SMC and PA lab accounts, the SMC agent's execution intents -- and then
asks the recorder (services/journal_recorder.py) what it made of them. The
records are checked against the facts the execution layer wrote, never the
other way round.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from data.cycle_store import CycleStore
from data.decision_store import DecisionStore
from data.journal_store import JournalStore
from data.ledger import SqliteLedger
from data.trade_record_store import TradeRecordStore
from execution.paper_engine import ForwardPaperExecutionEngine
from services.controls import TradingControl
from services.decision_journal import DecisionJournal
from services.journal_labs import PALabProjector, SMCLabProjector
from services.journal_legacy import LegacyJournalMigration, evolution_provenance
from services.journal_recorder import JournalRecorder, LedgerSource
from services.journal_reviews import review_finalized, review_trade
from services.signal_pipeline import SignalPipeline
from services.trading_instances import InstanceLedger

T0 = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0)


def _at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


class Env:
    """One trading instance on a file-backed ledger, plus the recorder."""

    def __init__(self, tmp_path, instance_id="inst-A", session="sess-1", equity=1000.0):
        self.tmp = tmp_path
        self.ledger = SqliteLedger(str(tmp_path / "ledger.db"))
        self.instance_id, self.session = instance_id, session
        self.decisions = DecisionStore(str(tmp_path / "decisions.db"))
        self.cycles = CycleStore(str(tmp_path / "cycles.db"))
        self.store = TradeRecordStore(str(tmp_path / "trade_records.db"))
        self._build(equity)

    def _build(self, equity, intents=None):
        scoped = InstanceLedger(self.ledger, self.instance_id, self.session)
        self.paper = ForwardPaperExecutionEngine(scoped, equity, initial_intents=intents)
        self.pipe = SignalPipeline(scoped, self.paper, TradingControl(), equity=equity)
        self.pipe.journal_context = {
            "instance_id": self.instance_id, "simulation_session_id": self.session,
            "strategy_id": "three_candle_rejection", "strategy_name": "3-Candle Rejection · EMA 9/33",
            "strategy_version": "1.0.0", "execution_mode": "paper", "market_data_mode": "live",
            "exchange": "binance_usdm", "instrument_type": "perpetual"}

    def recorder(self, store=None) -> JournalRecorder:
        rec = JournalRecorder(store or self.store)
        rec.add_ledger(LedgerSource("MAIN", self.ledger, decision_store=self.decisions,
                                    cycle_store=self.cycles))
        return rec

    def entry(self, alert="a1", side="BUY", entry=100.0, stop=99.0, target=102.0, minute=0,
              symbol="BTCUSDT"):
        return self.pipe.process({
            "alert_id": alert, "symbol": symbol, "side": side, "entry": entry, "stop": stop,
            "target": target, "timestamp": _at(minute), "timeframe": "5m",
            "strategy": "3-Candle Rejection · EMA 9/33", "strategy_id": "three_candle_rejection",
            "regime": "Trending", "reason": "3-candle rejection at support",
            "journal_quality_gate": {"score": 80, "regime": "Trending", "htf_bias": "bullish",
                                     "setup_type": "rejection", "blocks": []},
            "journal_decision_id": 1, "decision_identity": f"{self.instance_id}:{symbol}:{alert}",
            "snapshot": {"support": 99.5, "ema9": 99.9, "ema33": 99.4}, "confidence": 0.8})

    def fill(self, bid=100.0, ask=100.0, at=None):
        # A real quote arrives after the order was parked, so it is stamped now.
        self.last_quote_at = at or datetime.now(timezone.utc).isoformat()
        return self.paper.process_quote({"bid": bid, "ask": ask, "mark": (bid + ask) / 2,
                                         "received_at": self.last_quote_at})

    def close(self, price, *, alert="c1", reason=None, minute=30, symbol="BTCUSDT"):
        payload = {"alert_id": alert, "symbol": symbol, "side": "CLOSE", "entry": price,
                   "stop": None, "timestamp": _at(minute)}
        if reason:
            payload.update({"exit_reason": reason, "mfe_r": 1.5, "mae_r": -0.4})
        return self.pipe.process(payload)

    def records(self, **kw):
        return self.store.query_trades(**kw)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _one(env) -> dict:
    rows = env.records()
    assert len(rows) == 1, [r["execution_key"] for r in rows]
    return env.store.get(rows[0]["journal_record_id"])


# ─────────────────────────── 1-3: outcomes ───────────────────────────
def test_01_winning_trade(env):
    assert env.entry().accepted
    env.fill()
    assert env.close(102.0, reason="take-profit").accepted
    env.recorder().reconcile()
    r = _one(env)
    assert (r["status"], r["outcome"], r["finalized"]) == ("CLOSED", "WIN", 1)
    assert r["record_source"] == "INSTANCE" and r["record_origin"] == "FORWARD_PAPER"
    assert r["realized_r"] > 0 and r["net_pnl"] > 0
    assert r["planned_entry"] == 100.0 and r["actual_entry"] == 100.0 and r["actual_exit"] == 102.0


def test_02_losing_trade(env):
    env.entry(); env.fill()
    env.close(99.0, reason="stop-loss")
    env.recorder().reconcile()
    r = _one(env)
    assert r["outcome"] == "LOSS" and r["realized_r"] == pytest.approx(-1.0, abs=0.05)
    assert r["exit_reason"] == "stop-loss"


def test_03_breakeven(env):
    env.entry(); env.fill()
    env.close(100.0, reason="stop-loss")
    env.recorder().reconcile()
    r = _one(env)
    assert r["outcome"] == "BREAKEVEN" and abs(r["realized_r"]) <= 0.05
    assert "BREAKEVEN" in r["source_ref"]["outcome_basis"]


# ─────────────────────────── 4-6: material decisions ───────────────────────────
def _decision(env, **kw):
    row = {"symbol": "BTCUSDT", "timeframe": "5m", "strategy": "3-Candle Rejection · EMA 9/33",
           "side": "long", "decision": "rejected", "reason": "", "instance_id": env.instance_id,
           "ts": _at(0), "decision_identity": kw.pop("identity"), **kw}
    return env.decisions.record(row)


def test_04_risk_blocked_decision_is_recorded_and_makes_no_trade(env):
    _decision(env, identity="d-risk", final_state="GATE_REJECTED", gate_stage="risk",
              blocker="GATE_REJECTED: MAX_OPEN_POSITIONS", reason="max open positions reached")
    env.recorder().reconcile()
    assert env.records() == []
    [d] = env.store.query_decisions()
    assert d["decision_type"] == "RISK_BLOCKED" and d["journal_record_id"] is None
    assert d["reason"] == "max open positions reached"


def test_05_stale_market_data_is_one_incident_not_a_row_per_candle(env):
    _decision(env, identity="d-stale", final_state="GATE_REJECTED", gate_stage="market_quality",
              blocker="GATE_REJECTED: DATA_STALE", reason="last candle is stale")
    for i in range(3):
        env.cycles.record({"symbol": "BTCUSDT", "timeframe": "5m", "decision": "SKIP",
                           "instance_id": env.instance_id, "ts": _at(i * 5),
                           "blocker": "GATE_REJECTED: DATA_STALE"})
    env.cycles.record({"symbol": "BTCUSDT", "timeframe": "5m", "decision": "WAIT",
                       "instance_id": env.instance_id, "ts": _at(20),
                       "blocker": "GATE_REJECTED: NO_SETUP"})
    env.recorder().reconcile()
    types = sorted(d["decision_type"] for d in env.store.query_decisions())
    assert types == ["STALE_DATA", "STALE_DATA"]           # the signal + one incident
    incident = next(d for d in env.store.query_decisions() if d["status"] == "INCIDENT")
    assert incident["evidence"]["candles"] == 3             # three candles, one record
    assert env.records() == []


def test_06_signals_only(env):
    _decision(env, identity="d-sig", final_state="SIGNALS_ONLY", gate_stage="operating_mode",
              reason="signals only")
    env.recorder().reconcile()
    [d] = env.store.query_decisions()
    assert d["decision_type"] == "SIGNALS_ONLY" and env.records() == []


# ─────────────────────────── 7-9: rejected / failed / uncertain ───────────────────────────
def test_07_rejected_order_makes_no_trade_record(tmp_path, monkeypatch):
    from execution.paper_engine import FillResult
    env = Env(tmp_path)
    # The engine's own affordability check refuses the order.
    monkeypatch.setattr(env.paper, "_reject_unaffordable", lambda symbol, side, size, entry: FillResult(
        "rejected", symbol, "long", size, entry, reason="GATE_REJECTED: INSUFFICIENT_PAPER_CAPITAL"))
    res = env.entry()
    assert not res.accepted and res.stage == "execution"
    _decision(env, identity="d-rej", final_state="GATE_REJECTED", gate_stage="execution",
              blocker="GATE_REJECTED: INSUFFICIENT_PAPER_CAPITAL", reason=res.reason)
    env.recorder().reconcile()
    assert env.records() == []
    [d] = env.store.query_decisions()
    assert d["decision_type"] == "ORDER_REJECTED"


def _smc(tmp_path):
    from services.smc_agent_journal import SMCAgentJournal
    from services.smc_strategy_lab import SMCPaperAccount
    account = SMCPaperAccount(str(tmp_path / "smc.db"))
    journal = SMCAgentJournal(str(tmp_path / "agent.db"))
    return account, journal


def test_08_execution_failure_is_a_visible_record(tmp_path):
    account, journal = _smc(tmp_path)
    session = account.session()["id"]
    journal.create_execution_intent(execution_key="ek-1", symbol="BTCUSDT", timeframe="5m",
                                    proposal_id="prop-1", session_id=session, decision_id="dec-1",
                                    payload={"direction": "long"})
    journal.transition_execution("ek-1", "EXECUTION_FAILED", error="broker rejected: margin")
    store = TradeRecordStore()
    SMCLabProjector(account, agent_journal=journal).project(store)
    [r] = store.query_trades()
    assert (r["status"], r["outcome"]) == ("EXECUTION_FAILED", "EXECUTION_FAILED")
    assert r["execution_key"] == f"SMC_LAB:{session}:prop-1" and r["agent_id"] == "smc_agent"


def test_09_execution_uncertainty_is_visible_and_not_a_result(tmp_path):
    account, journal = _smc(tmp_path)
    session = account.session()["id"]
    journal.create_execution_intent(execution_key="ek-2", symbol="BTCUSDT", timeframe="5m",
                                    proposal_id="prop-2", session_id=session)
    journal.transition_execution("ek-2", "EXECUTION_UNCERTAIN", error="timeout after submit")
    store = TradeRecordStore()
    SMCLabProjector(account, agent_journal=journal).project(store)
    [r] = store.query_trades()
    assert r["status"] == "EXECUTION_UNCERTAIN" and r["net_pnl"] is None
    assert r["finalized"] == 0                       # it can still resolve


# ─────────────────────────── 10-13: crash points ───────────────────────────
def test_10_crash_before_order_leaves_no_trade(env):
    # The pipeline claimed the idempotency key, then the process died.
    env.ledger.insert_webhook_event(alert_id="a-crash", symbol="BTCUSDT", side="BUY", entry=100,
                                    stop=99, payload={"timestamp": _at(0)}, status="claimed",
                                    instance_id=env.instance_id)
    env.recorder().reconcile()
    assert env.records() == []


def test_11_crash_after_order_then_fill_is_still_one_record(env):
    assert env.entry().accepted
    parked = env.paper.pending_intents()
    env.recorder().reconcile()
    [pending] = env.records()
    assert pending["status"] == "PENDING"
    env._build(1000.0, intents=parked)               # the process restarts
    env.fill()
    env.recorder().reconcile()
    r = _one(env)
    assert r["status"] == "OPEN" and r["journal_record_id"] == pending["journal_record_id"]


def test_12_crash_after_fill_before_journal_is_repaired(env):
    env.entry(); env.fill()                          # no recorder ran: the "crash"
    r_rec = env.recorder()
    r_rec.reconcile()
    r = _one(env)
    assert r["status"] == "OPEN" and r["bid"] == 100.0
    assert r["entry_filled_at"] == datetime.fromisoformat(env.last_quote_at).isoformat()


def test_13_journal_write_failure_is_repaired_by_the_next_pass(env, monkeypatch):
    env.entry(); env.fill(); env.close(102.0, reason="take-profit")
    rec = env.recorder()
    original = env.store.upsert_trade
    calls = {"n": 0}

    def failing(*a, **k):
        calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(env.store, "upsert_trade", failing)
    report = rec.reconcile()
    assert report["errors"] and env.records() == []
    monkeypatch.setattr(env.store, "upsert_trade", original)
    rec.reconcile()
    assert _one(env)["outcome"] == "WIN"


# ─────────────────────────── 14-16: reconciliation and duplicates ───────────────────────────
def test_14_reconciliation_is_idempotent(env):
    env.entry(); env.fill(); env.close(102.0, reason="take-profit")
    rec = env.recorder()
    rec.reconcile()
    before = _one(env)
    for _ in range(3):
        rec.reconcile()
    after = _one(env)
    assert after["facts_hash"] == before["facts_hash"] and after["updated_at"] == before["updated_at"]


def test_15_duplicate_decision_is_one_trade(env):
    assert env.entry(alert="dup").accepted
    second = env.entry(alert="dup")
    assert not second.accepted and second.stage == "dedup"
    env.fill()
    env.recorder().reconcile()
    assert _one(env)["status"] == "OPEN"


def test_16_duplicate_fill_callback_is_one_trade(env):
    env.entry()
    env.fill()
    env.fill(at=env.last_quote_at)                   # the same quote delivered again
    rec = env.recorder()
    rec.notify(); rec.notify()
    rec.reconcile(); rec.reconcile()
    assert len(env.ledger.get_paper_trades()) == 1
    assert _one(env)["status"] == "OPEN"


# ─────────────────────────── 17-19: exits ───────────────────────────
@pytest.mark.parametrize("price,reason,outcome", [(99.0, "stop-loss", "LOSS"),
                                                   (102.0, "take-profit", "WIN")])
def test_17_18_stop_and_target_exits_finalize_the_same_record(env, price, reason, outcome):
    env.entry(); env.fill()
    rec = env.recorder()
    rec.reconcile()
    opened = _one(env)
    env.close(price, reason=reason)
    rec.reconcile()
    closed = _one(env)
    assert closed["journal_record_id"] == opened["journal_record_id"]    # no second record
    assert (closed["exit_reason"], closed["outcome"]) == (reason, outcome)
    assert closed["mfe_r"] == 1.5 and closed["mae_r"] == -0.4


def test_19_manual_close(env):
    env.entry(); env.fill()
    env.close(101.0)                                 # a CLOSE with no reason: operator close
    env.recorder().reconcile()
    r = _one(env)
    assert r["exit_reason"] == "manual-close" and r["outcome"] == "WIN"


# ─────────────────────────── 20-22: sources ───────────────────────────
def _meta(account, table, **row):
    cols = ",".join(row)
    with account._lock:
        account._db.execute(f"INSERT INTO {table}({cols}) VALUES ({','.join('?' * len(row))})",
                            list(row.values()))


def _lab_trade(account, *, symbol="BTCUSDT", exit_bid=102.5):
    broker = account.broker
    order = broker.submit(symbol=symbol, side="buy", order_type="market", quantity=0.01,
                          protection_stop_loss=99.0, protection_take_profit=102.0,
                          signal_timestamp=_at(0), decision_timestamp=_at(0), signal_price=100.0,
                          requested_price=100.0, strategy="LAB_STRATEGY", strategy_version="1.0",
                          timeframe="5m", market_data_source="Binance USD-M public stream",
                          candle_id="c1")
    broker.process_tick(symbol, {"bid": 100.0, "ask": 100.02, "mark": 100.01,
                                 "received_at": _at(0.1), "sequence": 1})
    return order, lambda: broker.process_tick(symbol, {
        "bid": exit_bid, "ask": exit_bid + 0.02, "mark": exit_bid + 0.01,
        "received_at": _at(20), "sequence": 2})


def test_20_pa_lab_records_one_trade_per_lifecycle(tmp_path):
    from services.price_action_lab import PriceActionPaperAccount
    account = PriceActionPaperAccount(str(tmp_path / "pa.db"))
    session = account.session()["id"]
    order, exit_ = _lab_trade(account)
    _meta(account, "pa_order_meta", order_id=order["id"], session_id=session,
          proposal_id="pa-prop-1", setup_id="setup-1", zone_id="z1", direction="long",
          strategy_id="PA1_SR_REJECTION", config_json="{}", status="PLACED", reason="",
          created_at=_at(0), updated_at=_at(0), valid_until_index=5)
    store = TradeRecordStore()
    projector = PALabProjector(account)
    projector.project(store)
    [opened] = store.query_trades()
    assert opened["status"] == "OPEN" and opened["record_source"] == "PA_LAB"
    exit_()
    projector.project(store); projector.project(store)
    [closed] = store.query_trades()
    assert closed["journal_record_id"] == opened["journal_record_id"]
    assert closed["status"] == "CLOSED" and closed["exit_reason"] == "take-profit"
    assert closed["net_pnl"] == pytest.approx(
        sum(f["realized_pnl"] - f["fee"] for f in account.broker.fills()))


def test_21_smc_lab_merges_the_agent_into_the_same_record(tmp_path):
    account, journal = _smc(tmp_path)
    session = account.session()["id"]
    order, exit_ = _lab_trade(account, exit_bid=98.5)          # stopped out
    _meta(account, "smc_order_meta", order_id=order["id"], session_id=session,
          ownership="strategy", idempotency_key="ik1", proposal_id="smc-prop-1", setup_id="s1",
          model_id="SMC_M1_SWEEP_REVERSAL", model_version="1.0", direction="long", entry=100.0,
          stop=99.0, target_1=102.0, risk_pct=0.5, status="PLACED", reason="",
          config_json="{}", created_at=_at(0), updated_at=_at(0))
    _meta(account, "smc_candidates", proposal_id="smc-prop-1", session_id=session, setup_id="s1",
          model_id="SMC_M1_SWEEP_REVERSAL", status="PLACED", reason="entry ready",
          payload=json.dumps({"evaluation": {
              "state": "ENTRY_READY", "missing_conditions": [],
              "ordered_condition_results": [{"name": "liquidity_sweep", "passed": True},
                                            {"name": "choch", "passed": True}],
              "mtf_evidence": {"primary": {"bias": "bullish"}},
              "proposal": {"symbol": "BTCUSDT", "timeframe": "5m", "direction": "long",
                           "signal_timestamp": _at(0)}}}),
          created_at=_at(0), updated_at=_at(0))
    with journal._lock:
        journal._db.execute(
            "INSERT INTO agent_trades(id, decision_id, opened_at, symbol, timeframe, direction, "
            "entry, stop, target, planned_rr, size, proposal_id, why) VALUES "
            "('at1','dec-9',?, 'BTCUSDT','5m','long',100,99,102,2,0.01,'smc-prop-1','approved')",
            (_at(0),))
        journal._db.commit()
    exit_()
    store = TradeRecordStore()
    SMCLabProjector(account, agent_journal=journal).project(store)
    [r] = store.query_trades()
    full = store.get(r["journal_record_id"])
    assert full["agent_id"] == "smc_agent" and full["decision_id"] == "dec-9"
    assert full["exit_reason"] == "stop-loss" and full["outcome"] == "LOSS"
    assert full["setup"]["conditions_passed"] == ["liquidity_sweep", "choch"]
    assert full["htf_bias"] == "bullish"
    [d] = [d for d in store.query_decisions() if d["record_source"] == "SMC_LAB"]
    assert d["journal_record_id"] == r["journal_record_id"]     # the decision links to its trade


def test_22_trading_instance_record_carries_frozen_decision_evidence(env):
    env.entry(); env.fill(); env.close(102.0, reason="take-profit")
    env.recorder().reconcile()
    r = _one(env)
    assert r["instance_id"] == env.instance_id and r["session_id"] == env.session
    assert r["strategy_version"] == "1.0.0" and r["setup_type"] == "rejection"
    assert r["evidence"]["strategy_snapshot"] == {"support": 99.5, "ema9": 99.9, "ema33": 99.4}
    assert [e["status"] for e in r["timeline"]] == ["DONE"] * 10
    assert r["data_completeness"] == "FULL" and r["missing"] == []


# ─────────────────────────── 23: restart ───────────────────────────
def test_23_service_restart_creates_no_duplicates(env):
    env.entry(); env.fill(); env.close(102.0, reason="take-profit")
    env.recorder().reconcile()
    before = _one(env)
    restarted = TradeRecordStore(str(env.tmp / "trade_records.db"))       # a new process
    env.recorder(restarted).reconcile()
    rows = restarted.query_trades()
    assert len(rows) == 1 and rows[0]["facts_hash"] == before["facts_hash"]


# ─────────────────────────── 24: legacy migration ───────────────────────────
def test_24_legacy_migration_verifies_what_it_can_and_labels_the_rest(tmp_path):
    from execution.paper_engine import PaperExecutionEngine
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    old = JournalStore(str(tmp_path / "journal.db"))
    dj = DecisionJournal(old)
    paper = PaperExecutionEngine(ledger, 10_000)
    ids = []
    for alert, side, exit_price in (("L1", "buy", 102.0), ("L2", "sell", 51.0)):
        entry, stop = (100.0, 99.0) if side == "buy" else (50.0, 51.0)
        f = paper.open(symbol="BTCUSDT" if side == "buy" else "ETHUSDT", side=side, size=0.1,
                       entry=entry, stop=stop, target=None, alert_id=alert)
        dj.record_entry(trade_id=f.trade_id, mode="paper", symbol=f.symbol, side=f.side,
                        strategy="Decision Brain", timeframe="15m", entry=entry, stop=stop,
                        target=None, size=0.1, equity=10_000, confidence=0.7, brain_score=80,
                        regime="Trending", steps=[], payload={"timestamp": _at(0)})
        c = paper.close(symbol=f.symbol, exit_price=exit_price)
        dj.record_exit(trade_id=f.trade_id, exit_price=exit_price, pnl=c.pnl, exit_reason="x")
        ids.append(f.trade_id)
    # the second trade's ledger row is gone (a confirmed paper-account reset)
    with ledger._lock:
        ledger._c.execute("DELETE FROM paper_trades WHERE id=?", (ids[1],))
        ledger._c.commit()
    store = TradeRecordStore()
    rec = JournalRecorder(store)
    rec.add_ledger(LedgerSource("MAIN", ledger))
    rec.legacy = LegacyJournalMigration(old)
    rec.reconcile(); rec.reconcile()
    rows = {r["trade_id"]: r for r in store.query_trades()}
    assert len(rows) == 2
    # The ledger still has the first trade: its execution is VERIFIED. Nothing
    # recorded which market data it ran on, so it is not forward paper.
    assert rows[ids[0]]["verification"] == "VERIFIED" and rows[ids[0]]["record_source"] == "LEGACY_ENGINE"
    assert rows[ids[0]]["record_origin"] == "LEGACY_MIGRATION"
    assert "no decision-time evidence" in store.get(ids[0])["source_ref"]["origin_basis"]
    # The second trade's ledger row is gone: only the old journal row remains.
    assert rows[ids[1]]["record_origin"] == "LEGACY_MIGRATION"
    assert rows[ids[1]]["verification"] == "UNVERIFIED"
    assert rows[ids[1]]["planned_take_profit"] is None                   # never invented
    labels = {row["side"]: row["provenance"] for row in evolution_provenance(old, store)}
    assert labels == {"long": "VERIFIED", "short": "LEGACY"}
    # legacy never counts as forward-paper performance
    assert store.count_trades(where="record_origin='FORWARD_PAPER'") == 0
    # the old journal was only read
    assert old._c.execute("SELECT COUNT(*) FROM trade_decision_journal").fetchone()[0] == 2


# ─────────────────────────── facts vs interpretation ───────────────────────────
def test_finalized_facts_cannot_be_rewritten_even_by_raw_sql(env):
    env.entry(); env.fill(); env.close(102.0, reason="take-profit")
    env.recorder().reconcile()
    r = _one(env)
    with pytest.raises(sqlite3.IntegrityError):
        env.store._c.execute("UPDATE trade_records SET net_pnl=999 WHERE journal_record_id=?",
                             (r["journal_record_id"],))
    env.store._c.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        env.store._c.execute("DELETE FROM trade_records WHERE journal_record_id=?",
                             (r["journal_record_id"],))
    env.store._c.rollback()


def test_an_agent_review_never_changes_the_facts(env):
    env.entry(); env.fill(); env.close(99.0, reason="stop-loss")
    env.recorder().reconcile()
    before = _one(env)
    assert review_finalized(env.store) == 1
    assert review_finalized(env.store) == 0                          # once per version
    after = _one(env)
    assert after["facts_hash"] == before["facts_hash"]
    [review] = after["reviews"]
    assert review["risk_compliance"] == "COMPLIANT"
    assert "stop honoured" in " ".join(review["positive_behaviours"])


def test_a_loss_beyond_planned_risk_is_flagged_by_the_review_not_hidden():
    review = review_trade({"journal_record_id": "x", "realized_r": -1.6, "risk_amount": 10,
                           "planned_stop_loss": 99, "outcome": "LOSS", "exit_reason": "stop-loss"})
    assert review["risk_compliance"] == "VIOLATION"
    assert review["rule_violations"][0]["rule"] == "LOSS_WITHIN_PLANNED_RISK"


def test_simulation_and_research_never_enter_forward_paper_statistics(tmp_path):
    env = Env(tmp_path)
    env.pipe.journal_context["market_data_mode"] = "replay"
    env.entry(); env.fill(); env.close(102.0, reason="take-profit")
    env.recorder().reconcile()
    r = _one(env)
    assert r["record_origin"] == "SIMULATION"
    assert env.store.count_trades(where="record_origin='FORWARD_PAPER'") == 0


def test_instances_never_share_a_record(tmp_path):
    a = Env(tmp_path, instance_id="A")
    b = Env.__new__(Env)
    b.__dict__.update(a.__dict__)
    b.instance_id = "B"
    b._build(1000.0)
    # Autonomous order ids carry the instance; the same symbol and candle
    # traded by two instances are two executions.
    a.entry(alert="A:BTCUSDT:t0:buy"); a.fill()
    b.entry(alert="B:BTCUSDT:t0:buy"); b.fill()
    a.recorder().reconcile()
    rows = a.records()
    assert len(rows) == 2 and {r["instance_id"] for r in rows} == {"A", "B"}
