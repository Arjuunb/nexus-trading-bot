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
