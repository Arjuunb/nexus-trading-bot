"""Execution ids drawn from the engine's counter never repeat across runs.

The replay key and the approved-idea key were "auto-{sym}-{action}-{n}" and
"approved-{sym}-{n}", with n from a counter that restarts at 1 in every
engine. The ledger keeps every earlier run's ids, and execution ids are
unique across the whole ledger. So a second replay session, a restart, or a
second replay instance on the same ledger reused an id: within five minutes
the trade was refused as a duplicate, and after that the execution insert
failed and the engine raised StrategyExecutionError, which stops the
instance. Everything here runs the real 3-Candle Rejection strategy through
AutoStrategyEngine, the signal pipeline and the paper engine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from services.approvals import ApprovalStore
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.signal_pipeline import SignalPipeline
from services.strategy_factory import make_builtin_strategy
from services.trading_instances import InstanceLedger
from tests.test_three_candle_rejection import _history, _long_pattern

TF = timedelta(minutes=5)
T0 = datetime(2026, 3, 2, tzinfo=timezone.utc)


def _engine(ledger, instance_id: str, session: str, *, live: bool = False):
    scoped = InstanceLedger(ledger, instance_id, session) if instance_id else ledger
    paper = PaperExecutionEngine(scoped, 10_000)
    pipe = SignalPipeline(scoped, paper, TradingControl(), equity=10_000,
                          risk_per_trade_pct=0.01, exposure_limit_pct=0.05)
    engine = AutoStrategyEngine(
        pipe, paper, scoped, symbols=["BTCUSDT"], timeframe="5m", live=live,
        strategy_factory=lambda s: make_builtin_strategy("three_candle_rejection", s),
        fetcher=lambda *a, **k: ([], "replay"), entry_mode="market",
        instance_id=instance_id or None)
    engine.quality_gate_bypass = lambda: True
    return engine, paper


def _signal_bars(start: datetime):
    rows, i = _history()
    series = rows + _long_pattern(i)
    return [Bar(start + TF * k, r.open, r.high, r.low, r.close, r.volume)
            for k, r in enumerate(series)]


def _trade_once(engine, paper, start: datetime) -> bool:
    """Feed the strategy's long pattern, then take the position out at its
    target. True when the strategy's trade was opened."""
    bars = _signal_bars(start)
    strategy = engine.strategy_factory("BTCUSDT")
    strategy.bars.extend(bars[:-3])
    for bar in bars[-3:]:
        engine._process_bar("BTCUSDT", bar, strategy)
    pos = paper.open_position("BTCUSDT")
    if pos is None:
        return False
    t = bars[-1].timestamp + TF
    engine._process_bar("BTCUSDT", Bar(t, pos["entry"], pos["target"] + 0.2, pos["entry"] - 0.1,
                                       pos["target"], 1.0), strategy)
    assert paper.open_position("BTCUSDT") is None
    return True


def _age_ledger(ledger, minutes: int = 10) -> None:
    """Move every recorded event back in time, past the duplicate window."""
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    ledger._c.execute("UPDATE webhook_events SET received_at=?", (old,))
    ledger._c.commit()


def _closed(ledger, instance_id: str) -> list[str]:
    return [r[0] for r in ledger._c.execute(
        "SELECT simulation_session_id FROM paper_trades WHERE instance_id=? AND status='closed' "
        "ORDER BY opened_at", (instance_id,))]


def test_a_second_replay_session_takes_its_trade(tmp_path):
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    assert _trade_once(*_engine(ledger, "inst-r", "sess-1"), T0)
    assert _trade_once(*_engine(ledger, "inst-r", "sess-2"), T0 + timedelta(days=3)), \
        "the second session's trade was refused as a duplicate of the first session's"
    assert _closed(ledger, "inst-r") == ["sess-1", "sess-2"]


def test_a_later_replay_run_does_not_stop_the_instance(tmp_path):
    """Past the five-minute duplicate window only the ledger's unique ids are
    left to catch a reused id, and the engine raised on it."""
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    assert _trade_once(*_engine(ledger, "inst-r", "sess-1"), T0)
    _age_ledger(ledger)
    assert _trade_once(*_engine(ledger, "inst-r", "sess-1"), T0 + timedelta(days=3))
    assert _closed(ledger, "inst-r") == ["sess-1", "sess-1"]


def test_two_replay_instances_on_one_ledger_both_trade(tmp_path):
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    assert _trade_once(*_engine(ledger, "inst-a", "sess-a"), T0)
    _age_ledger(ledger)
    assert _trade_once(*_engine(ledger, "inst-b", "sess-b"), T0)
    assert _closed(ledger, "inst-a") == ["sess-a"] and _closed(ledger, "inst-b") == ["sess-b"]


def test_an_approved_idea_after_a_restart_is_executed(tmp_path):
    """Semi-auto mode: the strategy's idea waits for approval, then goes
    through the same pipeline. The first approval after a restart reused the
    id of the first approval before it."""
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=2)
    for run, begin in enumerate((start, start + timedelta(days=1))):
        engine, paper = _engine(ledger, "", "", live=True)     # a restart: a new engine
        engine.trading_mode, engine.approvals = "semi", ApprovalStore()
        bars = _signal_bars(begin)
        strategy = engine.strategy_factory("BTCUSDT")
        strategy.bars.extend(bars[:-3])
        for bar in bars[-3:]:
            engine._process_bar("BTCUSDT", bar, strategy)
        [idea] = engine.approvals.list_pending()
        result = engine.execute_approved(engine.approvals.approve(idea["id"]))
        assert result["ok"] and result["action"] == "opened", (run, result)
        pos = paper.open_position("BTCUSDT")
        t = bars[-1].timestamp + TF                       # the engine's own exit at target
        engine._process_bar("BTCUSDT", Bar(t, pos["entry"], pos["target"] + 0.2,
                                           pos["entry"] - 0.1, pos["target"], 1.0), strategy)
        assert paper.open_position("BTCUSDT") is None
        _age_ledger(ledger)
    assert len([r for r in ledger._c.execute(
        "SELECT id FROM paper_trades WHERE status='closed'")]) == 2


def test_a_forward_candle_keeps_its_permanent_id(tmp_path):
    """Forward ids are the candle's own identity and must stay stable, so a
    recovered candle is refused rather than traded twice."""
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    first, _ = _engine(ledger, "inst-f", "sess-1", live=True)
    again, _ = _engine(ledger, "inst-f", "sess-1", live=True)       # after a restart
    assert first._auto_execution_id("BTCUSDT", T0, "buy") == again._auto_execution_id("BTCUSDT", T0, "buy") \
        == f"auto:inst-f:BTCUSDT:5m:{T0.isoformat()}:buy"


@pytest.mark.parametrize("action", ["buy", "close"])
def test_replay_ids_differ_between_engines(tmp_path, action):
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    one, _ = _engine(ledger, "inst-r", "sess-1")
    two, _ = _engine(ledger, "inst-r", "sess-1")
    assert one._auto_execution_id("BTCUSDT", T0, action) != two._auto_execution_id("BTCUSDT", T0, action)
