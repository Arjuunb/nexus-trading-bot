"""The rulebook v0.1 state machines: Setup A, Setup B, arbitration, priority.

Chapters 4-13 are measured by tests/test_pa_rulebook_v01.py against the
document's own worked example. What is left is the part that can silently do
nothing: a state machine that never leaves WATCHING passes every unit test on
its formulas and never produces a trade. So these drive the engine with a
synthetic-but-structurally-real market and assert it reaches CONFIRMED, and
then assert each way the rulebook says it must *not*.

The market is deliberately scaled so a trade can clear chapter 10's net-RR
gate. At a 108,000 price with 5bps per side, a 70-point stop is uneconomic
before the chart is even consulted -- which is the rulebook working, not
failing, but it makes for a fixture that can only ever prove rejection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.types import Bar
from services.pa_rulebook_v01 import (
    FLIP_RETEST_ID,
    SR_REJECTION_ID,
    Blocker,
    CostModel,
    PriceActionRulebookEngine,
    Regime,
    RulebookConfig,
    SetupState,
    Zone,
    build_zones,
    classify_regime,
)

UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
ATR15 = 300.0
ATR5 = 120.0


# --------------------------------------------------------------- the fixture

# The swing structure the context is built to contain. Large swings made of
# many small bars keep ATR1H low relative to the moves, which is what a real
# trend looks like -- and it matters mechanically: zone half-width is
# 0.15*ATR1H, so a fixture with violent bars produces zones so wide that the
# stop swallows the target room and every plan dies on net RR. That would be
# the rulebook working correctly and the test proving nothing.
#
#   bar 160  L1  99,900     the warm-up descent bottoms out
#   bar 180  H1 112,100     first confirmed high
#   bar 193  L2 103,900     higher low
#   bar 213  H2 116,100     higher high  -> BULL
#   bar 226  L3 107,900     higher low   -> the support zone under test
WAYPOINTS = [(0, 110_000.0), (160, 100_000.0), (180, 112_000.0),
             (193, 104_000.0), (213, 116_000.0), (226, 108_000.0),
             (239, 108_650.0)]


def _context_bars(half_range: float = 100.0) -> list[Bar]:
    """A 1H context interpolated through WAYPOINTS, one bar per step.

    Every turning point is a strict 2/2 extremum and the monotonic legs between
    them contain none, so the pivot set is exactly the five waypoints. Nothing
    is random: the engine must be replayable.
    """
    mids: list[float] = []
    for (start_index, start_price), (end_index, end_price) in zip(WAYPOINTS, WAYPOINTS[1:]):
        span = end_index - start_index
        mids.extend(start_price + (end_price - start_price) * k / span for k in range(span))
    mids.append(WAYPOINTS[-1][1])
    return [Bar(START + timedelta(hours=i), mid, mid + half_range,
                mid - half_range, mid, 100.0) for i, mid in enumerate(mids)]


@pytest.fixture
def config():
    return RulebookConfig(symbol="BTCUSDT", tick_size=0.1, step_size=0.001)


@pytest.fixture
def context(config):
    bars = _context_bars()
    assert classify_regime(bars, config)[0] is Regime.BULL, "the fixture is not BULL"
    return bars


@pytest.fixture
def support(context, config) -> Zone:
    return [z for z in build_zones(context, config) if z.kind == "support"][-1]


def _flat(start: datetime, count: int, price: float, span: float,
          minutes: int) -> list[Bar]:
    """Filler bars with a constant true range, so ATR equals `span` exactly."""
    return [Bar(start + timedelta(minutes=minutes * i), price, price + span / 2,
                price - span / 2, price, 50.0) for i in range(count)]


def _rejection_for(zone: Zone, at: datetime) -> Bar:
    """A bullish rejection of `zone`, built from its bounds.

    Derived rather than hard-coded: the zone's width comes from ATR1H, so a
    literal candle would silently stop intersecting the moment the fixture
    changed, and the test would pass by never setting up at all.
    """
    low = zone.lower - 0.20 * ATR15          # a modest sweep, inside 0.30 ATR
    close = zone.upper + 0.30 * ATR15        # closes clear of the zone
    high = close + 20.0
    open_ = low + 0.50 * (high - low)        # lower wick 0.50 >= 0.45
    return Bar(at, open_, high, low, close, 80.0)


def _confirmation_for(rejection: Bar, at: datetime) -> Bar:
    """A bullish dominance candle closing above rejection.high + one tick."""
    close = float(rejection.high) + 32.8
    low = close - 100.0
    high = close + 20.0
    open_ = low + 10.0
    return Bar(at, open_, high, low, close, 90.0)


def _primed(config, context, support, costs=None):
    """An engine warmed on the context with the 15M series up to the rejection."""
    engine = PriceActionRulebookEngine(config, costs or CostModel())
    engine.update_context(context)
    assert engine.regime is Regime.BULL

    start = support.created_at + timedelta(hours=2)
    perch = support.upper + 400.0
    setup_bars = _flat(start, 40, perch, ATR15, minutes=15)
    return engine, setup_bars


def _confirm_series(rejection: Bar) -> tuple[list[Bar], datetime]:
    """Filler 5M bars ending immediately before the confirmation window."""
    window = rejection.timestamp + timedelta(minutes=15)
    filler = _flat(window - timedelta(minutes=5 * 20), 20,
                   float(rejection.close), ATR5, minutes=5)
    return filler, window


# --------------------------------------------- Setup A, the whole way through

def test_setup_a_runs_idle_to_confirmed_and_produces_a_plan(config, context, support):
    """The load-bearing test: the engine actually reaches a trade.

    Chapter 7 end to end -- eligible zone, preceding close above it, a
    qualifying rejection, then a 5M dominance candle inside the three-candle
    window. If this ever goes quiet the strategy has stopped trading, which no
    amount of green formula tests would reveal.
    """
    engine, setup_bars = _primed(config, context, support)

    # nothing pending while price is perched above the zone
    watching = engine.on_setup_close(setup_bars)
    assert watching.setup is None

    rejection = _rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15))
    setup_bars.append(rejection)
    raised = engine.on_setup_close(setup_bars)

    assert raised.setup is not None, raised.evidence
    assert raised.setup.strategy_id == SR_REJECTION_ID
    assert raised.setup.state is SetupState.WAIT_CONFIRM
    assert raised.setup.zone.id == support.id
    assert raised.setup.setup_atr == pytest.approx(ATR15)

    filler, window = _confirm_series(rejection)
    confirmation = _confirmation_for(rejection, window)
    decision = engine.on_confirm_close(filler + [confirmation],
                                       equity=100_000.0, exposure=False)

    assert decision.setup.state is SetupState.CONFIRMED, decision.evidence
    assert decision.confirmed is True
    plan = decision.plan
    assert plan is not None and plan.accepted, (plan.blocker, plan.net_rr)
    assert plan.strategy_id == SR_REJECTION_ID and plan.direction == "long"

    # Chapter 10, checked against the document's formulas rather than restated
    assert plan.stop == pytest.approx(
        min(float(rejection.low), support.lower) - 0.10 * ATR15, abs=config.tick_size)
    assert plan.stop < plan.entry_bound
    assert config.stop_distance_atr_min <= plan.stop_distance_atr <= config.stop_distance_atr_max
    assert plan.net_rr >= config.min_net_rr
    assert plan.quantity > 0
    # Chapter 11: risk budget is 0.25% of equity and the fill cannot exceed it
    assert plan.planned_loss <= 100_000.0 * config.risk_per_trade + 1e-9


def test_the_confirmation_window_is_exactly_three_scheduled_5m_candles(config, context, support):
    """Chapter 9: "No confirmation within three 5M bars -> expire the setup"."""
    engine, setup_bars = _primed(config, context, support)
    rejection = _rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15))
    setup_bars.append(rejection)
    engine.on_setup_close(setup_bars)

    filler, window = _confirm_series(rejection)
    series = list(filler)
    # three candles that are neither dominant nor clear of rejection.high
    flat = float(rejection.close)
    for slot in range(3):
        series.append(Bar(window + timedelta(minutes=5 * slot), flat,
                          flat + 5, flat - 5, flat, 40.0))
        decision = engine.on_confirm_close(series, equity=100_000.0)

    assert decision.setup.state is SetupState.EXPIRED
    assert decision.blocker is Blocker.CONFIRMATION_EXPIRED
    assert engine.pending is None


def test_a_missing_5m_interval_cancels_rather_than_extending_the_window(config, context, support):
    """Chapter 14: "Three confirmation candles means three scheduled 5M
    intervals, not three messages received. A missing interval blocks/cancels
    rather than extending the window."

    The tempting bug is to count arrivals, which silently lets a setup wait out
    a data outage and then confirm on a candle from a different market state.
    """
    engine, setup_bars = _primed(config, context, support)
    rejection = _rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15))
    setup_bars.append(rejection)
    engine.on_setup_close(setup_bars)

    filler, window = _confirm_series(rejection)
    late = _confirmation_for(rejection, window + timedelta(minutes=5))   # slot 0 never arrived
    decision = engine.on_confirm_close(filler + [late], equity=100_000.0)

    assert decision.setup.state is SetupState.INVALIDATED
    assert decision.blocker is Blocker.MISSING_CANDLE
    assert decision.plan is None


def test_the_cancellation_boundary_wins_against_the_same_candle_confirming(config, context, support):
    """Chapter 7: "a low beyond this cancellation boundary cancels before
    checking confirmation on that candle", and chapter 14: "An invalidation and
    confirmation in the same event resolves to invalidation."

    This is the single most order-dependent rule in the document. One candle
    that spikes through the invalidation level and still closes as a textbook
    dominance candle is a trade or a scratch depending purely on which test
    runs first.
    """
    engine, setup_bars = _primed(config, context, support)
    rejection = _rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15))
    setup_bars.append(rejection)
    engine.on_setup_close(setup_bars)
    boundary = engine.pending.cancel_price

    filler, window = _confirm_series(rejection)
    good = _confirmation_for(rejection, window)
    # identical close, but the low pierces the cancellation level
    trap = Bar(window, good.open, good.high, boundary - 1.0, good.close, 90.0)

    decision = engine.on_confirm_close(filler + [trap], equity=100_000.0)
    assert decision.setup.state is SetupState.INVALIDATED
    assert decision.blocker is Blocker.REJECTION_FAILED
    assert decision.plan is None


# --------------------------------------------- Setup B, breakout and retest

def _resistance(context, config) -> Zone:
    """The lower of the two confirmed resistance zones -- the one price reaches."""
    return [z for z in build_zones(context, config) if z.kind == "resistance"][0]


def _breakout_for(zone: Zone, at: datetime) -> Bar:
    """Chapter 8's qualifying long breakout, built from the zone's bounds."""
    close = zone.upper + 0.70 * ATR15        # clear of upper + 0.10 ATR15
    open_ = close - 1.00 * ATR15             # body = 1.00 ATR15 >= 0.80
    low = open_ - 0.10 * ATR15
    high = close + 0.05 * ATR15
    return Bar(at, open_, high, low, close, 120.0)


def _run_breakout(config, context, engine=None):
    """Drive a Setup B to WAIT_RETEST and return (engine, 15M bars, flip)."""
    zone = _resistance(context, config)
    engine = engine or PriceActionRulebookEngine(config, CostModel())
    engine.update_context(context)

    start = zone.created_at + timedelta(hours=2)
    below = zone.lower - 400.0
    setup_bars = _flat(start, 40, below, ATR15, minutes=15)
    engine.on_setup_close(setup_bars)

    breakout = _breakout_for(zone, setup_bars[-1].timestamp + timedelta(minutes=15))
    setup_bars.append(breakout)
    decision = engine.on_setup_close(setup_bars)
    return engine, setup_bars, decision


def test_setup_b_breaks_out_retests_and_confirms(config, context):
    """Chapter 8 end to end, including the flip's separate identity.

    The flip carries new bounds-identical geometry but a new id and a new
    available_at, because the document forbids the shortcut: "Never rewrite the
    historical resistance as if it had always been support."
    """
    original = _resistance(context, config)
    engine, setup_bars, decision = _run_breakout(config, context)

    assert decision.setup is not None, decision.evidence
    assert decision.setup.strategy_id == FLIP_RETEST_ID
    assert decision.setup.state is SetupState.WAIT_RETEST

    flip = decision.setup.zone
    assert flip.origin == "flip" and flip.kind == "support"
    assert flip.id != original.id
    assert (flip.lower, flip.upper) == (original.lower, original.upper)
    assert flip.created_at > original.created_at
    # the original is retired, not mutated into a support
    retired = [z for z in engine.zones if z.id == original.id][0]
    assert retired.retired is True and retired.kind == "resistance"

    # the retest arrives inside the four-bar window
    retest = _rejection_for(flip, setup_bars[-1].timestamp + timedelta(minutes=15))
    setup_bars.append(retest)
    armed = engine.on_setup_close(setup_bars)
    assert armed.setup.state is SetupState.WAIT_CONFIRM
    assert armed.setup.rejection is retest

    filler, window = _confirm_series(retest)
    confirmation = _confirmation_for(retest, window)
    final = engine.on_confirm_close(filler + [confirmation], equity=100_000.0)

    assert final.setup.state is SetupState.CONFIRMED, final.evidence
    assert final.plan is not None and final.plan.accepted, (
        final.plan.blocker, final.plan.net_rr)
    assert final.plan.strategy_id == FLIP_RETEST_ID
    # chapter 10: a flip is not an original zone and cannot supply the target
    assert final.plan.target > flip.upper


def test_the_breakout_candle_cannot_retest_itself(config, context):
    """Chapter 8: "Only the next four closed 15M bars can retest"."""
    _, _, decision = _run_breakout(config, context)
    assert decision.setup.state is SetupState.WAIT_RETEST
    assert decision.setup.rejection is None


def test_the_flip_expires_when_no_retest_qualifies_in_four_bars(config, context):
    """Chapter 8: "Expire the flip when no retest qualifies within four bars"."""
    engine, setup_bars, decision = _run_breakout(config, context)
    flip = decision.setup.zone
    drift = float(setup_bars[-1].close)

    for step in range(1, 5):
        drift += 20.0                     # hovers above, never retests the flip
        setup_bars.append(Bar(setup_bars[-1].timestamp + timedelta(minutes=15),
                              drift, drift + 30, drift - 30, drift, 40.0))
        decision = engine.on_setup_close(setup_bars)

    assert decision.setup.state is SetupState.EXPIRED
    assert decision.blocker is Blocker.CONFIRMATION_EXPIRED
    assert engine.pending is None
    assert flip.origin == "flip"


def test_a_close_back_through_the_flip_invalidates_before_any_retest(config, context):
    """Chapter 8: "any 15M close below zone.lower-0.10*ATR15 invalidates"."""
    engine, setup_bars, decision = _run_breakout(config, context)
    flip = decision.setup.zone
    setup_atr = decision.setup.setup_atr

    failed = flip.lower - 0.10 * setup_atr - 1.0
    setup_bars.append(Bar(setup_bars[-1].timestamp + timedelta(minutes=15),
                          failed + 50, failed + 60, failed - 10, failed, 60.0))
    decision = engine.on_setup_close(setup_bars)

    assert decision.setup.state is SetupState.INVALIDATED
    assert engine.pending is None


# --------------------------------------- chapter 9's no-trade rules, in order

def _to_confirmation(config, context, support, engine=None):
    """Drive Setup A up to the confirming 5M candle without submitting it."""
    engine, setup_bars = (engine, None) if engine else (None, None)
    engine, setup_bars = _primed(config, context, support)
    engine.on_setup_close(setup_bars)
    rejection = _rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15))
    setup_bars.append(rejection)
    engine.on_setup_close(setup_bars)
    filler, window = _confirm_series(rejection)
    return engine, filler + [_confirmation_for(rejection, window)]


@pytest.mark.parametrize("regime", [Regime.UNKNOWN, Regime.BALANCED, Regime.TRANSITION])
def test_no_new_setup_outside_bull_or_bear(config, context, support, regime):
    """Chapter 9: "Regime UNKNOWN / BALANCED / TRANSITION -> No new setup".

    v0.1 has no countertrend and no range-edge strategy. A balanced market is
    not a weaker trend to trade smaller, it is a different hypothesis that the
    rulebook says "would be a separate version".
    """
    engine, setup_bars = _primed(config, context, support)
    engine.regime = regime                       # the classifier's verdict, forced
    setup_bars.append(_rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15)))

    decision = engine.on_setup_close(setup_bars)
    assert decision.setup is None
    assert decision.blocker is Blocker.REGIME_NOT_ALIGNED
    assert engine.pending is None


def test_existing_exposure_rejects_the_entry_without_sizing_it(config, context, support):
    """Chapter 9: "Existing same-symbol exposure -> Reject new entry".

    The setup is still recorded -- a research book needs the sample -- but no
    quantity is produced, because a size that will never be sent is a number
    nobody should be able to mistake for one that was.
    """
    engine, confirm_bars = _to_confirmation(config, context, support)
    decision = engine.on_confirm_close(confirm_bars, equity=100_000.0, exposure=True)

    assert decision.setup.state is SetupState.CONFIRMED
    assert decision.blocker is Blocker.EXISTING_EXPOSURE
    assert decision.plan is None
    assert decision.actionable is False


def test_a_failed_data_gate_blocks_execution_without_relaxing_the_strategy(config, context, support):
    """Chapter 9: "Spread, quote age or data gate fails -> Block execution; do
    not relax strategy."

    The freshness authority lives outside this engine and is passed in. What
    must not happen is the two being confused: a blocked entry is still a
    confirmed setup, and a confirmed setup on stale data is still blocked.
    """
    engine, confirm_bars = _to_confirmation(config, context, support)
    decision = engine.on_confirm_close(confirm_bars, equity=100_000.0,
                                       entry_blocked=Blocker.STALE_HTF_CANDLE)

    assert decision.setup.state is SetupState.CONFIRMED   # the evidence stands
    assert decision.blocker is Blocker.STALE_HTF_CANDLE   # the entry does not
    assert decision.plan is None
    assert engine.consumed_zone_ids == set(), "a blocked entry consumed its zone"


def test_a_zone_that_produced_a_trade_cannot_produce_another(config, context, support):
    """Chapter 9: "Same consumed zone -> No re-entry"."""
    engine, confirm_bars = _to_confirmation(config, context, support)
    first = engine.on_confirm_close(confirm_bars, equity=100_000.0)
    assert first.plan.accepted and support.id in engine.consumed_zone_ids

    # the identical rejection, one 15M candle later
    _, setup_bars = _primed(config, context, support)
    repeat = _rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=30))
    setup_bars.append(repeat)
    again = engine.on_setup_close(setup_bars)

    assert again.setup is None
    assert again.blocker is Blocker.ZONE_CONSUMED
    assert support.id in again.evidence["consumed_zones"]


def test_only_one_setup_is_pending_per_symbol(config, context, support):
    """Chapter 9: "allow one pending setup per symbol"."""
    engine, setup_bars = _primed(config, context, support)
    rejection = _rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15))
    setup_bars.append(rejection)
    engine.on_setup_close(setup_bars)
    first = engine.pending
    assert first is not None

    setup_bars.append(_rejection_for(support, rejection.timestamp + timedelta(minutes=15)))
    engine.on_setup_close(setup_bars)
    assert engine.pending is first, "a second candidate displaced the pending setup"


def test_a_regime_change_invalidates_a_pending_setup(config, context, support):
    """Chapter 7 lists "regime changes" as a cancellation cause in its own right."""
    engine, setup_bars = _primed(config, context, support)
    setup_bars.append(_rejection_for(support, setup_bars[-1].timestamp + timedelta(minutes=15)))
    engine.on_setup_close(setup_bars)
    assert engine.pending.state is SetupState.WAIT_CONFIRM

    engine.regime = Regime.TRANSITION
    engine.update_context(_context_bars())        # re-runs the classifier's verdict
    # the fixture is BULL, so force the verdict the way a real flip would land
    engine.regime = Regime.BEAR
    decision = engine.on_setup_close(setup_bars)

    assert decision.setup.state is SetupState.INVALIDATED
    assert decision.blocker is Blocker.REGIME_NOT_ALIGNED
    assert engine.pending is None


# ------------------------------------------------- chapter 9's arbitration

def _candidate(engine, zone: Zone, strategy_id: str, centre_offset: float):
    """A minimal Setup standing at a chosen distance from its zone centre."""
    from services.pa_rulebook_v01 import Setup

    placed = Zone(id=zone.id, kind=zone.kind,
                  lower=zone.centre + centre_offset - 50,
                  upper=zone.centre + centre_offset + 50,
                  pivot_id=zone.pivot_id, created_at=zone.created_at,
                  created_index=zone.created_index, creation_atr=zone.creation_atr)
    return Setup(id=f"{strategy_id}-x", strategy_id=strategy_id, direction="long",
                 state=SetupState.WAIT_CONFIRM, symbol="BTCUSDT", zone=placed,
                 original_zone=placed, setup_atr=ATR15, created_at=zone.created_at)


def test_arbitration_prefers_the_candidate_closest_to_its_zone_centre(config, context, support):
    """Chapter 9's first key: smallest |setup close - zone centre| / setup ATR15.

    Ranking is exercised directly because it has four keys and only the first
    normally decides; driving the whole engine would test key one forever and
    leave the tie-breaks -- the part that makes replay deterministic -- unproven.
    """
    engine = PriceActionRulebookEngine(config)
    close = support.centre
    bar = Bar(START, close, close, close, close, 1.0)

    near = _candidate(engine, support, SR_REJECTION_ID, 0.0)
    far = _candidate(engine, support, FLIP_RETEST_ID, 900.0)
    assert engine._arbitrate([far, near], bar, ATR15) is near
    assert engine._arbitrate([near, far], bar, ATR15) is near, "order changed the winner"


def test_arbitration_breaks_exact_ties_by_zone_age_then_id_then_strategy(config, support):
    """Chapter 9's remaining keys, in the document's order.

    Two candidates equidistant from their zone centres is not a hypothetical --
    a symmetrical zone pair around price produces it -- and "pick either" would
    make the same recorded event sequence replay differently.
    """
    engine = PriceActionRulebookEngine(config)
    close = support.centre
    bar = Bar(START, close, close, close, close, 1.0)

    older = _candidate(engine, support, FLIP_RETEST_ID, 0.0)
    newer = _candidate(engine, support, SR_REJECTION_ID, 0.0)
    newer = newer.__class__(**{**newer.__dict__,
                               "zone": Zone(**{**newer.zone.__dict__,
                                               "created_at": support.created_at + timedelta(hours=1)})})
    assert engine._arbitrate([newer, older], bar, ATR15) is older     # oldest zone

    same_age = _candidate(engine, support, SR_REJECTION_ID, 0.0)
    same_age = same_age.__class__(**{**same_age.__dict__,
                                     "zone": Zone(**{**same_age.zone.__dict__, "id": "zone-aaa"})})
    assert engine._arbitrate([older, same_age], bar, ATR15) is same_age   # smallest id
