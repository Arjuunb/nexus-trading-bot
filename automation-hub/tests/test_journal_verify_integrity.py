"""Verification: financial truth, idempotent reconciliation, aggregation,
mode/strategy isolation, immutability and performance (sections 3-8, 12).

The lab scenarios drive the real PriceActionPaperAccount / SMCPaperAccount and
their PaperBrokerV2 ledgers; the instance scenario drives a real Trading
Instance worker. Every journal figure is checked against the ledger that
actually moved the paper balance, and the API is checked against the journal.
Expected aggregates are computed here from the dataset definition, never by
calling the analytics under test.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from data.journal_store import JournalStore
from data.ledger import SqliteLedger
from data.trade_journal_store import CORRECTABLE_FIELDS, TradeJournalStore
from services import journal_analytics
from services.journal_ingest import JournalSync, ingest_v2_lab
from services.journal_sessions import classify_session, timing_fields
from services.trade_journal import TradeJournalRecorder

TABLES = ("journal_trades", "journal_trade_links", "journal_executions", "journal_fees",
          "journal_modifications", "journal_snapshots", "journal_events", "journal_reviews",
          "journal_corrections", "journal_notes", "journal_weekly_reviews", "journal_sequences",
          "journal_meta")


# ------------------------------------------------------------------ helpers
def _dump(store: TradeJournalStore) -> dict:
    """Every row of every journal table, canonically ordered."""
    out = {}
    with store.lock:
        for table in TABLES:
            rows = [tuple(r) for r in store._c.execute(f"SELECT * FROM {table}")]
            out[table] = sorted(rows, key=repr)
    return out


def _digest(store: TradeJournalStore) -> str:
    return hashlib.sha256(json.dumps(_dump(store), default=str, sort_keys=True).encode()).hexdigest()


class _Trace:
    """Counts the SQL statements a store connection executes."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn, self.statements = conn, []

    def __enter__(self):
        self.conn.set_trace_callback(self.statements.append)
        return self

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)

    def selects(self) -> list[str]:
        return [s for s in self.statements if s.lstrip().upper().startswith("SELECT")]

    def writes(self) -> list[str]:
        return [s for s in self.statements
                if s.lstrip().upper().split(" ", 1)[0] in ("INSERT", "UPDATE", "DELETE", "REPLACE")]


@pytest.fixture()
def api(monkeypatch):
    from fastapi.testclient import TestClient

    import app as appmod
    import webhook_api as wa
    from config import settings

    def bind(store: TradeJournalStore):
        monkeypatch.setattr(wa, "trade_journal_store", store)
        monkeypatch.setattr(wa, "trade_journal", TradeJournalRecorder(store))
        return TestClient(appmod.app), {"x-webhook-secret": settings.admin_key}
    return bind


_RULES_ETH = {"tick_size": 0.01, "quantity_step": 0.001, "min_quantity": 0.001,
              "min_notional": 5.0, "max_quantity": 10000}
_RULES_BTC = {"tick_size": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
              "min_notional": 5.0, "max_quantity": 1000}


def _wall() -> datetime:
    """Quotes are stamped when they are received, as in production."""
    time.sleep(0.002)
    return datetime.now(timezone.utc)


def _quote(at: datetime, bid: float, ask: float, mark: float, seq: int) -> dict:
    return {"bid": bid, "ask": ask, "mark": mark, "received_at": at.isoformat(),
            "event_timestamp": at.isoformat(), "sequence": seq}


def _pa_lab_trade(path, *, symbol="ETHUSDT", leverage=5):
    """A Price Action Lab long: placed by the lab's own placement path, filled
    from a post-decision quote, charged one funding event, closed at the
    broker-armed take profit."""
    from services.price_action_lab import PaperExecutionConfig, PriceActionPaperAccount
    acct = PriceActionPaperAccount(str(path))
    acct.start(mode="LIVE_PAPER", symbol=symbol, timeframe="5m")
    acct.set_leverage(leverage)
    now = datetime.now(timezone.utc)
    decided = now - timedelta(seconds=60)
    proposal = {"id": "prop-1", "setup_id": "setup-1", "strategy_id": "PA1_SR_REJECTION",
                "direction": "bullish", "entry": 2500.0, "stop": 2480.0, "target": 2550.0,
                "valid_until_index": 999, "_decision_timestamp": decided.isoformat(),
                "created_at": decided.isoformat(), "symbol": symbol, "entry_model": "break_of_trigger",
                "trigger_low": 2490.0, "trigger_high": 2500.0}
    setup = {"id": "setup-1", "zone_id": "zone-1", "phase": "ORDER_PENDING", "strategy_id": "PA1_SR_REJECTION",
             "direction": "bullish", "reasons": ["support zone touched", "bullish rejection"],
             "missing_conditions": [], "pattern_metadata": [{"pattern": "pin_bar"}],
             "context_snapshot": {"structure_state": "bullish",
                                  "zone": {"role": "support", "low": 2485, "high": 2495, "flipped": False},
                                  "trigger_event": {"event_type": "rejection", "level": 2490}}}
    placed = acct._place_proposal(proposal, setup, _RULES_ETH, PaperExecutionConfig(), source="automatic",
                                  correlation_id="corr-1", idempotency_key="idem-1")
    assert placed["accepted"], placed
    acct.process_quote(symbol, _quote(_wall(), 2500.5, 2501.0, 2500.8, 1), feed_reliable=True)
    armed = dict(acct.broker.positions()[0])
    funding = acct.apply_funding_once(symbol=symbol, funding_time="2026-10-05T16:00:00+00:00",
                                      rate=0.0001, mark_price=2510.0)
    assert funding["applied"]
    tp = float(armed["take_profit"])
    acct.process_quote(symbol, _quote(_wall(), tp + 1, tp + 1.5, tp + 1.2, 2), feed_reliable=True)
    assert acct.broker.positions() == []
    return acct, armed, placed["order"]


def _smc_lab_trade(path, *, symbol="BTCUSDT", leverage=3):
    """An SMC Lab short: entry, the lab's own T1 scale-out, then the
    protective stop on the remainder."""
    from services.smc_strategy_lab import SMCPaperAccount
    acct = SMCPaperAccount(str(path))
    acct.start(mode="LIVE_PAPER", symbol=symbol, timeframe="5m")
    acct.set_leverage(leverage)
    now = datetime.now(timezone.utc)
    decided = now - timedelta(seconds=60)
    out = acct.submit_order(symbol=symbol, side="sell", order_type="market", rules=_RULES_BTC,
                            reference_price=62000.0, risk_pct=0.5, stop_loss=62400.0, target_1=61400.0,
                            target_2=60800.0, idempotency_key="smc-idem-1", ownership="strategy",
                            proposal_id="smc-prop-1", setup_id="smc-setup-1", poi_id="poi-1",
                            model_id="SMC_M1_SWEEP_REVERSAL", creation_candle=decided.isoformat(),
                            decision_timestamp=decided.isoformat(), correlation_id="smc-corr-1")
    assert out["accepted"], out
    acct.process_quote(symbol, _quote(_wall(), 61990.0, 62000.0, 61995.0, 1), feed_reliable=True)
    armed = dict(acct.broker.positions()[0])
    t1 = [o for o in acct.broker.orders() if o["reduce_only"]][0]["limit_price"]
    acct.process_quote(symbol, _quote(_wall(), t1 - 5, t1, t1 - 2, 2), feed_reliable=True)
    acct.process_quote(symbol, _quote(_wall(), 62405.0, 62410.0, 62407.0, 3), feed_reliable=True)
    assert acct.broker.positions() == []
    return acct, armed, out["order"]


def _ledger_truth(acct) -> dict:
    """The broker's stored account row (``account()`` rounds to 8 dp for display)."""
    account = dict(acct.broker._account_row())
    return {"net": account["balance"] - account["starting_balance"], "fees": account["fees_paid"],
            "funding": account["funding_paid"], "gross": account["realized_pnl"]}


def _api_matches_journal(client, auth, trade: dict, mode: str) -> dict:
    """The list row, the detail and the summary must carry the journal's
    values unchanged."""
    keys = ("trade_ref", "status", "direction", "symbol", "entry_price", "exit_price", "quantity",
            "leverage", "margin_used", "notional_value", "initial_stop", "initial_target", "risk_amount",
            "risk_pct", "planned_rr", "realised_r", "gross_pnl", "fees_total", "funding_total", "net_pnl",
            "result", "exit_reason", "entry_session", "entry_filled_at", "exit_at", "duration_s",
            "trading_mode", "strategy_name", "partial_exit_count")
    listed = client.get(f"/journal/v2/trades?modes={mode}", headers=auth).json()
    assert listed["modes_applied"] == [mode] and listed["mixed_modes"] is False
    row = next(r for r in listed["trades"] if r["trade_id"] == trade["trade_id"])
    assert {k: row[k] for k in keys} == {k: trade[k] for k in keys}
    detail = client.get(f"/journal/v2/trades/{trade['trade_ref']}", headers=auth).json()
    assert {k: detail["trade"][k] for k in keys} == {k: trade[k] for k in keys}
    summary = client.get(f"/journal/v2/summary?modes={mode}", headers=auth).json()
    return {"row": row, "detail": detail, "summary": summary}


# ============================================================ SECTION 4 — financial truth
def test_fin_price_action_lab_trade_ledger_journal_api_agree(tmp_path, api):
    acct, armed, order = _pa_lab_trade(tmp_path / "pa.db")
    store = TradeJournalStore(":memory:")
    sync = JournalSync(TradeJournalRecorder(store), labs=[acct])
    result = sync.run_once()
    assert result["lab:PriceActionPaperAccount"] == {"created": 1, "exits_added": 1, "finalised": 1}
    (trade,) = store.list_trades(modes=["ALL"])
    ledger = _ledger_truth(acct)
    fills = acct.journal_export()["fills"]
    entry_fill, exit_fill = fills
    # ---- ledger == journal
    assert trade["trading_mode"] == "ISOLATED_FORWARD_PAPER" and trade["lab_id"] == "PRICE_ACTION_LAB"
    assert trade["net_pnl"] == pytest.approx(ledger["net"], abs=1e-9)
    assert trade["gross_pnl"] == pytest.approx(ledger["gross"], abs=1e-9)
    assert trade["fees_total"] == pytest.approx(ledger["fees"], abs=1e-9)
    assert trade["funding_total"] == pytest.approx(ledger["funding"], abs=1e-12)
    assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - trade["fees_total"] - trade["funding_total"],
                                             abs=1e-9)
    assert trade["entry_price"] == entry_fill["price"] and trade["exit_price"] == exit_fill["price"]
    assert trade["quantity"] == entry_fill["quantity"] == order["quantity"]
    # ---- the stop and target the broker actually armed on the position
    assert trade["initial_stop"] == armed["stop_loss"] == order["protection_stop_loss"]
    assert trade["initial_target"] == armed["take_profit"]
    assert armed["take_profit"] != order["protection_take_profit"]   # re-anchored from the real fill
    risk = (trade["entry_price"] - armed["stop_loss"]) * trade["quantity"]
    assert trade["risk_amount"] == pytest.approx(risk, rel=1e-12)
    assert trade["planned_rr"] == pytest.approx(
        (armed["take_profit"] - trade["entry_price"]) / (trade["entry_price"] - armed["stop_loss"]), rel=1e-12)
    assert trade["planned_rr"] == pytest.approx(order["protection_target_r"], abs=1e-3)
    assert trade["realised_r"] == pytest.approx(trade["net_pnl"] / risk, rel=1e-12)
    assert trade["leverage"] == 5.0
    assert trade["margin_used"] == pytest.approx(trade["entry_price"] * trade["quantity"] / 5.0, rel=1e-12)
    assert (trade["result"], trade["exit_reason"]) == ("WIN", "TAKE_PROFIT")
    assert trade["entry_session"] == classify_session(trade["entry_filled_at"])
    # ---- each broker fill is one execution with the same price, size and fee
    execs = {e["source_ref"]: e for e in store.executions(trade["trade_id"])}
    for fill in fills:
        e = execs[fill["id"]]
        assert (e["price"], e["quantity"], e["fee"]) == (fill["price"], fill["quantity"], fill["fee"])
    fee_rows = store.fees(trade["trade_id"])
    assert sorted(f["fee_type"] for f in fee_rows) == ["ENTRY_COMMISSION", "EXIT_COMMISSION", "FUNDING"]
    assert all(f["rate"] is not None for f in fee_rows)
    # ---- the pre-fill plan is kept in the frozen snapshot, not lost
    risk_snapshot = store.snapshot(trade["trade_id"])["risk"]
    assert risk_snapshot["pre_fill_target"] == order["protection_take_profit"]
    assert risk_snapshot["armed_target"] == armed["take_profit"]
    # ---- API == journal
    client, auth = api(store)
    out = _api_matches_journal(client, auth, trade, "ISOLATED_FORWARD_PAPER")
    assert out["summary"]["net_pnl"] == pytest.approx(ledger["net"], abs=1e-8)
    assert out["summary"]["total_fees"] == pytest.approx(ledger["fees"], abs=1e-8)
    assert out["summary"]["metrics"]["total_funding"] == pytest.approx(ledger["funding"], abs=1e-8)
    default = client.get("/journal/v2/trades", headers=auth).json()   # no forward paper: busiest mode
    assert default["modes_applied"] == ["ISOLATED_FORWARD_PAPER"] and default["total"] == 1
    print(f"EVIDENCE PA ledger={ledger} journal_net={trade['net_pnl']} api_net={out['row']['net_pnl']} "
          f"summary_net={out['summary']['net_pnl']} target={trade['initial_target']} "
          f"planned_rr={trade['planned_rr']} realised_r={trade['realised_r']}")


def test_fin_smc_lab_trade_ledger_journal_api_agree(tmp_path, api):
    acct, armed, order = _smc_lab_trade(tmp_path / "smc.db")
    store = TradeJournalStore(":memory:")
    sync = JournalSync(TradeJournalRecorder(store), labs=[acct])
    assert sync.run_once()["lab:SMCPaperAccount"] == {"created": 1, "exits_added": 2, "finalised": 1}
    (trade,) = store.list_trades(modes=["ALL"])
    ledger = _ledger_truth(acct)
    fills = acct.journal_export()["fills"]
    assert len(fills) == 3
    assert trade["net_pnl"] == pytest.approx(ledger["net"], abs=1e-9)
    assert trade["gross_pnl"] == pytest.approx(ledger["gross"], abs=1e-9)
    assert trade["fees_total"] == pytest.approx(ledger["fees"], abs=1e-8)
    assert ledger["funding"] == 0 and trade["funding_total"] is None   # no funding event: unknown, not 0
    assert trade["direction"] == "SHORT" and trade["leverage"] == 3.0
    assert trade["initial_stop"] == armed["stop_loss"] and trade["initial_target"] == armed["take_profit"]
    assert trade["planned_rr"] == pytest.approx(order["protection_target_r"], abs=1e-3)
    exit_qty = fills[1]["quantity"] + fills[2]["quantity"]
    assert trade["exit_price"] == pytest.approx(
        (fills[1]["price"] * fills[1]["quantity"] + fills[2]["price"] * fills[2]["quantity"]) / exit_qty, rel=1e-12)
    assert trade["partial_exit_count"] == 1
    assert (trade["result"], trade["exit_reason"]) == ("PARTIAL_WIN", "STOP_LOSS")
    events = [e["detail"] for e in store.events(trade["trade_id"]) if e["kind"] in ("partial-exit", "exit-filled")]
    assert events[0].startswith("partial take profit") and events[1].startswith("stop loss")
    # the lab's own account view (its metrics() net counts only candidate-driven
    # trades, which this direct order is not; fees come from the same ledger)
    lab_metrics = acct.metrics()
    assert lab_metrics["fees_paid"] == pytest.approx(trade["fees_total"], abs=1e-8)
    client, auth = api(store)
    out = _api_matches_journal(client, auth, trade, "ISOLATED_FORWARD_PAPER")
    assert out["summary"]["net_pnl"] == pytest.approx(ledger["net"], abs=1e-8)
    print(f"EVIDENCE SMC ledger={ledger} lab_fees={lab_metrics['fees_paid']} journal_net={trade['net_pnl']} "
          f"api_net={out['row']['net_pnl']} target={trade['initial_target']} planned_rr={trade['planned_rr']}")


def test_fin_trading_instance_trade_ledger_journal_api_agree(tmp_path, monkeypatch, api):
    from tests.test_journal_verify_lifecycles import _instance_entry, _instance_rig
    rig, manager, instance, engine = _instance_rig(tmp_path, monkeypatch)
    try:
        _instance_entry(rig, instance, engine)
        with engine._cycle_lock:
            from tests.test_journal_verify_lifecycles import _bar
            assert engine._check_exit("BTCUSDT", _bar(1, 101.0, 115.5, 100.5, 115.2)) is True
        (trade,) = rig.store.list_trades(modes=["ALL"])
        rows = rig.ledger.get_paper_trades(instance_id=instance.id)
        assert len(rows) == 1
        row = rows[0]
        assert trade["net_pnl"] == pytest.approx(float(row["pnl"]), abs=1e-9)
        assert trade["fees_total"] == pytest.approx(float(row["fees"]), abs=1e-12)
        assert trade["entry_price"] == pytest.approx(float(row["entry"]), rel=1e-12)
        assert trade["exit_price"] == pytest.approx(float(row["exit"]), rel=1e-12)
        assert trade["quantity"] == pytest.approx(float(row["size"]), rel=1e-12)
        metrics = manager.metrics(instance.id)
        assert metrics["fees"] == pytest.approx(trade["fees_total"], abs=1e-8)
        realized = metrics.get("realized_pnl", metrics.get("net_pnl"))
        assert realized == round(trade["net_pnl"], 2)            # the instance card shows cents
        client, auth = api(rig.store)
        out = _api_matches_journal(client, auth, trade, "FORWARD_PAPER")
        assert out["summary"]["net_pnl"] == pytest.approx(float(row["pnl"]), abs=1e-8)
        scoped = client.get(f"/journal/v2/summary?modes=FORWARD_PAPER&instance_id={instance.id}",
                            headers=auth).json()
        assert scoped["net_pnl"] == out["summary"]["net_pnl"]
        print(f"EVIDENCE INSTANCE ledger_pnl={row['pnl']} instance_metrics={realized} "
              f"journal_net={trade['net_pnl']} api_net={out['row']['net_pnl']}")
    finally:
        manager.shutdown()


# ============================================================ SECTION 3 — reconciliation
def _engine_world(tmp_path):
    """A ledger with closed, partially-closed and open engine trades, a
    legacy decision journal, and the canonical journal — all on disk."""
    from tests.test_journal_verify_lifecycles import _close, _rig, _signal
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    legacy = JournalStore(str(tmp_path / "legacy.db"))
    store = TradeJournalStore(str(tmp_path / "journal.db"))
    closed = _rig(ledger=ledger, store=store, legacy_store=legacy, scope="inst-a")
    assert _signal(closed).accepted and _close(closed, 112.0, reason="take-profit").accepted
    partial = _rig(ledger=ledger, store=store, legacy_store=legacy, scope="inst-b")
    assert _signal(partial, symbol="ETHUSDT").accepted
    assert partial.paper.reduce(symbol="ETHUSDT", exit_price=104.0, fraction=0.5).action == "reduced"
    still_open = _rig(ledger=ledger, store=store, legacy_store=legacy, scope="inst-c")
    assert _signal(still_open, symbol="SOLUSDT").accepted
    return ledger, legacy, store


def test_reconcile_restart_is_idempotent_with_zero_mutations(tmp_path):
    ledger, legacy, store = _engine_world(tmp_path)
    pa, _armed, _order = _pa_lab_trade(tmp_path / "pa.db")
    smc, _armed2, _order2 = _smc_lab_trade(tmp_path / "smc.db")
    # simulate a restart: a fresh store object on the same file
    store._c.close()
    store = TradeJournalStore(str(tmp_path / "journal.db"))
    sync = JournalSync(TradeJournalRecorder(store), legacy_store=legacy, ledger=ledger, labs=[pa, smc])
    first = sync.run_once()                          # boot: labs are ingested once
    assert first["lab:PriceActionPaperAccount"]["created"] == 1
    assert first["lab:SMCPaperAccount"]["created"] == 1
    assert first["ledger_reconciliation"]["created"] == 0
    assert first["legacy_migration"]["migrated"] == 0
    baseline = _dump(store)
    trades_before = {t["trade_id"]: t for t in store.list_trades(modes=["ALL"])}
    for label, kwargs in (("boot", {}), ("timer", {"include_legacy": False}), ("boot-again", {})):
        changes = store._c.total_changes
        with _Trace(store._c) as trace:
            result = sync.run_once(**kwargs)
        assert store._c.total_changes == changes, label           # ZERO rows written
        assert trace.writes() == [], label
        assert _dump(store) == baseline, label
        assert result["ledger_reconciliation"] == {"created": 0, "linked_remainders": 0, "closed": 0,
                                                   "uncertain": 0, "skipped_recent": 0}
        assert result["lab:PriceActionPaperAccount"] == {"created": 0, "exits_added": 0, "finalised": 0}
        assert result["lab:SMCPaperAccount"] == {"created": 0, "exits_added": 0, "finalised": 0}
        if "legacy_migration" in result:
            assert result["legacy_migration"]["migrated"] == 0
    # a second restart on top: still the same records, same TRD refs
    store._c.close()
    again = TradeJournalStore(str(tmp_path / "journal.db"))
    JournalSync(TradeJournalRecorder(again), legacy_store=legacy, ledger=ledger, labs=[pa, smc]).run_once()
    assert _dump(again) == baseline
    assert {t["trade_id"]: t["trade_ref"] for t in again.list_trades(modes=["ALL"])} == \
        {k: v["trade_ref"] for k, v in trades_before.items()}
    print(f"EVIDENCE RECONCILE trades={len(trades_before)} digest={_digest(again)[:16]} "
          f"rows={ {t: len(r) for t, r in baseline.items()} }")


# ============================================================ SECTION 5 — aggregation and filters
_DATASET = [
    # strategy, symbol, direction, mode, entry (UTC), net, risk, planned_rr, lev, exit_reason
    ("SMC Lab", "BTCUSDT", "LONG", "FORWARD_PAPER", "2026-10-05T02:15:00+00:00", 30.0, 10.0, 3.0, 1.0, "TAKE_PROFIT"),
    ("SMC Lab", "BTCUSDT", "SHORT", "FORWARD_PAPER", "2026-10-05T08:30:00+00:00", -10.0, 10.0, 2.0, 1.0, "STOP_LOSS"),
    ("SMC Lab", "ETHUSDT", "LONG", "FORWARD_PAPER", "2026-10-05T13:00:00+00:00", 0.2, 10.0, 2.5, 3.0, "BREAK_EVEN_STOP"),
    ("SMC Lab", "ETHUSDT", "SHORT", "FORWARD_PAPER", "2026-10-06T16:45:00+00:00", 25.0, 10.0, 2.5, 3.0, "TAKE_PROFIT"),
    ("Price Action Lab", "BTCUSDT", "LONG", "FORWARD_PAPER", "2026-10-05T09:10:00+00:00", -12.0, 12.0, 2.0, 5.0, "STOP_LOSS"),
    ("Price Action Lab", "BTCUSDT", "LONG", "FORWARD_PAPER", "2026-10-06T12:40:00+00:00", 36.0, 12.0, 3.0, 5.0, "TAKE_PROFIT"),
    ("Price Action Lab", "SOLUSDT", "SHORT", "FORWARD_PAPER", "2026-10-06T22:30:00+00:00", -6.0, 12.0, 1.5, 2.0, "MANUAL_CLOSE"),
    ("Price Action Lab", "SOLUSDT", "SHORT", "FORWARD_PAPER", "2026-10-07T03:00:00+00:00", 18.0, 12.0, 1.5, 2.0, "TAKE_PROFIT"),
    ("Decision Brain", "ETHUSDT", "LONG", "FORWARD_PAPER", "2026-10-07T14:00:00+00:00", -8.0, 8.0, 2.0, 1.0, "STOP_LOSS"),
    ("Decision Brain", "ETHUSDT", "LONG", "FORWARD_PAPER", "2026-10-07T19:00:00+00:00", 16.0, 8.0, 2.0, 1.0, "TRAILING_STOP"),
    # other modes: must never appear in the forward-paper figures
    ("SMC Lab", "BTCUSDT", "LONG", "BACKTEST", "2026-10-05T10:00:00+00:00", 999.0, 10.0, 3.0, 1.0, "TAKE_PROFIT"),
    ("SMC Lab", "BTCUSDT", "LONG", "SIMULATION", "2026-10-05T10:00:00+00:00", 500.0, 10.0, 3.0, 1.0, "TAKE_PROFIT"),
    ("Price Action Lab", "BTCUSDT", "LONG", "ISOLATED_FORWARD_PAPER", "2026-10-05T10:00:00+00:00", -77.0, 11.0, 2.0, 5.0, "STOP_LOSS"),
    ("Price Action Lab", "BTCUSDT", "LONG", "LIVE", "2026-10-05T10:00:00+00:00", 4.0, 11.0, 2.0, 5.0, "TAKE_PROFIT"),
]


def _seed(store: TradeJournalStore) -> list[dict]:
    rows = []
    for i, (strategy, symbol, direction, mode, entry, net, risk, rr, lev, reason) in enumerate(_DATASET):
        r = net / risk
        result = "BREAK_EVEN" if abs(r) <= 0.05 else ("WIN" if net > 0 else "LOSS")
        exit_at = (datetime.fromisoformat(entry) + timedelta(minutes=45 + i)).isoformat()
        fields = {
            "source_system": "VERIFY", "source_trade_key": f"v{i}", "strategy_name": strategy,
            "strategy_id": strategy.lower().replace(" ", "_"), "strategy_family": strategy.split()[0].upper(),
            "symbol": symbol, "direction": direction, "trading_mode": mode, "status": "CLOSED",
            "entry_filled_at": entry, "exit_at": exit_at, "net_pnl": net, "gross_pnl": net + 0.5,
            "fees_total": 0.5, "risk_amount": risk, "realised_r": r, "planned_rr": rr, "leverage": lev,
            "result": result, "counts_in_stats": 1, "exit_reason": reason, "entry_locked": 1,
            "finalised_at": exit_at, "timeframe": "5m", "trade_source": "VERIFY",
            **timing_fields(entry, exit_at),
        }
        trade_id, _ = store.create_trade(fields)
        rows.append({**fields, "trade_id": trade_id, "session": classify_session(entry)})
    return rows


def _expect(rows: list[dict]) -> dict:
    """Independent arithmetic over the dataset definition."""
    nets = [r["net_pnl"] for r in rows]
    wins = [r for r in rows if r["result"] == "WIN"]
    losses = [r for r in rows if r["result"] == "LOSS"]
    be = [r for r in rows if r["result"] == "BREAK_EVEN"]
    gp, gl = sum(n for n in nets if n > 0), -sum(n for n in nets if n < 0)
    decided = len(wins) + len(losses) + len(be)
    equity = peak = dd = 0.0
    for r in sorted(rows, key=lambda r: r["exit_at"]):
        equity += r["net_pnl"]
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    return {"total_trades": len(rows), "wins": len(wins), "losses": len(losses), "break_even": len(be),
            "net_pnl": round(sum(nets), 8), "gross_profit": round(gp, 8), "gross_loss": round(gl, 8),
            "win_rate": round(len(wins) / decided * 100, 2) if decided else None,
            # documented: no losing trade -> "INF" (or None when nothing was won either)
            "profit_factor": round(gp / gl, 4) if gl else ("INF" if gp else None),
            "avg_r": round(sum(r["realised_r"] for r in rows) / len(rows), 4) if rows else None,
            "expectancy": round(sum(nets) / len(rows), 8) if rows else None,
            "max_drawdown": round(dd, 8)}


def _check(got: dict, rows: list[dict]) -> None:
    got = got.get("metrics", got)
    want = _expect(rows)
    for key, value in want.items():
        assert got[key] == (pytest.approx(value) if isinstance(value, float) else value), key


def test_aggregates_and_composed_filters_match_independent_arithmetic(api):
    store = TradeJournalStore(":memory:")
    rows = _seed(store)
    client, auth = api(store)
    fp = [r for r in rows if r["trading_mode"] == "FORWARD_PAPER"]
    _check(client.get("/journal/v2/summary", headers=auth).json(), fp)          # default: forward paper only
    # per strategy
    perf = client.get("/journal/v2/performance/strategies?modes=FORWARD_PAPER", headers=auth).json()
    by_label = {row["label"]: row for row in perf["rows"]}
    for name in ("SMC Lab", "Price Action Lab", "Decision Brain"):
        _check(by_label[name], [r for r in fp if r["strategy_name"] == name])
    # per session (classified independently by zoneinfo in _seed)
    sessions = client.get("/journal/v2/performance/sessions?modes=FORWARD_PAPER", headers=auth).json()
    session_rows = {row["key"]: row for row in sessions["sessions"]}
    for key in {r["session"] for r in fp}:
        _check(session_rows[key], [r for r in fp if r["session"] == key])
    # per symbol and per direction
    symbols = client.get("/journal/v2/performance/symbols?modes=FORWARD_PAPER", headers=auth).json()
    for row in symbols["overall"]:
        _check(row, [r for r in fp if r["symbol"] == row["key"]])
    directions = client.get("/journal/v2/performance/directions?modes=FORWARD_PAPER", headers=auth).json()
    for side in ("LONG", "SHORT"):
        subset = [r for r in fp if r["direction"] == side]
        got, want = directions["overall"][side], _expect(subset)
        assert (got["trades"], got["wins"], got["losses"]) == (want["total_trades"], want["wins"], want["losses"])
        assert got["net_pnl"] == pytest.approx(want["net_pnl"]) and got["avg_r"] == pytest.approx(want["avg_r"])
        assert got["win_rate"] == pytest.approx(want["win_rate"])
    # composed filters: every combination must equal the same predicate in Python
    cases = [
        ({"strategy": "SMC Lab", "symbol": "ETHUSDT"},
         lambda r: r["strategy_name"] == "SMC Lab" and r["symbol"] == "ETHUSDT"),
        ({"strategy": "Price Action Lab", "direction": "SHORT", "result": "WINS"},
         lambda r: r["strategy_name"] == "Price Action Lab" and r["direction"] == "SHORT" and r["net_pnl"] > 0),
        ({"session": "LONDON_NY_OVERLAP", "leverage_min": "2"},
         lambda r: r["session"] == "LONDON_NY_OVERLAP" and r["leverage"] >= 2),
        ({"date_from": "2026-10-06", "date_to": "2026-10-06"},
         lambda r: r["entry_filled_at"].startswith("2026-10-06")),
        ({"rr_min": "2", "rr_max": "2.5", "pnl_min": "0"},
         lambda r: 2 <= r["planned_rr"] <= 2.5 and r["net_pnl"] >= 0),
        ({"realised_r_max": "-1", "exit_reason": "STOP_LOSS"},
         lambda r: r["realised_r"] <= -1 and r["exit_reason"] == "STOP_LOSS"),
        ({"symbol": "btcusdt", "result": "LOSSES", "modes": "FORWARD_PAPER,ISOLATED_FORWARD_PAPER"},
         lambda r: r["symbol"] == "BTCUSDT" and r["net_pnl"] < 0
         and r["trading_mode"] in ("FORWARD_PAPER", "ISOLATED_FORWARD_PAPER")),
    ]
    for params, predicate in cases:
        modes_given = "modes" in params
        expected = [r for r in rows if predicate(r) and (modes_given or r["trading_mode"] == "FORWARD_PAPER")]
        query = "&".join(f"{k}={v}" for k, v in params.items())
        listed = client.get(f"/journal/v2/trades?{query}&limit=1000", headers=auth).json()
        assert {t["trade_id"] for t in listed["trades"]} == {r["trade_id"] for r in expected}, params
        assert listed["total"] == len(expected), params
        if expected:
            _check(client.get(f"/journal/v2/summary?{query}", headers=auth).json(), expected)
    # metrics() itself, without HTTP, over the same rows
    _check(journal_analytics.metrics(store.list_trades(modes=["FORWARD_PAPER"])), fp)
    print(f"EVIDENCE AGG forward_paper={_expect(fp)}")


# ============================================================ SECTION 6 — mode isolation
def test_modes_are_never_mixed_unless_explicitly_requested(api):
    store = TradeJournalStore(":memory:")
    rows = _seed(store)
    client, auth = api(store)
    by_mode: dict[str, list] = {}
    for r in rows:
        by_mode.setdefault(r["trading_mode"], []).append(r)
    for mode in ("BACKTEST", "SIMULATION", "FORWARD_PAPER", "ISOLATED_FORWARD_PAPER", "LIVE"):
        listed = client.get(f"/journal/v2/trades?modes={mode}&limit=1000", headers=auth).json()
        assert listed["modes_applied"] == [mode] and listed["mixed_modes"] is False and not listed["mode_warning"]
        assert {t["trading_mode"] for t in listed["trades"]} == {mode}
        assert listed["total"] == len(by_mode[mode])
        summary = client.get(f"/journal/v2/summary?modes={mode}", headers=auth).json()
        assert summary["net_pnl"] == pytest.approx(sum(r["net_pnl"] for r in by_mode[mode]))
        for endpoint in ("analytics", "performance/strategies", "performance/sessions", "weekly-review?week=2026-W41"):
            sep = "&" if "?" in endpoint else "?"
            body = client.get(f"/journal/v2/{endpoint}{sep}modes={mode}", headers=auth).json()
            assert body.get("modes_applied", [mode]) == [mode], endpoint
    # default = ONE mode; backtest never wins the default even when it is the biggest
    default = client.get("/journal/v2/trades?limit=1000", headers=auth).json()
    assert default["modes_applied"] == ["FORWARD_PAPER"] and default["mixed_modes"] is False
    for i in range(30):
        store.create_trade({"source_system": "VERIFY", "source_trade_key": f"bt{i}", "trading_mode": "BACKTEST",
                            "status": "CLOSED", "symbol": "BTCUSDT", "direction": "LONG", "net_pnl": 1.0,
                            "result": "WIN", "counts_in_stats": 1})
    assert client.get("/journal/v2/trades", headers=auth).json()["modes_applied"] == ["FORWARD_PAPER"]
    meta = client.get("/journal/v2/meta", headers=auth).json()
    assert meta["default_modes"] == ["FORWARD_PAPER"]
    # explicit mixing is labelled
    mixed = client.get("/journal/v2/summary?modes=FORWARD_PAPER,LIVE", headers=auth).json()
    assert mixed["mixed_modes"] is True and mixed["mode_warning"]
    assert mixed["net_pnl"] == pytest.approx(sum(r["net_pnl"] for r in by_mode["FORWARD_PAPER"] + by_mode["LIVE"]))
    everything = client.get("/journal/v2/trades?modes=ALL&limit=1000", headers=auth).json()
    assert everything["mixed_modes"] is True and everything["total"] == len(rows) + 30
    # with no forward paper at all, the default is the busiest NON-backtest mode
    alt = TradeJournalStore(":memory:")
    for i, mode in enumerate(["BACKTEST"] * 5 + ["SIMULATION"] * 2 + ["ISOLATED_FORWARD_PAPER"] * 3):
        alt.create_trade({"source_system": "VERIFY", "source_trade_key": f"m{i}", "trading_mode": mode,
                          "status": "CLOSED", "symbol": "BTCUSDT", "direction": "LONG"})
    client3, auth3 = api(alt)
    assert client3.get("/journal/v2/trades", headers=auth3).json()["modes_applied"] == ["ISOLATED_FORWARD_PAPER"]
    assert client3.get("/journal/v2/trades?modes=BOGUS", headers=auth3).status_code == 400


# ============================================================ SECTION 7 — strategy isolation
def test_two_labs_and_the_engine_on_one_symbol_stay_separate(tmp_path, api):
    """PA Lab and SMC Lab both trade BTCUSDT while an engine trade is open on
    BTCUSDT: each lands in its own record, stop changes on the engine trade
    never touch a lab record, and per-strategy figures never bleed."""
    from tests.test_journal_verify_lifecycles import _rig, _signal
    pa, _a, _o = _pa_lab_trade(tmp_path / "pa.db", symbol="BTCUSDT")
    smc, _b, _p = _smc_lab_trade(tmp_path / "smc.db", symbol="BTCUSDT")
    store = TradeJournalStore(":memory:")
    rig = _rig(store=store)
    assert _signal(rig, entry=62000.0, stop=61000.0, target=65000.0).accepted
    recorder = rig.rec
    for acct in (pa, smc):
        ingest_v2_lab(recorder, acct.journal_export())
    lab_before = {t["trade_id"]: t for t in store.list_trades(modes=["ALL"]) if t["lab_id"]}
    assert {t["lab_id"] for t in lab_before.values()} == {"PRICE_ACTION_LAB", "SMC_LAB"}
    rig.paper.update_stop("BTCUSDT", 61500.0)              # operator moves the ENGINE trade's stop
    engine_trade = next(t for t in store.list_trades(modes=["ALL"]) if not t["lab_id"])
    assert engine_trade["current_stop"] == 61500.0
    assert [m["new_value"] for m in store.modifications(engine_trade["trade_id"])] == [61500.0]
    for trade_id, before in lab_before.items():
        after = store.get_trade(trade_id)
        assert store.modifications(trade_id) == []
        assert {k: after[k] for k in after if k != "updated_at"} == {k: before[k] for k in before if k != "updated_at"}
    client, auth = api(store)
    for lab, acct in (("PRICE_ACTION_LAB", pa), ("SMC_LAB", smc)):
        listed = client.get(f"/journal/v2/trades?modes=ALL&lab={lab}", headers=auth).json()
        assert listed["total"] == 1
        truth = _ledger_truth(acct)["net"]
        assert listed["trades"][0]["net_pnl"] == pytest.approx(truth, abs=1e-9)
        summary = client.get(f"/journal/v2/summary?modes=ALL&lab={lab}", headers=auth).json()
        assert summary["net_pnl"] == pytest.approx(truth, abs=1e-8)
    comparison = client.get("/journal/v2/performance/comparison?modes=ISOLATED_FORWARD_PAPER", headers=auth).json()
    assert {r["strategy"]: r["net_pnl"] for r in comparison["rows"]} == {
        "Price Action Lab": pytest.approx(_ledger_truth(pa)["net"], abs=1e-8),
        "SMC Lab": pytest.approx(_ledger_truth(smc)["net"], abs=1e-8)}


# ============================================================ SECTION 8 — immutability
def _closed_trade(store) -> str:
    trade_id, _ = store.create_trade({
        "source_system": "VERIFY", "source_trade_key": "imm", "trading_mode": "FORWARD_PAPER",
        "symbol": "BTCUSDT", "direction": "LONG", "status": "CLOSED", "strategy_name": "SMC Lab",
        "entry_price": 100.0, "quantity": 2.0, "initial_stop": 95.0, "initial_target": 115.0,
        "entry_filled_at": "2026-10-05T08:30:00+00:00", "exit_price": 110.0, "exit_at": "2026-10-05T10:00:00+00:00",
        "gross_pnl": 20.0, "fees_total": 0.5, "net_pnl": 19.5, "realised_r": 1.95, "result": "WIN",
        "exit_reason": "TAKE_PROFIT", "counts_in_stats": 1, "entry_locked": 1,
        "finalised_at": "2026-10-05T10:00:01+00:00"}, links=[("ORDER", "imm-order")])
    store.add_execution(trade_id, {"execution_id": "imm-entry", "kind": "ENTRY", "side": "BUY", "quantity": 2.0,
                                   "price": 100.0, "executed_at": "2026-10-05T08:30:00+00:00"})
    store.add_fee(trade_id, fee_type="ENTRY_COMMISSION", amount=0.25, source_ref="imm-entry")
    return trade_id


def test_facts_are_immutable_and_corrections_are_audited(api):
    store = TradeJournalStore(":memory:")
    trade_id = _closed_trade(store)
    frozen = store.get_trade(trade_id)
    # direct writes to identity / entry / exit facts are refused by the database itself
    for sql, args in (
            ("UPDATE journal_trades SET symbol='ETHUSDT' WHERE trade_id=?", (trade_id,)),
            ("UPDATE journal_trades SET entry_price=101 WHERE trade_id=?", (trade_id,)),
            ("UPDATE journal_trades SET initial_stop=90 WHERE trade_id=?", (trade_id,)),
            ("UPDATE journal_trades SET net_pnl=999 WHERE trade_id=?", (trade_id,)),
            ("UPDATE journal_trades SET exit_reason='STOP_LOSS' WHERE trade_id=?", (trade_id,)),
            ("UPDATE journal_trades SET entry_locked=0 WHERE trade_id=?", (trade_id,)),
            ("UPDATE journal_trades SET finalised_at=NULL WHERE trade_id=?", (trade_id,)),
            ("DELETE FROM journal_trades WHERE trade_id=?", (trade_id,)),
            ("UPDATE journal_executions SET price=1 WHERE trade_id=?", (trade_id,)),
            ("DELETE FROM journal_executions WHERE trade_id=?", (trade_id,)),
            ("UPDATE journal_fees SET amount=0 WHERE trade_id=?", (trade_id,)),
            ("DELETE FROM journal_trade_links WHERE trade_id=?", (trade_id,)),
            ("UPDATE journal_trade_links SET trade_id='other' WHERE trade_id=?", (trade_id,))):
        with pytest.raises(sqlite3.DatabaseError):
            with store.lock:
                store._c.execute(sql, args)
        store._c.rollback()
    with pytest.raises(sqlite3.DatabaseError):
        store.update_trade(trade_id, {"net_pnl": 1.0})
    assert store.get_trade(trade_id) == frozen
    # a correction needs a reason and an actor, and only correctable fields
    with pytest.raises(ValueError):
        store.correct(trade_id, {"net_pnl": 19.0}, reason="", actor="ops")
    with pytest.raises(ValueError):
        store.correct(trade_id, {"net_pnl": 19.0}, reason="fee rebate", actor="")
    assert "trade_id" not in CORRECTABLE_FIELDS and "trade_ref" not in CORRECTABLE_FIELDS
    with pytest.raises(ValueError):
        store.correct(trade_id, {"trade_ref": "TRD-X"}, reason="x", actor="ops")
    before = datetime.now(timezone.utc).isoformat()
    store.correct(trade_id, {"net_pnl": 19.75, "fees_total": 0.25}, reason="exchange fee rebate", actor="ops")
    after = store.get_trade(trade_id)
    assert (after["net_pnl"], after["fees_total"], after["correction_seq"]) == (19.75, 0.25, 1)
    rows = {c["field"]: c for c in store.corrections(trade_id)}
    assert set(rows) == {"net_pnl", "fees_total"}
    for field, old, new in (("net_pnl", 19.5, 19.75), ("fees_total", 0.5, 0.25)):
        c = rows[field]
        assert (c["previous_value"], c["new_value"]) == (old, new)
        assert c["reason"] == "exchange fee rebate" and c["actor"] == "ops" and c["seq"] == 1
        assert c["corrected_at"] >= before
    # the correction itself is append-only
    with pytest.raises(sqlite3.DatabaseError):
        with store.lock:
            store._c.execute("UPDATE journal_corrections SET reason='x'")
    store._c.rollback()
    # reviews, notes and weekly reviews never touch a fact
    facts = {k: v for k, v in store.get_trade(trade_id).items() if k != "updated_at"}
    client, auth = api(store)
    ref = after["trade_ref"]
    assert client.post(f"/journal/v2/trades/{ref}/notes", json={"note": "late entry"}, headers=auth).status_code == 200
    assert client.post(f"/journal/v2/trades/{ref}/reviews", headers=auth).status_code == 200
    assert client.post(f"/journal/v2/trades/{ref}/reviews/external",
                       json={"reviewer": "coach", "summary": "ok", "net_pnl": 1e9, "result": "LOSS",
                             "entry_price": 1.0}, headers=auth).status_code == 200
    assert client.post("/journal/v2/weekly-review?week=2026-W41", headers=auth).status_code == 200
    assert {k: v for k, v in store.get_trade(trade_id).items() if k != "updated_at"} == facts
    assert len(store.reviews(trade_id)) == 2 and len(store.notes(trade_id)) == 1
    print(f"EVIDENCE IMMUTABLE corrections={[(c['field'], c['previous_value'], c['new_value'], c['reason'], c['actor'], c['corrected_at']) for c in store.corrections(trade_id)]}")


# ============================================================ SECTION 12 — performance
def _bulk_lab_export(n: int) -> dict:
    """A PA-shaped export with n closed round trips (entry + exit fill)."""
    fills, orders, meta = [], {}, {}
    t0 = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
    for i in range(n):
        at = t0 + timedelta(minutes=10 * i)
        oid = f"o{i}"
        orders[oid] = {"id": oid, "status": "filled", "created_at": at.isoformat(), "requested_price": 100.0,
                       "protection_stop_loss": 95.0, "protection_take_profit": 110.0,
                       "protection_target_r": 2.0, "protection_tick_size": 0.01}
        meta[oid] = {"order_id": oid, "session_id": "s1", "strategy_id": "PA1", "direction": "bullish",
                     "config": {"leverage": 2, "account_equity_before": 10000.0}}
        fills.append({"id": f"f{i}a", "order_id": oid, "symbol": "BTCUSDT", "side": "buy", "quantity": 1.0,
                      "price": 100.0, "fee": 0.04, "realized_pnl": 0.0, "fill_timestamp": at.isoformat()})
        fills.append({"id": f"f{i}b", "order_id": f"protective-{i}", "symbol": "BTCUSDT", "side": "sell",
                      "quantity": 1.0, "price": 110.0, "fee": 0.044, "realized_pnl": 10.0,
                      "fill_timestamp": (at + timedelta(minutes=5)).isoformat()})
    return {"lab_id": "PRICE_ACTION_LAB", "fills": fills, "orders": orders, "meta": meta, "funding": [],
            "sessions": {"s1": {"mode": "LIVE_PAPER", "timeframe": "5m"}}, "fee_rate": 0.0004,
            "research_lookup": lambda _oid: {}}


def test_sync_passes_skip_finalised_trips_and_scale_linearly_without_writes():
    timings = {}
    for n in (10, 1000):
        store = TradeJournalStore(":memory:")
        recorder = TradeJournalRecorder(store)
        export = _bulk_lab_export(n)
        assert ingest_v2_lab(recorder, export)["created"] == n
        research_calls = []
        export["research_lookup"] = lambda oid: research_calls.append(oid) or {}
        changes = store._c.total_changes
        with _Trace(store._c) as trace:
            started = time.perf_counter()
            summary = ingest_v2_lab(recorder, export)
            elapsed = time.perf_counter() - started
        assert summary == {"created": 0, "exits_added": 0, "finalised": 0}
        assert store._c.total_changes == changes and trace.writes() == []
        assert research_calls == []                          # finalised trips never reach the lab journal
        assert len(trace.statements) == 1, trace.statements  # one query, whatever the closed history
        timings[n] = (len(trace.statements), round(elapsed * 1000, 2))
    print(f"EVIDENCE PERF steady-state lab pass statements/ms: {timings}")


def test_trade_list_and_analytics_have_no_n_plus_one(api):
    counts = {}
    for n in (10, 1000):
        store = TradeJournalStore(":memory:")
        for i in range(n):
            store.create_trade({"source_system": "VERIFY", "source_trade_key": f"n{i}", "trading_mode": "FORWARD_PAPER",
                                "status": "CLOSED", "symbol": "BTCUSDT", "direction": "LONG",
                                "strategy_name": "SMC Lab", "net_pnl": 1.0, "result": "WIN", "counts_in_stats": 1,
                                "entry_filled_at": f"2026-10-05T08:{i % 60:02d}:00+00:00"})
        client, auth = api(store)
        per_endpoint = {}
        for url in ("/journal/v2/trades?limit=1000", "/journal/v2/summary", "/journal/v2/analytics",
                    "/journal/v2/meta", "/journal/v2/weekly-review?week=2026-W41"):
            with _Trace(store._c) as trace:
                assert client.get(url, headers=auth).status_code == 200
            per_endpoint[url] = len(trace.statements)
        counts[n] = per_endpoint
    weekly = "/journal/v2/weekly-review?week=2026-W41"
    assert counts[10][weekly] == 3 + 1 and counts[1000][weekly] == 3 + 2   # reviews batched 500 ids/statement
    for url in counts[10]:
        if url != weekly:
            assert counts[10][url] == counts[1000][url], url  # independent of the trade count
    print(f"EVIDENCE PERF statements per request (10 vs 1000 trades): {counts}")


def test_filtered_queries_use_indexes():
    store = TradeJournalStore(":memory:")
    plans = {}
    for name, sql, args in (
            ("mode+date", "SELECT * FROM journal_trades WHERE trading_mode IN (?) AND entry_filled_at >= ?",
             ("FORWARD_PAPER", "2026-01-01")),
            ("symbol", "SELECT * FROM journal_trades WHERE symbol=?", ("BTCUSDT",)),
            ("instance", "SELECT * FROM journal_trades WHERE instance_id=?", ("x",)),
            ("status", "SELECT * FROM journal_trades WHERE status IN ('OPEN','PARTIALLY_CLOSED')", ()),
            ("link", "SELECT trade_id FROM journal_trade_links WHERE link_type=? AND ref=?", ("LAB_FILL", "f")),
            ("links-of-trade", "SELECT * FROM journal_trade_links WHERE trade_id=?", ("t",)),
            ("executions", "SELECT * FROM journal_executions WHERE trade_id=? ORDER BY executed_at", ("t",)),
            ("events", "SELECT * FROM journal_events WHERE trade_id=? ORDER BY ts", ("t",)),
            ("finalised-lab-refs", "SELECT l.ref FROM journal_trade_links l JOIN journal_trades t "
             "ON t.trade_id = l.trade_id WHERE l.link_type=? AND t.finalised_at IS NOT NULL", ("LAB_FILL",))):
        plan = " | ".join(r[-1] for r in store._c.execute("EXPLAIN QUERY PLAN " + sql, args))
        assert "USING" in plan and "INDEX" in plan, (name, plan)
        plans[name] = plan
    print(f"EVIDENCE PERF query plans: {plans}")


def test_schema_migration_and_legacy_migration_do_not_repeat(tmp_path):
    path = str(tmp_path / "journal.db")
    TradeJournalStore(path)._c.close()
    conn = sqlite3.connect(path)
    schema = sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master"))
    conn.close()
    reopened = TradeJournalStore(path)
    assert sorted(tuple(r) for r in reopened._c.execute("SELECT type, name, sql FROM sqlite_master")) == schema
    # the timer never runs legacy migration; only boot / an explicit sync does
    calls = []
    sync = JournalSync(TradeJournalRecorder(reopened), legacy_store=object(), ledger=None, interval_s=10)
    sync.recorder.migrate_legacy = lambda *a, **k: calls.append("migrate") or {"migrated": 0}
    sync.run_once(include_legacy=False)
    assert calls == []
    sync.run_once()
    assert calls == ["migrate"]
    import inspect
    loop_source = inspect.getsource(JournalSync.start)
    assert "run_once(include_legacy=False)" in loop_source


def test_session_classifier_matches_zoneinfo_every_15_minutes_2026_2027():
    """Independent oracle: each centre's own local clock via zoneinfo."""
    from zoneinfo import ZoneInfo

    def oracle(utc: datetime) -> str:
        def open_(zone, start, end):
            local = utc.astimezone(ZoneInfo(zone))
            return start * 60 <= local.hour * 60 + local.minute < end * 60
        london, ny, tokyo = open_("Europe/London", 8, 17), open_("America/New_York", 8, 17), open_("Asia/Tokyo", 9, 18)
        if london and ny:
            return "LONDON_NY_OVERLAP"
        return "LONDON" if london else "NEW_YORK" if ny else "ASIA" if tokyo else "OFF_HOURS"

    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2028, 1, 1, tzinfo=timezone.utc)
    checked = mismatches = 0
    while t < end:
        if classify_session(t.isoformat()) != oracle(t):
            mismatches += 1
        checked += 1
        t += timedelta(minutes=15)
    assert mismatches == 0
    assert checked == 70080
    print(f"EVIDENCE SESSIONS checked={checked} mismatches={mismatches}")


def test_funding_booked_in_the_close_race_stays_with_its_position():
    """Funding stamped after the exit quote's receipt time but before the
    close was processed (the broker charged the still-open position) belongs
    to that trip — never dropped, never given to the next trip."""
    t = datetime(2026, 10, 5, 15, 59, 0, tzinfo=timezone.utc)

    def fill(fid, oid, side, price, at, pnl=0.0):
        return {"id": fid, "order_id": oid, "symbol": "BTCUSDT", "side": side, "quantity": 1.0, "price": price,
                "fee": 0.04, "realized_pnl": pnl, "fill_timestamp": at.isoformat()}

    export = {
        "lab_id": "PRICE_ACTION_LAB", "orders": {}, "meta": {}, "sessions": {}, "fee_rate": 0.0004,
        "fills": [fill("a1", "o1", "buy", 100.0, t),
                  fill("a2", "protective-1", "sell", 101.0, t + timedelta(seconds=59, milliseconds=900), 1.0),
                  fill("b1", "o2", "buy", 100.0, t + timedelta(minutes=5)),
                  fill("b2", "protective-2", "sell", 99.0, t + timedelta(minutes=9), -1.0)],
        "funding": [
            # booked at 16:00:00.000 for position P1, 100 ms after the exit quote was received
            {"funding_key": "k1", "position_id": "P1", "symbol": "BTCUSDT", "amount": 0.25, "rate": 0.0001,
             "funding_timestamp": (t + timedelta(minutes=1)).isoformat()},
            {"funding_key": "k2", "position_id": "P2", "symbol": "BTCUSDT", "amount": 0.5, "rate": 0.0001,
             "funding_timestamp": (t + timedelta(minutes=8)).isoformat()},
        ],
    }
    store = TradeJournalStore(":memory:")
    ingest_v2_lab(TradeJournalRecorder(store), export)
    first, second = store.list_trades(modes=["ALL"], order="asc")
    assert first["funding_total"] == 0.25 and second["funding_total"] == 0.5
    assert first["net_pnl"] == pytest.approx(1.0 - 0.08 - 0.25)
    assert second["net_pnl"] == pytest.approx(-1.0 - 0.08 - 0.5)
    assert first["net_pnl"] + second["net_pnl"] == pytest.approx(sum(f["realized_pnl"] - f["fee"]
                                                                    for f in export["fills"]) - 0.75)
