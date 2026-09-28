"""The losing streak and the strategy's health are read from its latest trades.

The paper ledger returns closed trades newest first. The engine passed that
list to consecutive_losses() and StrategyHealthMonitor, which both read the
END of the list as the latest trade -- so they looked at the oldest trades.
Five fresh losses after an earlier win showed no streak, and the losing-streak
cooldown (a safety block that applies even with the quality gate off) never
paused the symbol. An old losing streak followed by a win never ended, and
blocked the symbol for good. Everything here trades the real 3-Candle
Rejection strategy through AutoStrategyEngine, the pipeline and the paper
engine, with the quality gate off exactly as an owner can set it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.types import Bar
from data.ledger import SqliteLedger
from execution.paper_engine import PaperExecutionEngine
from risk.daily_limits import consecutive_losses
from services.auto_engine import AutoStrategyEngine
from services.controls import TradingControl
from services.signal_pipeline import SignalPipeline
from services.strategy_factory import make_builtin_strategy
from services.strategy_health import StrategyHealthMonitor
from services.trading_instances import InstanceLedger
from tests.test_three_candle_rejection import _history, _long_pattern

TF = timedelta(minutes=5)
T0 = datetime(2026, 3, 2, tzinfo=timezone.utc)


def _engine(tmp_path):
    ledger = SqliteLedger(str(tmp_path / "ledger.db"))
    scoped = InstanceLedger(ledger, "inst-s", "sess-1")
    paper = PaperExecutionEngine(scoped, 10_000)
    pipe = SignalPipeline(scoped, paper, TradingControl(), equity=10_000,
                          risk_per_trade_pct=0.01, exposure_limit_pct=0.05)
    engine = AutoStrategyEngine(
        pipe, paper, scoped, symbols=["BTCUSDT"], timeframe="5m", live=False,
        strategy_factory=lambda s: make_builtin_strategy("three_candle_rejection", s),
        fetcher=lambda *a, **k: ([], "replay"), entry_mode="market", instance_id="inst-s")
    engine.quality_gate_bypass = lambda: True     # gate off: only its safety blocks apply
    return ledger, engine, paper


def _attempt(engine, paper, day: int, outcome: str) -> bool:
    """One pass of the strategy's long pattern, taken out at its target (a win)
    or its stop (a loss). True when the strategy's entry was taken."""
    rows, i = _history()
    series = rows + _long_pattern(i)
    start = T0 + timedelta(days=day)
    bars = [Bar(start + TF * k, r.open, r.high, r.low, r.close, r.volume) for k, r in enumerate(series)]
    strategy = engine.strategy_factory("BTCUSDT")
    strategy.bars.extend(bars[:-3])
    for bar in bars[-3:]:
        engine._process_bar("BTCUSDT", bar, strategy)
    pos = paper.open_position("BTCUSDT")
    if pos is None:
        return False
    t = bars[-1].timestamp + TF
    if outcome == "win":
        exit_bar = Bar(t, pos["entry"], pos["target"] + 0.2, pos["entry"] - 0.1, pos["target"], 1.0)
    else:
        exit_bar = Bar(t, pos["entry"], pos["entry"] + 0.1, pos["stop"] - 0.2, pos["stop"] - 0.1, 1.0)
    engine._process_bar("BTCUSDT", exit_bar, strategy)
    assert paper.open_position("BTCUSDT") is None
    return True


def _let_the_pause_pass(ledger, paper, hours: int = 25) -> None:
    """Move every closed trade back in time, keeping their order."""
    old = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = ledger._c.execute("SELECT id FROM paper_trades ORDER BY opened_at").fetchall()
    for k, (trade_id,) in enumerate(rows):
        ledger._c.execute("UPDATE paper_trades SET opened_at=?, closed_at=? WHERE id=?",
                          ((old + timedelta(minutes=k)).isoformat(),
                           (old + timedelta(minutes=k, seconds=30)).isoformat(), trade_id))
    ledger._c.commit()
    paper._hist_cache = None


def _last_brain_block(ledger) -> str:
    rows = ledger._c.execute("SELECT message FROM bot_logs WHERE stage='brain' "
                             "ORDER BY ts DESC LIMIT 1").fetchall()
    return rows[0][0] if rows else ""


def test_five_fresh_losses_pause_the_symbol_after_an_earlier_win(tmp_path):
    ledger, engine, paper = _engine(tmp_path)
    for day, outcome in enumerate(["win", "loss", "loss", "loss", "loss", "loss"]):
        assert _attempt(engine, paper, day, outcome), (day, outcome)
    assert engine._symbol_loss_streak("BTCUSDT") == 5
    assert not _attempt(engine, paper, 10, "win"), "the sixth entry after five losses was taken"
    block = _last_brain_block(ledger)
    assert "losing-streak cooldown (5 in a row)" in block and "resumes" in block


def test_a_win_after_the_pause_ends_the_streak(tmp_path):
    ledger, engine, paper = _engine(tmp_path)
    for day in range(5):
        assert _attempt(engine, paper, day, "loss"), day
    assert not _attempt(engine, paper, 5, "win")              # paused for 24 hours
    _let_the_pause_pass(ledger, paper)
    assert _attempt(engine, paper, 6, "win")                  # the pause ended
    assert engine._symbol_loss_streak("BTCUSDT") == 0
    assert _attempt(engine, paper, 7, "win"), "a win did not end the old losing streak"


def test_health_is_judged_on_the_latest_trades(tmp_path):
    """Five losses, then five wins: the strategy is winning now. Read from
    the wrong end it showed five consecutive losses, marked the strategy
    Degrading and cut the next entry's risk to 75%."""
    ledger, engine, paper = _engine(tmp_path)
    for day in range(5):
        assert _attempt(engine, paper, day, "loss"), day
    _let_the_pause_pass(ledger, paper)
    for day in range(6, 11):
        assert _attempt(engine, paper, day, "win"), day
    assert engine._health_factor("BTCUSDT") == 1.0
    health = StrategyHealthMonitor().evaluate(paper.history())
    assert health.recent.consecutive_losses == 0 and health.status == "Healthy"


def test_a_list_without_close_times_keeps_its_order():
    """Callers that keep their own chronological list (the bot runtime) are
    unaffected."""
    trades = [{"pnl": 5.0}, {"pnl": -1.0}, {"pnl": -2.0}]
    assert consecutive_losses(trades) == 2
    assert consecutive_losses(list(reversed(trades))) == 0
