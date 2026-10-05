"""Canonical trade journal: sessions, analytics, filters, labs, weekly review and API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from data.trade_journal_store import TradeJournalStore
from execution.paper_broker_v2 import PaperBrokerV2
from services import journal_analytics as analytics
from services.journal_ingest import JournalSync, ingest_replay_journal, ingest_v2_lab, round_trips
from services.journal_sessions import (
    classify_session, duration_display, in_preferred_window, london_display, timing_fields,
)
from services.trade_journal import TradeJournalRecorder

_SEQ = [0]


def _trade(store, *, strategy="SMC Lab", symbol="BTCUSDT", direction="LONG", net=10.0, risk=10.0,
           mode="FORWARD_PAPER", entry_at="2026-10-05T08:30:00+00:00", leverage=1.0, planned_rr=3.0,
           exit_reason=None, result=None, instance_id=None, mfe_r=None, mae_r=None, **extra):
    _SEQ[0] += 1
    r = net / risk if risk else None
    if result is None:
        result = "BREAK_EVEN" if abs(r or 0) <= 0.05 else "WIN" if net > 0 else "LOSS"
    exit_at = (datetime.fromisoformat(entry_at) + timedelta(hours=1)).isoformat()
    fields = {
        "source_system": "TEST", "source_trade_key": f"k{_SEQ[0]}", "strategy_name": strategy,
        "strategy_id": strategy.lower().replace(" ", "_"), "strategy_family": strategy.split()[0].upper(),
        "symbol": symbol, "direction": direction, "trading_mode": mode, "status": "CLOSED",
        "entry_filled_at": entry_at, "exit_at": exit_at, "net_pnl": net, "gross_pnl": net + 1,
        "fees_total": 1.0, "risk_amount": risk, "realised_r": r, "gross_r": (net + 1) / risk,
        "planned_rr": planned_rr, "leverage": leverage, "result": result, "counts_in_stats": 1,
        "exit_reason": exit_reason or ("TAKE_PROFIT" if net > 0 else "STOP_LOSS"),
        "instance_id": instance_id, "mfe_r": mfe_r, "mae_r": mae_r, "entry_locked": 1,
        "finalised_at": exit_at, "notional_value": 1000.0, "risk_pct": 1.0, "duration_s": 3600,
        **timing_fields(entry_at, exit_at),
    }
    fields.update(extra)
    trade_id, _ = store.create_trade(fields)
    return trade_id


# ------------------------------------------------------------- sessions
@pytest.mark.parametrize("ts,expected", [
    ("2026-10-05T02:00:00+00:00", "ASIA"),
    ("2026-10-05T08:42:00+00:00", "LONDON"),            # 09:42 BST
    ("2026-10-05T12:30:00+00:00", "LONDON_NY_OVERLAP"),  # 13:30 BST, 08:30 EDT
    ("2026-10-05T16:30:00+00:00", "NEW_YORK"),
    ("2026-10-05T22:30:00+00:00", "OFF_HOURS"),
    ("2026-01-15T07:30:00+00:00", "ASIA"),               # 07:30 GMT, London not yet open
    ("2026-03-20T12:30:00+00:00", "LONDON_NY_OVERLAP"),  # US already on DST, UK not
])
def test_session_classification(ts, expected):
    assert classify_session(ts) == expected


def test_london_time_duration_and_preferred_window():
    assert london_display("2026-10-05T08:42:00+00:00") == "05 Oct 2026 09:42 London"
    assert london_display("2026-01-05T08:42:00+00:00") == "05 Jan 2026 08:42 London"
    fields = timing_fields("2026-10-05T08:42:00+00:00", "2026-10-05T10:18:00+00:00")
    assert fields["duration_s"] == 96 * 60 and duration_display(fields["duration_s"]) == "1h 36m"
    assert fields["entry_weekday"] == "Monday" and fields["entry_hour_london"] == 9
    assert in_preferred_window("2026-10-05T08:00:00+00:00", 0, 24) is None   # no restriction
    assert in_preferred_window("2026-10-05T08:00:00+00:00", 7, 16) is True
    assert in_preferred_window("2026-10-05T20:00:00+00:00", 7, 16) is False


# ------------------------------------------------------------- aggregation
def test_strategy_aggregation_metrics():
    store = TradeJournalStore(":memory:")
    for net in (30.0, -10.0, 20.0, -10.0, 0.2):
        _trade(store, net=net)
    m = analytics.metrics(store.list_trades())
    assert (m["total_trades"], m["wins"], m["losses"], m["break_even"]) == (5, 2, 2, 1)
    assert m["win_rate"] == pytest.approx(40.0)
    assert m["net_pnl"] == pytest.approx(30.2)
    assert m["gross_profit"] == pytest.approx(50.2) and m["gross_loss"] == pytest.approx(20.0)
    assert m["profit_factor"] == pytest.approx(50.2 / 20.0)
    assert m["avg_win"] == pytest.approx(25.0) and m["avg_loss"] == pytest.approx(-10.0)
    assert m["expectancy"] == pytest.approx(30.2 / 5)
    assert m["avg_r"] == pytest.approx(3.02 / 5)
    assert m["best_trade"]["net_pnl"] == 30.0 and m["worst_trade"]["net_pnl"] == -10.0
    assert m["max_drawdown"] == pytest.approx(-10.0)
    assert m["total_fees"] == pytest.approx(5.0) and m["sample_warning"] == "INSUFFICIENT_SAMPLE"


def test_operational_events_never_count_as_losses():
    store = TradeJournalStore(":memory:")
    _trade(store, net=10.0)
    store.create_trade({"source_system": "TEST", "source_trade_key": "failed", "symbol": "BTCUSDT",
                        "direction": "LONG", "status": "FAILED", "result": "EXECUTION_FAILED",
                        "is_operational": 1, "trading_mode": "FORWARD_PAPER", "strategy_name": "SMC Lab"})
    m = analytics.metrics(store.list_trades())
    assert m["total_trades"] == 1 and m["losses"] == 0 and m["win_rate"] == 100.0
    assert m["operational_events"] == 1 and m["operational_breakdown"] == {"EXECUTION_FAILED": 1}


def test_strategy_comparison_and_dashboard_best_worst():
    store = TradeJournalStore(":memory:")
    for net in (30.0, 30.0, -10.0):
        _trade(store, strategy="SMC Lab", net=net)
    for net in (-10.0, -10.0, 5.0):
        _trade(store, strategy="Price Action Lab", net=net, symbol="SOLUSDT",
               entry_at="2026-10-05T14:30:00+00:00")
    rows = analytics.strategy_comparison(store.list_trades())
    assert [r["strategy"] for r in rows] == ["SMC Lab", "Price Action Lab"]
    smc = rows[0]
    assert smc["trades"] == 3 and smc["net_pnl"] == pytest.approx(50.0)
    assert smc["best_session"] == "London" and smc["best_symbol"] == "BTCUSDT"
    assert rows[1]["verdict"].startswith("LOSING")
    dash = analytics.dashboard(store.list_trades())
    assert dash["best_strategy"]["key"] == "SMC Lab" and dash["worst_strategy"]["key"] == "Price Action Lab"
    assert dash["best_symbol"]["key"] == "BTCUSDT" and dash["best_session"]["key"] == "LONDON"


def test_symbol_aggregation_per_strategy():
    store = TradeJournalStore(":memory:")
    _trade(store, symbol="BTCUSDT", net=20)
    _trade(store, symbol="BTCUSDT", net=-10)
    _trade(store, symbol="ETHUSDT", net=15)
    _trade(store, symbol="SOLUSDT", net=-12)
    out = analytics.symbol_performance(store.list_trades())
    rows = {r["key"]: r for r in out["by_strategy"]["SMC Lab"]}
    assert rows["BTCUSDT"]["total_trades"] == 2 and rows["BTCUSDT"]["net_pnl"] == pytest.approx(10)
    assert rows["BTCUSDT"]["win_rate"] == pytest.approx(50.0)
    assert rows["SOLUSDT"]["net_pnl"] == pytest.approx(-12) and rows["ETHUSDT"]["avg_r"] == pytest.approx(1.5)


def test_long_short_analysis():
    store = TradeJournalStore(":memory:")
    for net in (20, 20, -10):
        _trade(store, direction="LONG", net=net)
    for net in (-10, -10, 5):
        _trade(store, direction="SHORT", net=net)
    out = analytics.direction_performance(store.list_trades())["by_strategy"]["SMC Lab"]
    assert out["LONG"]["trades"] == 3 and out["LONG"]["win_rate"] == pytest.approx(66.67, abs=0.01)
    assert out["SHORT"]["net_pnl"] == pytest.approx(-15) and out["stronger_side"] == "LONG"


def test_leverage_session_hour_rr_excursion_and_trend_blocks():
    store = TradeJournalStore(":memory:")
    for i in range(12):
        _trade(store, net=(-10 if i < 6 else 25), leverage=(1.0 if i % 2 else 10.0),
               entry_at=f"2026-09-{10 + i:02d}T08:15:00+00:00",  # chronological: losses, then wins
               mfe_r=2.0, mae_r=-0.5, exit_reason=("TAKE_PROFIT" if i >= 6 else "STOP_LOSS"),
               return_on_margin_pct=5.0)
    rows = store.list_trades()
    lev = {r["leverage"]: r for r in analytics.leverage_performance(rows)}
    assert set(lev) == {"1x", "10x"} and lev["10x"]["trades"] == 6
    assert lev["1x"]["avg_drawdown_r"] == pytest.approx(-0.5)
    hours = analytics.hour_performance(rows)
    assert hours[9]["label"] == "09:00–10:00" and hours[9]["trades"] == 12  # 08:15 UTC = 09:15 BST
    rr = analytics.rr_analysis(rows)["overall"]
    assert rr["avg_planned_rr"] == pytest.approx(3.0)
    assert rr["pct_full_target"] == pytest.approx(50.0) and rr["pct_stopped_before_target"] == pytest.approx(50.0)
    assert rr["difference_r"] == pytest.approx(((6 * -1.0 + 6 * 2.5) / 12) - 3.0)
    ex = analytics.excursion_analysis(rows)
    assert ex["coverage"]["tracked"] == 12 and ex["avg_mfe_r"] == pytest.approx(2.0)
    assert ex["losers_that_reached_1r"] == 6
    trend = analytics.trend(rows)
    assert trend["overall"]["status"] == "IMPROVING"
    assert len(trend["weekly"]) >= 1


# ------------------------------------------------------------- filters and data separation
def test_journal_filters_work_together():
    store = TradeJournalStore(":memory:")
    target = _trade(store, strategy="SMC Lab", symbol="ETHUSDT", direction="SHORT", net=25.0,
                    leverage=5.0, instance_id="inst-a", timeframe="5m",
                    entry_at="2026-10-05T12:30:00+00:00", rule_violation=0)
    _trade(store, strategy="SMC Lab", symbol="ETHUSDT", direction="LONG", net=25.0, leverage=5.0,
           instance_id="inst-a", timeframe="5m", entry_at="2026-10-05T12:30:00+00:00")
    _trade(store, strategy="SMC Lab", symbol="ETHUSDT", direction="SHORT", net=-10.0, leverage=5.0,
           instance_id="inst-a", timeframe="5m", entry_at="2026-10-05T12:30:00+00:00")
    _trade(store, strategy="Price Action Lab", symbol="ETHUSDT", direction="SHORT", net=25.0,
           leverage=5.0, entry_at="2026-10-05T12:30:00+00:00", timeframe="5m")
    _trade(store, strategy="SMC Lab", symbol="ETHUSDT", direction="SHORT", net=25.0, leverage=20.0,
           instance_id="inst-a", timeframe="5m", entry_at="2026-10-05T12:30:00+00:00")
    _trade(store, strategy="SMC Lab", symbol="ETHUSDT", direction="SHORT", net=25.0, leverage=5.0,
           instance_id="inst-a", timeframe="5m", entry_at="2026-09-01T12:30:00+00:00")
    rows = store.list_trades(modes=["FORWARD_PAPER"], date_from="2026-10-01", date_to="2026-10-05",
                             strategy="SMC Lab", instance_id="inst-a", symbol="ethusdt", direction="short",
                             result="WINS", session="LONDON_NY_OVERLAP", timeframe="5m", leverage_min=2,
                             leverage_max=10, rr_min=2, rr_max=4, pnl_min=0, pnl_max=100,
                             rule_violation=False, exit_reason="take_profit")
    assert [r["trade_id"] for r in rows] == [target]
    assert store.count_trades(modes=["FORWARD_PAPER"], strategy="SMC Lab", symbol="ETHUSDT") == 5


def test_backtest_results_never_inflate_forward_paper_performance():
    store = TradeJournalStore(":memory:")
    _trade(store, net=-10.0, mode="FORWARD_PAPER")
    for _ in range(5):
        _trade(store, net=500.0, mode="BACKTEST")
    _trade(store, net=40.0, mode="ISOLATED_FORWARD_PAPER")
    forward = analytics.dashboard(store.list_trades(modes=["FORWARD_PAPER"]))
    assert forward["net_pnl"] == pytest.approx(-10.0) and forward["total_trades"] == 1
    both = analytics.dashboard(store.list_trades(modes=["FORWARD_PAPER", "ISOLATED_FORWARD_PAPER"]))
    assert both["net_pnl"] == pytest.approx(30.0)
    everything = analytics.dashboard(store.list_trades(modes=["ALL"]))
    assert everything["total_trades"] == 7


# ------------------------------------------------------------- labs and backtests
def _lab_broker(path):
    return PaperBrokerV2(path, starting_balance=10_000, account_type="PA_LAB", execution_engine="PA_LAB",
                         fee_rate=0.0004, spread_bps=0, slippage_bps=0, participation_rate=1)


def test_lab_round_trip_ingestion_partial_fills_fees_and_idempotency(tmp_path):
    broker = _lab_broker(tmp_path / "pa.db")
    decided = datetime(2026, 10, 5, 8, 40, tzinfo=timezone.utc)
    order = broker.submit(symbol="BTCUSDT", side="buy", order_type="market", quantity=2,
                          protection_stop_loss=95, protection_take_profit=110,
                          decision_timestamp=decided.isoformat(), signal_timestamp=decided.isoformat(),
                          requested_price=100.0, strategy="PA1_SR_REJECTION", strategy_version="v1",
                          timeframe="5m")
    broker.process_tick("BTCUSDT", {"bid": 99.9, "ask": 100.0, "mark": 100.0,
                                    "received_at": (decided + timedelta(seconds=1)).isoformat()})
    meta = {order["id"]: {"order_id": order["id"], "session_id": "s1", "strategy_id": "PA1_SR_REJECTION",
                          "direction": "bullish", "status": "ENTERED", "created_at": decided.isoformat(),
                          "config": {"stop": 95, "target": 110, "risk_pct": 0.5, "leverage": 5,
                                     "account_equity_before": 10_000, "risk_amount": 10,
                                     "execution": {"max_risk_pct": 1.0},
                                     "setup": {"reasons": ["support zone touched", "bullish rejection"],
                                               "missing_conditions": [], "phase": "ENTERED",
                                               "pattern_metadata": [{"pattern": "pin_bar"}],
                                               "context_snapshot": {"structure_state": "bullish",
                                                                    "zone": {"role": "support", "low": 99,
                                                                             "high": 100, "flipped": False},
                                                                    "trigger_event": {"event_type": "rejection",
                                                                                      "level": 99.5}}},
                                     "proposal": {"entry_model": "break_of_trigger"}}}}
    sessions = {"s1": {"mode": "LIVE_PAPER", "timeframe": "5m"}}

    def export():
        return {**broker.journal_export(), "lab_id": "PRICE_ACTION_LAB", "meta": meta, "sessions": sessions,
                "research": {}, "strategy_version": "v1"}

    store = TradeJournalStore(":memory:")
    rec = TradeJournalRecorder(store)
    first = ingest_v2_lab(rec, export())
    assert first["created"] == 1
    t = store.list_trades()[0]
    assert t["status"] == "OPEN" and t["trading_mode"] == "ISOLATED_FORWARD_PAPER"
    assert t["strategy_name"] == "Price Action Lab" and t["lab_id"] == "PRICE_ACTION_LAB"
    assert t["leverage"] == 5 and t["margin_used"] == pytest.approx(t["notional_value"] / 5)
    assert t["risk_pct"] == pytest.approx(2 * 5 / 10_000 * 100)
    snap = store.snapshot(t["trade_id"])
    assert snap["setup"]["family"] == "PRICE_ACTION"
    assert snap["setup"]["support_resistance_level"]["status"] == "PASSED"
    assert snap["setup"]["ema_relationship"]["status"] == "NOT_EVALUATED"
    # the stop is hit on a later quote
    broker.process_tick("BTCUSDT", {"bid": 94.9, "ask": 95.0, "mark": 95.0,
                                    "received_at": (decided + timedelta(minutes=5)).isoformat()})
    second = ingest_v2_lab(rec, export())
    assert second["created"] == 0 and second["finalised"] == 1
    t = store.get_trade(t["trade_id"])
    assert t["status"] == "CLOSED" and t["exit_reason"] == "STOP_LOSS"
    assert t["exit_reason_source"] == "INFERRED_FROM_FILL_PRICE" and t["result"] == "LOSS"
    fills = broker.journal_export()["fills"]
    assert t["net_pnl"] == pytest.approx(sum(f["realized_pnl"] - f["fee"] for f in fills))
    assert t["fees_total"] == pytest.approx(sum(f["fee"] for f in fills))
    # idempotent
    third = ingest_v2_lab(rec, export())
    assert third == {"created": 0, "exits_added": 0, "finalised": 0}
    assert len(store.list_trades()) == 1


def test_round_trips_split_scale_ins_partials_and_reversals():
    fills = [
        {"id": "1", "symbol": "X", "side": "buy", "quantity": 1, "price": 10},
        {"id": "2", "symbol": "X", "side": "buy", "quantity": 1, "price": 11},
        {"id": "3", "symbol": "X", "side": "sell", "quantity": 1, "price": 12},
        {"id": "4", "symbol": "X", "side": "sell", "quantity": 3, "price": 13},
    ]
    trips = round_trips(fills)
    assert len(trips) == 2
    assert [f["id"] for f in trips[0]["entries"]] == ["1", "2"] and trips[0]["closed"]
    assert [f["quantity"] for f in trips[0]["exits"]] == [1, 1]
    assert trips[1]["direction"] == "SHORT" and trips[1]["entries"][0]["quantity"] == 2


def test_real_lab_accounts_expose_journal_exports(tmp_path):
    from services.price_action_lab import PriceActionPaperAccount
    from services.smc_strategy_lab import SMCPaperAccount
    pa = PriceActionPaperAccount(tmp_path / "pa.db")
    smc = SMCPaperAccount(tmp_path / "smc.db")
    store = TradeJournalStore(":memory:")
    sync = JournalSync(TradeJournalRecorder(store), labs=(pa, smc))
    out = sync.run_once()
    assert "error" not in str(out)
    assert pa.journal_export()["lab_id"] == "PRICE_ACTION_LAB" and smc.journal_export()["lab_id"] == "SMC_LAB"


def test_replay_journal_is_ingested_as_backtest_only(tmp_path):
    from services.journal import JournalStore as ReplayStore
    replay = ReplayStore(str(tmp_path / "journal.json"))
    replay.add_from_trades([{"id": 1, "side": "long", "rr": 2.0, "entry": 100, "exit": 110}],
                           symbol="BTCUSDT", strategy="Decision Brain", timeframe="15m")
    store = TradeJournalStore(":memory:")
    rec = TradeJournalRecorder(store)
    assert ingest_replay_journal(rec, replay)["created"] == 1
    assert ingest_replay_journal(rec, replay)["created"] == 0
    t = store.list_trades()[0]
    assert t["trading_mode"] == "BACKTEST" and t["realised_r"] == 2.0 and t["net_pnl"] is None
    assert store.list_trades(modes=["FORWARD_PAPER"]) == []


# ------------------------------------------------------------- weekly review
def test_weekly_review_observations_come_from_journal_data():
    store = TradeJournalStore(":memory:")
    for i in range(5):  # SMC wins in London, loses in New York
        _trade(store, strategy="SMC Lab", net=20.0, entry_at=f"2026-10-0{5 + i % 2}T08:30:00+00:00")
        _trade(store, strategy="SMC Lab", net=-10.0, entry_at=f"2026-10-0{5 + i % 2}T17:30:00+00:00")
    for symbol, net in (("BTCUSDT", 15.0), ("BTCUSDT", 15.0), ("BTCUSDT", -5.0),
                        ("SOLUSDT", -10.0), ("SOLUSDT", -10.0), ("SOLUSDT", 5.0)):
        _trade(store, strategy="Price Action Lab", symbol=symbol, net=net,
               entry_at="2026-10-06T10:00:00+00:00")
    week = store.list_trades()
    report = analytics.weekly_review(week, [], {}, week_key="2026-W41")
    text = " ".join(o["text"] for o in report["observations"])
    assert "SMC Lab had a 100% win rate during the London session but only 0% during New York" in text
    assert "Price Action Lab produced positive expectancy on BTCUSDT but negative expectancy on SOLUSDT" in text
    assert "Average planned RR was 3.00R" in text
    assert any("never changed" in g for g in report["guardrails"])
    assert all(o["confidence"] in ("LOW_SAMPLE", "MODERATE", "SUPPORTED") for o in report["observations"])


# ------------------------------------------------------------- API
@pytest.fixture()
def api(monkeypatch):
    from fastapi.testclient import TestClient
    import app as appmod
    import webhook_api as wa
    from config import settings
    store = TradeJournalStore(":memory:")
    monkeypatch.setattr(wa, "trade_journal_store", store)
    monkeypatch.setattr(wa, "trade_journal", TradeJournalRecorder(store))
    client = TestClient(appmod.app)
    return client, store, {"x-webhook-secret": settings.admin_key}


def test_api_lists_filters_and_explains_trades(api):
    client, store, auth = api
    trade_id = _trade(store, strategy="SMC Lab", net=26.91, risk=10.0, exit_reason="TAKE_PROFIT")
    _trade(store, strategy="SMC Lab", net=999.0, mode="BACKTEST")
    store.save_snapshot(trade_id, {"decision": "ENTER_LONG", "decision_reason": "sweep + POI",
                                   "conditions_passed": [{"name": "HTF bullish"}], "source": "TEST"})
    listed = client.get("/journal/v2/trades", headers=auth).json()
    assert listed["modes_applied"] == ["FORWARD_PAPER"] and listed["mixed_modes"] is False
    assert listed["total"] == 1 and listed["trades"][0]["trade_id"] == trade_id
    assert listed["trades"][0]["session_label"] == "London"
    mixed = client.get("/journal/v2/trades?modes=ALL", headers=auth).json()
    assert mixed["total"] == 2 and mixed["mixed_modes"] is True and mixed["mode_warning"]
    summary = client.get("/journal/v2/summary?symbol=BTCUSDT", headers=auth).json()
    assert summary["net_pnl"] == pytest.approx(26.91) and summary["total_trades"] == 1
    ref = store.get_trade(trade_id)["trade_ref"]
    detail = client.get(f"/journal/v2/trades/{ref}", headers=auth).json()
    for section in ("trade", "facts", "snapshot", "executions", "fees", "modifications", "timeline",
                    "reviews", "notes", "corrections", "links", "missing_fields", "strategy_context"):
        assert section in detail
    facts = {f["q"]: f["a"] for f in detail["facts"]}
    assert facts["Why it entered"] == "sweep + POI" and facts["Realised R"] == "+2.69R"
    assert detail["strategy_context"]["metrics"]["total_trades"] == 1
    analytics_payload = client.get("/journal/v2/analytics", headers=auth).json()
    assert analytics_payload["comparison"][0]["strategy"] == "SMC Lab"
    assert client.get("/journal/v2/trades/does-not-exist", headers=auth).status_code == 404
    assert client.get("/journal/v2/trades?modes=NOPE", headers=auth).status_code == 400
    weekly = client.get("/journal/v2/weekly-review?week=2026-W41", headers=auth).json()
    assert weekly["week"] == "2026-W41" and weekly["persisted"] is False
    csv_text = client.get("/journal/v2/trades.csv", headers=auth).text
    assert csv_text.splitlines()[0].startswith("trade_id,trade_ref")


def test_api_writes_require_the_control_credential_and_keep_an_audit_trail(api):
    client, store, auth = api
    trade_id = _trade(store, net=10.0)
    assert client.post(f"/journal/v2/trades/{trade_id}/notes", json={"note": "entered early"}).status_code == 401
    note = client.post(f"/journal/v2/trades/{trade_id}/notes", json={"note": "entered early"}, headers=auth)
    assert note.status_code == 200 and store.notes(trade_id)[0]["note"] == "entered early"
    bad = client.post(f"/journal/v2/trades/{trade_id}/corrections",
                      json={"changes": {"net_pnl": 11.0}}, headers=auth)
    assert bad.status_code == 400  # no reason
    ok = client.post(f"/journal/v2/trades/{trade_id}/corrections",
                     json={"changes": {"net_pnl": 11.0}, "reason": "fee rebate", "actor": "ops"}, headers=auth)
    assert ok.status_code == 200 and store.get_trade(trade_id)["net_pnl"] == 11.0
    assert store.corrections(trade_id)[0]["previous_value"] == 10.0
    review = client.post(f"/journal/v2/trades/{trade_id}/reviews/external",
                         json={"reviewer": "llm-coach", "summary": "fine", "mistakes": ["none"],
                               "net_pnl": 1_000_000}, headers=auth)
    assert review.status_code == 200 and store.get_trade(trade_id)["net_pnl"] == 11.0
    saved = client.post("/journal/v2/weekly-review?week=2026-W41", headers=auth)
    assert saved.status_code == 200
    assert client.get("/journal/v2/weekly-reviews", headers=auth).json()["reviews"][0]["week_key"] == "2026-W41"
