"""A trade record holds the strategy's own account of its signal as data.

The 3-Candle Rejection strategy's reason read "3-candle rejection at support
100 (2 touches) · EMA9 > EMA33 ..." -- and that sentence was the only place
the level and its touches existed on the record. The engine now freezes the
strategy's decision_report() with the signal. Everything here runs the real
strategy through AutoStrategyEngine, the pipeline and a forward fill.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.types import Bar
from data.decision_store import DecisionStore
from data.ledger import SqliteLedger
from data.trade_record_store import TradeRecordStore
from execution.paper_engine import ForwardPaperExecutionEngine
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.journal_recorder import JournalRecorder, LedgerSource
from services.signal_pipeline import SignalPipeline
from services.strategy_factory import make_builtin_strategy
from services.trading_instances import InstanceLedger
from tests.test_three_candle_rejection import _history, _long_pattern

TF = timedelta(minutes=5)


def _trade(tmp_path, strategy):
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    decisions = DecisionStore(str(tmp_path / "decisions.db"))
    scoped = InstanceLedger(ledger, "inst-1", "sess-1")
    paper = ForwardPaperExecutionEngine(scoped, 10_000)
    pipe = SignalPipeline(scoped, paper, TradingControl(), equity=10_000,
                          risk_per_trade_pct=0.01, exposure_limit_pct=0.05)
    pipe.journal_context = {"instance_id": "inst-1", "simulation_session_id": "sess-1",
                            "market_data_mode": "forward_paper"}
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
    return store.get(row["journal_record_id"])


def test_the_strategys_level_and_touches_are_on_the_record_as_data(tmp_path):
    strategy = make_builtin_strategy("three_candle_rejection", "BTCUSDT")
    record = _trade(tmp_path, strategy)
    report = record["evidence"]["strategy_report"]
    event = strategy.events[-1]                           # the strategy's own rejection event
    assert report["decision"] == "ENTER" and report["direction"] == "long"
    assert report["level"] == event.level == 100.0
    assert report["level_touches"] == 2
    assert f"support {report['level']:g} ({report['level_touches']} touches)" in report["reason"]


def test_a_strategy_whose_report_fails_still_trades_and_records_no_report(tmp_path):
    strategy = make_builtin_strategy("three_candle_rejection", "BTCUSDT")
    real = strategy.decision_report
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        if calls["n"] > 2:                          # the signal candle's report
            raise RuntimeError("report unavailable")
        return real()
    strategy.decision_report = failing
    record = _trade(tmp_path, strategy)
    assert record["status"] == "OPEN" and record["actual_entry"] is not None
    assert record["evidence"]["strategy_report"] is None
