"""An old evolution counter is VERIFIED only when forward-paper trades back it.

The old evolution memory is written by the synchronous paper path. A Trading
Instance in replay mode fills through that same path, so its trades on
replayed candles increment the counters too. The audit found such a counter
labelled VERIFIED because its trades exist in the ledger. Here the trades
are real: the 3-Candle Rejection strategy through AutoStrategyEngine,
SignalPipeline and the paper engine, with the decision journal attached
exactly as the platform attaches it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.types import Bar
from data.journal_store import JournalStore
from data.ledger import SqliteLedger
from data.trade_record_store import TradeRecordStore
from execution.paper_engine import PaperExecutionEngine
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.decision_journal import DecisionJournal
from services.journal_legacy import LegacyJournalMigration, evolution_provenance
from services.journal_recorder import JournalRecorder, LedgerSource
from services.signal_pipeline import SignalPipeline
from services.strategy_factory import make_builtin_strategy
from services.trading_instances import InstanceLedger
from tests.test_three_candle_rejection import _history, _long_pattern

TF = timedelta(minutes=5)


def _trade(ledger, journal, instance_id: str, market_data_mode: str, start: datetime) -> None:
    """One real 3-Candle Rejection long, entered and taken out at its target."""
    scoped = InstanceLedger(ledger, instance_id, f"sess-{instance_id}")
    paper = PaperExecutionEngine(scoped, 10_000)
    pipe = SignalPipeline(scoped, paper, TradingControl(), equity=10_000,
                          risk_per_trade_pct=0.01, exposure_limit_pct=0.05)
    pipe.journal = journal
    pipe.journal_context = {"instance_id": instance_id, "simulation_session_id": f"sess-{instance_id}",
                            "strategy_id": "three_candle_rejection",
                            "strategy_name": "3-Candle Rejection · EMA 9/33",
                            "strategy_version": "1.0.0", "market_data_mode": market_data_mode}
    engine = AutoStrategyEngine(
        pipe, paper, scoped, symbols=["BTCUSDT"], timeframe="5m", live=market_data_mode == "live",
        strategy_factory=lambda s: make_builtin_strategy("three_candle_rejection", s),
        fetcher=lambda *a, **k: ([], market_data_mode), entry_mode="market", instance_id=instance_id)
    engine.strategy_label, engine.strategy_version = "3-Candle Rejection · EMA 9/33", "1.0.0"
    engine.quality_gate_bypass = lambda: True
    rows, i = _history()
    series = rows + _long_pattern(i)
    bars = [Bar(start + TF * k, r.open, r.high, r.low, r.close, r.volume) for k, r in enumerate(series)]
    strategy = engine.strategy_factory("BTCUSDT")
    strategy.bars.extend(bars[:-3])
    for bar in bars[-3:]:
        engine._process_bar("BTCUSDT", bar, strategy)
    pos = paper.open_position("BTCUSDT")
    assert pos is not None, "the strategy did not open its trade"
    t = bars[-1].timestamp + TF
    engine._process_bar("BTCUSDT", Bar(t, pos["entry"], pos["target"] + 0.2, pos["entry"] - 0.1,
                                       pos["target"], 1.0), strategy)
    assert paper.open_position("BTCUSDT") is None, "the target did not close it"


def _provenance(tmp_path, runs) -> dict:
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    old = JournalStore(str(tmp_path / "journal.db"))
    journal = DecisionJournal(old)
    start = datetime(2026, 3, 2, tzinfo=timezone.utc)
    for n, (instance_id, mode) in enumerate(runs):
        _trade(ledger, journal, instance_id, mode, start + timedelta(days=n * 3))
    store = TradeRecordStore(str(tmp_path / "trade_records.db"))
    recorder = JournalRecorder(store)
    recorder.add_ledger(LedgerSource("MAIN", ledger))
    recorder.legacy = LegacyJournalMigration(old)
    recorder.reconcile()
    [counter] = evolution_provenance(old, store)
    return counter


def test_a_counter_built_on_replayed_candles_is_simulation_not_verified(tmp_path):
    counter = _provenance(tmp_path, [("inst-replay", "replay")])
    assert counter["trades"] == 1 and counter["verified_records"] == 1
    assert counter["backing_origins"] == {"SIMULATION": 1}
    assert counter["recorded_market_data"] == {"replay": 1}
    assert counter["provenance"] == "SIMULATION"
    assert "simulated market data" in counter["note"]


def test_a_counter_built_on_live_candles_is_verified(tmp_path):
    counter = _provenance(tmp_path, [("inst-live", "live")])
    assert counter["backing_origins"] == {"FORWARD_PAPER": 1}
    assert counter["provenance"] == "VERIFIED"


def test_a_counter_mixing_both_is_not_verified(tmp_path):
    counter = _provenance(tmp_path, [("inst-live", "live"), ("inst-replay", "replay")])
    assert counter["trades"] == 2
    assert counter["backing_origins"] == {"FORWARD_PAPER": 1, "SIMULATION": 1}
    assert counter["provenance"] == "MIXED"
