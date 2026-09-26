"""What the Decision Brain quality gate still enforces when it is switched off.

A Trading Instance owner can turn the gate off for one instance
(services/trading_instances.py, services/auto_engine.py). The Brain's score
and its views on the setup -- higher-timeframe trend, regime, volatility --
then stop blocking entries, but the hard blocks that guard position size and
the account stay: a stop too close sizes the position up, a wide one or a
sub-1R target is a bad bet by construction, and the cooldown follows a
losing streak.

The live engine and the simulators both take the rule from here, so a
backtest of "gate off" is the same gate-off the instance runs.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Optional

from strategies.brain import BrainConfig

#: How long TradeBrain's losing-streak block lasts. It used to have no end: the
#: streak resets only on a win, and a strategy the block refuses cannot win, so
#: five losses in a row stopped a symbol for good (until the paper account was
#: reset). The owner chose a fixed pause instead.
STREAK_PAUSE = timedelta(hours=24)
STREAK_BLOCK_AT = BrainConfig().streak_block_at

#: Prefixes of TradeBrain's hard-block messages (strategies/brain.py) that
#: still apply with the gate off.
SAFETY_BLOCKS = ("reward:risk", "stop too tight", "stop too wide", "losing-streak cooldown")


def safety_blocks(verdict) -> list[str]:
    return [b for b in (getattr(verdict, "blocks", None) or []) if b.startswith(SAFETY_BLOCKS)]


class SafetyOnlyBrain:
    """TradeBrain as an instance with the gate off applies it: the verdict is
    computed in full, but only the safety blocks can refuse the entry.

    Use with ``min_score=0`` in the simulators, so the score never blocks."""

    def __init__(self, brain=None):
        if brain is None:
            from strategies.brain import TradeBrain
            brain = TradeBrain()
        self.brain = brain

    def evaluate(self, *args, **kwargs):
        verdict = self.brain.evaluate(*args, **kwargs)
        keep = safety_blocks(verdict)
        return replace(verdict, allowed=not keep, blocks=keep)


def streak_for_gate(streak: int, last_loss_at: Optional[datetime], now: datetime,
                    *, pause: timedelta = STREAK_PAUSE) -> int:
    """The loss streak the Decision Brain is shown.

    Below the block threshold it is the real streak. At or above it the block
    holds for ``pause`` after the most recent loss; once that has passed the
    Brain is shown one loss short of the threshold -- its streak penalty still
    applies, the block does not -- so the next trade can happen. A further loss
    becomes the most recent one and starts another pause. With no time for the
    last loss the block holds, as it always did.
    """
    if streak < STREAK_BLOCK_AT or last_loss_at is None:
        return streak
    return STREAK_BLOCK_AT - 1 if now - last_loss_at >= pause else streak


def streak_resumes_at(streak: int, last_loss_at: Optional[datetime],
                      *, pause: timedelta = STREAK_PAUSE) -> Optional[datetime]:
    """When a losing-streak pause ends, or None if there is none."""
    if streak < STREAK_BLOCK_AT or last_loss_at is None:
        return None
    return last_loss_at + pause
