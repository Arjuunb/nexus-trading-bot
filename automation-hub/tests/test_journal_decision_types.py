"""A decision's type comes from the codes its producer wrote, not from words.

Every case here is produced by the real code: the 3-Candle Rejection
strategy's signal through AutoStrategyEngine and SignalPipeline in each
operating mode, the frozen SMC strategy through the SMC lab in each mode, and
the pipeline's own gate_blocker() vocabulary for every gate. The journal used
to read the free-text reason first, so a Decision Brain block whose reason
said "HTF context unavailable" was filed as a feed outage.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from data.decision_store import DecisionStore
from data.ledger import SqliteLedger
from data.trade_record_store import TradeRecordStore
from execution.paper_engine import ForwardPaperExecutionEngine
from services.approvals import ApprovalStore
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.journal_labs import SMCLabProjector
from services.journal_recorder import JournalRecorder, LedgerSource, classify_decision
from services.signal_pipeline import SignalPipeline, gate_blocker
from services.strategy_factory import make_builtin_strategy
from services.trading_instances import InstanceLedger
from tests.test_three_candle_rejection import _history, _long_pattern

TF = timedelta(minutes=5)


def _engine_decision(tmp_path, configure, *, mode=None) -> dict:
    """Run the real strategy over its own fixture candles in one real engine
    and return the single decision record the journal made of the signal."""
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    decisions = DecisionStore(str(tmp_path / "decisions.db"))
    scoped = InstanceLedger(ledger, "inst-1", "sess-1")
    paper = ForwardPaperExecutionEngine(scoped, 10_000)
    pipe = SignalPipeline(scoped, paper, TradingControl(), equity=10_000,
                          risk_per_trade_pct=0.01, exposure_limit_pct=0.05)
    pipe.journal_context = {"instance_id": "inst-1", "simulation_session_id": "sess-1",
                            "market_data_mode": "forward_paper"}
    engine = AutoStrategyEngine(
        pipe, paper, scoped, symbols=["BTCUSDT"], timeframe="5m", live=True,
        strategy_factory=lambda s: make_builtin_strategy("three_candle_rejection", s),
        fetcher=lambda *a, **k: ([], "live (test)"), entry_mode="market", instance_id="inst-1")
    engine.decisions = decisions
    engine.strategy_label, engine.strategy_version = "3-Candle Rejection · EMA 9/33", "1.0.0"
    configure(engine, pipe)
    rows, i = _history()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    series = rows + _long_pattern(i)
    bars = [Bar(now - TF * (len(series) - k), r.open, r.high, r.low, r.close, r.volume)
            for k, r in enumerate(series)]
    strategy = engine.strategy_factory("BTCUSDT")
    strategy.bars.extend(bars[:-3])
    for bar in bars[-3:]:
        engine._process_bar("BTCUSDT", bar, strategy)
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    recorder = JournalRecorder(store)
    recorder.add_ledger(LedgerSource(
        "MAIN", ledger, decision_store=decisions,
        instances=(lambda: {"inst-1": {"mode": mode}}) if mode else None))
    recorder.reconcile()
    [decision] = store.query_decisions()
    assert store.query_trades() == []
    return decision


def test_a_decision_brain_block_is_a_quality_block_not_a_feed_outage(tmp_path):
    decision = _engine_decision(tmp_path, lambda e, p: setattr(e, "quality_gate_bypass", lambda: False))
    assert decision["blocker"] == "GATE_REJECTED: BRAIN"
    assert "unavailable" in decision["reason"]          # the words that used to mislead
    assert decision["decision_type"] == "QUALITY_BLOCKED"


@pytest.mark.parametrize("mode,expected", [("signal", "SIGNALS_ONLY"), ("semi", "APPROVAL_REQUIRED")])
def test_operating_modes_are_recorded_as_themselves(tmp_path, mode, expected):
    def configure(engine, _pipe):
        engine.quality_gate_bypass = lambda: True
        engine.trading_mode, engine.approvals = mode, ApprovalStore()
    assert _engine_decision(tmp_path, configure)["decision_type"] == expected


def test_an_operator_pause_is_a_risk_block(tmp_path):
    def configure(engine, pipe):
        engine.quality_gate_bypass = lambda: True
        pipe.controls.pause_all()
    decision = _engine_decision(tmp_path, configure)
    assert (decision["blocker"], decision["decision_type"]) == ("GATE_REJECTED: PAUSED", "RISK_BLOCKED")


# ──────────────── the pipeline's own vocabulary, gate by gate ────────────────
@pytest.mark.parametrize("stage,reason,expected", [
    ("brain", "Score 52/100 below minimum 60", "QUALITY_BLOCKED"),
    ("context", "higher-timeframe trend opposes the signal", "HTF_BLOCKED"),
    ("context", "volatility regime unsuitable", "CONTEXT_BLOCKED"),
    ("controls", "Trading paused", "RISK_BLOCKED"),
    ("dedup", "Duplicate alert_id within 300s", "DUPLICATE_PREVENTED"),
    ("execution", "insufficient paper capital", "ORDER_REJECTED"),
    ("risk", "stop on the wrong side of entry", "RISK_BLOCKED"),
    ("risk_guard", "exposure limit reached", "RISK_BLOCKED"),
    ("daily_loss", "daily loss limit reached", "RISK_BLOCKED"),
    ("weekly_loss", "weekly loss limit reached", "RISK_BLOCKED"),
    ("cooldown", "cooling down after consecutive losses", "RISK_BLOCKED"),
    ("max_trades", "maximum trades for the day", "RISK_BLOCKED"),
    ("correlation", "correlated with an open position", "RISK_BLOCKED"),
    ("portfolio_exposure", "portfolio exposure cap", "RISK_BLOCKED"),
    ("session", "outside the trading session", "SESSION_BLOCKED"),
    ("trading_day", "weekend trading disabled", "SESSION_BLOCKED"),
    ("event_risk", "CPI release in 10 minutes", "NEWS_BLACKOUT"),
    ("market_quality", "spread 42bps above 20bps", "QUALITY_BLOCKED"),
    ("market_quality", "signal is stale: age 900s", "STALE_DATA"),
    ("strategy", "reward to risk 1.2 below 2.0", "SETUP_REJECTED"),
])
def test_every_pipeline_gate_code_has_its_own_type(stage, reason, expected):
    assert classify_decision("GATE_REJECTED", stage, gate_blocker(stage, reason), reason) == expected


# ──────────────── the SMC lab, driven by the real SMC strategy ────────────────
RULES = {"tick_size": 0.1, "quantity_step": 0.001, "min_quantity": 0.001,
         "max_quantity": 100.0, "min_notional": 5.0}


@pytest.mark.parametrize("mode,reliable,expected", [
    ("signals_only", True, "SIGNALS_ONLY"),
    ("manual_approval", True, "APPROVAL_REQUIRED"),
    ("automatic", False, "STALE_DATA"),
])
def test_smc_lab_candidates_keep_their_meaning(tmp_path, mode, reliable, expected):
    from services.smc_strategy_lab import SMCPaperAccount, SMCPaperConfig
    from services.smc_strategy_v1 import evaluate
    from tests.test_smc_strategy_ladder import seeded_engine

    account = SMCPaperAccount(str(tmp_path / "smc.db"))
    account.configure(config=SMCPaperConfig(operating_mode=mode))
    evaluation = evaluate(seeded_engine())
    account.synchronize_candidate(evaluation, rules=RULES,
                                  reference_price=evaluation["trade_plan"]["entry"],
                                  feed_reliable=reliable)
    store = TradeRecordStore()
    SMCLabProjector(account).project(store)
    [decision] = store.query_decisions()
    assert decision["decision_type"] == expected and store.query_trades() == []


# ──────────────── a trade the owner let through with the quality gate off ────────────────
def test_a_trade_taken_with_the_quality_gate_off_says_so(tmp_path):
    """The real signal is one the Decision Brain blocks. With the instance's
    quality-gate switch off it trades anyway; its record must not claim that
    every pre-trade gate passed."""
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    decisions = DecisionStore(str(tmp_path / "decisions.db"))
    scoped = InstanceLedger(ledger, "inst-1", "sess-1")
    paper = ForwardPaperExecutionEngine(scoped, 10_000)
    pipe = SignalPipeline(scoped, paper, TradingControl(), equity=10_000,
                          risk_per_trade_pct=0.01, exposure_limit_pct=0.05)
    pipe.journal_context = {"instance_id": "inst-1", "simulation_session_id": "sess-1",
                            "market_data_mode": "forward_paper"}
    engine = AutoStrategyEngine(
        pipe, paper, scoped, symbols=["BTCUSDT"], timeframe="5m", live=True,
        strategy_factory=lambda s: make_builtin_strategy("three_candle_rejection", s),
        fetcher=lambda *a, **k: ([], "live (test)"), entry_mode="market", instance_id="inst-1")
    engine.decisions = decisions
    engine.quality_gate_bypass = lambda: True
    rows, i = _history()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    series = rows + _long_pattern(i)
    bars = [Bar(now - TF * (len(series) - k), r.open, r.high, r.low, r.close, r.volume)
            for k, r in enumerate(series)]
    strategy = engine.strategy_factory("BTCUSDT")
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
    record = store.get(row["journal_record_id"])
    risk = record["risk_check"]
    assert record["evidence"]["quality_gate"]["allowed"] is False     # what the Brain said
    assert risk["result"] == "PASSED_WITH_QUALITY_GATE_OFF"
    assert risk["quality_gate"]["bypassed_by_owner"] is True
    assert risk["quality_gate"]["would_have_blocked_for"] == record["evidence"]["quality_gate"]["blocks"]
    assert "every pre-trade gate passed;" not in risk["basis"]


def test_a_trade_the_quality_gate_allowed_is_plain_passed(tmp_path):
    from tests.test_journal_integrity import Env
    env = Env(tmp_path)
    env.entry()
    env.fill()
    env.recorder().reconcile()
    [row] = env.store.query_trades()
    risk = env.store.get(row["journal_record_id"])["risk_check"]
    assert risk["result"] == "PASSED" and "quality_gate" not in risk


# ──────────────── which market data a decision ran on ────────────────
def _brain_blocked(engine, _pipe):
    engine.quality_gate_bypass = lambda: False


def test_a_replay_instances_decision_is_simulation(tmp_path):
    assert _engine_decision(tmp_path, _brain_blocked, mode="replay")["record_origin"] == "SIMULATION"


def test_a_trading_instances_decision_is_forward_paper(tmp_path):
    assert _engine_decision(tmp_path, _brain_blocked, mode="trading")["record_origin"] == "FORWARD_PAPER"


def test_a_decision_with_nothing_to_show_its_data_is_not_called_forward_paper(tmp_path):
    assert _engine_decision(tmp_path, _brain_blocked)["record_origin"] == "LEGACY_MIGRATION"


def test_a_decision_that_opened_a_trade_takes_the_trades_origin(tmp_path):
    """The trade's origin was proven from its fill: a forward quote after the
    decision. No instance metadata is needed, and none is given."""
    from tests.test_journal_integrity import Env
    env = Env(tmp_path)
    env.decisions.record({"symbol": "BTCUSDT", "timeframe": "5m", "strategy": "3CR", "side": "long",
                          "decision": "accepted", "reason": "", "instance_id": env.instance_id,
                          "ts": datetime.now(timezone.utc).isoformat(), "decision_identity": "d-1",
                          "final_state": "PENDING_INTENT", "gate_stage": "execution"})
    env.entry()
    env.fill()
    env.recorder().reconcile()
    [decision] = env.store.query_decisions()
    [trade] = env.store.query_trades()
    assert decision["journal_record_id"] == trade["journal_record_id"]
    assert decision["record_origin"] == trade["record_origin"] == "FORWARD_PAPER"


# ──────────────── a restart re-evaluates the candle that already traded ────────────────
def test_a_restarts_duplicate_refusal_does_not_overwrite_the_decision_that_traded(tmp_path):
    """The decision store keeps one row per candle. A worker restarted after
    its trade re-evaluates that candle, the pipeline refuses the replay as a
    duplicate, and that refusal is written over the row that opened the trade.
    The journal keeps what happened first and the refusal beside it."""
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    decisions = DecisionStore(str(tmp_path / "decisions.db"))
    rows, i = _history()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    series = rows + _long_pattern(i)
    bars = [Bar(now - TF * (len(series) - k), r.open, r.high, r.low, r.close, r.volume)
            for k, r in enumerate(series)]

    def worker(intents=None):
        scoped = InstanceLedger(ledger, "inst-1", "sess-1")
        paper = ForwardPaperExecutionEngine(scoped, 10_000, initial_intents=intents)
        pipe = SignalPipeline(scoped, paper, TradingControl(), equity=10_000,
                              risk_per_trade_pct=0.01, exposure_limit_pct=0.05)
        pipe.journal_context = {"instance_id": "inst-1", "simulation_session_id": "sess-1",
                                "market_data_mode": "forward_paper"}
        engine = AutoStrategyEngine(
            pipe, paper, scoped, symbols=["BTCUSDT"], timeframe="5m", live=True,
            strategy_factory=lambda s: make_builtin_strategy("three_candle_rejection", s),
            fetcher=lambda *a, **k: ([], "live (test)"), entry_mode="market", instance_id="inst-1")
        engine.decisions, engine.quality_gate_bypass = decisions, (lambda: True)
        strategy = engine.strategy_factory("BTCUSDT")
        strategy.bars.extend(bars[:-3])
        for bar in bars[-3:]:
            engine._process_bar("BTCUSDT", bar, strategy)
        return paper

    paper = worker()
    assert paper.process_quote({"bid": 102.2, "ask": 102.22, "mark": 102.21,
                                "received_at": datetime.now(timezone.utc).isoformat()})
    worker()                                            # the restart sees the same candles
    [upstream] = decisions._c.execute("SELECT gate_stage, blocker FROM decisions").fetchall()
    assert tuple(upstream) == ("dedup", "GATE_REJECTED: DUPLICATE_SIGNAL")   # overwritten upstream

    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    recorder = JournalRecorder(store)
    recorder.add_ledger(LedgerSource("MAIN", ledger, decision_store=decisions))
    recorder.reconcile()
    [trade] = store.query_trades()
    [decision] = store.query_decisions()
    assert decision["journal_record_id"] == trade["journal_record_id"]
    assert (decision["decision_type"], decision["status"], decision["blocker"]) == \
        ("TRADE_OPENED", "FILLED", None)
    assert "order filled" in decision["reason"]
    assert decision["evidence"]["duplicate_attempt"]["blocker"] == "GATE_REJECTED: DUPLICATE_SIGNAL"
