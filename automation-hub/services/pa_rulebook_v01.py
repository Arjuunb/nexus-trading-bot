"""Nexus Price Action rulebook v0.1 — the pure strategy engine.

An implementation of "PRICE ACTION FOR ALGORITHMS", research specification
v0.1. Two independently testable strategies on Binance USDT-margined
perpetuals, 1H context / 15M setup / 5M confirmation:

    PA_SR_REJECTION_V01   a trend-aligned pullback rejects an existing zone
    PA_FLIP_RETEST_V01    a trend-aligned breakout is followed by a retest

"Pure" is the rulebook's own word and its own requirement: "Use the same pure
strategy engine in backtest and paper execution", and "external clocks must be
injected rather than read unpredictably inside strategy functions". So nothing
here reads a clock, touches a database, opens a socket or mutates a global.
Every function is a deterministic function of validated closed candles plus an
injected decision time. Replaying the same event sequence reproduces identical
decisions, which is what makes a backtest and a forward run comparable at all.

It is also pure in the trader's sense. The rulebook is explicit: "Do not
automatically add EMA, volume, FVG, order blocks, sessions and ten score
weights. Every added filter changes the hypothesis and reduces sample size."
The only derived series here is ATR, which the specification mandates. There is
no indicator stack, no score, no confluence count.

Status, carried from the document and not softened: this is a research
hypothesis, not a proven edge. Every threshold is an initial engineering
choice. No backtest or forward experiment supports it. The design must not
route exchange orders.

What this module does NOT do, by design: it does not decide whether exposure is
allowed and it does not decide whether an intent can fill. The rulebook keeps
three systems separate -- "The strategy decides whether market evidence
qualifies. The risk engine decides whether exposure is allowed. The paper
broker decides whether an intent can fill." This is only the first of the
three, and it returns evidence for the other two rather than acting for them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, Mapping, Optional, Sequence

from bot.types import Bar

__all__ = [
    "SR_REJECTION_ID", "FLIP_RETEST_ID", "RULEBOOK_VERSION",
    "Regime", "SetupState", "Blocker",
    "RulebookConfig", "CandleFeatures", "Pivot", "Zone", "Setup", "TradePlan",
    "CostModel", "Decision",
    "features", "atr_series", "previous_atr", "confirmed_pivots",
    "is_bullish_rejection", "is_bearish_rejection", "is_dominance",
    "classify_regime", "build_zones", "build_trade_plan",
    "PriceActionRulebookEngine",
]

SR_REJECTION_ID = "PA_SR_REJECTION_V01"
FLIP_RETEST_ID = "PA_FLIP_RETEST_V01"
RULEBOOK_VERSION = "0.1.0"

CONTEXT_TF, SETUP_TF, CONFIRM_TF = "1h", "15m", "5m"


class Regime(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    BALANCED = "BALANCED"
    TRANSITION = "TRANSITION"
    UNKNOWN = "UNKNOWN"


class SetupState(str, Enum):
    WATCHING = "WATCHING"
    WAIT_RETEST = "WAIT_RETEST"
    WAIT_CONFIRM = "WAIT_CONFIRM"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class Blocker(str, Enum):
    """Chapter 16's catalogue. One primary code per decision, with evidence."""
    NON_REAL_DATA = "NON_REAL_DATA"
    STALE_HTF_CANDLE = "STALE_HTF_CANDLE"
    MISSING_CANDLE = "MISSING_CANDLE"
    HTF_NOT_READY = "HTF_NOT_READY"
    WARMING_UP = "WARMING_UP"
    REGIME_NOT_ALIGNED = "REGIME_NOT_ALIGNED"
    NO_ELIGIBLE_ZONE = "NO_ELIGIBLE_ZONE"
    REJECTION_FAILED = "REJECTION_FAILED"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
    STOP_DISTANCE_INVALID = "STOP_DISTANCE_INVALID"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    NET_RR_TOO_LOW = "NET_RR_TOO_LOW"
    EXISTING_EXPOSURE = "EXISTING_EXPOSURE"
    ZONE_CONSUMED = "ZONE_CONSUMED"


@dataclass(frozen=True)
class RulebookConfig:
    """Chapter 18's configuration contract.

    Every value is the document's stated initial hypothesis. They are named
    here so a change is a visible, versioned edit rather than a literal buried
    in a comparison -- the rulebook requires that omitted defaults be persisted
    explicitly "so a software upgrade cannot change behaviour invisibly".
    """
    symbol: str = "BTCUSDT"
    tick_size: float = 0.1
    step_size: float = 0.001

    warmup_bars: int = 200
    atr_period: int = 14

    pivot_left: int = 2
    pivot_right: int = 2
    structure_epsilon_atr: float = 0.10       # x previous ATR1H
    structure_max_age_bars: int = 48          # closed 1H bars

    zone_half_width_atr: float = 0.15         # x previous ATR1H
    zone_min_half_width_ticks: float = 2.0
    zone_expiry_bars: int = 72                # closed 1H bars

    # Chapter 4 rejection / dominance measures
    rejection_wick_fraction: float = 0.45
    rejection_close_location: float = 0.65    # mirrored to 0.35 for shorts
    rejection_body_fraction_max: float = 0.55
    rejection_range_atr_min: float = 0.5
    rejection_range_atr_max: float = 2.0
    dominance_body_fraction: float = 0.60
    dominance_close_location: float = 0.80    # mirrored to 0.20 for shorts
    dominance_body_atr: float = 0.50          # x previous ATR5

    penetration_atr: float = 0.30             # x setup ATR15
    stop_buffer_atr: float = 0.10             # x setup ATR15
    invalidation_buffer_atr: float = 0.10     # x previous ATR15

    confirmation_bars: int = 3                # later 5M bars
    retest_window_bars: int = 4               # later 15M bars

    breakout_body_fraction: float = 0.60
    breakout_close_location: float = 0.80
    breakout_body_atr: float = 0.80           # x previous ATR15

    stop_distance_atr_min: float = 0.30
    stop_distance_atr_max: float = 2.50
    min_net_rr: float = 2.5

    risk_per_trade: float = 0.0025            # 0.25% conservative equity
    aggregate_risk: float = 0.0075            # 0.75%

    def validate(self) -> None:
        """Chapter 18: reject impossible configuration at startup, not later."""
        if self.tick_size <= 0 or self.step_size <= 0:
            raise ValueError("tick_size and step_size must be positive")
        if self.atr_period < 2 or self.warmup_bars < self.atr_period:
            raise ValueError("warmup must cover the ATR period")
        if not 0 < self.min_net_rr:
            raise ValueError("min_net_rr must be positive")
        if self.stop_distance_atr_min >= self.stop_distance_atr_max:
            raise ValueError("stop distance band is inverted")
        for name in ("rejection_wick_fraction", "rejection_close_location",
                     "rejection_body_fraction_max", "dominance_body_fraction",
                     "dominance_close_location", "risk_per_trade",
                     "aggregate_risk"):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be a fraction in (0, 1]")


# --------------------------------------------------------------- chapter 4

@dataclass(frozen=True)
class CandleFeatures:
    """The measurable shape of one candle. Chapter 4's formulas, verbatim."""
    valid: bool
    range: float = 0.0
    body: float = 0.0
    lower_wick: float = 0.0
    upper_wick: float = 0.0
    body_fraction: float = 0.0
    close_location: float = 0.0
    lower_wick_fraction: float = 0.0
    upper_wick_fraction: float = 0.0
    bullish: bool = False
    bearish: bool = False


def features(bar: Bar) -> CandleFeatures:
    """Chapter 4. A zero-range candle returns invalid flags, not infinity.

    The rulebook is explicit that a zero-range candle "is stored but cannot
    satisfy rejection or dominance conditions" -- returning an arbitrary
    favourable ratio there would let a flat bar qualify as a perfect rejection.
    """
    high, low, open_, close = float(bar.high), float(bar.low), float(bar.open), float(bar.close)
    span = high - low
    if span <= 0:
        return CandleFeatures(valid=False)
    body = abs(close - open_)
    lower = min(open_, close) - low
    upper = high - max(open_, close)
    return CandleFeatures(
        valid=True, range=span, body=body, lower_wick=lower, upper_wick=upper,
        body_fraction=body / span, close_location=(close - low) / span,
        lower_wick_fraction=lower / span, upper_wick_fraction=upper / span,
        bullish=close > open_, bearish=close < open_)


def atr_series(bars: Sequence[Bar], period: int = 14) -> list[Optional[float]]:
    """Wilder ATR, seeded with the arithmetic mean of the first `period` TRs.

    Returns one entry per bar, None until the seed completes, so callers can
    index by bar position. Chapter 4 fixes this definition "across research and
    forward execution" -- the point of pinning it is that a different smoothing
    silently shifts every threshold expressed in ATR units.
    """
    out: list[Optional[float]] = [None] * len(bars)
    if period < 1 or len(bars) < period + 1:
        return out
    trs: list[float] = []
    for index in range(1, len(bars)):
        high, low = float(bars[index].high), float(bars[index].low)
        prev_close = float(bars[index - 1].close)
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    # trs[k] belongs to bars[k + 1]
    seed_at = period          # bars index of the last TR in the seed window
    current = sum(trs[:period]) / period
    out[seed_at] = current
    for index in range(period, len(trs)):
        current = ((period - 1) * current + trs[index]) / period
        out[index + 1] = current
    return out


def previous_atr(bars: Sequence[Bar], period: int = 14,
                 at: Optional[int] = None) -> Optional[float]:
    """ATR of the bar BEFORE `at`. Chapter 4: "Use the previous ATR".

    Thresholds for candle t use ATR[t-1] so the candidate's own large range
    cannot move the scale it is being measured against.
    """
    at = len(bars) - 1 if at is None else at
    if at < 1:
        return None
    return atr_series(bars, period)[at - 1]


def is_bullish_rejection(bar: Bar, prior_atr: Optional[float],
                         config: RulebookConfig) -> tuple[bool, dict]:
    """Chapter 4's 15M bullish rejection plus the range filter."""
    shape = features(bar)
    measured = {
        "lower_wick_fraction": shape.lower_wick_fraction,
        "close_location": shape.close_location,
        "body_fraction": shape.body_fraction,
        "range_atr": (shape.range / prior_atr) if prior_atr else None,
    }
    if not shape.valid or prior_atr is None or prior_atr <= 0:
        return False, measured
    ok = (shape.bullish
          and shape.lower_wick_fraction >= config.rejection_wick_fraction
          and shape.close_location >= config.rejection_close_location
          and shape.body_fraction <= config.rejection_body_fraction_max
          and config.rejection_range_atr_min <= shape.range / prior_atr
          <= config.rejection_range_atr_max)
    return ok, measured


def is_bearish_rejection(bar: Bar, prior_atr: Optional[float],
                         config: RulebookConfig) -> tuple[bool, dict]:
    """The explicit mirror. Chapter 7 requires it stated, not inferred."""
    shape = features(bar)
    measured = {
        "upper_wick_fraction": shape.upper_wick_fraction,
        "close_location": shape.close_location,
        "body_fraction": shape.body_fraction,
        "range_atr": (shape.range / prior_atr) if prior_atr else None,
    }
    if not shape.valid or prior_atr is None or prior_atr <= 0:
        return False, measured
    ok = (shape.bearish
          and shape.upper_wick_fraction >= config.rejection_wick_fraction
          and shape.close_location <= 1.0 - config.rejection_close_location
          and shape.body_fraction <= config.rejection_body_fraction_max
          and config.rejection_range_atr_min <= shape.range / prior_atr
          <= config.rejection_range_atr_max)
    return ok, measured


def is_dominance(bar: Bar, prior_atr: Optional[float], direction: str,
                 config: RulebookConfig) -> tuple[bool, dict]:
    """Chapter 4's 5M dominance candle, used only for confirmation."""
    shape = features(bar)
    measured = {"body_fraction": shape.body_fraction,
                "close_location": shape.close_location,
                "body_atr": (shape.body / prior_atr) if prior_atr else None}
    if not shape.valid or prior_atr is None or prior_atr <= 0:
        return False, measured
    if direction == "long":
        ok = (shape.bullish
              and shape.body_fraction >= config.dominance_body_fraction
              and shape.close_location >= config.dominance_close_location
              and shape.body >= config.dominance_body_atr * prior_atr)
    else:
        ok = (shape.bearish
              and shape.body_fraction >= config.dominance_body_fraction
              and shape.close_location <= 1.0 - config.dominance_close_location
              and shape.body >= config.dominance_body_atr * prior_atr)
    return ok, measured


# --------------------------------------------------------------- chapter 5

@dataclass(frozen=True)
class Pivot:
    """A confirmed swing. `available_at` is when the bot may first use it."""
    id: str
    kind: str                    # "high" | "low"
    price: float
    pivot_time: datetime
    available_at: datetime
    index: int


def confirmed_pivots(bars: Sequence[Bar], config: RulebookConfig) -> list[Pivot]:
    """Strict 2/2 extrema, with availability delayed to i + right.

    Chapter 5: "The pivot at i becomes known only when i+2 closes and arrives.
    Store pivot_time=i and available_at=i+2 separately. Never expose it to the
    strategy at i." Equal highs or lows produce no pivot, and a candle that
    qualifies as both is excluded as ambiguous -- both are deliberate choices
    that keep the definition decidable rather than nearly-decidable.
    """
    left, right = config.pivot_left, config.pivot_right
    out: list[Pivot] = []
    for index in range(left, len(bars) - right):
        bar = bars[index]
        window = range(index - left, index + right + 1)
        highs = [float(bars[j].high) for j in window if j != index]
        lows = [float(bars[j].low) for j in window if j != index]
        is_high = all(float(bar.high) > value for value in highs)
        is_low = all(float(bar.low) < value for value in lows)
        if is_high and is_low:
            continue                       # ambiguous, excluded
        available = bars[index + right].timestamp
        if is_high:
            out.append(Pivot(f"pivot-h-{index}", "high", float(bar.high),
                             bar.timestamp, available, index))
        elif is_low:
            out.append(Pivot(f"pivot-l-{index}", "low", float(bar.low),
                             bar.timestamp, available, index))
    return out


def classify_regime(context_bars: Sequence[Bar], config: RulebookConfig
                    ) -> tuple[Regime, dict]:
    """1H structure. Chapter 5.

    BULL needs both the latest two confirmed highs AND lows rising by epsilon;
    BEAR needs both falling. Within epsilon on both is BALANCED; every other
    mixed configuration is TRANSITION. Neither authorises an entry in v0.1 --
    the rulebook notes a range-edge strategy "would be a separate version",
    which is precisely the discipline that stops a trend system quietly
    becoming a mean-reversion system.
    """
    evidence: dict = {"highs": [], "lows": [], "epsilon": None}
    atr = previous_atr(context_bars, config.atr_period)
    if atr is None or atr <= 0 or len(context_bars) < config.warmup_bars:
        return Regime.UNKNOWN, {**evidence, "reason": "insufficient context history"}

    pivots = confirmed_pivots(context_bars, config)
    highs = [p for p in pivots if p.kind == "high"][-2:]
    lows = [p for p in pivots if p.kind == "low"][-2:]
    epsilon = config.structure_epsilon_atr * atr
    evidence = {"highs": [p.id for p in highs], "lows": [p.id for p in lows],
                "epsilon": epsilon}
    if len(highs) < 2 or len(lows) < 2:
        return Regime.UNKNOWN, {**evidence, "reason": "fewer than two confirmed pivots per side"}

    newest = len(context_bars) - 1
    oldest_allowed = newest - config.structure_max_age_bars
    if highs[-1].index < oldest_allowed or lows[-1].index < oldest_allowed:
        return Regime.UNKNOWN, {**evidence, "reason": "latest pivot older than the age limit"}

    high_rising = highs[1].price > highs[0].price + epsilon
    high_falling = highs[1].price < highs[0].price - epsilon
    low_rising = lows[1].price > lows[0].price + epsilon
    low_falling = lows[1].price < lows[0].price - epsilon
    if high_rising and low_rising:
        return Regime.BULL, evidence
    if high_falling and low_falling:
        return Regime.BEAR, evidence
    if not (high_rising or high_falling) and not (low_rising or low_falling):
        return Regime.BALANCED, evidence
    return Regime.TRANSITION, evidence


# --------------------------------------------------------------- chapter 6

@dataclass(frozen=True)
class Zone:
    """A versioned, immutable support or resistance object.

    Chapter 6 forbids merging and stretching: "Overlapping zones remain
    separate immutable objects; do not repeatedly stretch old zones to fit new
    reactions." That is what stops a level quietly becoming whatever shape the
    most recent reaction needed.
    """
    id: str
    kind: str                     # "support" | "resistance"
    lower: float
    upper: float
    pivot_id: str
    created_at: datetime          # availability, not the pivot timestamp
    created_index: int
    creation_atr: float
    origin: str = "pivot"         # "pivot" | "flip"
    retired: bool = False
    consumed: bool = False

    @property
    def centre(self) -> float:
        return (self.lower + self.upper) / 2.0

    def intersects(self, bar: Bar) -> bool:
        return float(bar.low) <= self.upper and float(bar.high) >= self.lower

    def expired_at(self, index: int, config: RulebookConfig) -> bool:
        return index - self.created_index > config.zone_expiry_bars

    def eligible(self, index: int, setup_open: datetime,
                 config: RulebookConfig) -> bool:
        """Eligible only if it existed before the setup candle opened."""
        return (not self.retired and not self.consumed
                and not self.expired_at(index, config)
                and self.created_at < setup_open)


def build_zones(context_bars: Sequence[Bar], config: RulebookConfig) -> list[Zone]:
    """One immutable zone per confirmed 1H pivot, frozen at availability."""
    atrs = atr_series(context_bars, config.atr_period)
    out: list[Zone] = []
    for pivot in confirmed_pivots(context_bars, config):
        prior = atrs[pivot.index - 1] if pivot.index >= 1 else None
        if prior is None or prior <= 0:
            continue
        half = max(config.zone_min_half_width_ticks * config.tick_size,
                   config.zone_half_width_atr * prior)
        kind = "resistance" if pivot.kind == "high" else "support"
        out.append(Zone(
            id=f"zone-{kind[:3]}-{pivot.index}", kind=kind,
            lower=pivot.price - half, upper=pivot.price + half,
            pivot_id=pivot.id,
            created_at=pivot.available_at,
            created_index=pivot.index + config.pivot_right,
            creation_atr=prior))
    return out


# -------------------------------------------------------- chapters 10 & 11

@dataclass(frozen=True)
class CostModel:
    """Chapter 12. Costs are gates, not footnotes.

    Rates are configuration with evidence attached, never an assumed universal
    exchange fee -- the rulebook's worked example uses 5 bps per side purely as
    an illustration and says so. ``per_unit_allowance`` is the adverse
    execution and funding allowance the document adds on both paths.
    """
    entry_fee_rate: float = 0.0005
    exit_fee_rate: float = 0.0005
    per_unit_allowance: float = 0.0

    def loss_path(self, entry: float, stop: float) -> float:
        return (self.entry_fee_rate * entry + self.exit_fee_rate * stop
                + self.per_unit_allowance)

    def win_path(self, entry: float, target: float) -> float:
        return (self.entry_fee_rate * entry + self.exit_fee_rate * target
                + self.per_unit_allowance)


@dataclass(frozen=True)
class TradePlan:
    """The decision, with every number that produced it.

    A plan is evidence for the risk engine and the paper broker, not an order.
    ``accepted`` False carries the primary blocker plus its measured value, so
    the UI can say which gate failed and by how much.
    """
    accepted: bool
    strategy_id: str
    direction: str
    symbol: str
    entry_bound: float
    stop: float
    target: Optional[float]
    stop_distance: float
    stop_distance_atr: Optional[float]
    net_rr: Optional[float]
    costs_loss: float
    costs_win: Optional[float]
    quantity: float
    planned_loss: float
    zone_id: str
    setup_atr: float
    blocker: Optional[Blocker] = None
    evidence: dict = field(default_factory=dict)


def _round_to_tick(price: float, tick: float, *, down: bool) -> float:
    """Quantise to a valid tick.

    Chapter 4 requires decimal or integer-tick arithmetic so "floating-point
    noise" cannot decide a breakout; working in whole ticks is the integer form
    of that rule. Long stops round down and short stops round up, never toward
    the entry.
    """
    if tick <= 0:
        return price
    units = price / tick
    whole = int(units // 1)
    if not down and units > whole:
        whole += 1
    return round(whole * tick, 10)


def build_trade_plan(*, direction: str, strategy_id: str, symbol: str,
                     rejection: Bar, zone: Zone, setup_atr: float,
                     entry_bound: float, zones: Sequence[Zone],
                     zone_index: int, equity: float,
                     config: RulebookConfig, costs: CostModel) -> TradePlan:
    """Chapter 10 and 11, in the order the rulebook gates them.

    Stop first (structural, never rescued by sizing), then the distance band,
    then a target taken only from structure that already existed, then net
    reward-to-risk after path-specific costs, then size. A valid chart pattern
    can still be an invalid trade; that is the chapter's whole lesson.
    """
    tick = config.tick_size
    buffer_ = config.stop_buffer_atr * setup_atr
    if direction == "long":
        stop = _round_to_tick(min(float(rejection.low), zone.lower) - buffer_, tick, down=True)
    else:
        stop = _round_to_tick(max(float(rejection.high), zone.upper) + buffer_, tick, down=False)

    distance = abs(entry_bound - stop)
    ratio = distance / setup_atr if setup_atr > 0 else None
    base = dict(strategy_id=strategy_id, direction=direction, symbol=symbol,
                entry_bound=entry_bound, stop=stop, stop_distance=distance,
                stop_distance_atr=ratio, zone_id=zone.id, setup_atr=setup_atr)

    # The entry must sit on the correct side of its own stop.
    wrong_side = (direction == "long" and entry_bound <= stop) or \
                 (direction == "short" and entry_bound >= stop)
    if wrong_side or ratio is None or not (
            config.stop_distance_atr_min <= ratio <= config.stop_distance_atr_max):
        return TradePlan(accepted=False, target=None, net_rr=None,
                         costs_loss=costs.loss_path(entry_bound, stop), costs_win=None,
                         quantity=0.0, planned_loss=0.0,
                         blocker=Blocker.STOP_DISTANCE_INVALID,
                         evidence={"stop_distance_atr": ratio,
                                   "band": [config.stop_distance_atr_min,
                                            config.stop_distance_atr_max]}, **base)

    # Target from structure that is already known. Never move an obstacle.
    target = _nearest_opposing_target(direction, entry_bound, zones, zone_index,
                                      config)
    costs_loss = costs.loss_path(entry_bound, stop)
    if target is None:
        return TradePlan(accepted=False, target=None, net_rr=None,
                         costs_loss=costs_loss, costs_win=None, quantity=0.0,
                         planned_loss=0.0, blocker=Blocker.TARGET_UNAVAILABLE,
                         evidence={"reason": "no unexpired opposing zone beyond the entry"},
                         **base)

    costs_win = costs.win_path(entry_bound, target)
    reward = abs(target - entry_bound) - costs_win
    risk = distance + costs_loss
    net_rr = reward / risk if risk > 0 else None
    if net_rr is None or net_rr < config.min_net_rr:
        return TradePlan(accepted=False, target=target, net_rr=net_rr,
                         costs_loss=costs_loss, costs_win=costs_win,
                         quantity=0.0, planned_loss=0.0,
                         blocker=Blocker.NET_RR_TOO_LOW,
                         evidence={"net_rr": net_rr, "required": config.min_net_rr,
                                   "gross_reward": abs(target - entry_bound)},
                         **base)

    # Chapter 11 sizing. Never round a below-minimum quantity up past budget.
    budget = max(0.0, float(equity)) * config.risk_per_trade
    loss_per_unit = distance + costs_loss
    raw_quantity = budget / loss_per_unit if loss_per_unit > 0 else 0.0
    steps = int((raw_quantity / config.step_size) // 1) if config.step_size > 0 else 0
    quantity = round(steps * config.step_size, 12)
    planned_loss = quantity * loss_per_unit
    return TradePlan(accepted=quantity > 0, target=target, net_rr=net_rr,
                     costs_loss=costs_loss, costs_win=costs_win,
                     quantity=quantity, planned_loss=planned_loss,
                     blocker=None if quantity > 0 else Blocker.STOP_DISTANCE_INVALID,
                     evidence={"net_rr": net_rr, "risk_budget": budget,
                               "raw_quantity": raw_quantity,
                               "gross_notional": quantity * entry_bound},
                     **base)


def _nearest_opposing_target(direction: str, entry: float, zones: Sequence[Zone],
                             index: int, config: RulebookConfig) -> Optional[float]:
    """Chapter 10's target: the nearest unexpired ORIGINAL opposing zone.

    "Zones must already be known at decision time. If E is inside an opposing
    zone, or no valid opposing zone exists, reject TARGET_UNAVAILABLE." A flip
    object is not an original zone and cannot supply a target here.
    """
    tick = config.tick_size
    live = [z for z in zones
            if z.origin == "pivot" and not z.retired and not z.expired_at(index, config)]
    if direction == "long":
        above = [z for z in live if z.kind == "resistance" and z.lower > entry]
        inside = [z for z in live if z.kind == "resistance" and z.lower <= entry <= z.upper]
        if inside or not above:
            return None
        return _round_to_tick(min(z.lower for z in above) - tick, tick, down=True)
    below = [z for z in live if z.kind == "support" and z.upper < entry]
    inside = [z for z in live if z.kind == "support" and z.lower <= entry <= z.upper]
    if inside or not below:
        return None
    return _round_to_tick(max(z.upper for z in below) + tick, tick, down=False)


# ---------------------------------------------------- chapters 7, 8, 9 & 14

CONTEXT_SECONDS, SETUP_SECONDS, CONFIRM_SECONDS = 3600, 900, 300


@dataclass(frozen=True)
class Setup:
    """One pending candidate, frozen at every transition.

    Chapter 7 requires the evidence be frozen at confirmation -- "rejection
    OHLC, confirmation OHLC, timeframe snapshots, zone version, ATR values,
    side and strategy version". Making the whole object immutable and
    re-creating it on each transition is the cheap way to guarantee that: a
    later event cannot quietly rewrite the candle a decision was made on.

    A Setup is not an order and not an intent. It is the strategy's answer to
    "does the market evidence qualify", which chapter 1 keeps separate from
    whether exposure is allowed and whether an intent can fill.
    """
    id: str
    strategy_id: str
    direction: str                             # "long" | "short"
    state: SetupState
    symbol: str
    zone: Zone                                 # the zone being defended/retested
    original_zone: Zone                        # for B, the pre-breakout zone
    setup_atr: float                           # ATR15 previous, frozen
    created_at: datetime
    rejection: Optional[Bar] = None            # rejection (A) or retest (B)
    rejection_index: Optional[int] = None
    breakout: Optional[Bar] = None
    breakout_index: Optional[int] = None
    retest_deadline_index: Optional[int] = None
    confirm_window_start: Optional[datetime] = None
    confirm_slots_used: int = 0
    confirmation: Optional[Bar] = None
    cancel_price: Optional[float] = None
    blocker: Optional[Blocker] = None
    evidence: dict = field(default_factory=dict)

    @property
    def zone_id(self) -> str:
        return self.zone.id

    @property
    def terminal(self) -> bool:
        return self.state in (SetupState.EXPIRED, SetupState.INVALIDATED)


@dataclass(frozen=True)
class Decision:
    """What the engine concluded for one closed candle.

    Always returned, including when nothing happened, because chapter 9 draws
    a distinction the UI has to show: "no eligible zone" is *watch, not error*,
    while a failed mandatory measure is a rejection that must carry "measured
    value and threshold". A silent None would collapse the two.
    """
    timeframe: str
    at: datetime
    regime: Regime
    setup: Optional[Setup] = None
    plan: Optional[TradePlan] = None
    blocker: Optional[Blocker] = None
    evidence: dict = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.setup is not None and self.state is SetupState.CONFIRMED

    @property
    def state(self) -> Optional[SetupState]:
        return self.setup.state if self.setup else None

    @property
    def actionable(self) -> bool:
        """A plan the risk engine should look at. Never an order."""
        return self.plan is not None and self.plan.accepted


class PriceActionRulebookEngine:
    """The rulebook's state machine. Pure, deterministic, paper-only.

    Three entry points, one per timeframe, mirroring chapter 17's control flow
    and chapter 14's transition priority. Within any single event the order is
    fixed and is not a matter of taste: cancellations and expiry resolve before
    context changes, which resolve before new confirmations. The document states
    the tie directly -- "An invalidation and confirmation in the same event
    resolves to invalidation" -- and that is the only ordering under which a
    setup cannot be confirmed by the very candle that killed it.

    The engine reads no clock. Every deadline in the specification is expressed
    in scheduled candle boundaries, so the closed candles carry their own time
    and replaying an event sequence reproduces identical decisions. Freshness
    and provenance are deliberately *not* decided here: they are the caller's
    gate (services/market_data_freshness.py), passed in as ``entry_blocked``.
    Chapter 9 is explicit that a failed data gate must "block execution; do not
    relax strategy", so a blocked engine still advances and still expires its
    setups -- it simply cannot produce an accepted plan.
    """

    def __init__(self, config: Optional[RulebookConfig] = None,
                 costs: Optional[CostModel] = None, *,
                 strategies: Optional[Iterable[str]] = None) -> None:
        self.config = config or RulebookConfig()
        self.config.validate()
        self.costs = costs or CostModel()
        self.strategies = tuple(strategies or (SR_REJECTION_ID, FLIP_RETEST_ID))
        for strategy_id in self.strategies:
            if strategy_id not in (SR_REJECTION_ID, FLIP_RETEST_ID):
                raise ValueError(f"unknown strategy {strategy_id!r}")
        self.regime = Regime.UNKNOWN
        self.regime_evidence: dict = {}
        self.zones: list[Zone] = []
        self.pending: Optional[Setup] = None
        self.consumed_zone_ids: set = set()
        self.history: list[Setup] = []
        self._context_index = -1
        self._sequence = 0

    # ------------------------------------------------------ chapters 5 and 6

    def update_context(self, context_bars: Sequence[Bar]) -> Regime:
        """Recompute the 1H regime and merge the zone registry.

        Chapter 6 forbids mutating a published zone, so zones discovered again
        on a later pass keep the object already issued -- with whatever
        retired/consumed flags its history gave it. Only genuinely new pivots
        add objects. Rebuilding the list wholesale would silently resurrect a
        zone a breakout had retired.
        """
        self._context_index = len(context_bars) - 1
        self.regime, self.regime_evidence = classify_regime(context_bars, self.config)

        known = {zone.id: zone for zone in self.zones}
        for zone in build_zones(context_bars, self.config):
            if zone.id not in known:
                known[zone.id] = zone
        self.zones = sorted(known.values(), key=lambda z: (z.created_at, z.id))

        # A regime that stops supporting the pending setup kills it here, not
        # at confirmation time: chapter 7 lists "regime changes" as a
        # cancellation cause in its own right.
        if self.pending is not None and not self.pending.terminal:
            if not self._regime_supports(self.pending.direction):
                self._retire_pending(Blocker.REGIME_NOT_ALIGNED,
                                     {"regime": self.regime.value},
                                     SetupState.INVALIDATED)
        return self.regime

    def _regime_supports(self, direction: str) -> bool:
        return ((direction == "long" and self.regime is Regime.BULL)
                or (direction == "short" and self.regime is Regime.BEAR))

    def _eligible_zones(self, kind: str, setup_open: datetime) -> list[Zone]:
        return [z for z in self._live_zones(kind, setup_open)
                if z.id not in self.consumed_zone_ids]

    def _live_zones(self, kind: str, setup_open: datetime) -> list[Zone]:
        """Everything eligible except for the consumed check."""
        return [z for z in self.zones
                if z.kind == kind
                and z.origin == "pivot"
                and z.eligible(self._context_index, setup_open, self.config)]

    def _consumed_but_otherwise_eligible(self, setup_open: datetime) -> list[str]:
        """Zones this event would have looked at had they not already traded."""
        return [z.id for kind in ("support", "resistance")
                for z in self._live_zones(kind, setup_open)
                if z.id in self.consumed_zone_ids]

    def _retire_pending(self, blocker: Blocker, evidence: dict,
                        state: SetupState) -> Setup:
        retired = replace(self.pending, state=state, blocker=blocker,
                          evidence={**self.pending.evidence, **evidence})
        self.history.append(retired)
        self.pending = None
        return retired

    def _next_id(self, strategy_id: str) -> str:
        self._sequence += 1
        return f"{strategy_id}-{self._sequence}"

    # --------------------------------------------------- chapters 7, 8 and 9

    def on_setup_close(self, setup_bars: Sequence[Bar]) -> Decision:
        """A 15M candle closed. Chapter 14's priority order, in order.

        Expiry and invalidation of what is already pending resolve first; only
        then may a new candidate be raised. Both A and B can fire on the same
        candle, so the survivors go through chapter 9's deterministic ranking
        rather than whichever branch happened to run first.
        """
        if len(setup_bars) < 2:
            return self._watch(SETUP_TF, None, Blocker.WARMING_UP,
                               {"reason": "fewer than two closed 15M bars"})

        index = len(setup_bars) - 1
        bar = setup_bars[index]
        atrs = atr_series(setup_bars, self.config.atr_period)
        prior_atr = atrs[index - 1]
        if prior_atr is None or prior_atr <= 0:
            return self._watch(SETUP_TF, bar, Blocker.WARMING_UP,
                               {"reason": "ATR15 not seeded"})

        # (1) cancellations and expiry, before anything else looks attractive
        if self.pending is not None and not self.pending.terminal:
            resolved = self._advance_pending_on_setup_close(
                setup_bars, index, prior_atr)
            if resolved is not None:
                return resolved
            return Decision(SETUP_TF, bar.timestamp, self.regime,
                            setup=self.pending,
                            evidence={"atr15_previous": prior_atr})

        # (2) new candidates. One pending setup per symbol (chapter 9).
        if not (self.regime is Regime.BULL or self.regime is Regime.BEAR):
            return self._watch(SETUP_TF, bar, Blocker.REGIME_NOT_ALIGNED,
                               {"regime": self.regime.value,
                                **self.regime_evidence})

        direction = "long" if self.regime is Regime.BULL else "short"
        candidates: list[Setup] = []
        misses: list[dict] = []
        if SR_REJECTION_ID in self.strategies:
            found, why = self._scan_rejection(setup_bars, index, prior_atr, direction)
            candidates.extend(found)
            misses.extend(why)
        if FLIP_RETEST_ID in self.strategies:
            found, why = self._scan_breakout(setup_bars, index, prior_atr, direction)
            candidates.extend(found)
            misses.extend(why)

        if not candidates:
            # Chapter 16 wants "one primary code per decision, with evidence",
            # and chapter 9 lists these as three separate rows. Order them by
            # how specific they are: a consumed zone is a settled fact about
            # that level, a failed measure is the routine outcome on every
            # other candle, and no eligible zone at all is "watch, not error".
            # The misses travel in the evidence either way, so naming the most
            # specific cause costs nothing.
            spent = self._consumed_but_otherwise_eligible(bar.timestamp)
            if spent:
                blocker = Blocker.ZONE_CONSUMED
            elif misses:
                blocker = Blocker.REJECTION_FAILED
            else:
                blocker = Blocker.NO_ELIGIBLE_ZONE
            return self._watch(SETUP_TF, bar, blocker,
                               {"atr15_previous": prior_atr, "measured": misses,
                                "consumed_zones": spent})

        self.pending = self._commit_breakout(self._arbitrate(candidates, bar, prior_atr))
        return Decision(SETUP_TF, bar.timestamp, self.regime, setup=self.pending,
                        evidence={"atr15_previous": prior_atr,
                                  "considered": [c.id for c in candidates]})

    def _watch(self, timeframe: str, bar: Optional[Bar], blocker: Blocker,
               evidence: dict) -> Decision:
        at = bar.timestamp if bar is not None else datetime.min.replace(tzinfo=timezone.utc)
        return Decision(timeframe, at, self.regime, setup=self.pending,
                        blocker=blocker, evidence=evidence)

    # ------------------------------------------------------ Setup A, chapter 7

    def _scan_rejection(self, bars: Sequence[Bar], index: int, prior_atr: float,
                        direction: str) -> tuple[list[Setup], list[dict]]:
        """A trend-aligned pullback rejects an existing zone.

        The three separate distance rules matter and are easy to collapse by
        accident. Intersection says the candle reached the zone; the 0.30 ATR
        penetration floor "permits a modest sweep but rejects deep
        penetration"; and the close must finish clear of the zone entirely.
        A candle can satisfy any two and fail the third.
        """
        config, bar = self.config, bars[index]
        previous_close = float(bars[index - 1].close)
        kind = "support" if direction == "long" else "resistance"
        found: list[Setup] = []
        misses: list[dict] = []

        for zone in self._eligible_zones(kind, bar.timestamp):
            if direction == "long":
                preceded = previous_close > zone.upper
                penetration = float(bar.low) >= zone.lower - config.penetration_atr * prior_atr
                closed_clear = float(bar.close) > zone.upper
                shaped, measured = is_bullish_rejection(bar, prior_atr, config)
            else:
                preceded = previous_close < zone.lower
                penetration = float(bar.high) <= zone.upper + config.penetration_atr * prior_atr
                closed_clear = float(bar.close) < zone.lower
                shaped, measured = is_bearish_rejection(bar, prior_atr, config)

            checks = {"zone_id": zone.id, "preceding_close_clear": preceded,
                      "intersects": zone.intersects(bar),
                      "penetration_within_limit": penetration,
                      "closed_clear_of_zone": closed_clear,
                      "rejection_shape": shaped, **measured}
            if not (preceded and zone.intersects(bar) and penetration
                    and closed_clear and shaped):
                misses.append(checks)
                continue

            found.append(Setup(
                id=self._next_id(SR_REJECTION_ID), strategy_id=SR_REJECTION_ID,
                direction=direction, state=SetupState.WAIT_CONFIRM,
                symbol=config.symbol, zone=zone, original_zone=zone,
                setup_atr=prior_atr, created_at=bar.timestamp,
                rejection=bar, rejection_index=index,
                confirm_window_start=bar.timestamp + timedelta(seconds=SETUP_SECONDS),
                cancel_price=self._cancel_boundary(bar, direction, prior_atr),
                evidence=checks))
        return found, misses

    def _cancel_boundary(self, rejection: Bar, direction: str, setup_atr: float) -> float:
        """Chapter 7's pre-confirmation cancellation price."""
        offset = self.config.invalidation_buffer_atr * setup_atr
        if direction == "long":
            return float(rejection.low) - offset
        return float(rejection.high) + offset

    # ------------------------------------------------------ Setup B, chapter 8

    def _scan_breakout(self, bars: Sequence[Bar], index: int, prior_atr: float,
                       direction: str) -> tuple[list[Setup], list[dict]]:
        """A trend-aligned breakout of an existing zone, awaiting its retest.

        The breakout retires the original zone and freezes a flip at the same
        bounds. Chapter 8 is emphatic that this is a new object: "Never rewrite
        the historical resistance as if it had always been support. A flip is a
        new event with a new available_at timestamp." Rewriting the original in
        place would also corrupt every backtest that had already used it, since
        the zone registry is shared across both strategies.
        """
        config, bar = self.config, bars[index]
        previous_close = float(bars[index - 1].close)
        kind = "resistance" if direction == "long" else "support"
        shape = features(bar)
        found: list[Setup] = []
        misses: list[dict] = []

        for zone in self._eligible_zones(kind, bar.timestamp):
            if direction == "long":
                preceded = previous_close <= zone.upper
                broke = float(bar.close) > zone.upper + config.invalidation_buffer_atr * prior_atr
                located = shape.close_location >= config.breakout_close_location
                directional = shape.bullish
            else:
                preceded = previous_close >= zone.lower
                broke = float(bar.close) < zone.lower - config.invalidation_buffer_atr * prior_atr
                located = shape.close_location <= 1.0 - config.breakout_close_location
                directional = shape.bearish

            checks = {"zone_id": zone.id, "preceding_close_inside": preceded,
                      "broke_level": broke, "directional": directional,
                      "body_fraction": shape.body_fraction,
                      "close_location": shape.close_location,
                      "body_atr": (shape.body / prior_atr) if prior_atr else None}
            if not (shape.valid and preceded and broke and directional and located
                    and shape.body_fraction >= config.breakout_body_fraction
                    and shape.body >= config.breakout_body_atr * prior_atr):
                misses.append(checks)
                continue

            flip = replace(
                zone,
                id=f"flip-{zone.id}-{index}",
                kind="support" if direction == "long" else "resistance",
                origin="flip",
                created_at=bar.timestamp + timedelta(seconds=SETUP_SECONDS),
                creation_atr=prior_atr,
                retired=False, consumed=False)
            # The original zone is NOT retired here. A breakout that loses
            # chapter 9's arbitration must leave the registry untouched, and
            # retiring during the scan would destroy a zone on behalf of a
            # candidate that was never selected. Retirement is the winner's,
            # applied in _commit_breakout once selection is final.
            found.append(Setup(
                id=self._next_id(FLIP_RETEST_ID), strategy_id=FLIP_RETEST_ID,
                direction=direction, state=SetupState.WAIT_RETEST,
                symbol=config.symbol, zone=flip, original_zone=zone,
                setup_atr=prior_atr, created_at=bar.timestamp,
                breakout=bar, breakout_index=index,
                retest_deadline_index=index + config.retest_window_bars,
                evidence=checks))
        return found, misses

    def _advance_pending_on_setup_close(self, bars: Sequence[Bar], index: int,
                                        prior_atr: float) -> Optional[Decision]:
        """Expiry, invalidation and the B retest. Returns a terminal Decision.

        None means the pending setup survived this candle unchanged, which is
        the common case for a Setup A waiting on 5M confirmation.
        """
        setup, config, bar = self.pending, self.config, bars[index]

        if not self._regime_supports(setup.direction):
            return self._terminal(self._retire_pending(
                Blocker.REGIME_NOT_ALIGNED, {"regime": self.regime.value},
                SetupState.INVALIDATED), SETUP_TF, bar)

        if setup.original_zone.expired_at(self._context_index, config):
            return self._terminal(self._retire_pending(
                Blocker.CONFIRMATION_EXPIRED, {"reason": "zone expired"},
                SetupState.EXPIRED), SETUP_TF, bar)

        if setup.state is not SetupState.WAIT_RETEST:
            return None

        # Invalidation before a retest is armed -- checked before the retest, so
        # a candle that breaks back through cannot also arm one.
        flip, close = setup.zone, float(bar.close)
        margin = config.invalidation_buffer_atr * setup.setup_atr
        broke_back = (close < flip.lower - margin if setup.direction == "long"
                      else close > flip.upper + margin)
        if broke_back:
            return self._terminal(self._retire_pending(
                Blocker.REJECTION_FAILED,
                {"reason": "flip invalidated before retest", "close": close},
                SetupState.INVALIDATED), SETUP_TF, bar)

        if setup.direction == "long":
            penetration = float(bar.low) >= flip.lower - config.penetration_atr * prior_atr
            closed_clear = close > flip.upper
            shaped, measured = is_bullish_rejection(bar, prior_atr, config)
        else:
            penetration = float(bar.high) <= flip.upper + config.penetration_atr * prior_atr
            closed_clear = close < flip.lower
            shaped, measured = is_bearish_rejection(bar, prior_atr, config)

        if (flip.intersects(bar) and penetration and closed_clear and shaped):
            self.pending = replace(
                setup, state=SetupState.WAIT_CONFIRM,
                rejection=bar, rejection_index=index,
                confirm_window_start=bar.timestamp + timedelta(seconds=SETUP_SECONDS),
                cancel_price=self._cancel_boundary(bar, setup.direction, setup.setup_atr),
                evidence={**setup.evidence, "retest": {**measured, "index": index}})
            return None

        # "Expire the flip when no retest qualifies within four bars." The
        # breakout candle cannot retest itself, so the window is (i, i+4].
        if index >= setup.retest_deadline_index:
            return self._terminal(self._retire_pending(
                Blocker.CONFIRMATION_EXPIRED,
                {"reason": "no qualifying retest within the window",
                 "window_bars": config.retest_window_bars},
                SetupState.EXPIRED), SETUP_TF, bar)
        return None

    def _terminal(self, setup: Setup, timeframe: str, bar: Bar) -> Decision:
        return Decision(timeframe, bar.timestamp, self.regime, setup=setup,
                        blocker=setup.blocker, evidence=setup.evidence)

    # --------------------------------------------------------------- chapter 9

    def _arbitrate(self, candidates: Sequence[Setup], bar: Bar,
                   prior_atr: float) -> Setup:
        """Deterministic ranking. Chapter 9's four keys, in its order.

        Every key is a total order on values already frozen, so the winner does
        not depend on scan order, dict iteration or float equality luck. That
        is the whole point: the document adds "Once selected, do not switch to
        a different candidate because it later looks better."
        """
        close = float(bar.close)

        def rank(setup: Setup):
            distance = abs(close - setup.zone.centre) / prior_atr
            return (distance, setup.zone.created_at, setup.zone.id, setup.strategy_id)

        return min(candidates, key=rank)

    def _commit_breakout(self, winner: Setup) -> Setup:
        """Apply the winning breakout's side effect, and only the winner's.

        Chapter 8 retires the original zone when a breakout is taken. Doing
        that at scan time would also retire the zone of a candidate that lost
        arbitration, silently deleting a level that was never traded -- and
        because the registry is shared with Setup A, the damage would show up
        as a rejection setup that mysteriously stopped finding its zone.
        """
        if winner.strategy_id != FLIP_RETEST_ID:
            return winner
        self.zones = [replace(z, retired=True) if z.id == winner.original_zone.id else z
                      for z in self.zones]
        return winner

    # ---------------------------------------- the 5M confirmation window, ch.7

    def on_confirm_close(self, confirm_bars: Sequence[Bar], *,
                         equity: float, exposure: bool = False,
                         entry_blocked: Optional[Blocker] = None) -> Decision:
        """A 5M candle closed. Cancel, then confirm, then plan.

        The cancellation check runs first on purpose. Chapter 7: "For
        candle-only research, a low beyond this cancellation boundary cancels
        before checking confirmation on that candle." A single 5M candle can
        both spike through the invalidation level and close as a textbook
        dominance candle, and the order of these two tests is the only thing
        that decides whether that candle is a trade or a scratch.
        """
        if not confirm_bars:
            return self._watch(CONFIRM_TF, None, Blocker.MISSING_CANDLE,
                               {"reason": "no 5M candles"})

        index = len(confirm_bars) - 1
        bar = confirm_bars[index]
        setup = self.pending
        if setup is None or setup.state is not SetupState.WAIT_CONFIRM:
            return Decision(CONFIRM_TF, bar.timestamp, self.regime, setup=setup,
                            evidence={"reason": "no setup awaiting confirmation"})

        # Only candles at or after the 15M close boundary count, and the
        # constituent 5M bars inside the rejection are explicitly excluded.
        if bar.timestamp < setup.confirm_window_start:
            return Decision(CONFIRM_TF, bar.timestamp, self.regime, setup=setup,
                            evidence={"reason": "before the confirmation window"})

        # "Three confirmation candles means three scheduled 5M intervals, not
        # three messages received. A missing interval blocks/cancels rather
        # than extending the window."
        expected = setup.confirm_window_start + timedelta(
            seconds=CONFIRM_SECONDS * setup.confirm_slots_used)
        if bar.timestamp != expected:
            return self._terminal(self._retire_pending(
                Blocker.MISSING_CANDLE,
                {"expected_open": expected.isoformat(),
                 "observed_open": bar.timestamp.isoformat()},
                SetupState.INVALIDATED), CONFIRM_TF, bar)

        # (1) cancellation, before confirmation is even measured
        breached = (float(bar.low) < setup.cancel_price if setup.direction == "long"
                    else float(bar.high) > setup.cancel_price)
        if breached:
            return self._terminal(self._retire_pending(
                Blocker.REJECTION_FAILED,
                {"reason": "cancellation boundary breached",
                 "cancel_price": setup.cancel_price,
                 "observed": float(bar.low) if setup.direction == "long" else float(bar.high)},
                SetupState.INVALIDATED), CONFIRM_TF, bar)

        used = setup.confirm_slots_used + 1
        prior_atr = previous_atr(confirm_bars, self.config.atr_period, at=index)
        dominant, measured = is_dominance(bar, prior_atr, setup.direction, self.config)
        tick = self.config.tick_size
        if setup.direction == "long":
            cleared = float(bar.close) > float(setup.rejection.high) + tick
        else:
            cleared = float(bar.close) < float(setup.rejection.low) - tick
        measured = {**measured, "slot": used, "cleared_rejection_extreme": cleared}

        if not (dominant and cleared):
            if used >= self.config.confirmation_bars:
                return self._terminal(self._retire_pending(
                    Blocker.CONFIRMATION_EXPIRED,
                    {"reason": "no confirmation within the window", **measured},
                    SetupState.EXPIRED), CONFIRM_TF, bar)
            self.pending = replace(setup, confirm_slots_used=used)
            return Decision(CONFIRM_TF, bar.timestamp, self.regime,
                            setup=self.pending, evidence=measured)

        # (2) confirmed. An immutable intent candidate, not a fill.
        confirmed = replace(setup, state=SetupState.CONFIRMED, confirmation=bar,
                            confirm_slots_used=used,
                            evidence={**setup.evidence, "confirmation": measured})
        self.pending = confirmed
        return self._plan_for(confirmed, bar, equity=equity, exposure=exposure,
                              entry_blocked=entry_blocked)

    def _plan_for(self, setup: Setup, confirmation: Bar, *, equity: float,
                  exposure: bool, entry_blocked: Optional[Blocker]) -> Decision:
        """Chapters 9, 10 and 11 -- the gates that apply after the shape passes.

        Chapter 9's table puts exposure and consumed-zone rejections alongside
        the data gate, and all three precede sizing: a setup that cannot be
        taken should not produce a size at all. The confirmed setup is still
        recorded, because "reject with measured value" is a reportable outcome
        and the research book needs the sample.
        """
        blocker: Optional[Blocker] = None
        if entry_blocked is not None:
            blocker = entry_blocked
        elif exposure:
            blocker = Blocker.EXISTING_EXPOSURE
        elif setup.original_zone.id in self.consumed_zone_ids:
            blocker = Blocker.ZONE_CONSUMED

        if blocker is not None:
            self.history.append(replace(setup, blocker=blocker))
            self.pending = None
            return Decision(CONFIRM_TF, confirmation.timestamp, self.regime,
                            setup=setup, blocker=blocker,
                            evidence={**setup.evidence, "blocked_before_sizing": True})

        entry_bound = _round_to_tick(
            float(confirmation.close), self.config.tick_size,
            down=setup.direction == "short")
        plan = build_trade_plan(
            direction=setup.direction, strategy_id=setup.strategy_id,
            symbol=setup.symbol, rejection=setup.rejection, zone=setup.zone,
            setup_atr=setup.setup_atr, entry_bound=entry_bound,
            zones=self.zones, zone_index=self._context_index, equity=equity,
            config=self.config, costs=self.costs)

        self.history.append(setup)
        if plan.accepted:
            self.consumed_zone_ids.add(setup.original_zone.id)
        self.pending = None
        return Decision(CONFIRM_TF, confirmation.timestamp, self.regime,
                        setup=setup, plan=plan, blocker=plan.blocker,
                        evidence={**setup.evidence, "entry_bound": entry_bound})
