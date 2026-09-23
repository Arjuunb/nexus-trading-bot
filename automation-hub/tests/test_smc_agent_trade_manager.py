"""In-trade stop management: breakeven, structural trail, and the widening ban.

The module's whole job is to move a stop in one direction only. Most of these
tests are about the ways it could fail to do that, because a stop that widens
turns the R the journal recorded into a number that means nothing.
"""
import pytest

from services.smc_agent_trade_manager import (BREAKEVEN, TRAIL, Candle,
                                              OpenTrade, StopMove,
                                              TradeManagementPolicy,
                                              favourable_excursion_r,
                                              plan_stop_move)

# entry 100, stop 90 -> R = 10. 1R = 110 for a long.
LONG = OpenTrade(symbol="BTCUSDT", side="buy", entry=100.0,
                 original_stop=90.0, current_stop=90.0)
SHORT = OpenTrade(symbol="BTCUSDT", side="sell", entry=100.0,
                  original_stop=110.0, current_stop=110.0)

BREAKEVEN_ONLY = TradeManagementPolicy(enabled=True, breakeven_at_r=1.0)
TRAILING = TradeManagementPolicy(enabled=True, breakeven_at_r=1.0,
                                 trail_after_r=2.0, trail_lookback=3,
                                 trail_buffer_r=0.1)


def bars(*rows):
    return [Candle(high=h, low=l, close=c) for h, l, c in rows]


# ────────────────────────────────── off ───────────────────────────────────

def test_a_disabled_policy_never_moves_anything():
    """Off is the default, and off must mean off even when every threshold
    is met -- this changes results, so it may only run when asked for."""
    reached = bars((115.0, 99.0, 114.0))

    assert plan_stop_move(LONG, reached, TradeManagementPolicy()) is None


def test_a_trade_that_has_not_reached_the_threshold_is_left_alone():
    assert plan_stop_move(LONG, bars((105.0, 99.0, 104.0)), BREAKEVEN_ONLY) is None


# ───────────────────────────────── breakeven ──────────────────────────────

def test_one_r_moves_a_long_stop_to_entry():
    move = plan_stop_move(LONG, bars((110.0, 99.0, 109.0)), BREAKEVEN_ONLY)

    assert isinstance(move, StopMove)
    assert move.to_price == 100.0
    assert move.reason == BREAKEVEN
    assert move.progress_r == pytest.approx(1.0)


def test_one_r_moves_a_short_stop_to_entry():
    move = plan_stop_move(SHORT, bars((101.0, 90.0, 91.0)), BREAKEVEN_ONLY)

    assert move.to_price == 100.0
    assert move.reason == BREAKEVEN


def test_the_offset_puts_the_stop_past_entry_not_short_of_it():
    """A 'breakeven' exit that is actually a small loss after fees is the
    thing the offset exists to prevent, so it must move the stop further into
    profit, never back toward the original."""
    policy = TradeManagementPolicy(enabled=True, breakeven_at_r=1.0,
                                   breakeven_offset_r=0.05)

    long_move = plan_stop_move(LONG, bars((110.0, 99.0, 109.0)), policy)
    short_move = plan_stop_move(SHORT, bars((101.0, 90.0, 91.0)), policy)

    assert long_move.to_price == pytest.approx(100.5)   # above entry
    assert short_move.to_price == pytest.approx(99.5)   # below entry


def test_a_negative_offset_is_refused_at_construction():
    with pytest.raises(ValueError, match="below entry is a losing stop"):
        TradeManagementPolicy(breakeven_offset_r=-0.1).validated()


# ────────────────────────────── the widening ban ──────────────────────────

def test_a_long_stop_already_above_the_candidate_is_not_dragged_back_down():
    """The central guard. Price ran, the stop trailed up, then price came
    back: breakeven now computes BELOW the current stop and must be refused,
    not applied."""
    trailed = OpenTrade(symbol="BTCUSDT", side="buy", entry=100.0,
                        original_stop=90.0, current_stop=104.0)

    assert plan_stop_move(trailed, bars((112.0, 99.0, 105.0)), BREAKEVEN_ONLY) is None


def test_a_short_stop_already_below_the_candidate_is_not_dragged_back_up():
    trailed = OpenTrade(symbol="BTCUSDT", side="sell", entry=100.0,
                        original_stop=110.0, current_stop=96.0)

    assert plan_stop_move(trailed, bars((101.0, 88.0, 95.0)), BREAKEVEN_ONLY) is None


def test_no_reachable_policy_can_produce_a_stop_worse_than_the_current_one():
    """Swept across thresholds, offsets, lookbacks and both directions: the
    returned stop is never further from entry than where it already was."""
    for at_r in (0.5, 1.0, 2.0):
        for trail_r in (None, 1.0, 3.0):
            for offset in (0.0, 0.05, 0.5):
                policy = TradeManagementPolicy(
                    enabled=True, breakeven_at_r=at_r, trail_after_r=trail_r,
                    breakeven_offset_r=offset).validated()
                for trade in (LONG, SHORT,
                              OpenTrade("BTCUSDT", "buy", 100.0, 90.0, 103.0),
                              OpenTrade("BTCUSDT", "sell", 100.0, 110.0, 97.0)):
                    for candles in (bars((110.0, 99.0, 109.0)),
                                    bars((140.0, 60.0, 130.0)),
                                    bars((104.0, 70.0, 72.0)),
                                    bars((101.0, 88.0, 90.0))):
                        move = plan_stop_move(trade, candles, policy)
                        if move is None:
                            continue
                        if trade.is_long:
                            assert move.to_price > trade.current_stop
                        else:
                            assert move.to_price < trade.current_stop


# ───────────────────────────────── trailing ───────────────────────────────

#: A runner: three closed candles whose lowest low (105) sits well above
#: entry, so the structural stop is clearly more protective than breakeven
#: and the two cannot tie.
RAN = ((112.0, 105.0, 111.0), (118.0, 108.0, 117.0), (125.0, 114.0, 124.0))


def test_trailing_follows_structure_once_far_enough_in_profit():
    move = plan_stop_move(LONG, bars(*RAN), TRAILING)

    assert move.reason == TRAIL
    # lowest low of the last three closed candles, less a 0.1R buffer.
    assert move.to_price == pytest.approx(105.0 - 1.0)


def test_the_trail_wins_when_it_protects_more_than_breakeven():
    """Both fire at once on a trade that ran. Taking the more protective of
    the two means a runner never steps back down to entry."""
    move = plan_stop_move(LONG, bars(*RAN), TRAILING)

    assert move.to_price > 100.0, "the trail lost to breakeven"


def test_a_tie_between_the_two_is_resolved_and_still_protective():
    """When structure lands exactly on entry both rules produce the same
    price. The result must still be a single, favourable move rather than
    depending on which rule was evaluated first."""
    level = bars((112.0, 101.0, 111.0), (118.0, 108.0, 117.0),
                 (125.0, 114.0, 124.0))

    move = plan_stop_move(LONG, level, TRAILING)

    assert move.to_price == pytest.approx(100.0)
    assert move.to_price > LONG.current_stop


def test_breakeven_wins_when_structure_is_still_below_entry():
    """A trade far enough in profit to trail, whose recent structure is still
    under the entry. The trail would loosen the stop, so breakeven holds."""
    candles = bars((121.0, 95.0, 120.0), (122.0, 96.0, 121.0),
                   (123.0, 97.0, 122.0))

    move = plan_stop_move(LONG, candles, TRAILING)

    assert move.reason == BREAKEVEN
    assert move.to_price == 100.0


def test_a_trail_that_would_close_the_position_instantly_is_refused():
    """Price retraced THROUGH the structural level: the last close (125) is
    below the lowest low of the window (126), so trailing there would sit the
    stop above price and close the trade at the next tick -- recorded as a
    stop-out rather than as the error it is."""
    candles = bars((130.0, 126.0, 129.0), (131.0, 127.0, 130.0),
                   (132.0, 128.0, 125.0))
    policy = TradeManagementPolicy(enabled=True, breakeven_at_r=None,
                                   trail_after_r=1.0, trail_lookback=3,
                                   trail_buffer_r=0.0)

    assert min(candle.low for candle in candles) > candles[-1].close, \
        "the fixture does not reach the guard it is testing"

    assert plan_stop_move(LONG, candles, policy) is None


# ─────────────────────────────── measurement ──────────────────────────────

def test_progress_is_measured_from_the_journalled_risk_not_the_moved_stop():
    """Once the stop has moved, the position can no longer say what R was.
    Thresholds must stay anchored to the risk the agent actually took."""
    moved = OpenTrade(symbol="BTCUSDT", side="buy", entry=100.0,
                      original_stop=90.0, current_stop=100.0)

    # 120 is 2R against the ORIGINAL 10-point risk, not 20R against a
    # zero-width current stop.
    assert favourable_excursion_r(moved, bars((120.0, 99.0, 119.0))) == pytest.approx(2.0)


def test_a_zero_risk_trade_is_never_managed():
    """Entry equal to stop has no scale to measure against, so every
    threshold would be meaningless rather than merely unmet."""
    degenerate = OpenTrade(symbol="BTCUSDT", side="buy", entry=100.0,
                           original_stop=100.0, current_stop=100.0)

    assert favourable_excursion_r(degenerate, bars((150.0, 99.0, 149.0))) is None
    assert plan_stop_move(degenerate, bars((150.0, 99.0, 149.0)), TRAILING) is None


def test_no_candles_means_no_decision():
    """A tick with no closed candle is not an opportunity to guess."""
    assert plan_stop_move(LONG, [], TRAILING) is None


def test_a_stop_already_at_the_candidate_price_is_not_moved_again():
    """Equal is not favourable. Emitting a move to the price the stop already
    sits at would journal a stop change that changed nothing, on every candle
    for the rest of the trade."""
    at_breakeven = OpenTrade(symbol="BTCUSDT", side="buy", entry=100.0,
                             original_stop=90.0, current_stop=100.0)

    assert plan_stop_move(at_breakeven, bars((115.0, 99.0, 114.0)),
                          BREAKEVEN_ONLY) is None


def test_a_short_stop_already_at_the_candidate_price_is_not_moved_again():
    at_breakeven = OpenTrade(symbol="BTCUSDT", side="sell", entry=100.0,
                             original_stop=110.0, current_stop=100.0)

    assert plan_stop_move(at_breakeven, bars((101.0, 85.0, 86.0)),
                          BREAKEVEN_ONLY) is None
