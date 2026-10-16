"""What the agent's own history says about the setup in front of it.

The journal already records every decision and every outcome. This reads it
back before the next decision, so a pattern that has repeatedly lost can stop
being taken -- the thing a trader does without noticing and a rule-following
system never does.

It is the most dangerous of the agent's rules, and the danger is not the code:
it is that a small sample will always show a pattern. Eight trades will
produce a "setup that loses at 3am" whether or not one exists, and a filter
built on that removes real edge while looking like discipline. So:

**No opinion below the sample floor.** Under ``min_sample`` the gate passes
and says it had no basis. That is not the same failure mode as an input that
went missing -- a rule about history with no history has correctly nothing to
say, where a volatility rule that cannot see candles has been asked a question
it must not answer.

**Expectancy, not win rate.** A 30%-win setup at 4R is excellent and a
70%-win setup at 0.3R is not. Judging on win rate alone would veto exactly the
setups the 1:3 floor exists to find.

**It can only veto.** Like every context rule, the worst a bug here can do is
trade less. It cannot raise a size, take a setup the strategy did not offer,
or clear a gate another rule raised.

The scope deliberately stops at the setup family and, optionally, the hour.
Slicing further -- by day, by direction, by month -- multiplies the buckets and
guarantees that one of them looks damning on noise alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from services.smc_agent import Gate

PATTERN_EXPECTANCY = "pattern_expectancy"

#: A setup id looks like SMC_S5_ORDER_BLOCK_RETEST-BTCUSDT-5M-BULLISH-<stamp>.
#: The family is the part before the first dash: the pattern, without the
#: instrument, direction or moment that made this one unique.
FAMILY_SEPARATOR = "-"


@dataclass(frozen=True)
class MemoryPolicy:
    enabled: bool = False
    #: Closed trades of the same family required before the rule may speak.
    min_sample: int = 20
    #: Veto when realised expectancy is at or below this, in R per trade.
    #: 0.0 means "only veto a family that has actually lost money".
    veto_at_or_below_expectancy_r: float = 0.0
    #: Also split the history by hour of day. Off by default: it multiplies
    #: the buckets, so it needs far more history to mean anything.
    by_hour: bool = False

    def validated(self) -> "MemoryPolicy":
        if self.min_sample < 5:
            raise ValueError(
                "min_sample below 5 is not a sample, it is an anecdote")
        return self


@dataclass(frozen=True)
class Recall:
    """What the journal says about this family. Never an instruction."""

    family: str
    sample: int
    wins: int
    losses: int
    expectancy_r: Optional[float]
    sufficient: bool

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.sample if self.sample else None


def family_of(setup_id: str) -> str:
    text = str(setup_id or "").strip()
    return text.split(FAMILY_SEPARATOR, 1)[0] if text else ""


def recall(closed_trades: Sequence[dict], *, family: str,
           policy: MemoryPolicy, hour: Optional[int] = None) -> Recall:
    """Summarise the closed trades of one family.

    Trades with no realised R are skipped rather than counted as scratches:
    an unrecorded outcome is not a zero, and averaging one in would drag the
    expectancy toward nothing.
    """
    matched = []
    for trade in closed_trades:
        if family_of(trade.get("setup_id") or "") != family:
            continue
        realised = trade.get("realised_r")
        if realised is None:
            continue
        if policy.by_hour and hour is not None:
            closed_at = str(trade.get("closed_at") or "")
            try:
                if int(closed_at[11:13]) != hour:
                    continue
            except (ValueError, IndexError):
                continue
        matched.append(float(realised))

    sample = len(matched)
    wins = sum(1 for value in matched if value > 0)
    losses = sum(1 for value in matched if value < 0)
    expectancy = sum(matched) / sample if sample else None
    return Recall(family=family, sample=sample, wins=wins, losses=losses,
                  expectancy_r=expectancy,
                  sufficient=sample >= policy.min_sample)


def memory_gates(*, closed_trades: Sequence[dict], setup_id: str,
                 policy: MemoryPolicy, hour: Optional[int] = None) -> list[Gate]:
    """The memory veto, as a gate. Empty when the policy is off."""
    if not policy.enabled:
        return []
    family = family_of(setup_id)
    if not family:
        # Nothing to look up. The rule has no question to answer, which is
        # not the same as answering "no".
        return [Gate(name=PATTERN_EXPECTANCY, passed=True,
                     detail="this signal carries no setup id to recall")]

    seen = recall(closed_trades, family=family, policy=policy, hour=hour)
    scope = family + (f" at {hour:02d}:00 UTC" if policy.by_hour and hour is not None
                      else "")
    if not seen.sufficient:
        return [Gate(
            name=PATTERN_EXPECTANCY, passed=True,
            detail=(f"{seen.sample} closed trade(s) for {scope}; "
                    f"{policy.min_sample} needed before history may veto"),
            value=float(seen.sample), limit=float(policy.min_sample))]

    expectancy = seen.expectancy_r or 0.0
    passed = expectancy > policy.veto_at_or_below_expectancy_r
    return [Gate(
        name=PATTERN_EXPECTANCY, passed=passed,
        detail=(f"{scope} has returned {expectancy:+.2f}R per trade over "
                f"{seen.sample} closed trades ({seen.wins}W/{seen.losses}L)"
                + ("" if passed else
                   f"; at or below the {policy.veto_at_or_below_expectancy_r:+.2f}R floor")),
        value=expectancy, limit=policy.veto_at_or_below_expectancy_r)]
