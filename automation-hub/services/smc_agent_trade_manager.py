"""In-trade stop management for trades the agent opened.

The agent was human-like at the entry and nowhere after it: it placed the
order and walked away. A trader does not do that -- they move the stop to
breakeven once the trade has paid for itself, and they trail it behind
structure while the runner runs.

What this module does NOT do, deliberately:

**It does not take partials.** The lab already does. On an entry fill it
places a reduce-only limit at target_1 for half the filled quantity -- the
"50% scale-out at deterministic T1" in smc_strategy_lab -- driven by the
target_1 the SMC strategy put in the plan. A second partial mechanism here
would double-sell the same position and make the journal's size disagree with
the book.

**It does not move the target.** The runner keeps the reward-to-risk the agent
committed to when it took the trade, so the 1:3 minimum still means what it
said at entry. Raising a target mid-trade would let a trade be journaled at
one reward-to-risk and closed at another.

**It never widens a stop.** This is the guard the rest of the module exists to
serve. A stop that can move away from entry increases the risk already written
into the journal, which makes the recorded R meaningless and turns a 1R loss
into an unbounded one. Every path here ends in `_no_wider`, and a move that is
not strictly favourable is returned as no move at all.

The decision is a pure function of closed candles and the trade's own numbers,
so it is testable without a socket, a broker or a clock -- and so it cannot
act on the forming candle, which is display-only everywhere else in this
system and must not become an execution input here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

#: Why a stop moved. Recorded per move so the journal can answer "what
#: happened to that trade" without re-deriving it from prices.
BREAKEVEN = "BREAKEVEN"
TRAIL = "TRAIL_STRUCTURE"


@dataclass(frozen=True)
class TradeManagementPolicy:
    """How the agent manages a position it already owns.

    Off by default. Every field here changes the distribution of outcomes --
    moving a stop to breakeven converts some winners into scratches and some
    losers into scratches, and which of those dominates is an empirical
    question about YOUR setups, not something this module can assert. It is a
    hypothesis to backtest and forward-test, never an improvement.
    """

    enabled: bool = False
    #: Move the stop to entry once the trade has gone this many R in favour.
    breakeven_at_r: Optional[float] = 1.0
    #: Where to put it, in R from entry. 0.0 is entry exactly. A small
    #: positive value covers fees and spread so a "breakeven" exit is not a
    #: small loss; it also means the stop sits above entry, which is still a
    #: favourable move and so still allowed.
    breakeven_offset_r: float = 0.0
    #: Start trailing behind structure once the trade is this many R in
    #: favour. None disables trailing entirely.
    trail_after_r: Optional[float] = None
    #: How many closed candles the structural stop looks back over.
    trail_lookback: int = 3
    #: Keep the stop this many R away from the structural extreme, so it sits
    #: behind the swing rather than exactly on it.
    trail_buffer_r: float = 0.1

    def validated(self) -> "TradeManagementPolicy":
        if self.breakeven_at_r is not None and self.breakeven_at_r <= 0:
            raise ValueError("breakeven_at_r must be positive")
        if self.trail_after_r is not None and self.trail_after_r <= 0:
            raise ValueError("trail_after_r must be positive")
        if self.trail_lookback < 1:
            raise ValueError("trail_lookback must be at least one candle")
        if self.breakeven_offset_r < 0:
            raise ValueError("breakeven_offset_r may not be negative — a "
                             "'breakeven' stop below entry is a losing stop")
        if self.trail_buffer_r < 0:
            raise ValueError("trail_buffer_r may not be negative")
        return self


@dataclass(frozen=True)
class OpenTrade:
    """What the agent committed to, plus where the stop is now.

    ``entry`` and ``original_stop`` come from the JOURNAL, not from the live
    position: once the stop has been moved the position can no longer say what
    R was, and every threshold here is measured in the R the agent actually
    took the trade at.
    """

    symbol: str
    side: str                    # "buy" (long) or "sell" (short)
    entry: float
    original_stop: float
    current_stop: float

    @property
    def risk(self) -> float:
        return abs(self.entry - self.original_stop)

    @property
    def is_long(self) -> bool:
        return self.side.lower() in {"buy", "long", "bullish"}


@dataclass(frozen=True)
class StopMove:
    to_price: float
    reason: str
    progress_r: float
    detail: str


@dataclass(frozen=True)
class Candle:
    high: float
    low: float
    close: float


def favourable_excursion_r(trade: OpenTrade, candles: Sequence[Candle]) -> Optional[float]:
    """How far the trade has gone in favour, in R, over CLOSED candles only.

    None when R cannot be measured, which is treated everywhere below as "do
    nothing": a trade whose risk is zero or unknown has no scale to move a
    stop against.
    """
    if trade.risk <= 0 or not candles:
        return None
    if trade.is_long:
        best = max(candle.high for candle in candles)
        return (best - trade.entry) / trade.risk
    best = min(candle.low for candle in candles)
    return (trade.entry - best) / trade.risk


def _no_wider(trade: OpenTrade, candidate: float) -> Optional[float]:
    """Return the candidate only if it is strictly favourable.

    The single rule this module cannot get wrong. For a long, a stop may only
    rise; for a short, only fall. Anything else is widening the risk the
    journal already recorded.
    """
    if trade.is_long:
        return candidate if candidate > trade.current_stop else None
    return candidate if candidate < trade.current_stop else None


def _would_close_immediately(trade: OpenTrade, candidate: float,
                             last_close: float) -> bool:
    """A stop placed the wrong side of price is an instant market exit.

    Trailing off a structural extreme can produce one when price has already
    retraced through that level, and the caller must not send it: it would
    close the position at the next tick and be recorded as a stop-out rather
    than as the mistake it is.
    """
    return candidate >= last_close if trade.is_long else candidate <= last_close


def plan_stop_move(trade: OpenTrade, candles: Sequence[Candle],
                   policy: TradeManagementPolicy) -> Optional[StopMove]:
    """The one stop move this trade warrants now, or None.

    Breakeven and trail are both evaluated and the more protective of the two
    wins, so a trade that ran far enough to trail does not first step back to
    breakeven. Order of evaluation therefore cannot change the outcome.
    """
    if not policy.enabled:
        return None
    progress = favourable_excursion_r(trade, candles)
    if progress is None:
        return None

    risk, candidates = trade.risk, []

    if policy.breakeven_at_r is not None and progress >= policy.breakeven_at_r:
        offset = policy.breakeven_offset_r * risk
        price = trade.entry + offset if trade.is_long else trade.entry - offset
        candidates.append((price, BREAKEVEN,
                           f"{progress:.2f}R reached; stop to entry"
                           + (f" +{policy.breakeven_offset_r:.2f}R" if offset else "")))

    if policy.trail_after_r is not None and progress >= policy.trail_after_r:
        recent = list(candles)[-policy.trail_lookback:]
        if recent:
            buffer_distance = policy.trail_buffer_r * risk
            if trade.is_long:
                price = min(candle.low for candle in recent) - buffer_distance
            else:
                price = max(candle.high for candle in recent) + buffer_distance
            candidates.append((price, TRAIL,
                               f"{progress:.2f}R reached; stop behind the last "
                               f"{len(recent)} closed candles"))

    if not candidates:
        return None

    # The most protective candidate, then the widening guard. Taking the best
    # first means a trail that is tighter than breakeven wins, and a trail
    # that is looser never drags the stop back down.
    best = (max(candidates, key=lambda row: row[0]) if trade.is_long
            else min(candidates, key=lambda row: row[0]))
    price = _no_wider(trade, best[0])
    if price is None:
        return None
    if _would_close_immediately(trade, price, candles[-1].close):
        return None
    return StopMove(to_price=price, reason=best[1], progress_r=progress,
                    detail=best[2])
