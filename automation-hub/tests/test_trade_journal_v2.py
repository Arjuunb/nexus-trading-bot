"""Canonical trade journal: recording, lifecycle, integrity and recovery.

Each test drives the real pipeline, paper execution engine and ledger — the
same path production uses — and asserts on the canonical journal record.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from data.journal_store import JournalStore
from data.ledger import SqliteLedger
from data.trade_journal_store import TradeJournalStore
from execution.paper_engine import ForwardPaperExecutionEngine, PaperExecutionEngine
from services.controls import TradingControl
from services.decision_journal import DecisionJournal
from services.fill_model import RealisticFill
from services.signal_pipeline import SignalPipeline
from services.trade_journal import (
    TradeJournalRecorder, classify_result, excursion_fields, risk_fields,
)
from services.trading_instances import InstanceLedger

FEE = 0.0004
CONTEXT = {
    "instance_id": None, "strategy_id": "smc", "strategy_name": "SMC Lab",
    "strategy_version": "1.4", "market_data_mode": "forward_paper", "execution_mode": "paper",
    "exchange": "binance_usdm", "instrument_type": "perpetual",
}


def _fill_model(**kw):
    params = dict(spread_pct=0.0, slippage_pct=0.0, latency_pct=0.0, taker_fee_pct=FEE)
    params.update(kw)
    return RealisticFill(**params)


def _rig(*, ledger=None, scope="", forward=False, store=None, fill_model=None, equity=10_000.0,
         context=None, legacy=True):
    ledger = ledger or SqliteLedger(":memory:")
    led = InstanceLedger(ledger, scope, "session-1") if scope else ledger
    engine_type = ForwardPaperExecutionEngine if forward else PaperExecutionEngine
    paper = engine_type(led, equity, fill_model=fill_model or _fill_model())
    pipe = SignalPipeline(led, paper, TradingControl(), equity=equity, risk_per_trade_pct=0.01,
                          exposure_limit_pct=0.5, max_total_exposure_pct=1.0,
                          adaptive_risk=False, equity_throttle=False)
    pipe.journal = DecisionJournal(JournalStore(":memory:")) if legacy else None
    store = store or TradeJournalStore(":memory:")
    recorder = TradeJournalRecorder(store, legacy_journal=pipe.journal)
    pipe.trade_journal = recorder
    paper.journal = recorder
    pipe.journal_context = dict(context or {**CONTEXT, "instance_id": scope or None})
    paper.journal_provenance = pipe.journal_context
    return pipe, paper, recorder, store, ledger


def _entry(pipe, *, alert="auto:legacy:BTCUSDT:5m:2026-10-05T08:40:00+00:00:buy", side="BUY",
           entry=100.0, stop=95.0, target=115.0, ts="2026-10-05T08:40:00+00:00", **extra):
    payload = {"alert_id": alert, "symbol": "BTCUSDT", "side": side, "entry": entry, "stop": stop,
               "target": target, "confidence": 1.0, "regime": "Trending", "strategy": "SMC Lab",
               "timeframe": "5m", "timestamp": ts, "mode": "paper",
               "reason": "Bullish HTF + liquidity sweep + demand POI + rejection",
               "brain_checklist": [{"name": "HTF bullish", "status": "Passed"},
                                   {"name": "Liquidity sweep", "status": "Passed"}],
               "snapshot": {"mtf_evidence": {"primary": {"htf_timeframe": "1h", "htf_bias": "BULLISH",
                                                         "htf_close_timestamp": "2026-10-05T08:00:00+00:00"}}}}
    payload.update(extra)
    return pipe.process(payload)


def _close(pipe, price, *, reason="take-profit", alert="close-1", **extra):
    return pipe.process({"alert_id": alert, "symbol": "BTCUSDT", "side": "CLOSE", "entry": price,
                         "exit_reason": reason, **extra})


def _only(store):
    rows = store.list_trades()
    assert len(rows) == 1, rows
    return rows[0]


# ------------------------------------------------------------- record creation
def test_trade_record_creation_captures_identity_entry_risk_and_timing():
    pipe, paper, _rec, store, _ = _rig(fill_model=_fill_model(slippage_pct=0.001))
    assert _entry(pipe).accepted
    t = _only(store)
    assert t["trade_ref"].startswith("TRD-2026-")
    assert t["status"] == "OPEN" and t["entry_locked"] is True
    assert (t["strategy_name"], t["strategy_version"], t["strategy_family"]) == ("SMC Lab", "1.4", "SMC")
    assert (t["symbol"], t["base_asset"], t["quote_asset"]) == ("BTCUSDT", "BTC", "USDT")
    assert t["direction"] == "LONG" and t["timeframe"] == "5m" and t["htf_timeframe"] == "1h"
    assert t["trading_mode"] == "FORWARD_PAPER" and t["trade_source"] == "AUTO_ENGINE"
    assert t["exchange"] == "Binance USD-M Futures" and t["market_type"] == "PERPETUAL_FUTURES"
    assert t["order_id"].startswith("auto:") and t["execution_id"] and t["position_id"]
    # entry: requested vs filled, slippage, quantity, notional, leverage, margin
    assert t["requested_entry_price"] == 100.0
    assert t["entry_price"] == pytest.approx(100.1)
    assert t["entry_slippage"] == pytest.approx(0.1)
    assert t["quantity"] > 0
    assert t["notional_value"] == pytest.approx(t["entry_price"] * t["quantity"])
    assert t["leverage"] == 1.0 and t["leverage_source"] == "UNLEVERAGED_CASH_MODEL"
    assert t["margin_used"] == pytest.approx(t["notional_value"])
    assert t["account_balance_before"] == 10_000.0 and t["account_equity_before"] == 10_000.0
    assert t["available_margin_before"] == 10_000.0
    # risk: stop/target, distances, amount, %, planned R, rule check
    assert (t["initial_stop"], t["initial_target"]) == (95.0, 115.0)
    assert t["risk_amount"] == pytest.approx(abs(t["entry_price"] - 95.0) * t["quantity"])
    assert t["risk_pct"] == pytest.approx(t["risk_amount"] / 10_000 * 100)
    assert t["planned_rr"] == pytest.approx((115.0 - t["entry_price"]) / (t["entry_price"] - 95.0))
    assert t["max_allowed_risk_pct"] == pytest.approx(2.0)
    assert t["risk_rule_status"] == "PASSED"
    # timing
    assert t["signal_at"] == "2026-10-05T08:40:00+00:00"
    assert t["entry_session"] in ("ASIA", "LONDON", "NEW_YORK", "LONDON_NY_OVERLAP", "OFF_HOURS")
    assert t["entry_at_london"] and t["entry_weekday"]
    # frozen decision snapshot
    snap = store.snapshot(t["trade_id"])
    assert snap["decision"] == "ENTER_LONG"
    assert "liquidity sweep" in snap["decision_reason"].lower()
    assert {c["name"] for c in snap["conditions_passed"]} >= {"HTF bullish", "Liquidity sweep"}
    assert snap["htf_bias"] == "BULLISH" and snap["strategy_family"] == "SMC"
    assert snap["setup"]["family"] == "SMC" and "liquidity_sweep" in snap["setup"]
    kinds = [e["kind"] for e in store.events(t["trade_id"])]
    assert kinds[:6] == ["setup-detected", "decision", "risk-check-passed", "order-submitted",
                         "order-filled", "trade-opened"]


def test_trade_closure_gross_net_fees_and_realised_r_match_the_ledger():
    pipe, paper, _rec, store, ledger = _rig()
    assert _entry(pipe).accepted
    assert _close(pipe, 113.0).accepted
    t = _only(store)
    q = t["quantity"]
    assert t["status"] == "CLOSED" and t["finalised_at"]
    assert t["exit_reason"] == "TAKE_PROFIT" and t["exit_price"] == 113.0
    assert t["gross_pnl"] == pytest.approx((113.0 - 100.0) * q)
    assert t["fees_total"] == pytest.approx(FEE * q * (100.0 + 113.0))
    assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["fees_total"])
    assert t["realised_r"] == pytest.approx(t["net_pnl"] / t["risk_amount"])
    assert t["gross_r"] == pytest.approx(t["gross_pnl"] / t["risk_amount"])
    assert t["pnl_pct"] == pytest.approx(t["net_pnl"] / 10_000 * 100)
    assert t["result"] == "WIN" and t["counts_in_stats"] is True
    ledger_net = sum(r["pnl"] for r in ledger.get_paper_trades() if r["status"] == "closed")
    assert t["net_pnl"] == pytest.approx(ledger_net)
    assert t["duration_s"] is not None and t["exit_at_london"]


def test_fee_handling_splits_round_trip_commission_into_entry_and_exit():
    pipe, _paper, _rec, store, _ = _rig()
    _entry(pipe)
    _close(pipe, 110.0)
    t = _only(store)
    fees = {f["fee_type"]: f for f in store.fees(t["trade_id"])}
    q = t["quantity"]
    assert fees["ENTRY_COMMISSION"]["amount"] == pytest.approx(FEE * q * 100.0)
    assert fees["EXIT_COMMISSION"]["amount"] == pytest.approx(FEE * q * 110.0)
    assert fees["ENTRY_COMMISSION"]["rate"] == FEE
    assert t["funding_total"] is None  # not modelled by this engine: unknown, not zero


def test_rr_and_risk_percentage_math():
    plan = risk_fields(direction="LONG", entry=124_550, stop=124_550 - 666.67, target=124_550 + 2000,
                       quantity=0.015, equity_before=1000, max_allowed_risk_pct=1.0, leverage=10)
    assert plan["notional_value"] == pytest.approx(1868.25)
    assert plan["margin_used"] == pytest.approx(186.825)
    assert plan["risk_amount"] == pytest.approx(10.0, rel=1e-3)
    assert plan["risk_pct"] == pytest.approx(1.0, rel=1e-3)
    assert plan["planned_rr"] == pytest.approx(3.0, rel=1e-3)
    assert plan["planned_reward"] == pytest.approx(30.0)
    assert plan["risk_rule_status"] == "PASSED"
    over = risk_fields(direction="SHORT", entry=100, stop=110, target=70, quantity=2,
                       equity_before=1000, max_allowed_risk_pct=1.0)
    assert over["risk_pct"] == pytest.approx(2.0) and over["risk_rule_status"] == "FAILED"
    no_stop = risk_fields(direction="LONG", entry=100, stop=None, target=None, quantity=1, equity_before=1000)
    assert "risk_amount" not in no_stop and no_stop["risk_rule_status"] == "FAILED"


def test_result_classification_including_break_even_and_partials():
    assert classify_result(net_pnl=30, risk_amount=10, notional=1000) == "WIN"
    assert classify_result(net_pnl=-10, risk_amount=10, notional=1000) == "LOSS"
    assert classify_result(net_pnl=-0.3, risk_amount=10, notional=1000) == "BREAK_EVEN"
    assert classify_result(net_pnl=5, risk_amount=10, notional=1000, leg_pnls=[15, -10]) == "PARTIAL_WIN"
    assert classify_result(net_pnl=-5, risk_amount=10, notional=1000, leg_pnls=[5, -10]) == "PARTIAL_LOSS"
    assert classify_result(net_pnl=None, risk_amount=10, notional=1000) is None


def test_break_even_trade_via_break_even_stop():
    pipe, paper, _rec, store, _ = _rig(fill_model=_fill_model(taker_fee_pct=0.0))
    _entry(pipe)
    paper.update_management("BTCUSDT", stop=100.0, target=115.0, management={"be": True})
    _close(pipe, 100.0, reason="stop")
    t = _only(store)
    assert t["result"] == "BREAK_EVEN"
    assert t["exit_reason"] == "BREAK_EVEN_STOP"
    mods = store.modifications(t["trade_id"])
    assert mods[0]["reason"] == "BREAK_EVEN" and (mods[0]["old_value"], mods[0]["new_value"]) == (95.0, 100.0)
    # the original plan is preserved alongside the change
    assert t["initial_stop"] == 95.0 and t["current_stop"] == 100.0


def test_protection_changes_are_recorded_only_when_levels_move():
    pipe, paper, _rec, store, _ = _rig()
    _entry(pipe)
    for _ in range(3):  # per-bar checkpoints with unchanged levels
        paper.update_management("BTCUSDT", stop=95.0, target=115.0, management={"be": False})
    assert store.modifications(_only(store)["trade_id"]) == []
    paper.update_stop("BTCUSDT", 97.0)
    paper.update_management("BTCUSDT", stop=97.0, target=120.0, management={"be": False})
    mods = store.modifications(_only(store)["trade_id"])
    assert [(m["field"], m["reason"]) for m in mods] == [("STOP_LOSS", "MANUAL"), ("TAKE_PROFIT", "STRATEGY")]
    assert _only(store)["initial_target"] == 115.0 and _only(store)["current_target"] == 120.0


def test_protection_changes_never_touch_a_lab_trade_on_the_same_symbol():
    pipe, paper, _rec, store, _ = _rig()
    lab_id, _ = store.create_trade({"source_system": "SMC_LAB", "source_trade_key": "lab-1", "symbol": "BTCUSDT",
                                    "direction": "LONG", "status": "OPEN", "lab_id": "SMC_LAB",
                                    "current_stop": 90.0, "trading_mode": "ISOLATED_FORWARD_PAPER",
                                    "entry_filled_at": "2099-01-01T00:00:00+00:00"})  # newest: sorts first
    _entry(pipe)
    paper.update_stop("BTCUSDT", 97.0)
    assert store.modifications(lab_id) == [] and store.get_trade(lab_id)["current_stop"] == 90.0
    engine_trade = next(t for t in store.list_trades() if t["source_system"] == "PIPELINE")
    assert store.modifications(engine_trade["trade_id"])[0]["new_value"] == 97.0


def test_excursions_in_price_currency_and_r():
    out = excursion_fields(direction="LONG", entry=100, quantity=2, risk_amount=10, mfe_price=112,
                           mae_price=97, source="TEST")
    assert out["mfe_amount"] == pytest.approx(24) and out["mfe_r"] == pytest.approx(2.4)
    assert out["mae_amount"] == pytest.approx(-6) and out["mae_r"] == pytest.approx(-0.6)
    pipe, _paper, _rec, store, _ = _rig()
    _entry(pipe)
    _close(pipe, 108.0, mfe_price=112.0, mae_price=98.0)
    t = _only(store)
    assert t["mfe_price"] == 112.0 and t["mae_price"] == 98.0
    assert t["mfe_r"] == pytest.approx(12 / 5) and t["mae_r"] == pytest.approx(-2 / 5)


# ------------------------------------------------------------- partials
def test_partial_exit_keeps_one_canonical_trade_and_closes_the_legacy_journal():
    pipe, paper, rec, store, ledger = _rig()
    _entry(pipe)
    reduced = paper.reduce(symbol="BTCUSDT", exit_price=110.0, fraction=0.5)
    assert reduced.action == "reduced"
    assert _only(store)["status"] == "PARTIALLY_CLOSED" and _only(store)["partial_exit_count"] == 1
    assert _close(pipe, 105.0, reason="stop").accepted
    t = _only(store)
    assert t["status"] == "CLOSED" and t["closed_quantity"] == pytest.approx(t["quantity"])
    ledger_rows = ledger.get_paper_trades()
    assert len(ledger_rows) == 2  # the ledger split the trade in two rows ...
    assert t["net_pnl"] == pytest.approx(sum(r["pnl"] for r in ledger_rows))  # ... the journal did not
    assert {l["ref"] for l in store.links(t["trade_id"]) if l["link_type"] == "LEDGER_TRADE"} == \
        {r["id"] for r in ledger_rows}
    assert t["exit_price"] == pytest.approx((110.0 + 105.0) / 2, rel=1e-6)
    legacy = pipe.journal.store.list()
    assert len(legacy) == 1 and legacy[0]["status"] == "closed"
    kinds = [e["kind"] for e in store.events(t["trade_id"])]
    assert kinds.count("partial-exit") == 1 and "exit-filled" in kinds


# ------------------------------------------------------------- operational outcomes
def test_failed_execution_is_operational_not_a_loss():
    pipe, paper, _rec, store, _ = _rig()

    def boom(**_kw):
        raise RuntimeError("venue unavailable")
    paper.open = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        _entry(pipe)
    t = _only(store)
    assert (t["status"], t["result"]) == ("FAILED", "EXECUTION_FAILED")
    assert t["is_operational"] is True and t["counts_in_stats"] is False
    assert "venue unavailable" in t["result_reason"]


def test_rejected_execution_is_operational_not_a_loss():
    pipe, paper, _rec, store, _ = _rig(fill_model=_fill_model(reject_prob=1.0))
    result = _entry(pipe)  # every gate passes; the execution model refuses the order
    assert not result.accepted
    t = _only(store)
    assert (t["status"], t["result"], t["is_operational"]) == ("REJECTED", "REJECTED", True)
    assert t["net_pnl"] is None and t["counts_in_stats"] is False


def test_stale_pending_order_becomes_execution_uncertain_and_can_still_fill():
    pipe, paper, rec, store, ledger = _rig(forward=True)
    result = _entry(pipe)
    assert result.accepted and result.reason.startswith("paper order intent")
    t = _only(store)
    assert t["status"] == "PENDING"
    summary = rec.reconcile_ledger(ledger, pending_uncertain_after_s=-1)
    assert summary["uncertain"] == 1
    t = _only(store)
    assert (t["status"], t["result"], t["is_operational"]) == ("UNCERTAIN", "EXECUTION_UNCERTAIN", True)
    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    paper.process_quote({"bid": 100.0, "ask": 100.2, "mark": 100.1, "received_at": later.isoformat()})
    t = _only(store)
    assert t["status"] == "OPEN" and t["result"] is None and t["is_operational"] is False


# ------------------------------------------------------------- forward paper (the old gap)
def test_forward_paper_fill_from_a_later_quote_is_journaled_with_its_decision():
    pipe, paper, _rec, store, _ = _rig(forward=True, scope="instance-1",
                                       context={**CONTEXT, "instance_id": "instance-1",
                                                "instance_name": "BTCUSDT SMC 5m #INSTAN"})
    assert _entry(pipe, ts=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()).accepted
    assert _only(store)["status"] == "PENDING"
    fill_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    fills = paper.process_quote({"bid": 100.0, "ask": 100.2, "mark": 100.1,
                                 "received_at": fill_at.isoformat()})
    assert len(fills) == 1
    t = _only(store)
    assert t["status"] == "OPEN" and t["instance_id"] == "instance-1"
    assert t["trade_source"] == "TRADING_INSTANCE" and t["trading_mode"] == "FORWARD_PAPER"
    assert t["entry_filled_at"] == fill_at.isoformat()
    assert t["requested_entry_price"] == 100.0 and t["entry_price"] == pytest.approx(100.2)
    assert store.snapshot(t["trade_id"])["decision"] == "ENTER_LONG"
    # the legacy decision journal (trade memory's source) now sees forward fills too
    ledger_trade_id = next(l["ref"] for l in store.links(t["trade_id"]) if l["link_type"] == "LEDGER_TRADE")
    legacy = pipe.journal.store.get(ledger_trade_id)
    assert legacy is not None and legacy["status"] == "open"
    assert _close(pipe, 110.0).accepted
    assert _only(store)["result"] == "WIN"


def test_one_executed_trade_creates_exactly_one_canonical_trade():
    pipe, paper, rec, store, ledger = _rig(forward=True, scope="instance-1")
    _entry(pipe, ts=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat())
    at = datetime.now(timezone.utc) + timedelta(seconds=1)
    quote = {"bid": 100.0, "ask": 100.2, "mark": 100.1, "received_at": at.isoformat()}
    paper.process_quote(quote)
    paper.process_quote({**quote, "received_at": (at + timedelta(seconds=1)).isoformat()})
    # the same entry hook delivered again (e.g. a retry) is ignored
    ledger_id = ledger.get_paper_trades()[0]["id"]
    rec.on_entry_fill({"ledger_trade_id": ledger_id, "side": "long", "price": 100.2, "quantity": 1})
    rec.reconcile_ledger(ledger, grace_s=0)
    rec.migrate_legacy(pipe.journal.store, ledger)
    assert len(store.list_trades()) == 1
    assert len([e for e in store.executions(_only(store)["trade_id"]) if e["kind"] == "ENTRY"]) == 1


def test_duplicate_execution_ids_are_recorded_once():
    store = TradeJournalStore(":memory:")
    trade_id, _ = store.create_trade({"source_system": "T", "source_trade_key": "k", "symbol": "BTCUSDT",
                                      "direction": "LONG", "status": "OPEN"})
    assert store.add_execution(trade_id, {"execution_id": "x1", "kind": "ENTRY", "quantity": 1, "price": 1})
    assert not store.add_execution(trade_id, {"execution_id": "x1", "kind": "ENTRY", "quantity": 1, "price": 1})
    assert len(store.executions(trade_id)) == 1


# ------------------------------------------------------------- restart and reconciliation
def test_restart_does_not_create_duplicate_journal_records(tmp_path):
    db = tmp_path / "journal.db"
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    pipe, _paper, _rec, store, _ = _rig(ledger=ledger, store=TradeJournalStore(str(db)))
    _entry(pipe)
    _close(pipe, 112.0)
    before = _only(store)
    # simulate a process restart: new store, new recorder, same files
    reopened = TradeJournalStore(str(db))
    recorder = TradeJournalRecorder(reopened)
    recorder.reconcile_ledger(ledger, grace_s=0)
    recorder.migrate_legacy(pipe.journal.store, ledger)
    recorder.reconcile_ledger(ledger, grace_s=0)
    rows = reopened.list_trades()
    assert len(rows) == 1 and rows[0]["trade_ref"] == before["trade_ref"]
    assert rows[0]["net_pnl"] == pytest.approx(before["net_pnl"])


def test_reconciliation_restores_missing_records_and_missing_closes_safely():
    ledger = SqliteLedger(":memory:")
    # 1) a trade the journal never saw (the journal was down)
    blind = PaperExecutionEngine(ledger, 10_000, fill_model=_fill_model())
    blind.open(symbol="ETHUSDT", side="BUY", size=2, entry=2000, stop=1950, target=2100,
               sizing_context={"equity_before_trade": 10_000})
    blind.close(symbol="ETHUSDT", exit_price=2080)
    # 2) a trade the journal opened but whose close it missed
    pipe, paper, rec, store, _ = _rig(ledger=ledger)
    _entry(pipe)
    paper.journal = None
    paper.close(symbol="BTCUSDT", exit_price=111.0)
    paper.journal = rec
    summary = rec.reconcile_ledger(ledger, grace_s=0)
    # ETH is restored and then closed; BTC only needed its missing close
    assert summary["created"] == 1 and summary["closed"] == 2
    rows = {t["symbol"]: t for t in store.list_trades()}
    eth = rows["ETHUSDT"]
    assert eth["data_completeness"] == "RECONCILED_FROM_LEDGER" and eth["status"] == "CLOSED"
    assert eth["entry_price"] == 2000 and eth["quantity"] == 2 and eth["initial_stop"] == 1950
    assert eth["net_pnl"] == pytest.approx(next(r["pnl"] for r in ledger.get_paper_trades()
                                                if r["symbol"] == "ETHUSDT"))
    assert eth["exit_reason"] == "UNKNOWN" and eth["exit_reason_source"] == "RECONCILED_FROM_LEDGER"
    assert store.snapshot(eth["trade_id"]) is None  # never reconstructed
    btc = rows["BTCUSDT"]
    assert btc["status"] == "CLOSED" and btc["exit_price"] == 111.0
    assert "reconciled-close" in [e["kind"] for e in store.events(btc["trade_id"])]
    # idempotent
    again = rec.reconcile_ledger(ledger, grace_s=0)
    assert again["created"] == 0 and again["closed"] == 0 and len(store.list_trades()) == 2


def test_reconciliation_links_a_partial_exit_remainder_to_its_parent():
    ledger = SqliteLedger(":memory:")
    blind = PaperExecutionEngine(ledger, 10_000, fill_model=_fill_model())
    blind.open(symbol="SOLUSDT", side="SELL", size=10, entry=150, stop=160, target=120)
    blind.reduce(symbol="SOLUSDT", exit_price=140, fraction=0.5)
    blind.close(symbol="SOLUSDT", exit_price=145)
    store = TradeJournalStore(":memory:")
    rec = TradeJournalRecorder(store)
    rec.reconcile_ledger(ledger, grace_s=0)
    t = _only(store)
    assert t["direction"] == "SHORT" and t["partial_exit_count"] == 1 and t["status"] == "CLOSED"
    assert t["net_pnl"] == pytest.approx(sum(r["pnl"] for r in ledger.get_paper_trades()))


# ------------------------------------------------------------- integrity
def test_journal_immutability_and_the_correction_audit_trail():
    pipe, _paper, _rec, store, _ = _rig()
    _entry(pipe)
    _close(pipe, 112.0)
    t = _only(store)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.update_trade(t["trade_id"], {"entry_price": 1.0})
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.update_trade(t["trade_id"], {"net_pnl": 1_000_000.0})
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.update_trade(t["trade_id"], {"symbol": "ETHUSDT"})
    with pytest.raises(sqlite3.IntegrityError):
        store._c.execute("DELETE FROM journal_trades")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._c.execute("UPDATE journal_snapshots SET decision='ENTER_SHORT'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._c.execute("DELETE FROM journal_events")
    with pytest.raises(sqlite3.IntegrityError, match="released"):
        store.update_trade(t["trade_id"], {"finalised_at": None})
    with pytest.raises(ValueError, match="reason"):
        store.correct(t["trade_id"], {"exit_price": 111.5}, reason="", actor="ops")
    out = store.correct(t["trade_id"], {"exit_price": 111.5}, reason="venue fill report", actor="ops")
    assert out["changed"] == ["exit_price"]
    corrections = store.corrections(t["trade_id"])
    assert corrections == [{"seq": 1, "field": "exit_price", "previous_value": 112.0, "new_value": 111.5,
                            "reason": "venue fill report", "actor": "ops",
                            "corrected_at": corrections[0]["corrected_at"]}]
    assert store.get_trade(t["trade_id"])["exit_price"] == 111.5
    assert "journal-corrected" in [e["kind"] for e in store.events(t["trade_id"])]


def test_agent_review_is_stored_separately_and_cannot_change_the_trade():
    pipe, _paper, rec, store, _ = _rig()
    _entry(pipe)
    _close(pipe, 98.0, reason="stop")
    t = _only(store)
    reviews = store.reviews(t["trade_id"])
    assert len(reviews) == 1 and reviews[0]["reviewer"] == "trade-review-agent"
    review = reviews[0]
    for key in ("setup_quality", "execution_quality", "risk_management", "outcome", "mistakes",
                "went_well", "went_wrong", "improvement", "rule_violations"):
        assert key in review
    before = store.get_trade(t["trade_id"])
    rec.review(t["trade_id"])
    assert len(store.reviews(t["trade_id"])) == 2
    after = store.get_trade(t["trade_id"])
    assert {k: v for k, v in before.items() if k != "updated_at"} == \
        {k: v for k, v in after.items() if k != "updated_at"}


def test_simulation_reset_cancels_open_trades_without_fabricating_a_fill():
    pipe, _paper, rec, store, _ = _rig(scope="instance-9")
    _entry(pipe)
    assert rec.cancel_open_for_instance("instance-9", reason="account restart") == 1
    t = _only(store)
    assert (t["status"], t["result"], t["exit_reason"]) == ("CANCELLED", "CANCELLED", "SIMULATION_RESET")
    assert t["exit_price"] is None and t["net_pnl"] is None and t["counts_in_stats"] is False


def test_manual_close_records_the_operator_exit_reason():
    pipe, paper, _rec, store, _ = _rig()
    _entry(pipe)
    paper.close(symbol="BTCUSDT", exit_price=104.0, exit_context={"exit_reason": "MANUAL_CLOSE"})
    t = _only(store)
    assert t["exit_reason"] == "MANUAL_CLOSE" and t["status"] == "CLOSED"


# ------------------------------------------------------------- migration
def test_legacy_migration_maps_records_without_inventing_missing_fields():
    legacy_store = JournalStore(":memory:")
    legacy = DecisionJournal(legacy_store)
    legacy.record_entry(trade_id="old-1", mode="paper", symbol="BTCUSDT", side="long", strategy="Brain",
                        timeframe="15m", entry=100, stop=95, target=110, size=2, equity=10_000,
                        confidence=0.7, brain_score=72, regime="Trending", steps=[],
                        payload={"reason": "legacy reason",
                                 "journal_execution": {"execution_mode": "paper",
                                                       "market_data_mode": "legacy_live"}})
    legacy.record_exit(trade_id="old-1", exit_price=108, pnl=16.0, exit_reason="take-profit")
    legacy_store.record_entry({"trade_id": "old-2", "mode": "live", "symbol": "ETHUSDT",
                               "side": "short", "strategy": "Unknown", "entry": 2000, "size": 1,
                               "sections": {}})
    store = TradeJournalStore(":memory:")
    rec = TradeJournalRecorder(store, review_on_close=False)
    assert rec.migrate_legacy(legacy_store)["migrated"] == 2
    assert rec.migrate_legacy(legacy_store)["migrated"] == 0  # once
    by_symbol = {t["symbol"]: t for t in store.list_trades()}
    old1 = by_symbol["BTCUSDT"]
    assert old1["trading_mode"] == "FORWARD_PAPER" and old1["result"] == "WIN"
    assert old1["exit_reason"] == "TAKE_PROFIT" and old1["net_pnl"] == pytest.approx(16.0)
    assert old1["leverage"] is None and old1["margin_used"] is None  # never invented
    assert store.snapshot(old1["trade_id"])["decision_reason"] == "legacy reason"
    old2 = by_symbol["ETHUSDT"]
    assert old2["trading_mode"] == "UNKNOWN"  # an unverified record is not assigned a mode
    assert old2["initial_stop"] is None and old2["risk_amount"] is None and old2["planned_rr"] is None
    assert old2["status"] == "OPEN" and old2["data_completeness"] == "MIGRATED_FROM_LEGACY_JOURNAL"


def test_missing_optional_data_stays_null_on_fill_only_records():
    ledger = SqliteLedger(":memory:")
    paper = PaperExecutionEngine(ledger, 10_000)
    store = TradeJournalStore(":memory:")
    paper.journal = TradeJournalRecorder(store)
    paper.open(symbol="XAUUSD", side="SELL", size=1, entry=2400, stop=None)
    t = _only(store)
    assert t["data_completeness"] == "FILL_ONLY_NO_DECISION_SNAPSHOT"
    assert (t["base_asset"], t["quote_asset"]) == ("XAU", "USD")
    assert t["initial_stop"] is None and t["risk_amount"] is None and t["planned_rr"] is None
    assert t["risk_rule_status"] == "FAILED"  # no stop: the risk was undefined
    assert store.snapshot(t["trade_id"]) is None
    assert "decision-not-captured" in [e["kind"] for e in store.events(t["trade_id"])]
