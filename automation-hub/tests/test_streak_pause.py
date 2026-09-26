"""The Decision Brain's losing-streak block is a 24-hour pause, not a lock.

TradeBrain refuses entries at 5 losses in a row, and only a win resets the
streak. A refused strategy cannot win, so the block used to last until the
paper account was reset. The owner chose a 24-hour pause from the latest loss
(services/quality_gate.py); the live engine and the simulators both apply it.
"""
from datetime import datetime, timedelta, timezone

from services.quality_gate import STREAK_BLOCK_AT, STREAK_PAUSE, streak_for_gate, streak_resumes_at
from strategies.brain import BrainVerdict
from tests.test_parity_and_cache import _engine, _history, _sig, _Stub

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


# ─────────────────────────── the rule ───────────────────────────
def test_the_rule():
    assert STREAK_BLOCK_AT == 5 and STREAK_PAUSE == timedelta(hours=24)
    # below the threshold the real streak goes through untouched
    assert streak_for_gate(4, NOW - timedelta(minutes=1), NOW) == 4
    # inside the pause the Brain sees the real streak, so it blocks
    assert streak_for_gate(5, NOW - timedelta(hours=23, minutes=59), NOW) == 5
    assert streak_for_gate(8, NOW - timedelta(hours=2), NOW) == 8
    # after it, one short of the threshold: the penalty stays, the block goes
    assert streak_for_gate(5, NOW - timedelta(hours=24), NOW) == 4
    assert streak_for_gate(9, NOW - timedelta(days=3), NOW) == 4
    # no time for the last loss: the block holds, as it always did
    assert streak_for_gate(5, None, NOW) == 5
    assert streak_resumes_at(5, NOW) == NOW + timedelta(hours=24)
    assert streak_resumes_at(4, NOW) is None and streak_resumes_at(6, None) is None


# ─────────────────────────── the live engine ───────────────────────────
class _StreakBrain:
    """TradeBrain's streak behaviour only: blocks at 5, records what it saw."""

    def __init__(self):
        self.seen = []

    def evaluate(self, *_args, recent_losses=0, **_kwargs):
        self.seen.append(recent_losses)
        blocks = [f"losing-streak cooldown ({recent_losses} in a row)"] if recent_losses >= 5 else []
        return BrainVerdict(allowed=not blocks, score=90, regime="Trending", htf_bias="bullish",
                            setup_type="trend", blocks=blocks)


def _losses(n, last_closed_at):
    return [{"symbol": "BTCUSDT", "pnl": -1.0,
             "closed_at": (last_closed_at - timedelta(hours=n - 1 - i)).isoformat()} for i in range(n)]


def _run(history):
    decisions = []
    eng, paper, _ = _engine()
    eng._quality_brain = brain = _StreakBrain()
    eng.decisions = type("Store", (), {"record": lambda _s, d: decisions.append(dict(d)) or "d1"})()
    paper._hist_cache = history
    eng._process_bar("BTCUSDT", _history(91)[-1], _Stub([_sig(tp=112.0)], bars=_history()))
    return eng, paper, brain, decisions


def test_inside_the_pause_the_entry_is_refused_and_says_when_it_resumes():
    last = datetime.now(timezone.utc) - timedelta(hours=2)
    eng, paper, brain, decisions = _run(_losses(5, last))
    assert brain.seen == [5]
    assert paper.open_position("BTCUSDT") is None
    [decision] = decisions
    assert decision["decision"] == "rejected"
    resumes = (last + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
    assert f"BTCUSDT resumes {resumes} UTC" in decision["reason"]


def test_after_the_pause_the_symbol_trades_again():
    eng, paper, brain, decisions = _run(_losses(6, datetime.now(timezone.utc) - timedelta(hours=25)))
    assert brain.seen == [4]                      # the penalty's streak, not the block's
    assert paper.open_position("BTCUSDT") is not None
    assert decisions[0]["decision"] == "accepted"


def test_a_loss_with_no_close_time_keeps_the_block():
    history = [{"symbol": "BTCUSDT", "pnl": -1.0} for _ in range(5)]
    _, paper, brain, _ = _run(history)
    assert brain.seen == [5] and paper.open_position("BTCUSDT") is None


def test_another_symbols_losses_do_not_pause_this_one():
    history = [{**row, "symbol": "ETHUSDT"} for row in _losses(5, datetime.now(timezone.utc))]
    _, paper, brain, _ = _run(history)
    assert brain.seen == [0] and paper.open_position("BTCUSDT") is not None
