"""Context vetoes: the setups a human would pass on.

The agent's existing gates judge one plan in isolation -- is the
reward-to-risk there, is the size legal. A trader also declines setups that
pass on their own merits, because of everything around them: it is the third
loss today, it is 3am and the book is thin, the range has gone flat.

Every rule here can only ever SKIP a trade. None of them can make the agent
take one, raise a size, or overrule the SMC strategy into acting -- the
strategy decides what is on offer and this decides whether to stand aside.
That asymmetry is the whole safety argument for the module: the worst a bug
here can do is trade less.

A veto is a REJECTED decision, not a MISSED one. MISSED means the strategy
offered a trade and the agent failed to act, which is a fault to investigate;
this is the agent choosing, which is the job. They are separate outcomes in
the journal and must not blur, or the one row that means "something is
broken" gets lost among the rows that mean "working as intended".

Off by default, every rule independently. Each one reduces trade count, and a
filter that removes losers on your sample and winners on the next is the
easiest thing in this system to fool yourself with. These are hypotheses to
backtest, not improvements.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from services.smc_agent import Gate

DAILY_LOSS_CAP = "daily_loss_cap"
CONSECUTIVE_LOSSES = "consecutive_losses"
SESSION_HOURS = "session_hours"
MINIMUM_VOLATILITY = "minimum_volatility"


@dataclass(frozen=True)
class ContextPolicy:
    """Which context rules are live, and at what thresholds."""

    enabled: bool = False
    #: Stop for the day once the day's realised R is at or below -this.
    #: Measured in R, not currency, so it means the same at any equity.
    daily_loss_cap_r: Optional[float] = None
    #: Stand down after this many losing trades in a row.
    max_consecutive_losses: Optional[int] = None
    #: Trade only inside these UTC hour ranges, each [start, end). Empty
    #: means every hour is allowed.
    allowed_hours_utc: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    #: Require the recent average candle range to be at least this many basis
    #: points of price. A range that has gone flat produces setups whose stop
    #: is inside the noise.
    min_candle_range_bps: Optional[float] = None
    volatility_lookback: int = 10

    def validated(self) -> "ContextPolicy":
        if self.daily_loss_cap_r is not None and self.daily_loss_cap_r <= 0:
            raise ValueError("daily_loss_cap_r is a positive magnitude of loss")
        if self.max_consecutive_losses is not None and self.max_consecutive_losses < 1:
            raise ValueError("max_consecutive_losses must be at least one")
        if self.min_candle_range_bps is not None and self.min_candle_range_bps < 0:
            raise ValueError("min_candle_range_bps may not be negative")
        if self.volatility_lookback < 1:
            raise ValueError("volatility_lookback must be at least one candle")
        for start, end in self.allowed_hours_utc:
            if not (0 <= start <= 23 and 1 <= end <= 24):
                raise ValueError(f"hour range {start}-{end} is outside 0-24")
            if start >= end:
                raise ValueError(
                    f"hour range {start}-{end} does not move forward; a window "
                    "spanning midnight is written as two ranges")
        return self


def _day_realised_r(closed_trades: Sequence[dict], day: str) -> float:
    total = 0.0
    for trade in closed_trades:
        closed_at = str(trade.get("closed_at") or "")
        if closed_at[:10] != day:
            continue
        realised = trade.get("realised_r")
        if realised is not None:
            total += float(realised)
    return total


def _leading_losses(closed_trades: Sequence[dict]) -> int:
    """Losses at the head of the most-recent-first list.

    A scratch (exactly 0R) breaks the streak rather than extending it: it was
    not a loss, and counting it as one would stand the agent down over a run
    of breakeven exits.
    """
    streak = 0
    for trade in closed_trades:
        realised = trade.get("realised_r")
        if realised is None or float(realised) >= 0:
            break
        streak += 1
    return streak


def _average_range_bps(candles: Sequence[dict], lookback: int) -> Optional[float]:
    rows = list(candles)[-lookback:]
    ratios = []
    for row in rows:
        try:
            high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        except (KeyError, TypeError, ValueError):
            return None
        if close <= 0:
            return None
        ratios.append((high - low) / close * 10_000.0)
    return sum(ratios) / len(ratios) if ratios else None


def context_gates(*, now: Optional[datetime] = None,
                  closed_trades: Sequence[dict] = (),
                  candles: Sequence[dict] = (),
                  policy: ContextPolicy) -> list[Gate]:
    """The context vetoes, as gates. Empty when the policy is off.

    ``closed_trades`` is most-recent-first, as the journal returns them. Each
    gate is returned whether it passed or failed so the journal can show what
    was checked, not only what stopped the trade.
    """
    if not policy.enabled:
        return []
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    gates: list[Gate] = []

    if policy.daily_loss_cap_r is not None:
        today = _day_realised_r(closed_trades, moment.date().isoformat())
        limit = -abs(policy.daily_loss_cap_r)
        gates.append(Gate(
            name=DAILY_LOSS_CAP, passed=today > limit,
            detail=(f"the day is at {today:+.2f}R against a {limit:+.2f}R stop"
                    + ("" if today > limit else "; no more trades today")),
            value=today, limit=limit))

    if policy.max_consecutive_losses is not None:
        streak = _leading_losses(closed_trades)
        gates.append(Gate(
            name=CONSECUTIVE_LOSSES, passed=streak < policy.max_consecutive_losses,
            detail=(f"{streak} loss(es) in a row against a limit of "
                    f"{policy.max_consecutive_losses}"),
            value=float(streak), limit=float(policy.max_consecutive_losses)))

    if policy.allowed_hours_utc:
        hour = moment.hour
        allowed = any(start <= hour < end for start, end in policy.allowed_hours_utc)
        windows = ", ".join(f"{start:02d}:00-{end:02d}:00"
                            for start, end in policy.allowed_hours_utc)
        gates.append(Gate(
            name=SESSION_HOURS, passed=allowed,
            detail=(f"{hour:02d}:00 UTC is "
                    + ("inside" if allowed else "outside")
                    + f" the traded session ({windows})"),
            value=float(hour)))

    if policy.min_candle_range_bps is not None:
        average = _average_range_bps(candles, policy.volatility_lookback)
        if average is None:
            # Unmeasurable volatility is not permission to trade: the rule was
            # asked for, and a gate that silently passes when its input is
            # missing is a rule that switches itself off in the dark.
            gates.append(Gate(
                name=MINIMUM_VOLATILITY, passed=False,
                detail="recent candle range could not be measured",
                limit=policy.min_candle_range_bps))
        else:
            gates.append(Gate(
                name=MINIMUM_VOLATILITY,
                passed=average >= policy.min_candle_range_bps,
                detail=(f"average range over the last {policy.volatility_lookback} "
                        f"candles is {average:.1f} bps against a "
                        f"{policy.min_candle_range_bps:.1f} bps floor"),
                value=average, limit=policy.min_candle_range_bps))

    return gates
