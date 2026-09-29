"""Guardian Phase 2: every strategy evaluation leaves a decision trace, rejected
setups are traced to the condition or gate that stopped them, and near-valid
setups are recorded as almost-trades -- never as a verdict on a rule.

The strategies are real: the 3-Candle Rejection strategy through
AutoStrategyEngine and its pipeline; the frozen SMC strategy through the SMC
agent into its journal; the frozen Price Action engine through the PA lab's
own per-candle evaluation. Guardian reads the labs through read-only
connections.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

import services.guardian as guardian
from bot.types import Bar
from services.guardian.bus import EventBus
from services.guardian.service import GuardianService
from services.guardian.store import GuardianStore
from services.guardian.strategy import (
    NOTE, PAEvaluationReader, SMCDecisionReader, StrategyTelemetry, almost_trade, read_only,
)
from tests.test_loss_streak_order import T0, TF, _engine

STRATEGY = "three_candle_rejection"


@pytest.fixture
def g():
    store = GuardianStore()
    bus = EventBus(store, flush_interval_s=0.05)
    bus.start()
    guardian.install(bus)
    yield store, bus
    guardian.uninstall()
    bus.stop()


def _drain(bus) -> None:
    bus.flush()


def _traces(store, **filters) -> list[dict]:
    return list(reversed(store.events(category="strategy", limit=1000, **filters)))


def _cond(trace: dict) -> dict:
    return {c["id"]: c for c in trace["conditions"]}


def _bars(rows) -> list[Bar]:
    return [Bar(T0 + TF * k, r.open, r.high, r.low, r.close, r.volume) for k, r in enumerate(rows)]


def _run(engine, rows, *, last: int = 3):
    bars = _bars(rows)
    strategy = engine.strategy_factory("BTCUSDT")
    strategy.bars.extend(bars[:-last])
    for bar in bars[-last:]:
        engine._process_bar("BTCUSDT", bar, strategy)
    return strategy, bars


def _instance(tmp_path, *, gate_off: bool = True):
    ledger, engine, paper = _engine(tmp_path)
    engine.strategy_key = STRATEGY              # what TradingInstanceManager sets
    engine.guardian_trace = True                # replay engines do not publish by default
    if not gate_off:
        engine.quality_gate_bypass = lambda: False
    return ledger, engine, paper


# ------------------------------------------------ PRD §8: instance traces
def test_every_candle_is_traced_and_an_ema_filtered_rejection_is_one_condition_short(tmp_path, g):
    """A complete push, rejection and confirmation at support that EMA 9/33
    filters out: four of the strategy's conditions passed, the trend filter
    failed, and nothing after it ran."""
    from tests.test_three_candle_rejection import _history, _long_pattern
    store, bus = g
    _, engine, paper = _instance(tmp_path)
    rows, i = _history(trend="down")
    strategy, _ = _run(engine, rows + _long_pattern(i, open_=101.6))
    _drain(bus)
    traces = _traces(store, source_component="instance:inst-s")
    assert len(traces) == 3                               # one per closed candle processed
    last = traces[-1]
    report = strategy.decision_report()
    assert last["event_type"] == "evaluation_completed" and last["decision"] == "NO_SETUP"
    trace = last["evidence"]
    c = _cond(trace)
    assert trace["blocker_code"] == report["blocker_code"] == "EMA_TREND_NOT_ALIGNED"
    assert [c[k]["state"] for k in ("levels_ready", "at_level", "rejection", "confirmation")] == ["PASS"] * 4
    assert c["ema_trend"]["state"] == "FAIL" and c["ema_trend"]["code"] == "EMA_TREND_NOT_ALIGNED"
    assert c["ema_trend"]["detail"] == report["reason"]
    assert all(c[k]["state"] == "NOT_REACHED" for k in ("decision_brain", "risk_sizing", "broker_accepts"))
    # Replay data: the higher timeframe was never fetched, so it did not "pass".
    assert c["htf_available"]["state"] == "NOT_APPLICABLE"
    assert trace["direction"] == "long" and last["strategy_id"] == STRATEGY
    assert paper.open_position("BTCUSDT") is None

    [almost] = store.almost_trades()
    assert almost["kind"] == "ONE_CONDITION_SHORT" and almost["classification"] == "MISSED_OPPORTUNITY_CANDIDATE"
    assert (almost["passed"], almost["evaluated"]) == (4, 5)
    assert almost["prevented_by"]["code"] == "EMA_TREND_NOT_ALIGNED"
    assert almost["note"] == NOTE and "does NOT mean the rule was wrong" in NOTE
    assert almost["first_event_id"] == last["event_id"]


def test_a_taken_trade_passes_every_gate_and_a_bypassed_quality_gate_says_so(tmp_path, g):
    from tests.test_three_candle_rejection import _history, _long_pattern
    store, bus = g
    _, engine, paper = _instance(tmp_path)
    rows, i = _history()
    _run(engine, rows + _long_pattern(i))
    assert paper.open_position("BTCUSDT") is not None
    _drain(bus)
    entry = _traces(store)[-1]
    assert entry["event_type"] == "setup_detected" and entry["decision"] == "ENTERED"
    c = _cond(entry["evidence"])
    strategy_rows = [r for r in entry["evidence"]["conditions"] if r["kind"] == "strategy"]
    assert strategy_rows and all(r["state"] == "PASS" for r in strategy_rows)
    assert all(c[k]["state"] == "PASS" for k in ("risk_sizing", "exposure_limits", "broker_accepts"))
    quality = entry["evidence"]["quality"]
    refused = (not quality["allowed"]) or quality["score"] < quality["min_score"]
    # The owner switched the gate off; the trace must not call that a pass.
    assert c["decision_brain"]["state"] == ("BYPASSED" if refused else "PASS")
    assert store.almost_trades() == []


def test_a_signal_the_decision_brain_refuses_is_traced_to_the_brain(tmp_path, g):
    from tests.test_three_candle_rejection import _history, _long_pattern
    store, bus = g
    _, engine, paper = _instance(tmp_path, gate_off=False)
    engine.min_quality_score = 101                  # above any score: the Brain must refuse
    rows, i = _history()
    _run(engine, rows + _long_pattern(i))
    assert paper.open_position("BTCUSDT") is None
    _drain(bus)
    rejected = _traces(store)[-1]
    assert rejected["event_type"] == "setup_rejected" and rejected["decision"] == "REJECTED"
    trace = rejected["evidence"]
    c = _cond(trace)
    assert c["decision_brain"]["state"] == "FAIL" and c["cross_asset_context"]["state"] == "NOT_REACHED"
    assert all(r["state"] == "PASS" for r in trace["conditions"] if r["kind"] == "strategy")
    codes = [b["code"] for b in trace["blocking"]]
    assert "SCORE_BELOW_MINIMUM" in codes
    assert trace["quality"]["min_score"] == 101
    assert [b["detail"] for b in trace["blocking"] if b["code"] == "HARD_BLOCK"] == trace["quality"]["hard_blocks"]
    # Exactly one refusal makes it an almost-trade; several do not.
    assert bool(store.almost_trades()) == (len(trace["blocking"]) == 1)


def test_a_missing_higher_timeframe_is_reported_once_and_its_return_once(tmp_path, g):
    from services.auto_engine import EngineFeedError
    from tests.test_three_candle_rejection import _history, _long_pattern
    store, bus = g
    _, engine, _ = _instance(tmp_path)
    rows, i = _history(trend="down")
    bars = _bars(rows + _long_pattern(i, open_=101.6))

    def unavailable(symbol, timeframe, limit):
        raise EngineFeedError(f"{symbol} {timeframe} provider timeout")
    engine._forward_fetch_for_timeframe = unavailable
    strategy = engine.strategy_factory("BTCUSDT")
    for _ in range(3):                                   # three polls, one outage
        with pytest.raises(EngineFeedError):
            engine._refresh_multi_timeframe_context("BTCUSDT", strategy, entry_bars=bars)
    strategy.bars.extend(bars[:-1])
    engine._process_bar("BTCUSDT", bars[-1], strategy)   # the next candle evaluates normally
    _drain(bus)
    htf = [e for e in reversed(store.events(category="market_data", limit=100))]
    assert [e["event_type"] for e in htf] == ["missing_htf_candle", "htf_candle_recovered"]
    missing = htf[0]
    assert missing["severity"] == "WARNING" and missing["instance_id"] == "inst-s"
    assert missing["evidence"]["htf_timeframe"] != "5m" and "provider timeout" in missing["reason"]


def test_a_replay_engine_does_not_flood_guardian_with_simulated_candles(tmp_path, g):
    from tests.test_three_candle_rejection import _history, _long_pattern
    store, bus = g
    _, engine, _ = _engine(tmp_path)                    # live=False: a replay
    engine.strategy_key = STRATEGY
    rows, i = _history()
    _run(engine, rows + _long_pattern(i))
    _drain(bus)
    assert store.events(category="strategy") == []


def test_the_brains_own_htf_block_is_recognised_from_its_words_exactly():
    from services.strategy_trace import BRAIN_HTF_BLOCK, htf_problem
    assert htf_problem({"conditions": [], "quality": {"hard_blocks": [BRAIN_HTF_BLOCK]}})[0] == "missing_htf_candle"
    assert htf_problem({"conditions": [], "quality": {"hard_blocks": ["HTF looks odd"]}}) is None
    assert htf_problem({"conditions": [{"state": "FAIL", "code": "STALE_HTF_CANDLE",
                                        "label": "Higher timeframe fresh"}]})[0] == "stale_htf_candle"


def test_trading_does_not_wait_for_or_depend_on_the_trace(tmp_path, monkeypatch):
    """Guardian's queue full and the trace builder broken: the trade is the
    same as with no Guardian at all."""
    from services import strategy_trace
    from tests.test_three_candle_rejection import _history, _long_pattern
    store = GuardianStore()
    full = EventBus(store, capacity=1)                 # never started: fills at once
    guardian.install(full)
    try:
        _, engine, paper = _instance(tmp_path)
        rows, i = _history()
        _run(engine, rows + _long_pattern(i))
        assert paper.open_position("BTCUSDT") is not None
        assert full.stats()["dropped"] >= 1
        monkeypatch.setattr(strategy_trace, "instance_trace",
                            lambda **_: (_ for _ in ()).throw(RuntimeError("trace bug")))
        _, engine2, paper2 = _instance(tmp_path / "b")
        _run(engine2, rows + _long_pattern(i))
        assert paper2.open_position("BTCUSDT") is not None
    finally:
        guardian.uninstall()


# ------------------------------------------------------- SMC lab (frozen)
def _smc_evaluations():
    from dataclasses import replace
    from services.native_smc import PriceAction, SMCConfig, SMCMarketStructureEngine
    from services.smc_strategy_v1 import evaluate
    from tests.test_smc_strategy_ladder import bar, seeded_engine
    ready = evaluate(seeded_engine())
    one_short = seeded_engine()
    now = list(one_short.snapshots)[-1]
    one_short.snapshots[now] = replace(one_short.snapshots[now],
                                       price_action=PriceAction(False, False, .5, .2, 2.0))
    one_short.latest_snapshot = one_short.snapshots[now]
    plain = SMCMarketStructureEngine(SMCConfig("BTCUSDT", htf_minutes=5, require_volume_surge=False))
    for i in range(70):
        plain.process_closed_bar(bar(i, o=100 + i * .03, h=101 + i * .03, l=99 + i * .03, c=100.2 + i * .03))
    return ready, evaluate(one_short), evaluate(plain)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_smc_decisions_are_read_only_and_the_rejection_short_setup_is_an_almost_trade(tmp_path):
    from services.smc_agent import SMCAgent
    from services.smc_agent_journal import SMCAgentJournal
    path = tmp_path / "agent.db"
    journal = SMCAgentJournal(str(path))
    agent = SMCAgent(journal, equity=10_000.0)
    ready, one_short, plain = _smc_evaluations()
    assert one_short["missing_conditions"] == ["Bullish rejection"]     # the strategy's own verdict
    outcomes = [agent.observe(ev, candle_time=f"2026-09-19T1{k}:00:00+00:00")["outcome"]
                for k, ev in enumerate((ready, one_short, plain))]
    assert outcomes == ["TAKEN", "NOT_READY", "NOT_READY"]
    journal._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = _digest(path)

    store = GuardianStore()
    telemetry = StrategyTelemetry(store, [SMCDecisionReader(str(path))])
    result = telemetry.poll()
    assert result["smc"]["ok"] and result["smc"]["written"] == 3
    assert _digest(path) == before                       # Guardian changed nothing
    events = _traces(store, source_component="lab:smc")
    assert [e["decision"] for e in events] == ["ENTERED", "NO_SETUP", "NO_SETUP"]
    for event, ev in zip(events, (ready, one_short, plain)):
        rows = ev["ordered_condition_results"]
        assert [(c["label"], c["state"]) for c in event["evidence"]["conditions"] if c["kind"] == "strategy"] == [
            (r["label"], "PASS" if r["status"] == "PASS" else "NOT_REQUIRED" if r["status"] == "NOT_REQUIRED"
             else "FAIL") for r in rows]
    [almost] = store.almost_trades()
    assert almost["kind"] == "ONE_CONDITION_SHORT" and (almost["passed"], almost["evaluated"]) == (6, 7)
    assert almost["prevented_by"]["condition"] == "Bullish rejection"
    assert almost["first_event_id"] == events[1]["event_id"]

    # A second poll, and a restart that lost its place, add nothing twice.
    assert telemetry.poll()["smc"]["written"] == 0
    store.set_meta("telemetry.smc.agent_decisions", {})
    again = StrategyTelemetry(store, [SMCDecisionReader(str(path))]).poll()
    assert again["smc"]["read"] == 3 and again["smc"]["written"] == 0
    assert sum(r["count"] for r in store.strategy_rollup(since_day="2000-01-01")) == 3
    assert store.almost_trades()[0]["sightings"] == 1


def test_the_reader_cannot_write_to_a_lab_database(tmp_path):
    from services.smc_agent_journal import SMCAgentJournal
    path = tmp_path / "agent.db"
    SMCAgentJournal(str(path))
    conn = read_only(str(path))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM agent_decisions")
    conn.close()


# ------------------------------------------------ Price Action lab (frozen)
def _pa_run(tmp_path, **config):
    from services.native_price_action import NativePriceActionEngine, PriceActionConfig
    from tests.test_journal_pa_decisions import _account, _candles
    account = _account(tmp_path)
    engine = NativePriceActionEngine(PriceActionConfig(symbol="BTCUSDT", timeframe="5m", **config))
    bars = _candles()
    engine.ingest_closed_bars(bars[:150])
    for bar in bars[150:]:
        engine.process_closed_bar(bar, market_data_health="SYNCHRONIZED")
        account.record_evaluation(engine.visual_state(candle_window=3000), bar, {"state": "SYNCHRONIZED"})
    rows = [dict(r) for r in account._db.execute("SELECT * FROM pa_evaluations")]
    store = GuardianStore()
    telemetry = StrategyTelemetry(store, [PAEvaluationReader(account.path)], batch=40)
    for _ in range(10):                                  # a backlog is read in batches
        if not telemetry.poll()["pa"]["backlog"]:
            break
    return rows, store, _traces(store, source_component="lab:pa")


def _one_short(rows) -> set[str]:
    """Evaluations whose own trace shows every condition but one passing,
    with at least two passing -- computed from the lab's rows directly."""
    out = set()
    for r in rows:
        trace = json.loads(r["payload_json"]).get("trace") or {}
        statuses = [c["status"] for c in trace.get("conditions") or []]
        if (r["state"] == "WATCHING" and trace.get("state") != "ORDER_PENDING"
                and statuses.count("PASS") >= 2 and len(statuses) - statuses.count("PASS") == 1):
            out.add(r["correlation_id"])
    return out


def test_every_pa_evaluation_is_traced_and_a_two_condition_setup_is_never_almost(tmp_path):
    """PA1 has two conditions; one of two passing is not "most"."""
    rows, store, events = _pa_run(tmp_path)
    assert len(rows) == 150
    lifecycle = sum(len((json.loads(r["payload_json"]).get("lifecycle") or [])) for r in rows)
    assert len(events) == 150 + lifecycle
    pending = [e for e in events if e["decision"] == "SETUP_PENDING"]
    assert pending and all(e["event_type"] == "setup_detected" for e in pending)
    assert {len(e["evidence"]["conditions"]) for e in events} == {2}
    assert _one_short(rows) == set() and store.almost_trades() == []


def test_a_pa_pattern_refused_only_by_the_pin_bar_filter_is_an_almost_trade(tmp_path):
    """With the pin-bar experiment on, a complete PA1 pattern whose trigger
    candle is not a pin bar is one condition short -- recorded once per
    setup, however many candles it stayed that way."""
    rows, store, events = _pa_run(tmp_path, trigger_filter="pin_bar_only")
    expected = _one_short(rows)
    almost = store.almost_trades(limit=500)
    assert expected and almost
    first = {a["first_event_id"] for a in almost}
    assert {e["correlation_id"] for e in events if e["event_id"] in first} <= expected
    assert all(a["prevented_by"]["condition"] == "pin bar only" and a["kind"] == "ONE_CONDITION_SHORT"
               for a in almost)
    assert sum(a["sightings"] for a in almost) == len(expected)   # every candle counted, once
    assert len(almost) < len(expected)                            # grouped by setup


# ------------------------------------------------ PRD §37: strategy view
def test_the_strategy_view_counts_outcomes_reasons_and_real_results(tmp_path, g):
    from data.trade_record_store import TradeRecordStore
    from services.journal_recorder import JournalRecorder, LedgerSource
    from tests.test_loss_streak_order import _attempt
    from tests.test_three_candle_rejection import _history, _long_pattern
    store, bus = g
    ledger, engine, paper = _instance(tmp_path)
    assert _attempt(engine, paper, 0, "win")             # a real closed trade
    rows, i = _history(trend="down")
    _run(engine, rows + _long_pattern(i, open_=101.6))   # a filtered rejection
    _drain(bus)
    records = TradeRecordStore(str(tmp_path / "records.db"))
    recorder = JournalRecorder(records)
    recorder.add_ledger(LedgerSource("MAIN", ledger))
    assert not recorder.reconcile()["errors"]
    service = GuardianService(store, bus, performance_path=str(tmp_path / "records.db"))
    view = service.strategies(days=3650)
    [card] = [c for c in view["strategies"] if c["scope"] == "instance:inst-s"]
    assert card["strategy_id"] == STRATEGY and card["evaluations"] == 7
    assert card["entries"] == 1 and card["almost_trades"] == 1
    assert {"decision": "NO_SETUP", "code": "EMA_TREND_NOT_ALIGNED", "count": 1} in card["top_rejection_reasons"]
    [perf] = card["performance"]
    assert (perf["trades"], perf["wins"], perf["losses"]) == (1, 1, 0)
    [closed] = records.query_trades()
    assert perf["net_pnl"] == pytest.approx(closed["net_pnl"], abs=0.01)
    assert view["research"]["built"] is False             # Phase 5 is not pretended


def test_a_lab_that_cannot_be_read_is_reported_not_guessed(tmp_path):
    store = GuardianStore()
    bus = EventBus(store)
    bad = tmp_path / "not-a-db"
    bad.write_text("this is not sqlite")
    service = GuardianService(store, bus, telemetry=StrategyTelemetry(
        store, [SMCDecisionReader(str(bad))]))
    nodes = service.cycle()
    bus.flush()
    assert nodes["guardian"].raw == "DEGRADED" and "smc" in nodes["guardian"].detail
    failed = store.events(event_type="collector_failed")
    assert failed and failed[0]["lab_id"] == "smc"
    assert store.events(category="strategy") == []       # nothing invented


def test_almost_trade_rules_are_structural():
    one_short = {"final": "NO_SETUP", "conditions": [
        {"kind": "strategy", "state": "PASS", "stage": "SETUP"},
        {"kind": "strategy", "state": "PASS", "stage": "SETUP"},
        {"kind": "strategy", "state": "FAIL", "stage": "CONFIRMATION", "code": "MISSING"}]}
    assert almost_trade(one_short)["kind"] == "ONE_CONDITION_SHORT"
    two_short = {**one_short, "conditions": one_short["conditions"] + [
        {"kind": "strategy", "state": "FAIL", "stage": "SETUP", "code": "MISSING"}]}
    assert almost_trade(two_short) is None
    warmup = {**one_short, "conditions": one_short["conditions"][:2] + [
        {"kind": "strategy", "state": "FAIL", "stage": "FEATURES", "code": "WARMUP"}]}
    assert almost_trade(warmup) is None
    invalidated = {**one_short, "conditions": one_short["conditions"][:2] + [
        {"kind": "strategy", "state": "FAIL", "stage": "SETUP", "code": "INVALIDATED"}]}
    assert almost_trade(invalidated) is None
    refused = {"final": "REJECTED", "conditions": one_short["conditions"][:2],
               "blocking": [{"id": "risk", "code": "DAILY_LOSS_LIMIT"}]}
    assert almost_trade(refused)["kind"] == "VALID_SETUP_REFUSED"
    assert almost_trade({**refused, "blocking": [{"code": "DUPLICATE_SIGNAL"}]}) is None
    assert almost_trade({**refused, "blocking": [{"code": "A"}, {"code": "B"}]}) is None


def test_the_guardian_strategy_api(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webhook_api
    import routers.guardian  # noqa: F401
    app = FastAPI()
    app.include_router(webhook_api.router)
    client = TestClient(app)
    view = client.get("/guardian/strategies", params={"days": 7}).json()
    assert {"strategies", "performance", "research", "telemetry"} <= set(view)
    assert client.get("/guardian/almost-trades").status_code == 200
    assert client.get("/guardian/events/does-not-exist").status_code == 404
    assert client.post("/guardian/strategies").status_code == 405
    assert timedelta  # noqa: B018
